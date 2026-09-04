from __future__ import annotations

import os


def readiness(settings) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not settings.radar_root.exists():
        reasons.append("radar root missing")
    elif not os.access(settings.radar_root, os.W_OK):
        reasons.append("radar root is not writable")
    if not settings.dry_run:
        if not (settings.vault_root / ".git").exists():
            reasons.append("production Vault is not a Git checkout")
        if not settings.state_path.exists():
            reasons.append("source state is missing")
        if not settings.git_url:
            reasons.append("VAULT_GIT_URL is missing")
    return not reasons, reasons
