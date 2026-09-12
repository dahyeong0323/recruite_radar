from __future__ import annotations

from collections import Counter

from app.models import IndexEntry
from app.telegram.client import TelegramClient
from app.utils.clock import today
from app.utils.dates import days_until


LIST_COMMANDS = {"/today", "/intern", "/junior", "/saved", "/deadline"}
HELP_TEXT = """사용할 수 있는 명령어

/status - 봇과 공고 현황
/today - 오늘 등록된 공고
/intern - 인턴·트레이니 공고
/junior - 주니어·신입 공고
/saved - 관심·지원예정 공고
/deadline - 7일 이내 마감
/help - 명령어 안내"""


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


def _status_text(entries: list[IndexEntry], health: str) -> str:
    active = [entry for entry in entries if entry.status == "active"]
    priorities = Counter(entry.priority for entry in active)
    user_statuses = Counter(entry.user_status for entry in entries)
    status_icon = "✅" if health.upper() == "HEALTHY" else "⚠️"
    return f"""📡 Recruiting Radar

{status_icon} 시스템: {health}
📋 활성 공고: {len(active)}개
🔥 A: {priorities['A']}개 · ⭐ B: {priorities['B']}개
⭐ 관심: {user_statuses['interested']}개
📝 지원예정: {user_statuses['will_apply']}개

/help 로 명령어를 확인하세요."""


def _deadline_text(entry: IndexEntry) -> str:
    remaining = days_until(entry.deadline)
    if remaining is None:
        return "마감 미상"
    if remaining >= 0:
        return f"D-{remaining}"
    return f"마감 D+{-remaining}"


def _list_message(command: str, entries: list[IndexEntry], *, limit: int = 8) -> tuple[str, dict | None]:
    labels = {
        "/today": "오늘 등록된 공고",
        "/intern": "인턴·트레이니 공고",
        "/junior": "주니어·신입 공고",
        "/saved": "관심·지원예정 공고",
        "/deadline": "7일 이내 마감 공고",
    }
    shown = entries[:limit]
    header = f"📋 {labels[command]}\n총 {len(entries)}개"
    if len(entries) > limit:
        header += f" · 상위 {limit}개 표시"

    blocks = []
    buttons = []
    for number, entry in enumerate(shown, start=1):
        icon = "🔥" if entry.priority == "A" else "⭐" if entry.priority == "B" else "▫️"
        company = entry.company or "회사 미상"
        blocks.append(
            f"{icon} {number}. {company}\n"
            f"{entry.title}\n"
            f"{entry.sector} · {entry.seniority} · {_deadline_text(entry)} · 점수 {entry.relevance_score}"
        )
        urls = entry.application_urls or entry.source_urls
        if urls:
            button_title = f"{number}. {company} — {entry.title}"
            if len(button_title) > 48:
                button_title = button_title[:47] + "…"
            buttons.append([{"text": button_title, "url": urls[0]}])

    markup = {"inline_keyboard": buttons} if buttons else None
    return header + "\n\n" + "\n\n".join(blocks), markup


async def handle_command(telegram: TelegramClient, chat_id: str, command: str, entries: list[IndexEntry], health: str) -> None:
    command = command.split("@", 1)[0].lower()
    if command in {"/status", "/health"}:
        await telegram.send_message(chat_id, _status_text(entries, health))
        return
    if command in {"/start", "/help"}:
        await telegram.send_message(chat_id, HELP_TEXT)
        return
    if command not in LIST_COMMANDS:
        await telegram.send_message(chat_id, "지원하지 않는 명령입니다.\n\n" + HELP_TEXT)
        return
    selected = select_entries(command, entries)
    if not selected:
        await telegram.send_message(chat_id, "조건에 맞는 공고가 없습니다.")
        return
    text, markup = _list_message(command, selected)
    await telegram.send_message(chat_id, text, reply_markup=markup)
