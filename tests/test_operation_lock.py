import asyncio

import pytest

from app.vault.operation_lock import OperationInProgress, operation_lock, operation_lock_path
from app.service import RadarService


def test_operation_lock_path_is_stable_and_outside_vault(tmp_path):
    first = operation_lock_path(tmp_path / "vault")
    second = operation_lock_path(tmp_path / "vault")
    assert first == second
    assert not first.is_relative_to(tmp_path / "vault")


def test_operation_lock_blocks_a_second_instance(tmp_path):
    async def run():
        async with operation_lock(tmp_path / "vault"):
            with pytest.raises(OperationInProgress):
                async with operation_lock(tmp_path / "vault"):
                    pass

    asyncio.run(run())


def test_collect_source_fails_safely_when_another_process_holds_lock(settings):
    async def run():
        service = RadarService(settings)
        async with operation_lock(settings.vault_root):
            result = await service.collect_source("kvca")
        assert "already running" in result["error"]

    asyncio.run(run())
