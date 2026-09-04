from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(project_root: Path) -> None:
    path = project_root / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    project_root: Path
    vault_root: Path
    vault_relative_path: Path
    dry_run: bool
    branch: str
    git_url: str | None
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    telegram_webhook_secret: str | None
    saramin_access_key: str | None
    openai_api_key: str | None
    openai_model_classifier: str | None
    timezone: str
    kvca_list_url: str
    vcs_list_url: str
    kofia_list_url: str
    saramin_api_url: str
    http_timeout_seconds: float = 15.0
    http_retries: int = 3
    max_domain_concurrency: int = 2

    @property
    def radar_root(self) -> Path:
        return self.vault_root / self.vault_relative_path

    @property
    def index_path(self) -> Path:
        return self.radar_root / "_System" / "index.json"

    @property
    def state_path(self) -> Path:
        return self.radar_root / "_System" / "state.json"


def load_settings(project_root: Path | None = None) -> Settings:
    root = (project_root or Path(__file__).resolve().parents[1]).resolve()
    _load_dotenv(root)
    dry_run = _bool_env("DRY_RUN", True)
    configured_vault = os.getenv("VAULT_ROOT")
    dry_run_vault = os.getenv("DRY_RUN_VAULT_ROOT")
    if dry_run:
        vault_root = Path(dry_run_vault) if dry_run_vault else root / ".runtime" / "dry-run-vault"
        vault_root = vault_root if vault_root.is_absolute() else (root / vault_root).resolve()
    elif configured_vault:
        vault_root = Path(configured_vault)
        if not vault_root.is_absolute():
            vault_root = (root / vault_root).resolve()
    else:
        vault_root = root.parent

    return Settings(
        project_root=root,
        vault_root=vault_root,
        vault_relative_path=Path(os.getenv("VAULT_RELATIVE_PATH", "Career/Recruiting_Radar")),
        dry_run=dry_run,
        branch=os.getenv("VAULT_BRANCH", "main"),
        git_url=os.getenv("VAULT_GIT_URL") or None,
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
        telegram_webhook_secret=os.getenv("TELEGRAM_WEBHOOK_SECRET") or None,
        saramin_access_key=os.getenv("SARAMIN_ACCESS_KEY") or None,
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        openai_model_classifier=os.getenv("OPENAI_MODEL_CLASSIFIER") or None,
        timezone=os.getenv("TZ", "Asia/Seoul"),
        kvca_list_url=os.getenv(
            "KVCA_LIST_URL",
            "https://roadshow.kvca.or.kr/Program/user_board/list.html?a_cd=7&a_gb=board&a_item=0&key=&keyfield=&page={page}&sm=3_2",
        ),
        vcs_list_url=os.getenv(
            "VCS_LIST_URL", "https://www.vcs.go.kr/web/portal/recruitment/list?page={page}"
        ),
        kofia_list_url=os.getenv(
            "KOFIA_LIST_URL",
            "https://www.kofia.or.kr/brd/m_96/list.do?company_cd=&company_nm=&itm_seq_1=0&itm_seq_2=0&multi_itm_seq=0&page={page}&srchFr=&srchTo=&srchTp=&srchWord=",
        ),
        saramin_api_url=os.getenv("SARAMIN_API_URL", "https://oapi.saramin.co.kr/job-search"),
    )
