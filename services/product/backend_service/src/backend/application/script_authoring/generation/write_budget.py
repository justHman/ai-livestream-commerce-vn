"""Durable dispatch budget, separate from the number of user generation actions."""

import asyncio


def reserve_from_worker(repo, loop, batch_id, product_id, index):
    """A sync provider worker waits for a committed reservation on the owner loop."""
    return asyncio.run_coroutine_threadsafe(
        repo.reserve(batch_id, product_id, index), loop
    ).result()
