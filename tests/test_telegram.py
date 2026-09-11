import asyncio
from datetime import datetime
from dataclasses import replace
from types import SimpleNamespace

import app.telegram.callbacks as callbacks_module
from app.models import SourceItem
from app.pipeline.classify import rule_based_classify
from app.telegram.formatter import job_alert
from app.telegram.callbacks import handle_callback
from app.vault.note_writer import write_job_note


class FakeTelegram:
    def __init__(self):
        self.answered = []

    async def answer_callback(self, callback_id, text):
        self.answered.append((callback_id, text))


def test_formatter_has_required_buttons():
    item = SourceItem(source="kvca", source_id="1", source_url="https://example.test", title_raw="투자본부 인턴", body_text="기업분석", discovered_at=datetime.now().astimezone())
    text, markup = job_alert("KRFIN-1", item, rule_based_classify(item))
    assert "공고 열기" in str(markup)
    assert "interest:KRFIN-1" in str(markup)
    assert "Actionability" in text


def test_callback_writes_status(settings):
    item = SourceItem(source="kvca", source_id="1", source_url="https://example.test", company_raw="테스트 VC", title_raw="투자본부 인턴", body_text="기업분석", discovered_at=datetime.now().astimezone(), active=True)
    metadata = write_job_note(settings.radar_root, item, rule_based_classify(item))[1]
    fake = FakeTelegram()
    asyncio.run(handle_callback(settings, fake, {"id": "callback-1", "data": f"interest:{metadata['id']}", "message": {"chat": {"id": "1"}}}))
    assert fake.answered and "interested" in fake.answered[0][1]


def test_production_callback_passes_private_remote_credentials(settings, monkeypatch):
    item = SourceItem(source="kvca", source_id="2", source_url="https://example.test", company_raw="테스트 VC", title_raw="투자본부 인턴", body_text="기업분석", discovered_at=datetime.now().astimezone(), active=True)
    metadata = write_job_note(settings.radar_root, item, rule_based_classify(item))[1]
    configured = replace(
        settings,
        dry_run=False,
        git_url="ssh://git@ssh.github.com:443/owner/vault.git",
        ssh_deploy_key="private-key",
        telegram_chat_id="1",
    )
    captured = {}

    class FakeGitSync:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        def commit_and_push(self, message):
            return SimpleNamespace(pushed=True, message="pushed")

    monkeypatch.setattr(callbacks_module, "GitSync", FakeGitSync)
    fake = FakeTelegram()
    asyncio.run(handle_callback(configured, fake, {"id": "callback-2", "data": f"interest:{metadata['id']}", "message": {"chat": {"id": "1"}}}))
    assert captured["git_url"] == configured.git_url
    assert captured["ssh_deploy_key"] == configured.ssh_deploy_key
    assert fake.answered and "interested" in fake.answered[0][1]
