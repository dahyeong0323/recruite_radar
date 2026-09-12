import subprocess
from pathlib import Path

import pytest

from app.vault.git_sync import GitSync, VaultCheckoutError


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, text=True, encoding="utf-8", errors="replace", capture_output=True, check=True)
    return (result.stdout or "").strip()


def make_remote(tmp_path: Path) -> tuple[Path, Path]:
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-b", "main")
    git(seed, "config", "user.name", "Test")
    git(seed, "config", "user.email", "test@example.test")
    (seed / "Career" / "Recruiting_Radar").mkdir(parents=True)
    (seed / "Career" / "Recruiting_Radar" / "README.md").write_text("seed\n", encoding="utf-8")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "seed")
    remote = tmp_path / "remote.git"
    git(tmp_path, "clone", "--bare", str(seed), str(remote))
    return seed, remote


def test_fresh_vault_checkout_clones_configured_branch(tmp_path):
    _, remote = make_remote(tmp_path)
    checkout = tmp_path / "checkout"
    result = GitSync(checkout, branch="main", dry_run=False, git_url=str(remote)).ensure_vault_checkout()
    assert result.pushed is True
    assert (checkout / ".git").exists()
    assert (checkout / "Career" / "Recruiting_Radar" / "README.md").exists()


def test_production_checkout_fails_closed_without_url(tmp_path):
    with pytest.raises(VaultCheckoutError, match="VAULT_GIT_URL"):
        GitSync(tmp_path / "missing", dry_run=False).ensure_vault_checkout()


def test_nonempty_non_git_vault_fails_closed(tmp_path):
    root = tmp_path / "bad"
    root.mkdir()
    (root / "personal.md").write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(VaultCheckoutError, match="not an empty"):
        GitSync(root, dry_run=False, git_url="unused").ensure_vault_checkout()


def test_origin_is_repaired_and_branch_is_checked_out(tmp_path):
    _, remote = make_remote(tmp_path)
    checkout = tmp_path / "checkout"
    git(tmp_path, "clone", str(remote), str(checkout))
    git(checkout, "remote", "set-url", "origin", str(tmp_path / "wrong.git"))
    GitSync(checkout, branch="main", dry_run=False, git_url=str(remote)).ensure_vault_checkout()
    assert git(checkout, "remote", "get-url", "origin") == str(remote)
    assert git(checkout, "branch", "--show-current") == "main"


def test_partial_clone_git_directory_recovers_remote_tracking_branch(tmp_path):
    _, remote = make_remote(tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    git(checkout, "init")
    git(checkout, "remote", "add", "origin", str(remote))

    result = GitSync(
        checkout,
        branch="main",
        dry_run=False,
        git_url=str(remote),
    ).ensure_vault_checkout()

    assert result.pushed is True
    assert git(checkout, "branch", "--show-current") == "main"
    assert git(checkout, "rev-parse", "origin/main") == git(checkout, "rev-parse", "HEAD")


def test_existing_checkout_recovers_interrupted_managed_changes(tmp_path):
    _, remote = make_remote(tmp_path)
    checkout = tmp_path / "checkout"
    sync = GitSync(checkout, branch="main", dry_run=False, git_url=str(remote))
    sync.ensure_vault_checkout()
    health = checkout / "Career" / "Recruiting_Radar" / "Health.md"
    health.write_text("recovered\n", encoding="utf-8")

    result = sync.ensure_vault_checkout()

    assert result.pushed is True
    assert git(checkout, "status", "--short") == ""
    fresh = tmp_path / "fresh"
    git(tmp_path, "clone", str(remote), str(fresh))
    assert (fresh / "Career" / "Recruiting_Radar" / "Health.md").read_text(encoding="utf-8") == "recovered\n"


def test_clean_but_ahead_commit_is_recovered(tmp_path):
    _, remote = make_remote(tmp_path)
    checkout = tmp_path / "checkout"
    sync = GitSync(checkout, branch="main", dry_run=False, git_url=str(remote))
    sync.ensure_vault_checkout()
    managed = checkout / "Career" / "Recruiting_Radar" / "ahead.md"
    managed.write_text("ahead\n", encoding="utf-8")
    git(checkout, "add", str(managed.relative_to(checkout)))
    git(checkout, "commit", "-m", "local only")
    assert int(git(checkout, "rev-list", "--count", "origin/main..HEAD")) == 1

    sync.ensure_vault_checkout()

    fresh = tmp_path / "fresh"
    git(tmp_path, "clone", str(remote), str(fresh))
    assert (fresh / "Career" / "Recruiting_Radar" / "ahead.md").read_text(encoding="utf-8") == "ahead\n"


def test_commit_and_push_recovers_clean_ahead_commit(tmp_path):
    _, remote = make_remote(tmp_path)
    checkout = tmp_path / "checkout"
    sync = GitSync(checkout, branch="main", dry_run=False, git_url=str(remote))
    sync.ensure_vault_checkout()
    managed = checkout / "Career" / "Recruiting_Radar" / "ahead-via-commit.md"
    managed.write_text("ahead\n", encoding="utf-8")
    git(checkout, "add", str(managed.relative_to(checkout)))
    git(checkout, "commit", "-m", "local only")

    result = sync.commit_and_push("no working-tree changes")

    assert result.pushed is True
    assert "recovered 1" in result.message
    assert git(checkout, "rev-list", "--count", "origin/main..HEAD") == "0"


def test_existing_checkout_refuses_to_recover_unmanaged_changes(tmp_path):
    _, remote = make_remote(tmp_path)
    checkout = tmp_path / "checkout"
    sync = GitSync(checkout, branch="main", dry_run=False, git_url=str(remote))
    sync.ensure_vault_checkout()
    (checkout / "private-note.md").write_text("do not commit\n", encoding="utf-8")
    health = checkout / "Career" / "Recruiting_Radar" / "Health.md"
    health.write_text("pending\n", encoding="utf-8")

    with pytest.raises(VaultCheckoutError, match="unmanaged local changes"):
        sync.ensure_vault_checkout()

    assert not (tmp_path / "remote.git" / "refs" / "heads" / "unexpected").exists()


def test_radar_commit_excludes_unrelated_staged_file(tmp_path):
    _, remote = make_remote(tmp_path)
    checkout = tmp_path / "checkout"
    sync = GitSync(checkout, branch="main", dry_run=False, git_url=str(remote))
    sync.ensure_vault_checkout()
    unrelated = checkout / "private-note.md"
    unrelated.write_text("private\n", encoding="utf-8")
    git(checkout, "add", "private-note.md")
    managed = checkout / "Career" / "Recruiting_Radar" / "job.md"
    managed.write_text("job\n", encoding="utf-8")
    result = sync.commit_and_push("radar: safe scoped commit")
    assert result.pushed is True
    names = git(checkout, "show", "--pretty=", "--name-only", "HEAD").splitlines()
    assert "Career/Recruiting_Radar/job.md" in names
    assert "private-note.md" not in names
    assert "A  private-note.md" in git(checkout, "status", "--short")


def test_git_failure_message_redacts_token(tmp_path):
    token = "ghp_fake_super_secret"
    sync = GitSync(tmp_path / "checkout", branch="main", dry_run=False, git_url="https://invalid.test/no.git", github_token=token)
    with pytest.raises(VaultCheckoutError) as caught:
        sync.ensure_vault_checkout()
    assert token not in str(caught.value)


def test_ssh_url_user_is_not_treated_as_embedded_credentials(tmp_path, monkeypatch):
    sync = GitSync(
        tmp_path / "checkout",
        dry_run=False,
        git_url="ssh://git@ssh.github.com:443/owner/vault.git",
    )
    monkeypatch.setattr(
        sync,
        "_run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, "", "expected clone failure"),
    )
    with pytest.raises(VaultCheckoutError, match="git clone failed"):
        sync.ensure_vault_checkout()


def test_ssh_deploy_key_is_not_embedded_in_command(tmp_path):
    private_key = "-----BEGIN OPENSSH PRIVATE KEY-----\nfake-test-key\n-----END OPENSSH PRIVATE KEY-----"
    sync = GitSync(tmp_path, dry_run=False, git_url="git@github.com:owner/vault.git", ssh_deploy_key=private_key)
    key_path = sync._ssh_key_path()
    assert key_path is not None
    assert key_path.read_text(encoding="utf-8").strip() == private_key
    command = sync._git_env()["GIT_SSH_COMMAND"]
    assert command.startswith("ssh -i ")
    assert private_key not in command
