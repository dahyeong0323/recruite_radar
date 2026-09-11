from __future__ import annotations

from app.config import Settings
from app.telegram.client import TelegramClient
from app.vault.git_sync import GitSync
from app.vault.repository import GLOBAL_VAULT_LOCK
from app.vault.operation_lock import OperationInProgress, operation_lock
from app.vault.status import set_user_status_async


ACTION_STATUS = {"interest": "interested", "plan": "will_apply", "ignore": "ignored"}


async def handle_callback(settings: Settings, telegram: TelegramClient, query: dict) -> None:
    message = query.get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id", ""))
    if settings.telegram_chat_id and chat_id != str(settings.telegram_chat_id):
        return
    data = str(query.get("data", ""))
    action, _, job_id = data.partition(":")
    status = ACTION_STATUS.get(action)
    if not status or not job_id:
        await telegram.answer_callback(str(query.get("id")), "알 수 없는 요청")
        return
    try:
        async with operation_lock(settings.vault_root):
            await set_user_status_async(settings.vault_root, settings.radar_root, job_id, status)
            if not settings.dry_run:
                async with GLOBAL_VAULT_LOCK:
                    result = GitSync(
                        settings.vault_root,
                        branch=settings.branch,
                        radar_relative_path=settings.vault_relative_path,
                        dry_run=False,
                        git_url=settings.git_url,
                        github_token=settings.github_token,
                        ssh_deploy_key=settings.ssh_deploy_key,
                    ).commit_and_push(f"radar: mark {job_id} {status}")
                if not result.pushed and result.message != "no changes":
                    await telegram.answer_callback(str(query.get("id")), "상태는 저장됐지만 Git 동기화에 실패했습니다")
                    return
    except OperationInProgress:
        await telegram.answer_callback(str(query.get("id")), "다른 작업이 진행 중입니다. 잠시 후 다시 시도해 주세요")
        return
    except Exception:
        await telegram.answer_callback(str(query.get("id")), "공고 상태 업데이트 실패")
        return
    await telegram.answer_callback(str(query.get("id")), f"상태를 {status}로 변경했습니다")
