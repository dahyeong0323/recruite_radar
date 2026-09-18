from __future__ import annotations

import asyncio
import hashlib
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from filelock import FileLock, Timeout


class OperationInProgress(RuntimeError):
    pass


def operation_lock_path(vault_root: Path) -> Path:
    identity = str(Path(vault_root).resolve()).casefold().encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"recruiting-radar-{digest}.operation.lock"


@asynccontextmanager
async def operation_lock(vault_root: Path, *, timeout: float = 0.0):
    # Acquisition and release run in the executor and are not guaranteed to
    # use the same worker thread. A shared context is therefore required.
    lock = FileLock(operation_lock_path(vault_root), thread_local=False)
    try:
        await asyncio.to_thread(lock.acquire, timeout=timeout)
    except Timeout as error:
        raise OperationInProgress("another Recruiting Radar operation is already running") from error
    try:
        yield
    finally:
        await asyncio.to_thread(lock.release)
