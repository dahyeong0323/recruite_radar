from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        project_root=tmp_path / "project",
        vault_root=tmp_path / "vault",
        vault_relative_path=Path("Career/Recruiting_Radar"),
        dry_run=True,
        branch="main",
        git_url=None,
        telegram_bot_token=None,
        telegram_chat_id=None,
        telegram_webhook_secret=None,
        saramin_access_key="test-key",
        openai_api_key=None,
        openai_model_classifier=None,
        timezone="Asia/Seoul",
        kvca_list_url="https://kvca.test/list?page={page}",
        vcs_list_url="https://vcs.test/list?page={page}",
        kofia_list_url="https://kofia.test/list?page={page}",
        saramin_api_url="https://saramin.test/job-search",
    )
