from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.vault.frontmatter import atomic_write_text
from app.utils.clock import now


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"jobs": {}, "updated_at": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"jobs": {}, "updated_at": None}
    except (OSError, json.JSONDecodeError):
        return {"jobs": {}, "updated_at": None}


def set_delivery(path: Path, job_id: str, state: str, *, fingerprint: str | None = None, detail: str | None = None) -> None:
    data = load_state(path)
    row = {"state": state, "updated_at": now().isoformat()}
    if fingerprint:
        row["fingerprint"] = fingerprint
    if detail:
        row["detail"] = detail
    data.setdefault("jobs", {})[job_id] = row
    data["updated_at"] = row["updated_at"]
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def delivery_is_reserved(row: dict[str, Any], *, as_of: datetime | None = None, lease: timedelta = timedelta(minutes=10)) -> bool:
    """Delivered rows are final; sending rows block only while their lease is fresh."""
    state = row.get("state")
    if state == "delivered":
        return True
    if state != "sending":
        return False
    try:
        updated_at = datetime.fromisoformat(str(row.get("updated_at", "")).replace("Z", "+00:00"))
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    current = as_of or now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc) - updated_at.astimezone(timezone.utc) < lease
