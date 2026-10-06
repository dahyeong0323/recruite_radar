from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request

from app.config import load_settings
from app.health.monitor import health_state
from app.scheduler import configure_scheduler
from app.service import RadarService
from app.telegram.callbacks import handle_callback
from app.telegram.client import TelegramClient
from app.telegram.commands import handle_command
from app.vault.index import load_index
from app.vault.git_sync import ensure_vault_checkout
from app.health.readiness import readiness
from app.utils.security import safe_exception
from app.vault.frontmatter import atomic_write_text
import json


settings = load_settings()
service = RadarService(settings)
from app.events.service import EventService

event_service = None
event_initialization_error = None
if settings.event_radar_enabled:
    try:
        event_service = EventService(settings)
    except Exception as error:
        event_initialization_error = type(error).__name__
scheduler = configure_scheduler(service, settings.timezone, event_service)
_lifespan_active = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _lifespan_active
    if not settings.dry_run:
        ensure_vault_checkout(settings)
    settings.radar_root.mkdir(parents=True, exist_ok=True)
    (settings.radar_root / "_System").mkdir(parents=True, exist_ok=True)
    if not settings.state_path.exists():
        atomic_write_text(settings.state_path, json.dumps({"sources": {}, "updated_at": None}, indent=2) + "\n")
    ready, reasons = readiness(settings)
    if not ready:
        raise RuntimeError("production readiness failed: " + "; ".join(reasons))
    if settings.scheduler_enabled:
        scheduler.start()
    _lifespan_active = True
    if event_service and settings.scheduler_enabled:
        import asyncio
        # Catch up persisted due sources without delaying Job service readiness.
        event_catchup = asyncio.create_task(event_service.catch_up())
    else:
        event_catchup = None
    try:
        yield
    finally:
        _lifespan_active = False
        if event_catchup:
            event_catchup.cancel()
            import asyncio
            await asyncio.gather(event_catchup, return_exceptions=True)
        if settings.scheduler_enabled:
            scheduler.shutdown(wait=False)


app = FastAPI(title="Korea Finance & Content Recruiting Radar", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    ready, reasons = readiness(settings)
    status = service._health_state()
    if _lifespan_active and settings.scheduler_enabled and not getattr(scheduler, "running", False):
        status = "FAILED"
        reasons = [*reasons, "scheduler is not running"]
    result = {
        "status": status, "ready": ready,
        "dry_run": settings.dry_run, "reasons": reasons, "sources": service._health_rows(),
    }
    if event_service:
        result["events"] = event_service.health()
    elif event_initialization_error:
        result["events"] = {"status": "FAILED", "reason": "Event initialization failed: " + event_initialization_error}
    return result


@app.get("/livez")
async def livez() -> dict:
    return {"status": "alive"}


@app.get("/readyz")
async def readyz() -> dict:
    ready, reasons = readiness(settings)
    if not ready:
        raise HTTPException(status_code=503, detail={"status": "not_ready", "reasons": reasons})
    return {"status": "ready", "dry_run": settings.dry_run}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)) -> dict:
    if not settings.dry_run and settings.telegram_bot_token and not settings.telegram_webhook_secret:
        raise HTTPException(status_code=503, detail="telegram webhook secret is not configured")
    if settings.telegram_webhook_secret and x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(status_code=403, detail="invalid webhook secret")
    payload = await request.json()
    if not settings.telegram_bot_token:
        return {"ok": True, "ignored": "telegram is not configured"}
    client = TelegramClient(settings.telegram_bot_token)
    try:
        if "callback_query" in payload:
            query = payload["callback_query"]
            if event_service and str(query.get("data", "")).startswith("ev:"):
                chat_id = str(((query.get("message") or {}).get("chat") or {}).get("id", ""))
                if settings.telegram_chat_id and chat_id != str(settings.telegram_chat_id):
                    return {"ok": True, "ignored": "unauthorized chat"}
                parts = str(query["data"]).split(":", 2)
                if len(parts) != 3:
                    await client.answer_callback(str(query.get("id")), "알 수 없는 행사 요청")
                    return {"ok": True}
                _, action, event_id = parts
                status = {"interest": "interested", "registered": "registered", "attended": "attended", "ignore": "ignored"}.get(action)
                if status:
                    try:
                        await event_service.set_user_status(event_id, status)
                        await client.answer_callback(str(query.get("id")), "행사 상태를 저장했습니다")
                    except Exception:
                        await client.answer_callback(str(query.get("id")), "행사 상태 저장 실패: 잠시 후 다시 시도하세요")
                return {"ok": True}
            await handle_callback(settings, client, payload["callback_query"])
        elif "message" in payload:
            message = payload["message"]
            chat_id = str((message.get("chat") or {}).get("id", ""))
            if settings.telegram_chat_id and chat_id != str(settings.telegram_chat_id):
                return {"ok": True, "ignored": "unauthorized chat"}
            text = str(message.get("text", "")).split()[0] if message.get("text") else ""
            if text:
                if event_service and text == "/help":
                    await client.send_message(chat_id, "🇨🇭 Event 명령어: /events /events_high /events_geneva /events_zurich /events_saved /events_status")
                if event_service and text in {"/events", "/events_high", "/events_geneva", "/events_zurich", "/events_saved", "/events_status"}:
                    await event_service.handle_command(client, chat_id, text)
                    return {"ok": True}
                await handle_command(client, chat_id, text, load_index(settings.index_path), service._health_state())
    finally:
        await client.close()
    return {"ok": True}
