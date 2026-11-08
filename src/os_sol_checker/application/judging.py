from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from ..domain.errors import RunnerError
from ..domain.models import CheckResult, Failure, JudgePolicy, QueueJob, StageOutcome
from ..domain.policies import compare, preview
from ..domain.verdicts import SubmissionStatus, Verdict
from ..ports.catalog import CatalogSource
from ..ports.sandbox import SandboxBackend

_MESSAGES = {
    Verdict.COMPILE_ERROR: "Ошибка компиляции.",
    Verdict.COMPILE_TIMEOUT: "Превышено время компиляции.",
    Verdict.RUNTIME_ERROR: "Ошибка выполнения программы.",
    Verdict.RUN_TIMEOUT: "Превышено время выполнения теста.",
    Verdict.MEMORY_LIMIT: "Превышен лимит памяти.",
    Verdict.OUTPUT_LIMIT: "Превышен лимит вывода.",
    Verdict.SUBMISSION_TIMEOUT: "Исчерпан общий бюджет проверки.",
    Verdict.WRONG_ANSWER: "Вывод не совпал с ожидаемым.",
    Verdict.INFRASTRUCTURE_ERROR: "Ошибка инфраструктуры проверки. Это не ошибка вашей программы.",
}


def failed(verdict: Verdict, total: int, passed: int = 0, *, stage: str = "infrastructure",
           test_id: str | None = None, index: int | None = None, outcome: StageOutcome | None = None,
           stdin: bytes | None = None, expected: bytes | None = None) -> CheckResult:
    diagnostics = None
    if outcome is not None:
        data = outcome.stderr if stage == "run" else outcome.stdout + outcome.stderr
        diagnostics = preview(data, 8192, truncated=outcome.status == "output_limit") if data else None
    return CheckResult(verdict=verdict, passed_tests=passed, total_tests=total,
                       first_failure=Failure(stage=stage, code=verdict, message=_MESSAGES[verdict],
                                             test_id=test_id, test_index=index, diagnostics=diagnostics,
                                             stdin_preview=preview(stdin) if stdin is not None else None,
                                             expected_preview=preview(expected) if expected is not None else None,
                                             actual_preview=preview(outcome.stdout) if outcome and stage == "run" else None))


def outcome_verdict(outcome: StageOutcome, stage: str) -> Verdict | None:
    if outcome.status == "completed" and outcome.exit_code == 0 and outcome.signal is None:
        return None
    if outcome.status in {verdict.value for verdict in Verdict if verdict not in {Verdict.ACCEPTED, Verdict.WRONG_ANSWER}}:
        return Verdict(outcome.status)
    if outcome.status == "completed":
        return Verdict.COMPILE_ERROR if stage == "compile" else Verdict.RUNTIME_ERROR
    return Verdict.INFRASTRUCTURE_ERROR


async def judge(job: QueueJob, catalog: CatalogSource, runner: SandboxBackend,
                is_current: Callable[[], Awaitable[bool]],
                stage_changed: Callable[[SubmissionStatus], Awaitable[None]]) -> CheckResult:
    total = 0
    deadline = time.monotonic() + job.context.limits.submission_deadline_seconds
    handle = None
    passed = 0
    result = None
    try:
        async with asyncio.timeout_at(deadline):
            snapshot = await asyncio.to_thread(catalog.load_snapshot, job.context.snapshot_id)
            task = snapshot.task(job.task_id)
            if task is None or not task.availability or not task.tests:
                return failed(Verdict.INFRASTRUCTURE_ERROR, 0)
            total = len(task.tests)
            if not await is_current():
                raise asyncio.CancelledError
            policy = JudgePolicy.model_validate(job.context.model_dump(include=set(JudgePolicy.model_fields)))
            handle = await runner.open(job.submission_id, policy, max(0.001, deadline - time.monotonic() - 0.25))
            await stage_changed(SubmissionStatus.COMPILING)
            compilation = await runner.compile(handle, job.source)
            verdict = outcome_verdict(compilation, "compile")
            if verdict:
                result = failed(verdict, total, stage="compile", outcome=compilation)
            elif not compilation.artifact_handle:
                result = failed(Verdict.INFRASTRUCTURE_ERROR, total)
            else:
                for index, test in enumerate(task.tests):
                    if not await is_current():
                        raise asyncio.CancelledError
                    await stage_changed(SubmissionStatus.RUNNING)
                    stdin, expected = await asyncio.to_thread(catalog.load_test, snapshot, task, index)
                    execution = await runner.run(handle, compilation.artifact_handle, stdin)
                    verdict = outcome_verdict(execution, "run")
                    if verdict is None and not compare(expected, execution.stdout):
                        verdict = Verdict.WRONG_ANSWER
                    if verdict:
                        result = failed(verdict, total, passed, stage="run", test_id=test.id,
                                        index=index + 1, outcome=execution, stdin=stdin, expected=expected)
                        break
                    passed += 1
                if result is None:
                    result = CheckResult(verdict=Verdict.ACCEPTED, passed_tests=passed, total_tests=total)
    except TimeoutError:
        result = failed(Verdict.SUBMISSION_TIMEOUT, total, passed, stage="submission")
    except RunnerError as exc:
        if exc.status_code == 429 and handle is None:
            raise
        result = failed(Verdict.INFRASTRUCTURE_ERROR, total, passed)
    except (OSError, ValueError):
        result = failed(Verdict.INFRASTRUCTURE_ERROR, total, passed)
    finally:
        if handle is not None:
            # No result, including accepted, is published before remote cleanup confirms.
            cleanup = asyncio.create_task(runner.destroy(handle))
            try:
                await asyncio.wait_for(asyncio.shield(cleanup), timeout=5)
            except asyncio.CancelledError:
                try:
                    await asyncio.wait_for(asyncio.shield(cleanup), timeout=5)
                except (TimeoutError, RunnerError):
                    cleanup.cancel()
                raise
            except (TimeoutError, RunnerError):
                cleanup.cancel()
                result = failed(Verdict.INFRASTRUCTURE_ERROR, total, passed)
    assert result is not None
    return result
