import asyncio
from datetime import datetime
from dataclasses import replace
from types import SimpleNamespace

import app.telegram.callbacks as callbacks_module
from app.models import SourceItem
from app.pipeline.classify import rule_based_classify
from app.telegram.formatter import job_alert
from app.telegram.callbacks import handle_callback
from app.telegram.commands import handle_command
from app.vault.index import rebuild_index
from app.vault.note_writer import write_job_note


class FakeTelegram:
    def __init__(self):
        self.answered = []
        self.messages = []

    async def answer_callback(self, callback_id, text):
        self.answered.append((callback_id, text))

    async def send_message(self, chat_id, text, *, reply_markup=None):
        self.messages.append((chat_id, text, reply_markup))


def test_formatter_has_required_buttons():
    item = SourceItem(source="kvca", source_id="1", source_url="https://example.test", title_raw="투자본부 인턴", body_text="기업분석", discovered_at=datetime.now().astimezone())
    text, markup = job_alert("KRFIN-1", item, rule_based_classify(item))
    assert "공고 열기" in str(markup)
    assert "interest:KRFIN-1" in str(markup)
    assert "Actionability" in text


def test_status_command_is_compact_and_does_not_render_obsidian_table(settings):
    item = SourceItem(
        source="kvca",
        source_id="status-1",
        source_url="https://example.test/status-1",
        company_raw="테스트 VC",
        title_raw="투자본부 인턴",
        body_text="기업분석",
        discovered_at=datetime.now().astimezone(),
        active=True,
    )
    write_job_note(settings.radar_root, item, rule_based_classify(item))
    entry = rebuild_index(settings.radar_root)[0]
    fake = FakeTelegram()

    asyncio.run(handle_command(fake, "1", "/status", [entry], "HEALTHY"))

    text = fake.messages[0][1]
    assert "✅ 시스템: HEALTHY" in text
    assert "활성 공고: 1개" in text
    assert "[[" not in text
    assert "| P |" not in text


def test_list_command_uses_telegram_text_and_url_buttons(settings):
    item = SourceItem(
        source="kvca",
        source_id="list-1",
        source_url="https://example.test/list-1",
        company_raw="테스트 VC",
        title_raw="투자본부 인턴",
        body_text="기업분석",
        discovered_at=datetime.now().astimezone(),
        active=True,
    )
    write_job_note(settings.radar_root, item, rule_based_classify(item))
    entry = rebuild_index(settings.radar_root)[0]
    fake = FakeTelegram()

    asyncio.run(handle_command(fake, "1", "/intern", [entry], "HEALTHY"))

    _, text, markup = fake.messages[0]
    assert "테스트 VC" in text
    assert "[[" not in text
    assert "| P |" not in text
    assert markup["inline_keyboard"][0][0]["url"] == "https://example.test/list-1"


def test_unknown_command_returns_help_instead_of_all_jobs(settings):
    item = SourceItem(
        source="kvca",
        source_id="unknown-1",
        source_url="https://example.test/unknown-1",
        title_raw="노출되면 안 되는 공고",
        body_text="기업분석",
        discovered_at=datetime.now().astimezone(),
        active=True,
    )
    write_job_note(settings.radar_root, item, rule_based_classify(item))
    entry = rebuild_index(settings.radar_root)[0]
    fake = FakeTelegram()

    asyncio.run(handle_command(fake, "1", "/statuz", [entry], "HEALTHY"))

    text = fake.messages[0][1]
    assert "지원하지 않는 명령" in text
    assert "노출되면 안 되는 공고" not in text


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
