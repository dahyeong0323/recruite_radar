from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar


T = TypeVar("T")


async def with_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    retries: int = 3,
    base_delay: float = 0.5,
    jitter: tuple[float, float] | None = None,
) -> T:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            return await operation()
        except Exception as error:  # noqa: BLE001 - retry boundary intentionally broad
            last_error = error
            if attempt == retries - 1:
                raise
            delay = base_delay * (2**attempt)
            if jitter:
                delay += random.uniform(*jitter)
            await asyncio.sleep(delay)
    assert last_error is not None
    raise last_error
