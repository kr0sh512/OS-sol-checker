from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
import unicodedata
from uuid import UUID, uuid4

from psycopg import AsyncConnection, Error
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ...config import Settings
from ...domain.errors import CheckerError
from ...domain.models import (
    CatalogSnapshot, CheckResult, Failure, JudgeContext, JudgePolicy, QueueJob,
    SessionProfile, Submission, Task,
)
from ...domain.policies import identify
from ...domain.verdicts import SubmissionStatus, Verdict
from .base import PostgresStore

# Fixed, database-local advisory keys shared by every process and replica.
CONTEXT_LOCK = 0x4F53430001
QUEUE_LOCK = 0x4F53430002
SCHEDULER_LOCK = 0x4F53430003


def _json(value):
    return Jsonb(value.model_dump(mode="json"))


def _submission(row: dict) -> Submission:
    fields = {key: row[key] for key in Submission.model_fields if key in row}
    fields["source"] = bytes(row["source"]) if row["source"] is not None else b""
    return Submission.model_validate(fields)


def _profile(row: dict) -> SessionProfile:
    return SessionProfile(id=row["id"], name=row["name"], group=row["group"], expires_at=row["expires_at"])


def _current(state: dict, context: JudgeContext) -> bool:
    return (
        state["catalog"] is not None and state["policy"] is not None
        and state["generation"] == context.generation
        and state["tests_digest"] == context.tests_digest
        and state["policy_digest"] == context.judge_policy_digest
        and state["policy"] == context.model_dump(mode="json", include=set(JudgePolicy.model_fields))
    )


def _validate_profile(name: str, group: str) -> tuple[str, str]:
    name, group = name.strip(), group.strip()
    if not (1 <= len(name) <= 120 and 1 <= len(group) <= 64) or any(
        unicodedata.category(char) in {"Cc", "Cf"} for char in name + group
    ):
        raise CheckerError("invalid_profile", "Укажите допустимые имя и группу.", 422)
    return name, group


class TransientStore(PostgresStore):
    database_kind = "transient"

    def __init__(self, settings: Settings):
        super().__init__(settings.transient_database_url)
        self.settings = settings
        self.ttl = timedelta(hours=settings.transient_ttl_hours)
        self.scheduler_id = uuid4()
        self._leader: AsyncConnection | None = None

    async def close(self) -> None:
        if self._leader is not None:
            await self._leader.close()
            self._leader = None
        await super().close()

    async def _state(self, connection, *, exclusive: bool = False) -> dict:
        lock = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
        await connection.execute(f"SELECT {lock}(%s)", (CONTEXT_LOCK,))
        return await (await connection.execute("SELECT * FROM checker_state WHERE singleton")).fetchone()

    async def _queue_lock(self, connection) -> None:
        await connection.execute("SELECT pg_advisory_xact_lock(%s)", (QUEUE_LOCK,))

    async def _load(self, connection, submission_id: UUID) -> Submission | None:
        row = await (await connection.execute(
            """SELECT s.*, CASE WHEN s.status = 'queued' AND j.state = 'queued' THEN
                (SELECT count(*) FROM queue_jobs q WHERE q.state = 'queued'
                 AND (q.created_at, q.id) <= (j.created_at, j.id)) ELSE NULL END AS queue_position
               FROM submissions s LEFT JOIN queue_jobs j ON j.id = s.queue_job_id WHERE s.id = %s""",
            (submission_id,),
        )).fetchone()
        return _submission(row) if row else None

    async def _invalidate(self, connection, reason: str, *, remove_finished: bool = False) -> None:
        # Keep job IDs/context for cancellation and cleanup; erase stale source and proof.
        await connection.execute(
            """UPDATE queue_jobs SET state = 'invalidated', stage = 'invalidated', source = NULL,
                result = NULL, finished_at = clock_timestamp(), state_reason = %s
               WHERE state IN ('queued', 'claimed')""", (reason,),
        )
        await connection.execute(
            """UPDATE submissions SET status = 'invalidated', result = NULL, proven_result = NULL,
                source = NULL, state_reason = %s, finished_at = clock_timestamp(),
                expires_at = clock_timestamp() + %s
               WHERE status NOT IN ('finished', 'invalidated')""", (reason, self.ttl),
        )
        if remove_finished:
            await connection.execute("DELETE FROM submissions WHERE status = 'finished'")
        await connection.execute("DELETE FROM result_cache")
        await connection.execute(
            "UPDATE queue_jobs SET source = NULL WHERE state IN ('finished', 'invalidated')"
        )

    async def publish_catalog(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        async with self.pool.connection() as connection, connection.transaction():
            state = await self._state(connection, exclusive=True)
            changed = state["tests_digest"] != snapshot.tests_digest
            generation = state["generation"] + int(changed)
            published = snapshot.model_copy(update={"generation": generation})
            # A snapshot ID names immutable content; generation is publication metadata.
            existing = await (await connection.execute(
                "SELECT snapshot FROM catalog_snapshots WHERE snapshot_id = %s", (snapshot.snapshot_id,),
            )).fetchone()
            if existing:
                prior = CatalogSnapshot.model_validate(existing["snapshot"])
                if prior.model_dump(exclude={"generation"}) != published.model_dump(exclude={"generation"}):
                    raise CheckerError("identity_conflict", "Конфликт идентичности снимка каталога.")
            else:
                await connection.execute(
                    "INSERT INTO catalog_snapshots(snapshot_id, snapshot) VALUES (%s, %s)",
                    (snapshot.snapshot_id, _json(published)),
                )
            if changed:
                await self._invalidate(connection, "stale_generation", remove_finished=True)
            await connection.execute(
                """UPDATE checker_state SET catalog = %s, generation = %s, tests_digest = %s
                   WHERE singleton""", (_json(published), generation, snapshot.tests_digest),
            )
            return published

    async def current_catalog(self) -> CatalogSnapshot | None:
        async with self.pool.connection() as connection:
            row = await (await connection.execute("SELECT catalog FROM checker_state WHERE singleton")).fetchone()
            return CatalogSnapshot.model_validate(row["catalog"]) if row["catalog"] is not None else None

    async def set_policy(self, policy: JudgePolicy) -> None:
        async with self.pool.connection() as connection, connection.transaction():
            state = await self._state(connection, exclusive=True)
            if state["policy_digest"] != policy.digest:
                await self._invalidate(connection, "stale_policy")
                await connection.execute(
                    "UPDATE checker_state SET policy = %s, policy_digest = %s WHERE singleton",
                    (_json(policy), policy.digest),
                )

    async def current_policy(self) -> JudgePolicy | None:
        async with self.pool.connection() as connection:
            row = await (await connection.execute("SELECT policy FROM checker_state WHERE singleton")).fetchone()
            return JudgePolicy.model_validate(row["policy"]) if row["policy"] is not None else None

    async def create_session(self, token_hash: str, ttl_days: int) -> SessionProfile:
        if ttl_days <= 0 or not token_hash:
            raise ValueError("session hash and positive TTL are required")
        async with self.pool.connection() as connection, connection.transaction():
            # Serialize the astronomically rare token collision without reviving an expired identity.
            await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (token_hash,))
            row = await (await connection.execute(
                "SELECT *, expires_at > clock_timestamp() AS active FROM sessions WHERE token_hash = %s FOR UPDATE",
                (token_hash,),
            )).fetchone()
            if row and row["active"]:
                return _profile(row)
            if row:
                await connection.execute(
                    'UPDATE sessions SET token_hash = NULL, name = NULL, "group" = NULL WHERE id = %s', (row["id"],),
                )
                await connection.execute("DELETE FROM session_last_submissions WHERE session_id = %s", (row["id"],))
            row = await (await connection.execute(
                "INSERT INTO sessions(id, token_hash, expires_at) VALUES (%s, %s, clock_timestamp() + %s) RETURNING *",
                (uuid4(), token_hash, timedelta(days=ttl_days)),
            )).fetchone()
            return _profile(row)

    async def get_session(self, token_hash: str, ttl_days: int) -> SessionProfile | None:
        if ttl_days <= 0:
            raise ValueError("session TTL must be positive")
        async with self.pool.connection() as connection:
            row = await (await connection.execute(
                """UPDATE sessions SET last_seen_at = clock_timestamp(), expires_at = clock_timestamp() + %s
                   WHERE token_hash = %s AND expires_at > clock_timestamp() RETURNING *""",
                (timedelta(days=ttl_days), token_hash),
            )).fetchone()
            return _profile(row) if row else None

    async def update_profile(self, session_id: UUID, name: str, group: str) -> SessionProfile:
        name, group = _validate_profile(name, group)
        async with self.pool.connection() as connection:
            row = await (await connection.execute(
                'UPDATE sessions SET name = %s, "group" = %s WHERE id = %s '
                'AND expires_at > clock_timestamp() AND token_hash IS NOT NULL RETURNING *',
                (name, group, session_id),
            )).fetchone()
            if not row:
                raise CheckerError("session_expired", "Сессия истекла.", 403)
            return _profile(row)

    async def last_submissions(self, session_id: UUID) -> list[dict]:
        async with self.pool.connection() as connection:
            rows = await (await connection.execute(
                """SELECT l.task_id, l.submission_id AS id,
                    (s.id IS NOT NULL AND (s.expires_at IS NULL OR s.expires_at > clock_timestamp())) AS available
                   FROM session_last_submissions l JOIN sessions owner ON owner.id = l.session_id
                   LEFT JOIN submissions s ON s.id = l.submission_id AND s.session_id = l.session_id
                   WHERE l.session_id = %s AND owner.expires_at > clock_timestamp()
                   AND owner.token_hash IS NOT NULL ORDER BY l.task_id""", (session_id,),
            )).fetchall()
            return [dict(row) for row in rows]

    async def _check_identity(self, connection, digest: str, size: int, source: bytes) -> None:
        row = await (await connection.execute(
            """WITH identity AS (SELECT %s::text AS hash, %s::integer AS size, %s::bytea AS source)
               SELECT EXISTS (
                SELECT 1 FROM submissions s, identity i WHERE s.sha256 = i.hash
                    AND s.byte_size = i.size AND s.source <> i.source
                UNION ALL SELECT 1 FROM queue_jobs j, identity i WHERE j.sha256 = i.hash
                    AND j.byte_size = i.size AND j.source <> i.source
                UNION ALL SELECT 1 FROM result_cache c, identity i WHERE c.sha256 = i.hash
                    AND c.byte_size = i.size AND c.source <> i.source
            ) AS conflict""", (digest, size, source),
        )).fetchone()
        if row["conflict"]:
            raise CheckerError("identity_conflict", "Конфликт идентичности исходника.")

    async def accept_submission(
        self, session_id: UUID, task: Task, context: JudgeContext, source: bytes,
        filename: str, queue_capacity: int,
    ) -> Submission:
        digest, size = identify(source)
        if size > min(self.settings.max_source_bytes, context.limits.source_bytes):
            raise CheckerError("source_too_large", "Слишком большой исходный файл.", 413)
        if queue_capacity <= 0:
            raise ValueError("queue capacity must be positive")
        async with self.pool.connection() as connection, connection.transaction():
            state = await self._state(connection)
            if state["catalog"] is None:
                raise CheckerError("catalog_unavailable", "Каталог временно недоступен.")
            if state["policy"] is None or not _current(state, context):
                raise CheckerError("stale_context", "Контекст проверки изменился.")
            catalog = CatalogSnapshot.model_validate(state["catalog"])
            actual_task = catalog.task(task.id)
            if actual_task is None:
                raise CheckerError("task_not_found", "Задача не найдена.", 404)
            if not actual_task.availability:
                raise CheckerError("task_inactive", "Задача временно недоступна.", 409)
            if actual_task != task or context != JudgeContext.create(catalog, JudgePolicy.model_validate(state["policy"])):
                raise CheckerError("stale_context", "Контекст проверки изменился.")
            await self._queue_lock(connection)
            profile = await (await connection.execute(
                "SELECT * FROM sessions WHERE id = %s AND expires_at > clock_timestamp() "
                "AND token_hash IS NOT NULL FOR UPDATE", (session_id,),
            )).fetchone()
            if not profile or not profile["name"] or not profile["group"]:
                raise CheckerError("profile_required", "Укажите имя и группу перед отправкой.", 422)
            _validate_profile(profile["name"], profile["group"])
            await self._check_identity(connection, digest, size, source)
            key = (task.id, context.generation, context.judge_policy_digest, digest, size)
            cache = await (await connection.execute(
                """SELECT result FROM result_cache WHERE
                   (task_id, generation, policy_digest, sha256, byte_size) = (%s, %s, %s, %s, %s)
                   AND expires_at > clock_timestamp()""", key,
            )).fetchone()
            submission_id, job_id, started_at = uuid4(), None, None
            result, proven, cache_hit = None, None, bool(cache)
            status = SubmissionStatus.QUEUED
            if cache:
                proof = CheckResult.model_validate(cache["result"])
                if proof.verdict == Verdict.ACCEPTED:
                    status, proven = SubmissionStatus.ARCHIVE_PENDING, proof
                else:
                    status, result = SubmissionStatus.FINISHED, proof
            else:
                pending = await (await connection.execute(
                    """SELECT proven_result, queue_job_id, started_at FROM submissions WHERE
                       (task_id, generation, policy_digest, sha256, byte_size) = (%s, %s, %s, %s, %s)
                       AND status = 'archive_pending' ORDER BY created_at, id LIMIT 1""", key,
                )).fetchone()
                if pending:
                    # A completed proof awaiting receipts is a follower, never another execution/cache hit.
                    status, proven = SubmissionStatus.ARCHIVE_PENDING, CheckResult.model_validate(pending["proven_result"])
                    job_id, started_at = pending["queue_job_id"], pending["started_at"]
                else:
                    job = await (await connection.execute(
                        """SELECT * FROM queue_jobs WHERE
                           (task_id, generation, policy_digest, sha256, byte_size) = (%s, %s, %s, %s, %s)
                           AND state IN ('queued', 'claimed')""", key,
                    )).fetchone()
                    if job:
                        job_id, status, started_at = job["id"], SubmissionStatus(job["stage"]), job["claimed_at"]
                    else:
                        count = await (await connection.execute(
                            "SELECT count(*) AS n FROM queue_jobs WHERE state = 'queued'"
                        )).fetchone()
                        if count["n"] >= queue_capacity:
                            raise CheckerError("queue_full", "Очередь заполнена, повторите позднее.")
                        job_id = uuid4()
                        await connection.execute(
                            """INSERT INTO queue_jobs(id, submission_id, task_id, generation, policy_digest,
                                sha256, byte_size, source, context, total_tests, state, stage)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'queued', 'queued')""",
                            (job_id, submission_id, *key, source, _json(context), len(actual_task.tests)),
                        )
            await connection.execute(
                """INSERT INTO submissions(id, session_id, author_name, author_group, task_id, context,
                    generation, policy_digest, sha256, byte_size, source, original_filename, status,
                    result, proven_result, cache_hit, queue_job_id, started_at, finished_at, expires_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    CASE WHEN %s THEN clock_timestamp() ELSE NULL END,
                    CASE WHEN %s THEN clock_timestamp() + %s ELSE NULL END)""",
                (submission_id, session_id, profile["name"], profile["group"], task.id, _json(context),
                 context.generation, context.judge_policy_digest, digest, size, source, filename, status.value,
                 _json(result) if result else None, _json(proven) if proven else None, cache_hit,
                 job_id, started_at, result is not None, result is not None, self.ttl),
            )
            await connection.execute(
                """INSERT INTO session_last_submissions(session_id, task_id, submission_id) VALUES (%s, %s, %s)
                   ON CONFLICT(session_id, task_id) DO UPDATE SET submission_id = EXCLUDED.submission_id,
                   updated_at = clock_timestamp()""", (session_id, task.id, submission_id),
            )
            return await self._load(connection, submission_id)

    async def get_submission(self, session_id: UUID, submission_id: UUID) -> Submission | None:
        async with self.pool.connection() as connection:
            row = await (await connection.execute(
                """SELECT s.id FROM submissions s JOIN sessions owner ON owner.id = s.session_id
                   WHERE s.id = %s AND s.session_id = %s AND owner.expires_at > clock_timestamp()
                   AND owner.token_hash IS NOT NULL AND (s.expires_at IS NULL OR s.expires_at > clock_timestamp())""",
                (submission_id, session_id),
            )).fetchone()
            return await self._load(connection, submission_id) if row else None

    async def claim_job(self) -> QueueJob | None:
        if not await self.leadership_alive():
            raise CheckerError("leadership_lost", "Планировщик не владеет очередью.")
        async with self.pool.connection() as connection, connection.transaction():
            state = await self._state(connection)
            await self._queue_lock(connection)
            row = await (await connection.execute(
                "SELECT * FROM queue_jobs WHERE state = 'queued' ORDER BY created_at, id LIMIT 1 FOR UPDATE"
            )).fetchone()
            if not row or not _current(state, JudgeContext.model_validate(row["context"])):
                return None
            row = await (await connection.execute(
                """UPDATE queue_jobs SET state = 'claimed', stage = 'compiling', scheduler_id = %s,
                    claimed_at = clock_timestamp(), execution_deadline = clock_timestamp() + %s
                   WHERE id = %s RETURNING *""",
                (self.scheduler_id, timedelta(seconds=JudgeContext.model_validate(row["context"]).limits.submission_deadline_seconds), row["id"]),
            )).fetchone()
            await connection.execute(
                """UPDATE submissions SET status = 'compiling', started_at = %s
                   WHERE queue_job_id = %s AND status = 'queued'""", (row["claimed_at"], row["id"]),
            )
            return QueueJob(id=row["id"], submission_id=row["submission_id"], task_id=row["task_id"],
                            context=JudgeContext.model_validate(row["context"]), source=bytes(row["source"]))

    async def set_job_stage(self, job_id: UUID, status: SubmissionStatus) -> None:
        if status not in {SubmissionStatus.COMPILING, SubmissionStatus.RUNNING}:
            raise ValueError("job stage must be compiling or running")
        if not await self.leadership_alive():
            raise CheckerError("leadership_lost", "Планировщик не владеет очередью.")
        async with self.pool.connection() as connection, connection.transaction():
            state = await self._state(connection)
            await self._queue_lock(connection)
            row = await (await connection.execute(
                "SELECT * FROM queue_jobs WHERE id = %s AND state = 'claimed' AND scheduler_id = %s FOR UPDATE",
                (job_id, self.scheduler_id),
            )).fetchone()
            if not row or not _current(state, JudgeContext.model_validate(row["context"])):
                return
            if row["stage"] == "running" and status == SubmissionStatus.COMPILING:
                raise ValueError("cannot return a running job to compilation")
            await connection.execute("UPDATE queue_jobs SET stage = %s WHERE id = %s", (status.value, job_id))
            await connection.execute(
                "UPDATE submissions SET status = %s WHERE queue_job_id = %s AND status IN ('queued', 'compiling', 'running')",
                (status.value, job_id),
            )

    async def requeue_job(self, job_id: UUID) -> None:
        """Runner refused open with 429: no execution exists, so preserve original FIFO order."""
        if not await self.leadership_alive():
            raise CheckerError("leadership_lost", "Планировщик не владеет очередью.")
        async with self.pool.connection() as connection, connection.transaction():
            state = await self._state(connection)
            await self._queue_lock(connection)
            row = await (await connection.execute(
                "SELECT * FROM queue_jobs WHERE id = %s AND state = 'claimed' AND scheduler_id = %s FOR UPDATE",
                (job_id, self.scheduler_id),
            )).fetchone()
            if not row or not _current(state, JudgeContext.model_validate(row["context"])):
                return
            if row["stage"] != "compiling":
                raise ValueError("a running execution must never be requeued")
            await connection.execute(
                """UPDATE queue_jobs SET state = 'queued', stage = 'queued', claimed_at = NULL,
                   scheduler_id = NULL, execution_deadline = NULL WHERE id = %s""", (job_id,),
            )
            await connection.execute(
                """UPDATE submissions SET status = 'queued', started_at = NULL
                   WHERE queue_job_id = %s AND status = 'compiling'""", (job_id,),
            )

    async def job_is_current(self, job_id: UUID) -> bool:
        async with self.pool.connection() as connection, connection.transaction():
            state = await self._state(connection)
            row = await (await connection.execute(
                "SELECT context FROM queue_jobs WHERE id = %s AND state IN ('queued', 'claimed')", (job_id,),
            )).fetchone()
            return bool(row and _current(state, JudgeContext.model_validate(row["context"])))

    async def _cache_result(self, connection, row: dict, result: CheckResult) -> None:
        await self._check_identity(connection, row["sha256"], row["byte_size"], bytes(row["source"]))
        await connection.execute(
            """DELETE FROM result_cache WHERE
               (task_id, generation, policy_digest, sha256, byte_size) = (%s, %s, %s, %s, %s)
               AND expires_at <= clock_timestamp()""",
            (row["task_id"], row["generation"], row["policy_digest"], row["sha256"], row["byte_size"]),
        )
        # DO NOTHING preserves original proof context and TTL, not the latest reader's context.
        await connection.execute(
            """INSERT INTO result_cache(task_id, generation, policy_digest, sha256, byte_size, source,
                context, result, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp() + %s)
               ON CONFLICT(task_id, generation, policy_digest, sha256, byte_size) DO NOTHING""",
            (row["task_id"], row["generation"], row["policy_digest"], row["sha256"], row["byte_size"],
             row["source"], Jsonb(row["context"]), _json(result), self.ttl),
        )

    async def complete_job(self, job_id: UUID, result: CheckResult) -> list[Submission]:
        if not await self.leadership_alive():
            raise CheckerError("leadership_lost", "Планировщик не владеет очередью.")
        async with self.pool.connection() as connection, connection.transaction():
            state = await self._state(connection)
            await self._queue_lock(connection)
            job = await (await connection.execute(
                "SELECT * FROM queue_jobs WHERE id = %s AND state = 'claimed' AND scheduler_id = %s FOR UPDATE",
                (job_id, self.scheduler_id),
            )).fetchone()
            if not job or not _current(state, JudgeContext.model_validate(job["context"])):
                return []
            if result.total_tests != job["total_tests"] or not 0 <= result.passed_tests <= result.total_tests:
                raise ValueError("result counters do not match the immutable job")
            accepted = result.verdict == Verdict.ACCEPTED
            if accepted and (result.passed_tests != result.total_tests or result.first_failure is not None):
                raise ValueError("accepted result must prove every test")
            if not accepted and result.first_failure is None:
                raise ValueError("failed result must include a failure")
            if not accepted and result.verdict != Verdict.INFRASTRUCTURE_ERROR:
                await self._cache_result(connection, job, result)
            rows = await (await connection.execute(
                """UPDATE submissions SET status = %s, result = %s, proven_result = %s,
                    finished_at = CASE WHEN %s THEN NULL ELSE clock_timestamp() END,
                    expires_at = CASE WHEN %s THEN NULL ELSE clock_timestamp() + %s END
                   WHERE queue_job_id = %s AND status IN ('queued', 'compiling', 'running') RETURNING *""",
                ("archive_pending" if accepted else "finished", None if accepted else _json(result),
                 _json(result) if accepted else None, accepted, accepted, self.ttl, job_id),
            )).fetchall()
            await connection.execute(
                """UPDATE queue_jobs SET state = 'finished', stage = 'finished', result = %s,
                    finished_at = clock_timestamp(), source = NULL WHERE id = %s""", (_json(result), job_id),
            )
            return [_submission(row) for row in rows]

    async def archive_pending(self, limit: int = 100) -> list[Submission]:
        if limit <= 0:
            return []
        async with self.pool.connection() as connection, connection.transaction():
            state = await self._state(connection)
            rows = await (await connection.execute(
                """SELECT * FROM submissions WHERE status = 'archive_pending' AND generation = %s
                   AND policy_digest = %s ORDER BY created_at, id LIMIT %s""",
                (state["generation"], state["policy_digest"], limit),
            )).fetchall()
            return [_submission(row) for row in rows if _current(state, JudgeContext.model_validate(row["context"]))]

    async def finish_archived(self, submission_id: UUID) -> None:
        # Called only after the archive transaction committed. This never holds a lock over archive I/O.
        async with self.pool.connection() as connection, connection.transaction():
            state = await self._state(connection)
            await self._queue_lock(connection)
            row = await (await connection.execute(
                "SELECT * FROM submissions WHERE id = %s AND status = 'archive_pending' FOR UPDATE", (submission_id,),
            )).fetchone()
            if not row or not _current(state, JudgeContext.model_validate(row["context"])):
                return
            proof = CheckResult.model_validate(row["proven_result"])
            await self._cache_result(connection, row, proof)
            await connection.execute(
                """UPDATE submissions SET status = 'finished', result = proven_result, proven_result = NULL,
                    archive_receipt_confirmed = true, finished_at = clock_timestamp(),
                    expires_at = clock_timestamp() + %s WHERE id = %s""", (self.ttl, submission_id),
            )

    @asynccontextmanager
    async def scheduler_lock(self):
        if self._leader is not None:
            raise RuntimeError("scheduler lock is not reentrant")
        connection = await AsyncConnection.connect(self.dsn, autocommit=True, row_factory=dict_row)
        acquired = False
        try:
            row = await (await connection.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (SCHEDULER_LOCK,))).fetchone()
            acquired = row["acquired"]
            if acquired:
                self._leader = connection
            yield acquired
        finally:
            if acquired:
                self._leader = None
                try:
                    await connection.execute("SELECT pg_advisory_unlock(%s)", (SCHEDULER_LOCK,))
                except (Error, OSError):
                    pass
            await connection.close()

    async def leadership_alive(self) -> bool:
        connection = self._leader
        if connection is None or connection.closed:
            return False
        try:
            row = await (await connection.execute(
                """SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted
                   AND pid = pg_backend_pid() AND classid::bigint = %s AND objid::bigint = %s
                   AND objsubid = 1) AS alive""", (SCHEDULER_LOCK >> 32, SCHEDULER_LOCK & 0xFFFFFFFF),
            )).fetchone()
            return row["alive"]
        except (Error, OSError):
            return False

    async def recover_claimed(self) -> None:
        """Caller must first destroy and confirm absence of all previous runner executions."""
        if not await self.leadership_alive():
            raise CheckerError("leadership_lost", "Планировщик не владеет очередью.")
        async with self.pool.connection() as connection, connection.transaction():
            state = await self._state(connection)
            await self._queue_lock(connection)
            jobs = await (await connection.execute("SELECT * FROM queue_jobs WHERE state = 'claimed' FOR UPDATE")).fetchall()
            for job in jobs:
                if not _current(state, JudgeContext.model_validate(job["context"])):
                    continue
                result = CheckResult(verdict=Verdict.INFRASTRUCTURE_ERROR, passed_tests=0,
                    total_tests=job["total_tests"], first_failure=Failure(stage="infrastructure",
                    code="infrastructure_error", message="Проверка прервана. Отправьте решение повторно."))
                await connection.execute(
                    """UPDATE submissions SET status = 'finished', result = %s, finished_at = clock_timestamp(),
                        expires_at = clock_timestamp() + %s WHERE queue_job_id = %s
                        AND status IN ('queued', 'compiling', 'running')""", (_json(result), self.ttl, job["id"]),
                )
                await connection.execute(
                    """UPDATE queue_jobs SET state = 'finished', stage = 'finished', source = NULL,
                        result = %s, finished_at = clock_timestamp(), state_reason = 'worker_restarted'
                       WHERE id = %s""", (_json(result), job["id"]),
                )

    async def cleanup(self) -> None:
        async with self.pool.connection() as connection, connection.transaction():
            await self._state(connection)
            await self._queue_lock(connection)
            await connection.execute("DELETE FROM result_cache WHERE expires_at <= clock_timestamp()")
            await connection.execute(
                "DELETE FROM submissions WHERE status IN ('finished', 'invalidated') AND expires_at <= clock_timestamp()"
            )
            await connection.execute(
                """DELETE FROM queue_jobs j WHERE state IN ('finished', 'invalidated')
                   AND NOT EXISTS (SELECT 1 FROM submissions s WHERE s.queue_job_id = j.id)"""
            )
            # Session/profile retention is independent from result retention. Active work retains only owner ID.
            await connection.execute(
                "DELETE FROM session_last_submissions l USING sessions s WHERE l.session_id = s.id AND s.expires_at <= clock_timestamp()"
            )
            await connection.execute(
                'UPDATE sessions SET token_hash = NULL, name = NULL, "group" = NULL WHERE expires_at <= clock_timestamp()'
            )
            await connection.execute(
                "DELETE FROM sessions s WHERE expires_at <= clock_timestamp() "
                "AND NOT EXISTS (SELECT 1 FROM submissions sub WHERE sub.session_id = s.id)"
            )
            await connection.execute(
                """DELETE FROM catalog_snapshots c WHERE c.snapshot_id <> COALESCE(
                    (SELECT catalog->>'snapshot_id' FROM checker_state WHERE singleton), '')
                   AND NOT EXISTS (SELECT 1 FROM queue_jobs j WHERE j.context->>'snapshot_id' = c.snapshot_id
                                   AND j.state IN ('queued', 'claimed'))
                   AND NOT EXISTS (SELECT 1 FROM submissions s WHERE s.context->>'snapshot_id' = c.snapshot_id
                                   AND s.status = 'archive_pending')"""
            )

    async def worker_heartbeat(self) -> None:
        if not await self.leadership_alive():
            raise CheckerError("leadership_lost", "Планировщик не владеет очередью.")
        async with self.pool.connection() as connection:
            await connection.execute(
                """INSERT INTO worker_status(singleton, scheduler_id, heartbeat_at) VALUES (true, %s, clock_timestamp())
                   ON CONFLICT(singleton) DO UPDATE SET scheduler_id = EXCLUDED.scheduler_id,
                   heartbeat_at = EXCLUDED.heartbeat_at""", (self.scheduler_id,),
            )

    async def worker_alive(self) -> bool:
        async with self.pool.connection() as connection:
            row = await (await connection.execute(
                "SELECT EXISTS (SELECT 1 FROM worker_status WHERE heartbeat_at > clock_timestamp() - interval '15 seconds') AS alive"
            )).fetchone()
            return row["alive"]
