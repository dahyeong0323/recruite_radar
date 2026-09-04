from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GitResult:
    committed: bool
    pushed: bool
    message: str


class GitSync:
    def __init__(self, repo_root: Path, *, branch: str = "main", radar_relative_path: Path | str = "Career/Recruiting_Radar", dry_run: bool = True, retries: int = 3) -> None:
        self.repo_root = repo_root
        self.branch = branch
        self.radar_relative_path = Path(radar_relative_path)
        self.dry_run = dry_run
        self.retries = retries

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=self.repo_root, text=True, capture_output=True, check=False)

    def sync_remote(self) -> GitResult:
        if self.dry_run:
            return GitResult(False, False, "dry-run: remote sync skipped")
        fetch = self._run("fetch", "origin", self.branch)
        if fetch.returncode != 0:
            return GitResult(False, False, "git fetch failed")
        pull = self._run("pull", "--rebase", "origin", self.branch)
        if pull.returncode != 0:
            return GitResult(False, False, "git pull --rebase failed")
        return GitResult(False, True, "remote synced")

    def commit_and_push(self, message: str) -> GitResult:
        if self.dry_run:
            return GitResult(False, False, "dry-run: Git mutation skipped")
        add = self._run("add", self.radar_relative_path.as_posix())
        if add.returncode != 0:
            return GitResult(False, False, "git add failed")
        status = self._run("status", "--short")
        if not status.stdout.strip():
            return GitResult(False, True, "no changes")
        commit = self._run("commit", "-m", message)
        if commit.returncode != 0:
            return GitResult(False, False, "git commit failed")
        for _ in range(self.retries):
            push = self._run("push", "origin", self.branch)
            if push.returncode == 0:
                return GitResult(True, True, "pushed")
            fetch = self._run("fetch", "origin", self.branch)
            if fetch.returncode != 0:
                continue
            pull = self._run("pull", "--rebase", "origin", self.branch)
            if pull.returncode != 0:
                continue
        return GitResult(True, False, "git push failed after retries")
