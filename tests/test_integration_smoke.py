import asyncio
import json
import subprocess
from dataclasses import replace
from pathlib import Path

from app.cli import bootstrap, load_fixture
from app.pipeline.update import IngestionPipeline
from app.vault.git_sync import GitSync
from app.vault.index import rebuild_index
from app.vault.status import set_user_status


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, text=True, encoding="utf-8", errors="replace", capture_output=True, check=True)
    return (result.stdout or "").strip()


def test_local_bare_git_end_to_end_smoke(settings, tmp_path, monkeypatch):
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-b", "main")
    git(seed, "config", "user.name", "Test")
    git(seed, "config", "user.email", "test@example.test")
    (seed / "README.md").write_text("vault\n", encoding="utf-8")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "seed")
    remote = tmp_path / "vault.git"
    git(tmp_path, "clone", "--bare", str(seed), str(remote))

    vault = tmp_path / "runtime" / "vault"
    prod = replace(settings, vault_root=vault, dry_run=False, branch="main", git_url=str(remote))
    sync = GitSync(vault, branch="main", radar_relative_path=prod.vault_relative_path, dry_run=False, git_url=str(remote))
    sync.ensure_vault_checkout()
    bootstrap(prod)
    fixtures = load_fixture(Path(__file__).parent / "fixtures" / "sample_items.json")

    pipeline = IngestionPipeline(prod)
    original = pipeline.repository.upsert_job

    async def fail_one(item, *args, **kwargs):
        if item.source_id == fixtures[2].source_id:
            raise OSError("simulated partial failure")
        return await original(item, *args, **kwargs)

    monkeypatch.setattr(pipeline.repository, "upsert_job", fail_one)
    partial = asyncio.run(pipeline.ingest(fixtures))
    assert partial.errors and partial.canonical_jobs_created >= 1
    monkeypatch.setattr(pipeline.repository, "upsert_job", original)
    recovered = asyncio.run(pipeline.ingest(fixtures))
    assert not recovered.errors
    duplicate = asyncio.run(IngestionPipeline(prod).ingest(fixtures))
    assert duplicate.canonical_jobs_created == 0

    entries = rebuild_index(prod.radar_root)
    target = entries[0]
    set_user_status(prod.vault_root, prod.radar_root, target.id, "interested")
    assert sync.commit_and_push("radar: integration smoke").pushed

    fresh = tmp_path / "fresh"
    git(tmp_path, "clone", str(remote), str(fresh))
    radar = fresh / "Career" / "Recruiting_Radar"
    fresh_entries = rebuild_index(radar)
    assert fresh_entries
    assert any(entry.user_status == "interested" for entry in fresh_entries)
    assert (radar / "00_Dashboard.md").exists()
    assert (radar / "_System" / "index.json").exists()
