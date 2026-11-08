from datetime import datetime, timezone
from uuid import uuid4

import pytest

from os_sol_checker.application.submissions import submission_dto
from os_sol_checker.domain.models import CatalogSnapshot, CheckResult, JudgeContext, JudgePolicy, Submission, Task, Topic
from os_sol_checker.domain.verdicts import SubmissionStatus, Verdict


def record(status):
    snapshot = CatalogSnapshot(snapshot_id="s", commit="a" * 40, tests_digest="b" * 64, root_path="/secret", generation=3,
        topics=[Topic(id="t", title="Topic", tasks=[Task(id="task", topic_id="t", title="Task", relative_path="private")])])
    policy = JudgePolicy(runner_image_digest="sha256:" + "c" * 64, sandbox_profile_digest="d" * 64, flags_digest="e" * 64)
    submission = Submission(id=uuid4(), session_id=uuid4(), author_name="PRIVATE NAME", author_group="PRIVATE GROUP",
        task_id="task", context=JudgeContext.create(snapshot, policy), sha256="f" * 64, byte_size=6, source=b"SECRET",
        original_filename="private.c", created_at=datetime.now(timezone.utc), status=status, queue_position=2)
    return submission, snapshot, policy


@pytest.mark.parametrize("status", list(SubmissionStatus))
def test_explicit_projection_never_serializes_private_storage_fields(status):
    submission, snapshot, policy = record(status)
    data = submission_dto(submission, snapshot, policy)
    assert set(data) == {"id", "task_id", "status", "verdict", "cache_hit", "generation", "context", "first_failure",
                         "passed_tests", "total_tests", "created_at", "finished_at", "stale", "state_reason", "queue_position"}
    assert "SECRET" not in str(data) and "PRIVATE" not in str(data)
    assert data["queue_position"] == (2 if status == SubmissionStatus.QUEUED else None)


def test_archive_pending_counters_are_not_published_as_accepted():
    submission, snapshot, policy = record(SubmissionStatus.ARCHIVE_PENDING)
    submission.proven_result = CheckResult(verdict=Verdict.ACCEPTED, passed_tests=7, total_tests=7)
    data = submission_dto(submission, snapshot, policy)
    assert data["verdict"] is None and data["finished_at"] is None
    assert data["passed_tests"] == data["total_tests"] == 7


def test_generation_and_policy_changes_mark_old_record_stale():
    submission, snapshot, policy = record(SubmissionStatus.FINISHED)
    assert not submission_dto(submission, snapshot, policy)["stale"]
    snapshot.generation += 1
    assert submission_dto(submission, snapshot, policy)["stale"]
    snapshot.generation -= 1
    policy.flags_digest = "0" * 64
    assert submission_dto(submission, snapshot, policy)["stale"]
    assert submission_dto(submission, None, None)["stale"]
