import asyncio
from uuid import uuid4

import pytest

from os_sol_checker.application.judging import judge, outcome_verdict
from os_sol_checker.domain.errors import RunnerError
from os_sol_checker.domain.models import (
    CatalogSnapshot, ExecutionHandle, JudgeContext, JudgePolicy, QueueJob,
    StageOutcome, Task, TestCase, Topic,
)
from os_sol_checker.domain.verdicts import Verdict


class Catalog:
    def __init__(self):
        self.task = Task(id="task", topic_id="topic", title="Task", relative_path="topic/1",
                         tests=[TestCase(id=str(i), input_path=f"{i}.in", expected_path=f"{i}.out")
                                for i in range(1, 4)])
        self.snapshot = CatalogSnapshot(snapshot_id="snapshot", commit="a" * 40,
                                        tests_digest="b" * 64, root_path="/unused", generation=1,
                                        topics=[Topic(id="topic", title="Topic", tasks=[self.task])])

    def load_snapshot(self, snapshot_id):
        return self.snapshot

    def load_test(self, snapshot, task, index):
        return str(index + 1).encode(), b"expected"


class Runner:
    def __init__(self, outputs, cleanup_fails=False):
        self.outputs = iter(outputs)
        self.inputs = []
        self.destroyed = False
        self.cleanup_fails = cleanup_fails

    async def open(self, submission_id, policy, remaining_seconds):
        return ExecutionHandle(execution_id=uuid4(), handle="execution")

    async def compile(self, handle, source):
        return StageOutcome(status="completed", exit_code=0, artifact_handle="artifact")

    async def run(self, handle, artifact, stdin):
        self.inputs.append(stdin)
        return StageOutcome(status="completed", exit_code=0, stdout=next(self.outputs))

    async def destroy(self, handle):
        self.destroyed = True
        if self.cleanup_fails:
            raise RunnerError("cleanup_failed", "Cleanup not confirmed")


def job(catalog):
    policy = JudgePolicy(runner_image_digest="sha256:" + "c" * 64,
                         sandbox_profile_digest="d" * 64, flags_digest="e" * 64)
    return QueueJob(id=uuid4(), submission_id=uuid4(), task_id="task", source=b"source",
                    context=JudgeContext.create(catalog.snapshot, policy))


async def current():
    return True


async def stage_changed(stage):
    return None


@pytest.mark.asyncio
async def test_first_failed_test_stops_execution_and_cleans_up():
    catalog = Catalog()
    runner = Runner([b"expected", b"wrong", b"expected"])
    result = await judge(job(catalog), catalog, runner, current, stage_changed)
    assert result.verdict == Verdict.WRONG_ANSWER
    assert result.passed_tests == 1
    assert result.first_failure.test_id == "2"
    assert runner.inputs == [b"1", b"2"]
    assert runner.destroyed


@pytest.mark.asyncio
async def test_cleanup_failure_cannot_publish_success():
    catalog = Catalog()
    runner = Runner([b"expected"] * 3, cleanup_fails=True)
    result = await judge(job(catalog), catalog, runner, current, stage_changed)
    assert result.verdict == Verdict.INFRASTRUCTURE_ERROR
    assert runner.destroyed


@pytest.mark.asyncio
async def test_invalidated_generation_stops_before_next_stage():
    catalog = Catalog()
    runner = Runner([b"expected"] * 3)
    checks = iter([True, False])

    async def is_current():
        return next(checks)

    with pytest.raises(asyncio.CancelledError):
        await judge(job(catalog), catalog, runner, is_current, stage_changed)
    assert runner.inputs == []
    assert runner.destroyed


def test_nonzero_exit_does_not_pass_even_if_stdout_matches():
    outcome = StageOutcome(status="completed", exit_code=7, stdout=b"expected")
    assert outcome_verdict(outcome, "run") == Verdict.RUNTIME_ERROR
