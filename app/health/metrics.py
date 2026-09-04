from __future__ import annotations

import json
from pathlib import Path

from app.models import RunMetrics
from app.vault.frontmatter import atomic_write_text
from app.utils.security import redact


def append_run_log(radar_root: Path, metrics: RunMetrics, *, secrets=()) -> None:
    path = radar_root / "_System" / "Run_Log.md"
    existing = path.read_text(encoding="utf-8") if path.exists() else "# Recruiting Radar Run Log\n"
    record = redact(json.dumps(metrics.model_dump(mode="json"), ensure_ascii=False), secrets)
    atomic_write_text(path, existing.rstrip() + "\n\n```json\n" + record + "\n```\n")
