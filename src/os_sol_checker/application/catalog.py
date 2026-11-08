"""Import fully before publishing; failed attempts never replace the last good catalog."""
from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from ..domain.models import CatalogSnapshot

_LOG = logging.getLogger(__name__)


class SnapshotImporter(Protocol):
    async def import_snapshot(self, ref: str | None = None) -> CatalogSnapshot: ...


class CatalogPublisher(Protocol):
    async def publish_catalog(self, snapshot: CatalogSnapshot) -> CatalogSnapshot: ...


class CatalogSynchronizer:
    def __init__(self, catalog: SnapshotImporter, store: CatalogPublisher, poll_seconds: float):
        if poll_seconds <= 0:
            raise ValueError("Catalog poll interval must be positive")
        self.catalog = catalog
        self.store = store
        self.poll_seconds = poll_seconds
        self.last_error: str | None = None
        self.last_attempt_commit: str | None = None
        self._lock = asyncio.Lock()

    async def sync(self, ref: str | None = None) -> CatalogSnapshot:
        # Includes publication: no second import can overtake this publication.
        async with self._lock:
            self.last_attempt_commit = None
            try:
                snapshot = await self.catalog.import_snapshot(ref)
                self.last_attempt_commit = snapshot.commit
                published = await self.store.publish_catalog(snapshot)
            except Exception as exc:
                self.last_error = str(exc)
                if self.last_attempt_commit is None:
                    self.last_attempt_commit = getattr(self.catalog, "last_attempt_commit", None)
                raise
            self.last_error = None
            return published

    async def run(self, ref: str | None = None) -> None:
        # The first attempt is immediate, including on a previously empty volume.
        while True:
            try:
                snapshot = await self.sync(ref)
                _LOG.info("Published catalog commit=%s generation=%s tests_digest=%s",
                          snapshot.commit, snapshot.generation, snapshot.tests_digest)
            except Exception:
                _LOG.exception("Catalog import/publication failed; keeping last complete snapshot (commit=%s)",
                               self.last_attempt_commit or "unknown")
            await asyncio.sleep(self.poll_seconds)
