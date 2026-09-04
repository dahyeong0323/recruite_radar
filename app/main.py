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
scheduler = configure_scheduler(service, settings.timezone)


@asynccontextmanager
async def lifespan(app: FastAPI):
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
    yield
    if settings.scheduler_enabled:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Korea Finance Recruiting Radar", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    ready, reasons = readiness(settings)
    return {
        "status": health_state(settings.state_path), "ready": ready,
        "dry_run": settings.dry_run, "reasons": reasons, "sources": service._health_rows(),
    }


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
    if settings.telegram_webhook_secret and x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(status_code=403, detail="invalid webhook secret")
    payload = await request.json()
    if not settings.telegram_bot_token:
        return {"ok": True, "ignored": "telegram is not configured"}
    client = TelegramClient(settings.telegram_bot_token)
    try:
        if "callback_query" in payload:
            await handle_callback(settings, client, payload["callback_query"])
        elif "message" in payload:
            message = payload["message"]
            chat_id = str((message.get("chat") or {}).get("id", ""))
            if settings.telegram_chat_id and chat_id != str(settings.telegram_chat_id):
                return {"ok": True, "ignored": "unauthorized chat"}
            text = str(message.get("text", "")).split()[0] if message.get("text") else ""
            if text:
                await handle_command(client, chat_id, text, load_index(settings.index_path), health_state(settings.state_path))
    finally:
        await client.close()
    return {"ok": True}
