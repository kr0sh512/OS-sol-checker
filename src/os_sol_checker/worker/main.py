import asyncio
import logging
import signal

from ..adapters.catalog.git import GitCatalog
from ..adapters.postgres.archive import ArchiveStore
from ..adapters.postgres.transient import TransientStore
from ..adapters.runner.client import RunnerClient
from ..config import Settings
from .scheduler import Scheduler


async def serve(settings: Settings) -> None:
    settings.require("worker")
    store, archive, runner = TransientStore(settings), ArchiveStore(settings), RunnerClient(settings)
    try:
        await store.open()
        await archive.open()
        scheduler = Scheduler(settings, store, archive, GitCatalog(settings), runner)
        for signum in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(signum, scheduler.stop.set)
        await scheduler.run()
    finally:
        await runner.close()
        await archive.close()
        await store.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve(Settings()))


if __name__ == "__main__":
    main()
