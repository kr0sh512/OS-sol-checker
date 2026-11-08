from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import os
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from os_sol_checker.adapters.postgres import ArchiveStore, TransientStore
from os_sol_checker.adapters.postgres.migrate import migrate_database
from os_sol_checker.config import Settings
from os_sol_checker.domain.errors import CheckerError
from os_sol_checker.domain.models import (
    CatalogSnapshot, CheckResult, Failure, JudgeContext, JudgePolicy, Task, TestCase, Topic,
)
from os_sol_checker.domain.policies import identify
from os_sol_checker.domain.verdicts import SubmissionStatus, Verdict


def catalog(*, digest="1" * 64, snapshot_id="snapshot-1", title="Task"):
    task = Task(id="topic/task", topic_id="topic", title=title, relative_path="topic/task",
                tests=[TestCase(id="1", input_path="tests/1.dat", expected_path="tests/1.ans")])
    return CatalogSnapshot(snapshot_id=snapshot_id, commit="a" * 40, tests_digest=digest,
                           root_path="/catalog/immutable", topics=[Topic(id="topic", title="Topic", tasks=[task])])


def policy():
    return JudgePolicy(runner_image_digest="sha256:" + "4" * 64,
                       sandbox_profile_digest="2" * 64, flags_digest="3" * 64)


def success():
    return CheckResult(verdict=Verdict.ACCEPTED, passed_tests=1, total_tests=1)


def failure(verdict=Verdict.COMPILE_ERROR):
    return CheckResult(verdict=verdict, passed_tests=0, total_tests=1,
                       first_failure=Failure(stage="compile", code=verdict.value, message="Ошибка проверки."))


@dataclass
class DatabasePair:
    transient: TransientStore
    archive: ArchiveStore
    settings: Settings
    snapshot: CatalogSnapshot
    policy: JudgePolicy

    @property
    def task(self):
        return self.snapshot.topics[0].tasks[0]

    @property
    def context(self):
        return JudgeContext.create(self.snapshot, self.policy)

    async def author(self, name="Author", group="Group"):
        session = await self.transient.create_session(uuid4().hex, 30)
        return await self.transient.update_profile(session.id, name, group)

    async def accept(self, author, source=b"int main(void) { return 0; }", capacity=1000):
        return await self.transient.accept_submission(author.id, self.task, self.context, source, "solution.c", capacity)


@pytest.fixture
async def databases():
    """Real PostgreSQL only. Every test gets two private, disposable schemas, never host execution."""
    dsns = [os.getenv("TEST_TRANSIENT_DATABASE_URL"), os.getenv("TEST_ARCHIVE_DATABASE_URL")]
    if not all(dsns):
        pytest.skip("set TEST_TRANSIENT_DATABASE_URL and TEST_ARCHIVE_DATABASE_URL to test-owner PostgreSQL DSNs")
    schemas = ["checker_test_" + uuid4().hex for _ in dsns]
    prepared = []
    transient = archive = None
    try:
        for dsn, schema in zip(dsns, schemas, strict=True):
            async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as connection:
                await connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            prepared.append((dsn, schema))
        settings = Settings(
            transient_database_url=make_conninfo(dsns[0], options=f"-c search_path={schemas[0]}"),
            archive_database_url=make_conninfo(dsns[1], options=f"-c search_path={schemas[1]}"),
        )
        await migrate_database(settings.transient_database_url, "transient")
        await migrate_database(settings.archive_database_url, "archive")
        # Reapplying the same packaged migrations must be idempotent.
        await migrate_database(settings.transient_database_url, "transient")
        await migrate_database(settings.archive_database_url, "archive")
        transient, archive = TransientStore(settings), ArchiveStore(settings)
        await transient.open()
        await archive.open()
        published, judge_policy = await transient.publish_catalog(catalog()), policy()
        await transient.set_policy(judge_policy)
        yield DatabasePair(transient, archive, settings, published, judge_policy)
    finally:
        if transient:
            await transient.close()
        if archive:
            await archive.close()
        for dsn, schema in reversed(prepared):
            async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as connection:
                await connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


async def test_concurrent_duplicate_accepts_one_job_and_own_author(databases):
    db = databases
    authors = [await db.author(f"Author {index}", f"Group {index}") for index in range(12)]
    submissions = await asyncio.gather(*(db.accept(author, capacity=1) for author in authors))
    assert len({item.id for item in submissions}) == len(authors)
    assert len({item.queue_job_id for item in submissions}) == 1
    assert all(item.queue_position == 1 and not item.cache_hit for item in submissions)
    for author, item in zip(authors, submissions, strict=True):
        assert item.author_name == author.name and item.author_group == author.group
        assert await db.transient.get_submission(authors[(authors.index(author) + 1) % len(authors)].id, item.id) is None
        assert await db.transient.last_submissions(author.id) == [dict(task_id=db.task.id, id=item.id, available=True)]


async def test_capacity_is_global_across_store_pools_and_rejection_preserves_last_pointer(databases):
    db = databases
    other = TransientStore(db.settings)
    await other.open()
    author = await db.author()
    try:
        outcomes = await asyncio.gather(*(
            (db.transient if index % 2 else other).accept_submission(
                author.id, db.task, db.context, f"/* {index} */".encode(), "solution.c", 3)
            for index in range(10)
        ), return_exceptions=True)
        accepted = [item for item in outcomes if not isinstance(item, BaseException)]
        rejected = [item for item in outcomes if isinstance(item, CheckerError)]
        assert len(accepted) == 3 and len(rejected) == 7
        assert all(error.code == "queue_full" for error in rejected)
        pointers = await db.transient.last_submissions(author.id)
        assert pointers[0]["id"] in {item.id for item in accepted}
        async with db.transient.pool.connection() as connection:
            count = await (await connection.execute("SELECT count(*) AS n FROM queue_jobs WHERE state = 'queued'")).fetchone()
            assert count["n"] == 3
    finally:
        await other.close()


async def test_fifo_stages_followers_and_requeue_before_runner_open(databases):
    db = databases
    author = await db.author()
    first = await db.accept(author, b"first")
    follower = await db.accept(author, b"first")
    second = await db.accept(author, b"second")
    async with db.transient.scheduler_lock() as acquired:
        assert acquired and await db.transient.leadership_alive()
        job = await db.transient.claim_job()
        assert job.id == first.queue_job_id
        assert (await db.transient.get_submission(author.id, follower.id)).status == SubmissionStatus.COMPILING
        await db.transient.requeue_job(job.id)
        reset = await db.transient.get_submission(author.id, follower.id)
        assert reset.status == SubmissionStatus.QUEUED and reset.started_at is None and reset.queue_position == 1
        assert (await db.transient.claim_job()).id == job.id
        await db.transient.set_job_stage(job.id, SubmissionStatus.RUNNING)
        assert (await db.transient.get_submission(author.id, follower.id)).status == SubmissionStatus.RUNNING
        await db.transient.complete_job(job.id, failure())
        assert (await db.transient.claim_job()).id == second.queue_job_id


async def test_failure_cache_never_leaks_other_submission_metadata_or_extends_ttl(databases):
    db = databases
    first_author, second_author = await db.author("First", "A"), await db.author("Second", "B")
    first = await db.accept(first_author)
    async with db.transient.scheduler_lock():
        job = await db.transient.claim_job()
        await db.transient.complete_job(job.id, failure())
    async with db.transient.pool.connection() as connection:
        before = await (await connection.execute("SELECT context, created_at, expires_at FROM result_cache")).fetchone()
    cached = await db.accept(second_author)
    assert cached.id != first.id and cached.session_id == second_author.id
    assert cached.author_name == "Second" and cached.author_group == "B"
    assert cached.cache_hit and cached.status == SubmissionStatus.FINISHED
    assert cached.result.verdict == Verdict.COMPILE_ERROR and cached.queue_job_id is None
    async with db.transient.pool.connection() as connection:
        after = await (await connection.execute("SELECT context, created_at, expires_at FROM result_cache")).fetchone()
    assert after == before


async def test_success_followers_and_cache_hits_need_individual_archive_receipts(databases):
    db = databases
    authors = [await db.author(f"Author {index}") for index in range(3)]
    first, follower = await db.accept(authors[0]), await db.accept(authors[1])
    async with db.transient.scheduler_lock():
        job = await db.transient.claim_job()
        finished = await db.transient.complete_job(job.id, success())
    assert {item.id for item in finished} == {first.id, follower.id}
    assert all(item.status == SubmissionStatus.ARCHIVE_PENDING and item.result is None for item in finished)
    # An arrival during archive outage follows the existing proof without running again.
    late_follower = await db.accept(authors[2], capacity=1)
    assert late_follower.status == SubmissionStatus.ARCHIVE_PENDING and not late_follower.cache_hit
    assert late_follower.queue_job_id == job.id
    pending = await db.transient.archive_pending(limit=2)
    assert len(pending) == 2
    all_pending = await db.transient.archive_pending()
    for item in all_pending:
        await db.archive.record_success(item)
        await db.transient.finish_archived(item.id)
    cached = await db.accept(authors[2])
    assert cached.cache_hit and cached.status == SubmissionStatus.ARCHIVE_PENDING
    assert cached.proven_result.verdict == Verdict.ACCEPTED and cached.result is None
    await asyncio.gather(*(db.archive.record_success(cached) for _ in range(5)))
    await db.transient.finish_archived(cached.id)
    own_result = await db.transient.get_submission(authors[2].id, cached.id)
    assert own_result.status == SubmissionStatus.FINISHED and own_result.result.verdict == Verdict.ACCEPTED
    async with db.archive.pool.connection() as connection:
        counts = await (await connection.execute(
            "SELECT (SELECT count(*) FROM blobs) AS blobs, (SELECT count(*) FROM accepted_solutions) AS solutions, "
            "(SELECT count(*) FROM success_receipts) AS receipts"
        )).fetchone()
        receipt = await (await connection.execute("SELECT * FROM success_receipts WHERE submission_id = %s", (cached.id,))).fetchone()
    assert counts == dict(blobs=1, solutions=1, receipts=4)
    assert receipt["author_name"] == authors[2].name and receipt["context"] == cached.context.model_dump(mode="json")


async def test_archive_commit_before_catalog_change_remains_historical_not_current(databases):
    db = databases
    author = await db.author()
    submission = await db.accept(author)
    async with db.transient.scheduler_lock():
        job = await db.transient.claim_job()
        [pending] = await db.transient.complete_job(job.id, success())
    await db.archive.record_success(pending)
    newer = await db.transient.publish_catalog(catalog(digest="5" * 64, snapshot_id="snapshot-2"))
    assert newer.generation == db.snapshot.generation + 1
    await db.transient.finish_archived(pending.id)
    stale = await db.transient.get_submission(author.id, submission.id)
    assert stale.status == SubmissionStatus.INVALIDATED and stale.state_reason == "stale_generation"
    assert stale.result is None and stale.proven_result is None and stale.source == b""
    assert not await db.transient.job_is_current(job.id)
    assert not await db.transient.archive_pending()
    async with db.archive.pool.connection() as connection:
        receipt = await (await connection.execute("SELECT context FROM success_receipts WHERE submission_id = %s", (pending.id,))).fetchone()
    assert receipt["context"]["generation"] == db.snapshot.generation
    async with db.transient.pool.connection() as connection:
        count = await (await connection.execute("SELECT count(*) AS n FROM result_cache")).fetchone()
    assert count["n"] == 0


async def test_concurrent_accept_and_publication_never_leave_stale_work_current(databases):
    db = databases
    author = await db.author()
    outcomes = await asyncio.gather(
        db.accept(author), db.transient.publish_catalog(catalog(digest="5" * 64, snapshot_id="snapshot-2")),
        return_exceptions=True,
    )
    accepted, newer = outcomes
    assert newer.generation == db.snapshot.generation + 1
    if isinstance(accepted, CheckerError):
        assert accepted.code == "stale_context"
        assert await db.transient.last_submissions(author.id) == []
    else:
        current = await db.transient.get_submission(author.id, accepted.id)
        assert current.status == SubmissionStatus.INVALIDATED
        assert not await db.transient.job_is_current(accepted.queue_job_id)


async def test_description_only_publication_preserves_generation_and_active_job(databases):
    db = databases
    author = await db.author()
    submitted = await db.accept(author)
    changed = await db.transient.publish_catalog(catalog(snapshot_id="snapshot-description", title="Changed description"))
    assert changed.generation == db.snapshot.generation
    assert await db.transient.job_is_current(submitted.queue_job_id)
    assert (await db.transient.current_catalog()).model_dump() == changed.model_dump()
    with pytest.raises(CheckerError, match="Контекст") as error:
        await db.accept(author)
    assert error.value.code == "stale_context"


async def test_policy_invalidation_does_not_increment_generation(databases):
    db = databases
    author = await db.author()
    submission = await db.accept(author)
    replacement = db.policy.model_copy(update={"flags_digest": "6" * 64})
    async with db.transient.scheduler_lock():
        job = await db.transient.claim_job()
        await db.transient.set_policy(replacement)
        assert await db.transient.complete_job(job.id, success()) == []
    invalidated = await db.transient.get_submission(author.id, submission.id)
    assert invalidated.status == SubmissionStatus.INVALIDATED and invalidated.state_reason == "stale_policy"
    assert (await db.transient.current_catalog()).generation == db.snapshot.generation
    assert (await db.transient.current_policy()).digest == replacement.digest


async def test_result_ttl_preserves_session_and_unavailable_last_pointer(databases):
    db = databases
    author = await db.author()
    submission = await db.accept(author)
    async with db.transient.scheduler_lock():
        job = await db.transient.claim_job()
        await db.transient.complete_job(job.id, failure())
    async with db.transient.pool.connection() as connection:
        await connection.execute("UPDATE submissions SET expires_at = clock_timestamp() - interval '1 second' WHERE id = %s", (submission.id,))
        await connection.execute("UPDATE result_cache SET expires_at = clock_timestamp() - interval '1 second'")
    assert await db.transient.get_submission(author.id, submission.id) is None
    await db.transient.cleanup()
    assert await db.transient.last_submissions(author.id) == [dict(task_id=db.task.id, id=submission.id, available=False)]
    assert (await db.transient.update_profile(author.id, "Still here", "Group")).name == "Still here"
    async with db.transient.pool.connection() as connection:
        row = await (await connection.execute("SELECT count(*) AS n FROM queue_jobs")).fetchone()
    assert row["n"] == 0


async def test_expired_session_does_not_revoke_active_author_snapshot_or_drop_pending(databases):
    db = databases
    token = uuid4().hex
    author = await db.transient.create_session(token, 30)
    author = await db.transient.update_profile(author.id, "Original author", "Original group")
    submission = await db.accept(author)
    async with db.transient.pool.connection() as connection:
        await connection.execute("UPDATE sessions SET expires_at = clock_timestamp() - interval '1 second' WHERE id = %s", (author.id,))
    assert await db.transient.get_session(token, 30) is None
    await db.transient.cleanup()
    replacement = await db.transient.create_session(token, 30)
    assert replacement.id != author.id and replacement.name is None
    assert await db.transient.get_submission(author.id, submission.id) is None
    async with db.transient.scheduler_lock():
        job = await db.transient.claim_job()
        [pending] = await db.transient.complete_job(job.id, success())
    await db.transient.cleanup()
    assert [item.id for item in await db.transient.archive_pending()] == [pending.id]
    await db.archive.record_success(pending)
    await db.transient.finish_archived(pending.id)
    async with db.archive.pool.connection() as connection:
        receipt = await (await connection.execute("SELECT author_name FROM success_receipts WHERE submission_id = %s", (submission.id,))).fetchone()
    assert receipt["author_name"] == "Original author"


async def test_scheduler_exclusion_recovery_and_infrastructure_error_not_cached(databases):
    db = databases
    author = await db.author()
    first = await db.accept(author)
    contender = TransientStore(db.settings)
    await contender.open()
    try:
        async with db.transient.scheduler_lock() as first_lock:
            assert first_lock
            await db.transient.worker_heartbeat()
            assert await contender.worker_alive()
            async with contender.scheduler_lock() as second_lock:
                assert not second_lock and not await contender.leadership_alive()
            job = await db.transient.claim_job()
        async with contender.scheduler_lock() as second_lock:
            assert second_lock
            # No runner handles exist in this repository-only test; recovery may now terminalize work.
            await contender.recover_claimed()
            recovered = await contender.get_submission(author.id, first.id)
            assert recovered.status == SubmissionStatus.FINISHED
            assert recovered.result.verdict == Verdict.INFRASTRUCTURE_ERROR
            repeated = await contender.accept_submission(author.id, db.task, db.context, first.source, "again.c", 1)
            assert repeated.queue_job_id != job.id and not repeated.cache_hit
    finally:
        await contender.close()


async def test_collision_rejects_acceptance_without_mutating_last_pointer(databases):
    db = databases
    author = await db.author()
    first = await db.accept(author, b"one")
    # Simulate corrupt storage/hash collision without changing the production identity algorithm.
    async with db.transient.pool.connection() as connection:
        await connection.execute("UPDATE submissions SET source = %s WHERE id = %s", (b"two", first.id))
    with pytest.raises(CheckerError) as error:
        await db.accept(author, b"one")
    assert error.value.code == "identity_conflict"
    assert (await db.transient.last_submissions(author.id))[0]["id"] == first.id


async def test_archive_collision_and_receipt_author_conflict_are_immutable(databases):
    db = databases
    author = await db.author()
    submitted = await db.accept(author, b"one")
    async with db.transient.scheduler_lock():
        job = await db.transient.claim_job()
        [pending] = await db.transient.complete_job(job.id, success())
    async with db.archive.pool.connection() as connection:
        await connection.execute("INSERT INTO blobs(sha256, byte_size, source) VALUES (%s, %s, %s)", (*identify(b"one"), b"two"))
    with pytest.raises(CheckerError) as collision:
        await db.archive.record_success(pending)
    assert collision.value.code == "identity_conflict"
    async with db.archive.pool.connection() as connection:
        stored = await (await connection.execute("SELECT source FROM blobs")).fetchone()
    assert bytes(stored["source"]) == b"two"
    # A different source can archive successfully, but its UUID receipt cannot change author afterwards.
    another = await db.accept(author, b"another")
    async with db.transient.scheduler_lock():
        job = await db.transient.claim_job()
        [proven] = await db.transient.complete_job(job.id, success())
    await db.archive.record_success(proven)
    with pytest.raises(CheckerError) as conflict:
        await db.archive.record_success(proven.model_copy(update={"author_name": "Other"}))
    assert conflict.value.code == "identity_conflict"
    async with db.archive.pool.connection() as connection:
        receipt = await (await connection.execute("SELECT author_name FROM success_receipts WHERE submission_id = %s", (another.id,))).fetchone()
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            await connection.execute("UPDATE success_receipts SET author_name = 'Other' WHERE submission_id = %s", (another.id,))
    assert receipt["author_name"] == author.name and submitted.id != another.id


async def test_empty_source_and_inclusive_source_boundary(databases):
    db = databases
    author = await db.author()
    empty = await db.accept(author, b"")
    exact = await db.accept(author, b"x" * 102400)
    assert empty.byte_size == 0 and exact.byte_size == 102400
    with pytest.raises(CheckerError) as error:
        await db.accept(author, b"x" * 102401)
    assert error.value.code == "source_too_large"
    assert (await db.transient.last_submissions(author.id))[0]["id"] == exact.id


async def test_session_refresh_is_sliding_thirty_days(databases):
    db = databases
    token = uuid4().hex
    profile = await db.transient.create_session(token, 30)
    async with db.transient.pool.connection() as connection:
        await connection.execute("UPDATE sessions SET expires_at = clock_timestamp() + interval '1 hour' WHERE id = %s", (profile.id,))
    refreshed = await db.transient.get_session(token, 30)
    assert refreshed.id == profile.id and refreshed.expires_at > profile.expires_at - timedelta(minutes=1)
    assert refreshed.expires_at - refreshed.expires_at.now(refreshed.expires_at.tzinfo) > timedelta(days=29)


async def test_permanent_acceptance_key_ignores_transient_generation_but_receipts_keep_context(databases):
    db = databases
    author = await db.author()
    await db.accept(author, b"\x00\xff\r\n")
    async with db.transient.scheduler_lock():
        job = await db.transient.claim_job()
        [pending] = await db.transient.complete_job(job.id, success())
    await db.archive.record_success(pending)
    later_context = pending.context.model_copy(update={
        "generation": pending.context.generation + 100, "snapshot_id": "later-immutable-snapshot",
    })
    later = pending.model_copy(update={"id": uuid4(), "context": later_context, "author_name": "Later author"})
    await db.archive.record_success(later)
    async with db.archive.pool.connection() as connection:
        acceptances = await (await connection.execute("SELECT context FROM accepted_solutions")).fetchall()
        receipts = await (await connection.execute("SELECT context, author_name FROM success_receipts")).fetchall()
        blob = await (await connection.execute("SELECT source FROM blobs")).fetchone()
    assert len(acceptances) == 1 and acceptances[0]["context"] == pending.context.model_dump(mode="json")
    assert {row["context"]["generation"] for row in receipts} == {pending.context.generation, later_context.generation}
    assert {row["author_name"] for row in receipts} == {author.name, "Later author"}
    assert bytes(blob["source"]) == b"\x00\xff\r\n"


async def test_finalization_racing_invalidation_never_publishes_stale_cache(databases):
    db = databases
    author = await db.author()
    await db.accept(author)
    async with db.transient.scheduler_lock():
        job = await db.transient.claim_job()
        [pending] = await db.transient.complete_job(job.id, success())
    await db.archive.record_success(pending)
    await asyncio.gather(
        db.transient.finish_archived(pending.id),
        db.transient.publish_catalog(catalog(digest="7" * 64, snapshot_id="racing-catalog")),
    )
    result = await db.transient.get_submission(author.id, pending.id)
    assert result is None or result.status == SubmissionStatus.INVALIDATED
    async with db.transient.pool.connection() as connection:
        cached = await (await connection.execute("SELECT count(*) AS n FROM result_cache")).fetchone()
    async with db.archive.pool.connection() as connection:
        archived = await (await connection.execute("SELECT count(*) AS n FROM success_receipts")).fetchone()
    assert cached["n"] == 0 and archived["n"] == 1


async def test_service_roles_allow_real_flows_but_deny_cross_role_mutation(databases):
    db = databases
    roles = ["checker_test_" + uuid4().hex for _ in range(3)]
    created, stores = [], []
    async with db.transient.pool.connection() as owner:
        capability = await (await owner.execute(
            "SELECT current_user AS name, rolcreaterole OR rolsuper AS allowed FROM pg_roles WHERE rolname = current_user"
        )).fetchone()
        if not capability["allowed"]:
            pytest.skip("service-role regression requires a test-owner DSN with CREATEROLE")
        schema = (await (await owner.execute("SELECT current_schema() AS name")).fetchone())["name"]
    try:
        async with db.transient.pool.connection() as owner:
            for role in roles:
                await owner.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
                created.append(role)
                await owner.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(role), sql.Identifier(capability["name"])))
        await migrate_database(db.settings.transient_database_url, "transient",
                               web_role=roles[0], worker_role=roles[1], catalog_role=roles[2])
        for role in roles:
            settings = Settings(transient_database_url=make_conninfo(
                db.settings.transient_database_url, options=f"-c search_path={schema} -c role={role}",
            ))
            store = TransientStore(settings)
            await store.open()
            stores.append(store)
        web, worker, catalog_store = stores
        author = await web.create_session(uuid4().hex, 30)
        author = await web.update_profile(author.id, "Role test", "Group")
        submitted = await web.accept_submission(author.id, db.task, db.context, b"role test", "test.c", 1)
        async with worker.scheduler_lock() as leader:
            assert leader
            job = await worker.claim_job()
            [pending] = await worker.complete_job(job.id, success())
            await db.archive.record_success(pending)
            await worker.finish_archived(pending.id)
            await worker.worker_heartbeat()
            await worker.cleanup()
        assert (await web.get_submission(author.id, submitted.id)).result.verdict == Verdict.ACCEPTED
        async with web.pool.connection() as connection:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await connection.execute("UPDATE submissions SET archive_receipt_confirmed = true")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await connection.execute("UPDATE checker_state SET generation = generation + 1")
        published = await catalog_store.publish_catalog(catalog(digest="8" * 64, snapshot_id="role-catalog"))
        assert published.generation == db.snapshot.generation + 1
        assert await web.last_submissions(author.id) == [dict(task_id=db.task.id, id=submitted.id, available=False)]
        async with catalog_store.pool.connection() as connection:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await connection.execute("SELECT source FROM submissions")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await connection.execute("SELECT token_hash FROM sessions")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await worker.publish_catalog(catalog(digest="9" * 64, snapshot_id="worker-cannot-publish"))
    finally:
        for store in stores:
            await store.close()
        async with db.transient.pool.connection() as owner:
            for role in reversed(created):
                await owner.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                await owner.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


async def test_follower_racing_stage_change_observes_current_job_stage(databases):
    db = databases
    author = await db.author()
    await db.accept(author)
    async with db.transient.scheduler_lock():
        job = await db.transient.claim_job()
        _, follower = await asyncio.gather(
            db.transient.set_job_stage(job.id, SubmissionStatus.RUNNING), db.accept(author),
        )
        current = await db.transient.get_submission(author.id, follower.id)
        assert current.status == SubmissionStatus.RUNNING
        assert current.started_at is not None and current.queue_job_id == job.id
        with pytest.raises(ValueError, match="never be requeued"):
            await db.transient.requeue_job(job.id)


async def test_expired_cache_can_be_replaced_without_reviving_old_result(databases):
    db = databases
    author = await db.author()
    first = await db.accept(author)
    async with db.transient.scheduler_lock():
        job = await db.transient.claim_job()
        await db.transient.complete_job(job.id, failure())
        async with db.transient.pool.connection() as connection:
            await connection.execute("UPDATE result_cache SET expires_at = clock_timestamp() - interval '1 second'")
        fresh = await db.accept(author)
        assert not fresh.cache_hit and fresh.queue_job_id != first.queue_job_id
        new_job = await db.transient.claim_job()
        await db.transient.complete_job(new_job.id, failure(Verdict.WRONG_ANSWER))
    cached = await db.accept(author)
    assert cached.cache_hit and cached.result.verdict == Verdict.WRONG_ANSWER


async def test_archive_survives_result_ttl_and_transient_cleanup(databases):
    db = databases
    author = await db.author()
    submitted = await db.accept(author)
    async with db.transient.scheduler_lock():
        job = await db.transient.claim_job()
        [pending] = await db.transient.complete_job(job.id, success())
    await db.archive.record_success(pending)
    await db.transient.finish_archived(submitted.id)
    async with db.transient.pool.connection() as connection:
        await connection.execute("UPDATE submissions SET expires_at = clock_timestamp() - interval '1 second'")
        await connection.execute("UPDATE result_cache SET expires_at = clock_timestamp() - interval '1 second'")
    await db.transient.cleanup()
    assert await db.transient.get_submission(author.id, submitted.id) is None
    assert await db.transient.last_submissions(author.id) == [dict(task_id=db.task.id, id=submitted.id, available=False)]
    async with db.archive.pool.connection() as connection:
        historical = await (await connection.execute(
            "SELECT r.context, r.author_name, b.source FROM success_receipts r "
            "JOIN accepted_solutions a ON a.id = r.accepted_solution_id "
            "JOIN blobs b ON (b.sha256, b.byte_size) = (a.sha256, a.byte_size)"
        )).fetchone()
    assert historical["context"] == submitted.context.model_dump(mode="json")
    assert historical["author_name"] == author.name and bytes(historical["source"]) == submitted.source


async def test_acceptance_rechecks_profile_and_task_with_no_partial_pointer(databases):
    db = databases
    author = await db.transient.create_session(uuid4().hex, 30)
    with pytest.raises(CheckerError) as profile:
        await db.accept(author)
    assert (profile.value.code, profile.value.status_code) == ("profile_required", 422)
    assert await db.transient.last_submissions(author.id) == []
    author = await db.transient.update_profile(author.id, "Author", "Group")
    unknown = db.task.model_copy(update={"id": "unknown/task"})
    with pytest.raises(CheckerError) as missing:
        await db.transient.accept_submission(author.id, unknown, db.context, b"", "empty.c", 1)
    assert (missing.value.code, missing.value.status_code) == ("task_not_found", 404)
    inactive_snapshot = catalog(snapshot_id="inactive-task")
    inactive_snapshot.topics[0].tasks[0].availability = False
    published = await db.transient.publish_catalog(inactive_snapshot)
    with pytest.raises(CheckerError) as inactive:
        await db.transient.accept_submission(author.id, published.topics[0].tasks[0],
            JudgeContext.create(published, db.policy), b"", "empty.c", 1)
    assert (inactive.value.code, inactive.value.status_code) == ("task_inactive", 409)
    assert await db.transient.last_submissions(author.id) == []
