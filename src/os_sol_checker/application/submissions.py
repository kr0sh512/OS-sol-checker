"""Public submission projection: never serialize a storage record wholesale."""
from __future__ import annotations

from ..domain.errors import CheckerError
from ..domain.models import CatalogSnapshot, JudgeContext, JudgePolicy, Submission
from ..domain.verdicts import SubmissionStatus


async def accept_submission(store, settings, profile, task_id: str, source: bytes, filename: str):
    if not profile.name or not profile.group:
        raise CheckerError("profile_required", "Сначала заполните имя и группу.", 422)
    snapshot = await store.current_catalog()
    if snapshot is None:
        raise CheckerError("catalog_unavailable", "Каталог заданий пока недоступен.", 503)
    task = snapshot.task(task_id)
    if task is None:
        raise CheckerError("task_not_found", "Задание не найдено.", 404)
    if not task.availability:
        raise CheckerError("task_inactive", "Задание недоступно для проверки.", 409)
    policy = await store.current_policy()
    if policy is None:
        raise CheckerError("storage_unavailable", "Проверка пока не готова. Попробуйте позднее.", 503)
    if len(source) > policy.limits.source_bytes:
        error = CheckerError("source_too_large", "Файл превышает лимит политики проверки.", 413)
        error.limit_bytes = policy.limits.source_bytes
        raise error
    return await store.accept_submission(profile.id, task, JudgeContext.create(snapshot, policy),
                                         source, filename, settings.queue_capacity)


def submission_dto(submission: Submission, snapshot: CatalogSnapshot | None, policy: JudgePolicy | None):
    result = submission.result if submission.status == SubmissionStatus.FINISHED else None
    counters = result or submission.proven_result
    task = snapshot.task(submission.task_id) if snapshot else None
    stale = (snapshot is None or policy is None
             or snapshot.generation != submission.context.generation
             or snapshot.tests_digest != submission.context.tests_digest
             or policy.digest != submission.context.judge_policy_digest
             or submission.status == SubmissionStatus.INVALIDATED)
    return {
        "id": str(submission.id), "task_id": submission.task_id,
        "status": submission.status.value, "verdict": result.verdict.value if result else None,
        "cache_hit": submission.cache_hit, "generation": submission.context.generation,
        "context": submission.context.model_dump(mode="json"),
        "first_failure": result.first_failure.model_dump(mode="json") if result and result.first_failure else None,
        "passed_tests": counters.passed_tests if counters else 0,
        "total_tests": counters.total_tests if counters else len(task.tests) if task else 0,
        "created_at": submission.created_at.isoformat(),
        "finished_at": submission.finished_at.isoformat() if submission.finished_at else None,
        "stale": stale, "state_reason": submission.state_reason,
        "queue_position": submission.queue_position if submission.status == SubmissionStatus.QUEUED else None,
    }
