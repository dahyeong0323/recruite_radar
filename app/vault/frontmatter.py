from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("\n---", 1)
    if len(parts) != 2:
        return {}, text
    header = parts[0][3:].lstrip("\n")
    body = parts[1].lstrip("\n")
    data = yaml.safe_load(header) or {}
    return (data if isinstance(data, dict) else {}), body


def render_frontmatter(data: dict[str, Any]) -> str:
    header = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False).rstrip()
    return f"---\n{header}\n---\n"


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
