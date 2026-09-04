from __future__ import annotations

from app.models import IndexEntry
from app.telegram.client import TelegramClient
from app.vault.dashboard import _table
from app.utils.clock import today


def select_entries(command: str, entries: list[IndexEntry], *, timezone_name: str = "Asia/Seoul") -> list[IndexEntry]:
    active = [entry for entry in entries if entry.status == "active"]
    if command == "/intern":
        return [entry for entry in active if entry.seniority in {"Intern", "Trainee"}]
    if command == "/junior":
        return [entry for entry in active if entry.seniority in {"Junior", "New Graduate"}]
    if command == "/saved":
        return [entry for entry in entries if entry.user_status in {"interested", "will_apply"}]
    if command == "/deadline":
        return [entry for entry in active if entry.deadline and 0 <= (entry.deadline - today(timezone_name)).days <= 7]
    if command == "/today":
        return [entry for entry in active if entry.posted_at == today(timezone_name)]
    return active


async def handle_command(telegram: TelegramClient, chat_id: str, command: str, entries: list[IndexEntry], health: str) -> None:
    if command == "/health":
        await telegram.send_message(chat_id, f"Recruiting Radar health: {health}")
        return
    if command == "/help":
        await telegram.send_message(chat_id, "/today /intern /junior /saved /deadline /health /help")
        return
    selected = select_entries(command, entries)
    await telegram.send_message(chat_id, _table(selected, limit=15) if selected else "조건에 맞는 공고가 없습니다.")
