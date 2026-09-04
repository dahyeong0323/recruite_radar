from __future__ import annotations

import re
from collections.abc import Iterable


_PATTERNS = (
    re.compile(r"(?i)(access[-_]?key|api[-_]?key|token|secret|password)=([^&\s]+)"),
    re.compile(r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)([^\s,;]+)"),
    re.compile(r"(?i)(https://api\.telegram\.org/bot)([^/\s]+)"),
    re.compile(r"(?i)(https://[^/@\s]+:)([^@/\s]+)(@)"),
)


def redact(value: object, secrets: Iterable[str | None] = ()) -> str:
    """Return useful diagnostics without credentials or configured secrets."""
    text = str(value)
    for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = _PATTERNS[0].sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = _PATTERNS[1].sub(lambda match: f"{match.group(1)}[REDACTED]", text)
    text = _PATTERNS[2].sub(lambda match: f"{match.group(1)}[REDACTED]", text)
    text = _PATTERNS[3].sub(lambda match: f"{match.group(1)}[REDACTED]{match.group(3)}", text)
    return text


def safe_exception(service: str, error: BaseException, secrets: Iterable[str | None] = ()) -> str:
    return redact(f"{service}: {type(error).__name__}: {error}", secrets)
