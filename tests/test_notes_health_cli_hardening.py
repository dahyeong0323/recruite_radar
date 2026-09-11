import asyncio
import json
from datetime import datetime, timezone

import pytest
import yaml
from types import SimpleNamespace
import app.cli as cli_module

from app.cli import _parser, run
from app.health.readiness import readiness
from app.models import SourceItem
from app.pipeline.classify import rule_based_classify
from app.pipeline.update import IngestionPipeline
from app.utils.clock import local_date
from app.vault.frontmatter import parse_frontmatter, render_frontmatter
from app.vault.index import rebuild_index
from app.vault.note_writer import write_job_note
from app.vault.status import set_user_status


def item(**updates):
    values = dict(
        source="kvca", source_id="1", source_url="https://example.test/1", company_raw="테스트캐피탈",
        title_raw="투자본부 인턴", body_text="VC 투자 기업분석 인턴", discovered_at=datetime(2026, 9, 4).astimezone(), active=True,
    )
    values.update(updates)
    return SourceItem(**values)


def test_change_log_bullets_do_not_multiply(settings):
    source = item()
    classification = rule_based_classify(source)
    path, metadata = write_job_note(settings.radar_root, source, classification, changes=["- created"])
    existing, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    write_job_note(settings.radar_root, source, classification, job_id=metadata["id"], existing_metadata=existing, existing_body=body, changes=["- refreshed"])
    text = path.read_text(encoding="utf-8")
    assert "- - created" not in text
    assert "- created" in text and "- refreshed" in text


def test_user_notes_survive_repeated_refresh(settings):
    source = item()
    classification = rule_based_classify(source)
    path, metadata = write_job_note(settings.radar_root, source, classification)
    meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    body += "\n## User Notes\n\n내가 직접 쓴 메모\n"
    for _ in range(2):
        path.write_text(render_frontmatter(meta) + "\n" + body, encoding="utf-8")
        meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        write_job_note(settings.radar_root, source, classification, job_id=metadata["id"], existing_metadata=meta, existing_body=body, changes=["refresh"])
        meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    text = path.read_text(encoding="utf-8")
    assert text.count("## User Notes") == 1
    assert text.count("내가 직접 쓴 메모") == 1


def test_status_changes_append_to_one_history_section(settings):
    path, metadata = write_job_note(settings.radar_root, item(), rule_based_classify(item()))
    set_user_status(settings.vault_root, settings.radar_root, metadata["id"], "interested")
    set_user_status(settings.vault_root, settings.radar_root, metadata["id"], "will_apply")
    text = path.read_text(encoding="utf-8")
    assert text.count("## Status History") == 1
    assert "`unreviewed` → `interested`" in text
    assert "`interested` → `will_apply`" in text


def test_malformed_yaml_isolated_and_reported(settings):
    valid, _ = write_job_note(settings.radar_root, item(), rule_based_classify(item()))
    malformed = settings.radar_root / "Jobs" / "2026" / "broken.md"
    malformed.parent.mkdir(parents=True, exist_ok=True)
    malformed.write_text("---\ntags: [unterminated\n---\n# broken\n", encoding="utf-8")
    entries = rebuild_index(settings.radar_root)
    diagnostics = json.loads((settings.radar_root / "_System" / "index_errors.json").read_text(encoding="utf-8"))
    assert len(entries) == 1
    assert diagnostics["count"] == 1
    assert "broken.md" in diagnostics["malformed_notes"][0]


def test_expired_posting_is_closed_even_when_source_says_active(settings):
    expired = item(deadline=datetime(2020, 1, 1).astimezone(), active=True)
    _, metadata = write_job_note(settings.radar_root, expired, rule_based_classify(expired))
    assert metadata["status"] == "closed"


def test_timezone_boundary_uses_korea_calendar_day():
    instant = datetime(2026, 9, 3, 15, 30, tzinfo=timezone.utc)
    assert local_date(instant, "Asia/Seoul").isoformat() == "2026-09-04"
    assert local_date(instant, "UTC").isoformat() == "2026-09-03"


def test_dry_run_readiness_only_requires_writable_runtime(settings):
    settings.radar_root.mkdir(parents=True)
    ready, reasons = readiness(settings)
    assert ready is True and reasons == []


def test_production_readiness_requires_git_checkout_and_state(settings):
    from dataclasses import replace
    prod = replace(settings, dry_run=False, git_url="https://example.test/vault.git")
    prod.radar_root.mkdir(parents=True)
    ready, reasons = readiness(prod)
    assert ready is False
    assert any("Git checkout" in reason for reason in reasons)
    assert any("state" in reason for reason in reasons)


@pytest.mark.parametrize("command", ["collect-all", "refresh-active", "digest", "deadline"])
def test_independent_scheduled_cli_commands_exist(command):
    args = _parser().parse_args([command])
    assert args.command == command


@pytest.mark.parametrize("command,method", [("collect-all", "collect_all"), ("refresh-active", "refresh_active"), ("digest", "send_digest"), ("deadline", "send_deadline_reminders")])
def test_independent_scheduled_cli_command_executes(settings, monkeypatch, command, method):
    called = []

    class FakeService:
        def __init__(self, configured):
            self.settings = configured

        async def collect_all(self):
            called.append("collect_all")
            return []

        async def refresh_active(self):
            called.append("refresh_active")
            return []

        async def send_digest(self):
            called.append("send_digest")

        async def send_deadline_reminders(self):
            called.append("send_deadline_reminders")

    monkeypatch.setattr(cli_module, "load_settings", lambda root=None: settings)
    monkeypatch.setattr(cli_module, "bootstrap", lambda configured: None)
    monkeypatch.setattr(cli_module, "RadarService", FakeService)
    assert asyncio.run(run(SimpleNamespace(command=command, project_root=None))) == 0
    assert called == [method]


def test_production_collect_does_not_dirty_checkout_with_implicit_bootstrap(settings, monkeypatch):
    from dataclasses import replace

    configured = replace(settings, dry_run=False, git_url="https://example.test/vault.git")
    called = []

    class FakeService:
        def __init__(self, value):
            self.settings = value

        async def collect_source(self, source):
            called.append(("collect", source))
            return {"source": source}

    monkeypatch.setattr(cli_module, "load_settings", lambda root=None: configured)
    monkeypatch.setattr(cli_module, "ensure_vault_checkout", lambda value: called.append(("ensure", value.branch)))
    monkeypatch.setattr(cli_module, "bootstrap", lambda value: called.append(("bootstrap", value.branch)))
    monkeypatch.setattr(cli_module, "RadarService", FakeService)

    args = SimpleNamespace(command="collect", source="kvca", project_root=None)
    assert asyncio.run(run(args)) == 0
    assert called == [("ensure", configured.branch), ("collect", "kvca")]


def test_batch_rebuilds_index_once_at_end(settings, monkeypatch):
    pipeline = IngestionPipeline(settings)
    calls = 0
    original = pipeline.repository.rebuild

    async def counted():
        nonlocal calls
        calls += 1
        await original()

    monkeypatch.setattr(pipeline.repository, "rebuild", counted)
    asyncio.run(pipeline.ingest([item(source_id="1"), item(source_id="2", title_raw="PE 인턴")]))
    assert calls == 1


def test_taxonomy_and_scoring_configs_are_valid_yaml():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "config"
    assert "sector" in yaml.safe_load((root / "scoring.yaml").read_text(encoding="utf-8"))["relevance"]
    assert "VC" in yaml.safe_load((root / "taxonomy.yaml").read_text(encoding="utf-8"))["sectors"]
