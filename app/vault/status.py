from __future__ import annotations

from pathlib import Path

from app.vault.dashboard import write_dashboards
from app.vault.frontmatter import atomic_write_text, parse_frontmatter, render_frontmatter
from app.vault.index import rebuild_index
from app.vault.repository import GLOBAL_VAULT_LOCK


VALID_STATUSES = {"unreviewed", "interested", "will_apply", "applied", "interview", "waiting", "rejected", "offer", "ignored", "withdrawn"}


def _set_user_status_unlocked(vault_root: Path, radar_root: Path, job_id: str, status: str) -> Path:
    if status not in VALID_STATUSES:
        raise ValueError(f"unsupported user status: {status}")
    matches = list((radar_root / "Jobs").rglob(f"{job_id}.md"))
    if len(matches) != 1:
        raise FileNotFoundError(f"expected one note for {job_id}, found {len(matches)}")
    path = matches[0]
    metadata, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    old = metadata.get("user_status", "unreviewed")
    metadata["user_status"] = status
    atomic_write_text(path, render_frontmatter(metadata) + "\n" + body.rstrip() + f"\n\n## Status Change\n\n- `{old}` → `{status}`\n")
    entries = rebuild_index(radar_root)
    write_dashboards(radar_root, entries)
    return path


def set_user_status(vault_root: Path, radar_root: Path, job_id: str, status: str) -> Path:
    return _set_user_status_unlocked(vault_root, radar_root, job_id, status)


async def set_user_status_async(vault_root: Path, radar_root: Path, job_id: str, status: str) -> Path:
    async with GLOBAL_VAULT_LOCK:
        return _set_user_status_unlocked(vault_root, radar_root, job_id, status)


async def mark_alerted(radar_root: Path, job_id: str) -> Path:
    async with GLOBAL_VAULT_LOCK:
        matches = list((radar_root / "Jobs").rglob(f"{job_id}.md"))
        if len(matches) != 1:
            raise FileNotFoundError(f"expected one note for {job_id}, found {len(matches)}")
        path = matches[0]
        metadata, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        from datetime import datetime

        metadata["telegram_alerted_at"] = datetime.now().astimezone().isoformat()
        atomic_write_text(path, render_frontmatter(metadata) + "\n" + body.rstrip() + "\n")
        entries = rebuild_index(radar_root)
        write_dashboards(radar_root, entries)
        return path
