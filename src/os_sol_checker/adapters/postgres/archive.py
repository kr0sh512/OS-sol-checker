from __future__ import annotations

from uuid import uuid4

from psycopg.types.json import Jsonb

from ...config import Settings
from ...domain.errors import CheckerError
from ...domain.models import JudgePolicy, Submission
from ...domain.policies import identify
from ...domain.verdicts import SubmissionStatus, Verdict
from .base import PostgresStore


class ArchiveStore(PostgresStore):
    database_kind = "archive"

    def __init__(self, settings: Settings):
        super().__init__(settings.archive_database_url)

    async def record_success(self, submission: Submission) -> None:
        proof = submission.proven_result or submission.result
        if submission.status not in {SubmissionStatus.ARCHIVE_PENDING, SubmissionStatus.FINISHED} or (
            proof is None or proof.verdict != Verdict.ACCEPTED or proof.first_failure is not None
            or proof.passed_tests != proof.total_tests
        ):
            raise ValueError("only a proven accepted result can enter the permanent archive")
        if identify(submission.source) != (submission.sha256, submission.byte_size):
            raise CheckerError("identity_conflict", "Конфликт идентичности архивируемого исходника.")
        context = submission.context.model_dump(mode="json")
        policy = JudgePolicy.model_validate(submission.context.model_dump(include=set(JudgePolicy.model_fields)))
        if policy.digest != submission.context.judge_policy_digest:
            raise ValueError("archive context has an inconsistent policy digest")
        identity = (submission.sha256, submission.byte_size)
        key = (submission.task_id, submission.context.tests_digest, submission.context.judge_policy_digest, *identity)
        async with self.pool.connection() as connection, connection.transaction():
            # PostgreSQL's unique-index wait serializes competing inserts, even across workers.
            await connection.execute(
                "INSERT INTO blobs(sha256, byte_size, source) VALUES (%s, %s, %s) "
                "ON CONFLICT(sha256, byte_size) DO NOTHING", (*identity, submission.source),
            )
            blob = await (await connection.execute(
                "SELECT source FROM blobs WHERE sha256 = %s AND byte_size = %s", identity,
            )).fetchone()
            if bytes(blob["source"]) != submission.source:
                raise CheckerError("identity_conflict", "Конфликт идентичности архивируемого исходника.")
            await connection.execute(
                """INSERT INTO accepted_solutions(id, task_id, tests_digest, policy_digest, sha256, byte_size, context)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT(task_id, tests_digest, policy_digest, sha256, byte_size) DO NOTHING""",
                (uuid4(), *key, Jsonb(context)),
            )
            acceptance = await (await connection.execute(
                """SELECT id FROM accepted_solutions WHERE
                   (task_id, tests_digest, policy_digest, sha256, byte_size) = (%s, %s, %s, %s, %s)""", key,
            )).fetchone()
            await connection.execute(
                """INSERT INTO success_receipts(submission_id, accepted_solution_id, context, author_name,
                    author_group, submitted_at) VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT(submission_id) DO NOTHING""",
                (submission.id, acceptance["id"], Jsonb(context), submission.author_name,
                 submission.author_group, submission.created_at),
            )
            receipt = await (await connection.execute(
                "SELECT * FROM success_receipts WHERE submission_id = %s", (submission.id,),
            )).fetchone()
            if (receipt["accepted_solution_id"], receipt["context"], receipt["author_name"],
                receipt["author_group"], receipt["submitted_at"]) != (
                acceptance["id"], context, submission.author_name, submission.author_group, submission.created_at
            ):
                raise CheckerError("identity_conflict", "Конфликт неизменяемой архивной квитанции.")
