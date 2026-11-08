from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from ..application.archive import deliver_successes
from ..application.judging import failed, judge
from ..config import Settings
from ..domain.errors import RunnerError
from ..domain.models import QueueJob
from ..domain.verdicts import Verdict
from ..ports.catalog import CatalogSource
from ..ports.sandbox import SandboxBackend
from ..ports.storage import AcceptedSolutionStore, SubmissionStore

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, settings: Settings, store: SubmissionStore, archive: AcceptedSolutionStore,
                 catalog: CatalogSource, runner: SandboxBackend):
        self.settings, self.store, self.archive = settings, store, archive
        self.catalog, self.runner = catalog, runner
        self.active: dict[UUID, asyncio.Task] = {}
        self.stop = asyncio.Event()

    async def process(self, job: QueueJob) -> None:
        try:
            result = await judge(job, self.catalog, self.runner,
                                 lambda: self.store.job_is_current(job.id),
                                 lambda stage: self.store.set_job_stage(job.id, stage))
            await self.store.complete_job(job.id, result)
        except RunnerError as exc:
            if exc.status_code == 429:
                await self.store.requeue_job(job.id)
            else:
                await self.store.complete_job(job.id, failed(Verdict.INFRASTRUCTURE_ERROR, 0))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Job infrastructure failure: %s", job.id)
            await self.store.complete_job(job.id, failed(Verdict.INFRASTRUCTURE_ERROR, 0))

    async def heartbeat(self) -> None:
        while not self.stop.is_set():
            if not await self.store.leadership_alive():
                raise RuntimeError("scheduler leadership lost")
            try:
                async with asyncio.timeout(5):
                    policy = await self.runner.policy()
                    await self.store.set_policy(policy)
                    if await self.archive.health():
                        await self.store.worker_heartbeat()
            except (RunnerError, OSError, TimeoutError):
                logger.warning("Runner unavailable; readiness heartbeat withheld")
            await asyncio.sleep(5)

    async def archive_deliveries(self) -> None:
        backoff = 1
        while not self.stop.is_set():
            try:
                await deliver_successes(self.store, self.archive)
                backoff = 1
            except Exception:
                logger.warning("Archive delivery pending; no code re-execution")
                backoff = min(30, backoff * 2)
            await asyncio.sleep(backoff)

    async def cleanup(self) -> None:
        while not self.stop.is_set():
            await self.store.cleanup()
            await asyncio.sleep(60)

    async def run(self) -> None:
        async with self.store.scheduler_lock() as acquired:
            if not acquired:
                raise RuntimeError("another scheduler owns the queue")
            for execution in await self.runner.executions():
                await self.runner.destroy(execution)
            await self.store.recover_claimed()
            policy = await self.runner.policy()
            await self.store.set_policy(policy)
            background = [
                asyncio.create_task(self.heartbeat()),
                asyncio.create_task(self.archive_deliveries()),
                asyncio.create_task(self.cleanup()),
            ]
            try:
                while not self.stop.is_set():
                    for task in background:
                        if task.done():
                            task.result()
                            raise RuntimeError("scheduler background task stopped")
                    for job_id, task in list(self.active.items()):
                        if task.done():
                            self.active.pop(job_id)
                            if not task.cancelled() and task.exception():
                                raise RuntimeError(f"job finalization failed: {job_id}") from None
                        elif not await self.store.job_is_current(job_id):
                            task.cancel()
                    while len(self.active) < self.settings.max_concurrent_sandboxes and await self.store.worker_alive():
                        job = await self.store.claim_job()
                        if job is None:
                            break
                        self.active[job.id] = asyncio.create_task(self.process(job))
                    try:
                        await asyncio.wait_for(self.stop.wait(), timeout=0.5)
                    except TimeoutError:
                        pass
            finally:
                tasks = [*self.active.values(), *background]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                self.active.clear()
