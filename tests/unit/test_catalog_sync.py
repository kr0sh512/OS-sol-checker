import asyncio
from hashlib import sha256

import pytest

from os_sol_checker.adapters.catalog.git import GitCatalog
from os_sol_checker.adapters.catalog.tree import CatalogError
from os_sol_checker.application.catalog import CatalogSynchronizer
from os_sol_checker.config import Settings
from os_sol_checker.domain.models import CatalogSnapshot


def snapshot(commit="1" * 40):
    return CatalogSnapshot(snapshot_id=sha256(commit.encode()).hexdigest(), commit=commit,
                           tests_digest="2" * 64, root_path="/immutable/snapshot")


class Importer:
    def __init__(self):
        self.failure = None
        self.calls = []
        self.last_attempt_commit = None

    async def import_snapshot(self, ref=None):
        self.calls.append(ref)
        if self.failure:
            self.last_attempt_commit = "f" * 40
            raise self.failure
        return snapshot()


class Publisher:
    def __init__(self):
        self.current = None
        self.calls = []

    async def publish_catalog(self, value):
        self.calls.append(value)
        self.current = value.model_copy(update={"generation": 1})
        return self.current


async def test_import_then_publish_uses_store_generation_and_preserves_last_on_failure():
    importer, store = Importer(), Publisher()
    sync = CatalogSynchronizer(importer, store, 60)
    result = await sync.sync("release")
    assert importer.calls == ["release"]
    assert result.generation == 1 and store.calls[0].generation == 0
    importer.failure = CatalogError("fetch failed")
    with pytest.raises(CatalogError):
        await sync.sync()
    assert store.current == result and len(store.calls) == 1
    assert sync.last_error == "fetch failed" and sync.last_attempt_commit == "f" * 40
    importer.failure = None
    await sync.sync()
    assert sync.last_error is None


async def test_polling_imports_immediately_and_waits_configured_interval_even_after_error(monkeypatch):
    import os_sol_checker.application.catalog as application
    importer, store = Importer(), Publisher()
    events = []
    importer.failure = CatalogError("offline")
    sync = CatalogSynchronizer(importer, store, 7.5)

    async def sleep(interval):
        events.append((interval, len(importer.calls), len(store.calls)))
        if len(events) == 1:
            importer.failure = None
        else:
            raise asyncio.CancelledError
    monkeypatch.setattr(application.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await sync.run("main")
    assert events == [(7.5, 1, 0), (7.5, 2, 1)]


async def test_import_and_publication_are_one_nonoverlapping_sync_operation():
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    class OrderedImporter:
        async def import_snapshot(self, ref=None):
            calls.append(("import", ref))
            return snapshot()

    class BlockingPublisher:
        async def publish_catalog(self, value):
            calls.append(("publish", len(calls)))
            if not entered.is_set():
                entered.set()
                await release.wait()
            return value
    sync = CatalogSynchronizer(OrderedImporter(), BlockingPublisher(), 1)
    first = asyncio.create_task(sync.sync("first"))
    await entered.wait()
    second = asyncio.create_task(sync.sync("second"))
    # The second coroutine is scheduled, but cannot import while publish is pending.
    await asyncio.sleep(0)
    assert calls == [("import", "first"), ("publish", 1)]
    release.set()
    await asyncio.gather(first, second)
    assert calls[2] == ("import", "second")


async def test_production_import_does_not_allow_local_fixture_url(tmp_path):
    catalog = GitCatalog(Settings(catalog_root=tmp_path / "catalog", tasks_repository_url=str(tmp_path)))
    with pytest.raises(CatalogError, match="HTTPS"):
        await catalog.import_snapshot()
    assert not (tmp_path / "catalog").exists()
