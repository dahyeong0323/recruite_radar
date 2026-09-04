from __future__ import annotations

import os
import subprocess
import base64
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from app.utils.security import redact


class VaultCheckoutError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitResult:
    committed: bool
    pushed: bool
    message: str


class GitSync:
    def __init__(
        self, repo_root: Path, *, branch: str = "main",
        radar_relative_path: Path | str = "Career/Recruiting_Radar",
        dry_run: bool = True, retries: int = 3, git_url: str | None = None,
        github_token: str | None = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.branch = branch
        self.radar_relative_path = Path(radar_relative_path)
        self.dry_run = dry_run
        self.retries = retries
        self.git_url = git_url
        self.github_token = github_token

    @property
    def _secrets(self) -> tuple[str | None, ...]:
        return (self.github_token,)

    def _git_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.github_token and (not self.git_url or self.git_url.startswith("https://")):
            basic = base64.b64encode(f"x-access-token:{self.github_token}".encode()).decode()
            env.update({
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
            })
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    def _run(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=cwd or self.repo_root, env=self._git_env(),
            text=True, encoding="utf-8", errors="replace", capture_output=True, check=False,
        )

    def _failure(self, operation: str, result: subprocess.CompletedProcess[str]) -> VaultCheckoutError:
        detail = redact((result.stderr or "").strip() or (result.stdout or "").strip() or f"exit {result.returncode}", self._secrets)
        return VaultCheckoutError(f"{operation} failed: {detail}")

    def ensure_vault_checkout(self) -> GitResult:
        if self.dry_run:
            return GitResult(False, False, "dry-run: Vault checkout skipped")
        if self.git_url:
            parsed = urlsplit(self.git_url)
            if parsed.username or parsed.password:
                raise VaultCheckoutError("VAULT_GIT_URL must not embed credentials; use GITHUB_TOKEN")
        git_marker = self.repo_root / ".git"
        if not git_marker.exists():
            if not self.git_url:
                raise VaultCheckoutError("VAULT_GIT_URL is required for a fresh production checkout")
            if self.repo_root.exists() and any(self.repo_root.iterdir()):
                raise VaultCheckoutError("VAULT_ROOT exists but is not an empty or valid Git checkout")
            self.repo_root.parent.mkdir(parents=True, exist_ok=True)
            clone = self._run(
                "clone", "--branch", self.branch, "--single-branch", self.git_url, str(self.repo_root),
                cwd=self.repo_root.parent,
            )
            if clone.returncode != 0:
                raise self._failure("git clone", clone)
        valid = self._run("rev-parse", "--is-inside-work-tree")
        if valid.returncode != 0 or valid.stdout.strip() != "true":
            raise VaultCheckoutError("VAULT_ROOT is not a valid Git working tree")
        if self.git_url:
            current = self._run("remote", "get-url", "origin")
            if current.returncode != 0:
                add = self._run("remote", "add", "origin", self.git_url)
                if add.returncode != 0:
                    raise self._failure("git remote add", add)
            elif current.stdout.strip() != self.git_url:
                repair = self._run("remote", "set-url", "origin", self.git_url)
                if repair.returncode != 0:
                    raise self._failure("git remote repair", repair)
        fetch = self._run("fetch", "origin", self.branch)
        if fetch.returncode != 0:
            raise self._failure("git fetch", fetch)
        local_branch = self._run("show-ref", "--verify", f"refs/heads/{self.branch}")
        checkout_args = ("checkout", self.branch) if local_branch.returncode == 0 else ("checkout", "-B", self.branch, f"origin/{self.branch}")
        checkout = self._run(*checkout_args)
        if checkout.returncode != 0:
            raise self._failure("git checkout", checkout)
        pull = self._run("pull", "--rebase", "origin", self.branch)
        if pull.returncode != 0:
            raise self._failure("git pull --rebase", pull)
        self._run("config", "user.name", "Recruiting Radar Bot")
        self._run("config", "user.email", "recruiting-radar@users.noreply.github.com")
        return GitResult(False, True, "Vault checkout ready")

    def sync_remote(self) -> GitResult:
        if self.dry_run:
            return GitResult(False, False, "dry-run: remote sync skipped")
        return self.ensure_vault_checkout()

    def commit_and_push(self, message: str) -> GitResult:
        if self.dry_run:
            return GitResult(False, False, "dry-run: Git mutation skipped")
        path = self.radar_relative_path.as_posix()
        status = self._run("status", "--short", "--", path)
        if status.returncode != 0:
            return GitResult(False, False, "git status failed")
        if not status.stdout.strip():
            return GitResult(False, True, "no changes")
        add = self._run("add", "--", path)
        if add.returncode != 0:
            return GitResult(False, False, "git add failed")
        commit = self._run("commit", "--only", "-m", redact(message, self._secrets), "--", path)
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


def ensure_vault_checkout(settings) -> GitResult:
    return GitSync(
        settings.vault_root, branch=settings.branch, radar_relative_path=settings.vault_relative_path,
        dry_run=settings.dry_run, git_url=settings.git_url, github_token=settings.github_token,
    ).ensure_vault_checkout()
