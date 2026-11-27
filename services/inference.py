"""Bound biometric work so expensive inference never blocks the API event loop."""
import asyncio
from fastapi.concurrency import run_in_threadpool

_slots = asyncio.Semaphore(2)


async def run_inference(function, *args):
    async with _slots:
        return await run_in_threadpool(function, *args)
