from __future__ import annotations

import json
from datetime import timedelta

from app.collectors.kofia import KofiaCollector
from app.collectors.kvca import KvcaCollector
from app.collectors.saramin import SaraminCollector
from app.collectors.vcs import VcsCollector
from app.config import Settings
from app.health.metrics import append_run_log
from app.health.monitor import health_state, update_source_state_async, write_health_note
from app.models import ClassificationResult, SourceItem
from app.pipeline.update import IngestionPipeline
from app.telegram.client import TelegramClient
from app.telegram.formatter import job_alert
from app.vault.frontmatter import parse_frontmatter
from app.vault.git_sync import GitSync
from app.vault.index import load_index
from app.vault.status import mark_alerted
from app.vault.repository import GLOBAL_VAULT_LOCK
from app.vault.operation_lock import OperationInProgress, operation_lock
from app.telegram.outbox import load_state as load_delivery_state, set_delivery
from app.utils.clock import now, today
from app.utils.security import safe_exception


class RadarService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.pipeline = IngestionPipeline(settings)

    def _known_ids(self, source: str) -> set[str]:
        entries = load_index(self.settings.index_path)
        return {entry.source_ids[source] for entry in entries if entry.source_ids.get(source)}

    def _git(self) -> GitSync:
        return GitSync(
            self.settings.vault_root, branch=self.settings.branch,
            radar_relative_path=self.settings.vault_relative_path, dry_run=self.settings.dry_run,
            git_url=self.settings.git_url, github_token=self.settings.github_token,
            ssh_deploy_key=self.settings.ssh_deploy_key,
        )

    async def _persist(self, message: str) -> bool:
        if self.settings.dry_run:
            return True
        async with GLOBAL_VAULT_LOCK:
            result = self._git().commit_and_push(message)
        return result.pushed

    def _collector(self, source: str):
        return {"kvca": KvcaCollector, "vcs": VcsCollector, "kofia": KofiaCollector, "saramin": SaraminCollector}[source](self.settings)

    def _refresh_ids(self, source: str) -> set[str]:
        entries = load_index(self.settings.index_path)
        selected = [entry for entry in entries if entry.priority in {"A", "B"} or entry.user_status in {"interested", "will_apply", "applied"}]
        return {entry.source_ids.get(source) for entry in selected if entry.source_ids.get(source)}

    def _last_success(self, source: str) -> str | None:
        if not self.settings.state_path.exists():
            return None
        data = json.loads(self.settings.state_path.read_text(encoding="utf-8"))
        return (data.get("sources", {}).get(source) or {}).get("last_success_at")

    async def collect_source(self, source: str, *, refresh: bool = False) -> dict:
        try:
            async with operation_lock(self.settings.vault_root):
                return await self._collect_source_unlocked(source, refresh=refresh)
        except OperationInProgress as error:
            return {"source": source, "error": safe_exception(source, error, self.settings.secrets)}

    async def _collect_source_unlocked(self, source: str, *, refresh: bool = False) -> dict:
        collector = self._collector(source)
        try:
            if not self.settings.dry_run:
                async with GLOBAL_VAULT_LOCK:
                    sync_result = self._git().sync_remote()
                if not sync_result.pushed:
                    raise RuntimeError(sync_result.message)
            async with collector:
                kwargs = {
                    "known_ids": self._known_ids(source),
                    "refresh_ids": self._refresh_ids(source) if refresh else set(),
                    "overlap_start": now(self.settings.timezone) - timedelta(days=7),
                }
                if source == "saramin":
                    kwargs["published_min"] = self._last_success(source)
                    kwargs["updated_min"] = self._last_success(source)
                items = await collector.collect(**kwargs)
            metrics = await self.pipeline.ingest(items, source=source)
            metrics.list_items_seen = len(items)
            metrics.new_source_items = len(items)
            metrics.detail_fetches = len(items)
            metrics.errors.extend(getattr(collector, "errors", []))
            newest = max((item.posted_at for item in items if item.posted_at), default=None)
            append_run_log(self.settings.radar_root, metrics, secrets=self.settings.secrets)
            durable = not metrics.errors and await self._persist(f"radar: persist {source} recruiting batch")
            await update_source_state_async(
                self.settings.state_path, source, success=durable,
                seen_ids=[item.source_id for item in items] if durable else None,
                newest_timestamp=newest if durable else None,
            )
            if not await self._persist(f"radar: record {source} collection state"):
                metrics.errors.append("git: source state push failed")
            if not refresh:
                try:
                    metrics.telegram_sent = await self._send_immediate_alerts(items)
                except Exception as error:  # Telegram failure must not discard collected jobs
                    metrics.errors.append(safe_exception("telegram", error, self.settings.secrets))
                    write_health_note(self.settings.radar_root, state="DEGRADED", source_rows=self._health_rows(), notes=["Telegram delivery failed; jobs were preserved."])
            if getattr(collector, "errors", []):
                write_health_note(self.settings.radar_root, state="DEGRADED", source_rows=self._health_rows(), notes=[f"{source}: {len(collector.errors)} detail page(s) fell back to list metadata."])
            if metrics.errors:
                write_health_note(self.settings.radar_root, state="DEGRADED", source_rows=self._health_rows(), notes=[f"{source}: partial run; watermark preserved."])
            return metrics.model_dump(mode="json")
        except Exception as error:  # source isolation is intentional
            await update_source_state_async(self.settings.state_path, source, success=False)
            safe = safe_exception(source, error, self.settings.secrets)
            write_health_note(self.settings.radar_root, state=health_state(self.settings.state_path), source_rows=self._health_rows(), notes=[safe], secrets=self.settings.secrets)
            await self._persist(f"radar: record {source} failure")
            return {"source": source, "error": safe}

    async def collect_all(self, *, refresh: bool = False) -> list[dict]:
        results = []
        for source in self.settings.enabled_sources:
            results.append(await self.collect_source(source, refresh=refresh))
        write_health_note(self.settings.radar_root, state=health_state(self.settings.state_path), source_rows=self._health_rows())
        return results

    def _health_rows(self) -> list[dict]:
        if not self.settings.state_path.exists():
            return []
        data = json.loads(self.settings.state_path.read_text(encoding="utf-8"))
        return [{"source": source, **row} for source, row in data.get("sources", {}).items()]

    async def refresh_active(self) -> list[dict]:
        return await self.collect_all(refresh=True)

    async def backfill(self, source: str, from_date) -> dict:
        try:
            async with operation_lock(self.settings.vault_root):
                return await self._backfill_unlocked(source, from_date)
        except OperationInProgress as error:
            return {"source": source, "error": safe_exception(source, error, self.settings.secrets)}

    async def _backfill_unlocked(self, source: str, from_date) -> dict:
        if not self.settings.dry_run:
            async with GLOBAL_VAULT_LOCK:
                sync_result = self._git().sync_remote()
            if not sync_result.pushed:
                return {"source": source, "error": sync_result.message}
        collector = self._collector(source)
        async with collector:
            kwargs = {"known_ids": set(), "refresh_ids": set(), "max_pages": 100}
            if source == "kofia":
                kwargs["backfill"] = True
            items = await collector.collect(**kwargs)
        selected = [item for item in items if not item.posted_at or item.posted_at.date() >= from_date]
        selected = [item.model_copy(update={"raw_metadata": {**item.raw_metadata, "historical": True}}) for item in selected]
        metrics = await self.pipeline.ingest(selected, source=f"backfill:{source}")
        append_run_log(self.settings.radar_root, metrics, secrets=self.settings.secrets)
        if not await self._persist(f"radar: backfill {source} recruiting posts"):
            metrics.errors.append("git: backfill push failed")
        return metrics.model_dump(mode="json")

    async def _send_immediate_alerts(self, items: list[SourceItem]) -> int:
        if self.settings.dry_run or not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
            return 0
        entries = load_index(self.settings.index_path)
        client = TelegramClient(self.settings.telegram_bot_token)
        outbox_path = self.settings.radar_root / "_System" / "notification_outbox.json"
        sent: set[str] = set()
        try:
            for item in items:
                entry = next((candidate for candidate in entries if item.source_id in candidate.source_ids.values()), None)
                if not entry or entry.id in sent or entry.priority != "A" or entry.status != "active":
                    continue
                frontmatter, _ = parse_frontmatter((self.settings.vault_root / entry.file_path).read_text(encoding="utf-8"))
                if frontmatter.get("telegram_alerted_at"):
                    continue
                delivery = (load_delivery_state(outbox_path).get("jobs", {}).get(entry.id) or {}).get("state")
                if delivery in {"sending", "delivered"}:
                    continue
                source_item = SourceItem(
                    source=item.source,
                    source_id=item.source_id,
                    source_url=(frontmatter.get("source_urls") or [item.source_url])[0],
                    company_raw=frontmatter.get("company"),
                    title_raw=frontmatter.get("title", item.title_raw),
                    deadline=item.deadline,
                    body_text=item.body_text,
                    discovered_at=item.discovered_at,
                    active=True,
                )
                classification = ClassificationResult(
                    sector=frontmatter.get("sector", "Unknown"),
                    subsector=frontmatter.get("subsector"),
                    role_family=frontmatter.get("role_family", "Other"),
                    seniority=frontmatter.get("seniority", "Unknown"),
                    front_office=bool(frontmatter.get("front_office")),
                    priority=entry.priority,
                    relevance_score=int(frontmatter.get("relevance_score", 0)),
                    actionability_score=int(frontmatter.get("actionability_score", 0)),
                    classification_confidence=float(frontmatter.get("classification_confidence", 0)),
                    student_eligible=frontmatter.get("student_eligible"),
                    conversion_possible=frontmatter.get("conversion_possible"),
                )
                text, markup = job_alert(entry.id, source_item, classification)
                set_delivery(outbox_path, entry.id, "sending", fingerprint=entry.fingerprint)
                if not await self._persist("radar: reserve Telegram alert"):
                    raise RuntimeError("could not durably reserve Telegram alert")
                try:
                    await client.send_message(self.settings.telegram_chat_id, text, reply_markup=markup)
                except Exception as error:
                    set_delivery(outbox_path, entry.id, "failed", fingerprint=entry.fingerprint, detail=safe_exception("Telegram", error, self.settings.secrets))
                    await self._persist("radar: record failed Telegram alert")
                    raise
                set_delivery(outbox_path, entry.id, "delivered", fingerprint=entry.fingerprint)
                await mark_alerted(self.settings.radar_root, entry.id)
                if not await self._persist("radar: record delivered Telegram alert"):
                    raise RuntimeError("Telegram delivered but delivery receipt push failed")
                sent.add(entry.id)
        finally:
            await client.close()
        return len(sent)

    async def send_digest(self) -> None:
        try:
            async with operation_lock(self.settings.vault_root):
                await self._send_digest_unlocked()
        except OperationInProgress:
            return

    async def _send_digest_unlocked(self) -> None:
        if self.settings.dry_run or not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
            return
        entries = load_index(self.settings.index_path)
        digest_path = self.settings.radar_root / "_System" / "digest_state.json"
        digest_state = load_delivery_state(digest_path).get("jobs", {})
        selected = [
            entry for entry in entries
            if entry.status == "active" and entry.priority == "B" and entry.user_status != "ignored"
            and (digest_state.get(entry.id) or {}).get("fingerprint") != f"{entry.fingerprint}:{entry.updated_at}"
        ]
        pending_a = []
        for entry in entries:
            if entry.status != "active" or entry.priority != "A":
                continue
            frontmatter, _ = parse_frontmatter((self.settings.vault_root / entry.file_path).read_text(encoding="utf-8"))
            if not frontmatter.get("telegram_alerted_at"):
                pending_a.append(entry)
        if not selected and not pending_a:
            return
        client = TelegramClient(self.settings.telegram_bot_token)
        try:
            lines = ["📡 Korea Finance Recruiting Radar — Daily Digest", ""]
            if pending_a:
                lines.append("즉시 알림 미전송 A 공고")
                lines.extend(f"A | {entry.company or 'Unknown'} — {entry.title} | {entry.deadline or '마감 미상'}" for entry in pending_a[:10])
                lines.append("")
            lines.append("신규/미확인 B 공고")
            lines.extend(f"B | {entry.company or 'Unknown'} — {entry.title} | {entry.deadline or '마감 미상'} | {entry.relevance_score}" for entry in selected[:20])
            lines.extend(["", f"통계: B {len(selected)}개 · A 알림대기 {len(pending_a)}개 · 전체 active {sum(1 for entry in entries if entry.status == 'active')}개"])
            await client.send_message(self.settings.telegram_chat_id, "\n".join(lines))
            for entry in selected[:20]:
                set_delivery(digest_path, entry.id, "delivered", fingerprint=f"{entry.fingerprint}:{entry.updated_at}")
            await self._persist("radar: record daily digest delivery")
        finally:
            await client.close()

    async def send_deadline_reminders(self) -> None:
        try:
            async with operation_lock(self.settings.vault_root):
                await self._send_deadline_reminders_unlocked()
        except OperationInProgress:
            return

    async def _send_deadline_reminders_unlocked(self) -> None:
        if self.settings.dry_run or not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
            return
        entries = load_index(self.settings.index_path)
        due = [entry for entry in entries if entry.user_status in {"interested", "will_apply"} and entry.deadline]
        due = [entry for entry in due if (entry.deadline - today(self.settings.timezone)).days in {1, 3}]
        if not due:
            return
        client = TelegramClient(self.settings.telegram_bot_token)
        try:
            await client.send_message(self.settings.telegram_chat_id, "⏰ 마감 알림\n\n" + "\n".join(f"{entry.title} — D-{(entry.deadline - today(self.settings.timezone)).days}" for entry in due))
        finally:
            await client.close()
