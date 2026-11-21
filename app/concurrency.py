from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

_POOL: ThreadPoolExecutor | None = None
_MAX_WORKERS = 32


def get_blocking_pool() -> ThreadPoolExecutor:
    global _POOL
    if _POOL is None:
        _POOL = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="bot-io")
    return _POOL


def shutdown_blocking_pool() -> None:
    global _POOL
    if _POOL is None:
        return
    _POOL.shutdown(wait=False, cancel_futures=False)
    _POOL = None


async def run_blocking(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    if kwargs:
        return await loop.run_in_executor(get_blocking_pool(), lambda: func(*args, **kwargs))
    return await loop.run_in_executor(get_blocking_pool(), func, *args)
