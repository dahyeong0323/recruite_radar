from app.vault.git_sync import GitSync


def test_dry_run_never_mutates_git(tmp_path):
    result = GitSync(tmp_path, dry_run=True).commit_and_push("radar: test")
    assert result.committed is False
    assert result.pushed is False
    assert "dry-run" in result.message
