from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.models import SourceState
from app.vault.frontmatter import atomic_write_text
from app.utils.security import redact
from app.utils.clock import now


def update_source_state(state_path: Path, source: str, *, success: bool, seen_ids: list[str] | None = None, newest_timestamp=None) -> dict[str, Any]:
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"sources": {}}
    source_state = SourceState.model_validate(state.setdefault("sources", {}).get(source, {}))
    if success:
        merged_ids = list(dict.fromkeys((seen_ids or []) + source_state.recent_ids))[:500]
        source_state = source_state.model_copy(update={"last_success_at": now(), "recent_ids": merged_ids, "newest_timestamp": newest_timestamp, "consecutive_failures": 0})
    else:
        source_state = source_state.model_copy(update={"consecutive_failures": source_state.consecutive_failures + 1})
    state["sources"][source] = source_state.model_dump(mode="json")
    state["updated_at"] = now().isoformat()
    atomic_write_text(state_path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    return state


async def update_source_state_async(state_path: Path, source: str, *, success: bool, seen_ids: list[str] | None = None, newest_timestamp=None) -> dict[str, Any]:
    from app.vault.repository import GLOBAL_VAULT_LOCK

    async with GLOBAL_VAULT_LOCK:
        return update_source_state(state_path, source, success=success, seen_ids=seen_ids, newest_timestamp=newest_timestamp)


def health_state(state_path: Path, *, git_push_failures: int = 0, telegram_failures: int = 0, pending_classifications: int = 0) -> str:
    if not state_path.exists():
        return "DEGRADED"
    data = json.loads(state_path.read_text(encoding="utf-8"))
    sources = data.get("sources", {})
    if not sources or not any(value.get("last_success_at") for value in sources.values()):
        return "DEGRADED"
    failures = [value.get("consecutive_failures", 0) for value in sources.values()]
    if git_push_failures >= 3 or telegram_failures >= 3 or pending_classifications > 0 and max(failures or [0]) >= 2:
        return "FAILED"
    if any(value >= 1 for value in failures) or pending_classifications > 0:
        return "DEGRADED"
    return "HEALTHY"


def write_health_note(radar_root: Path, *, state: str, source_rows: list[dict[str, Any]], notes: list[str] | None = None, secrets=()) -> None:
    lines = [
        "# Recruiting Radar Health",
        "",
        f"- State: **{state}**",
        f"- Checked at: {now().isoformat()}",
        "",
        "| Source | Consecutive failures | Last success |",
        "|---|---:|---|",
    ]
    for row in source_rows:
        lines.append(f"| {row.get('source')} | {row.get('consecutive_failures', 0)} | {row.get('last_success_at') or 'never'} |")
    if notes:
        lines.extend(["", "## Notes", "", *[f"- {redact(note, secrets)}" for note in notes]])
    atomic_write_text(radar_root / "_System" / "Health.md", "\n".join(lines) + "\n")
