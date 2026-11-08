import asyncio

from ..ports.storage import AcceptedSolutionStore, SubmissionStore


async def deliver_successes(store: SubmissionStore, archive: AcceptedSolutionStore, limit: int = 100) -> int:
    """Archive first; finish rechecks generation after this independent database commit."""
    completed = 0
    for submission in await store.archive_pending(limit):
        async with asyncio.timeout(5):
            await archive.record_success(submission)
            await store.finish_archived(submission.id)
        completed += 1
    return completed
