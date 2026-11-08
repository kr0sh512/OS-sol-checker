from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .verdicts import SubmissionStatus, Verdict


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Hint(Model):
    title: str
    html: str


class TestCase(Model):
    id: str
    input_path: str
    expected_path: str


class Task(Model):
    id: str
    topic_id: str
    title: str
    relative_path: str
    description_html: str = ""
    hints: list[Hint] = Field(default_factory=list)
    tests: list[TestCase] = Field(default_factory=list)
    availability: bool = True
    reason: str | None = None


class Topic(Model):
    id: str
    title: str
    tasks: list[Task] = Field(default_factory=list)


class CatalogSnapshot(Model):
    snapshot_id: str
    commit: str
    tests_digest: str
    root_path: str
    generation: int = 0
    topics: list[Topic] = Field(default_factory=list)

    def task(self, task_id: str) -> Task | None:
        return next((task for topic in self.topics for task in topic.tasks if task.id == task_id), None)


class Limits(Model):
    compile_timeout_seconds: float = 10
    run_timeout_seconds: float = 2
    submission_deadline_seconds: float = 60
    memory_mib: int = 256
    pids: int = 64
    tmp_mib: int = 32
    output_bytes: int = 65536
    artifact_bytes: int = 1048576
    source_bytes: int = 102400
    test_stdin_bytes: int = 1048576


class JudgePolicy(Model):
    runner_image_digest: str
    sandbox_profile_digest: str
    flags_digest: str
    comparator_version: str = "bytes-v1"
    limits: Limits = Field(default_factory=Limits)

    @property
    def digest(self) -> str:
        encoded = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        return sha256(encoded).hexdigest()


class JudgeContext(JudgePolicy):
    snapshot_id: str
    catalog_commit: str
    tests_digest: str
    generation: int
    judge_policy_digest: str

    @classmethod
    def create(cls, snapshot: CatalogSnapshot, policy: JudgePolicy) -> JudgeContext:
        return cls(**policy.model_dump(), snapshot_id=snapshot.snapshot_id,
                   catalog_commit=snapshot.commit, tests_digest=snapshot.tests_digest,
                   generation=snapshot.generation, judge_policy_digest=policy.digest)


class Preview(Model):
    text: str
    truncated: bool = False
    byte_size: int | None = None


class Failure(Model):
    stage: str
    code: str
    message: str
    test_id: str | None = None
    test_index: int | None = None
    diagnostics: Preview | None = None
    stdin_preview: Preview | None = None
    expected_preview: Preview | None = None
    actual_preview: Preview | None = None


class CheckResult(Model):
    verdict: Verdict
    passed_tests: int
    total_tests: int
    first_failure: Failure | None = None


class SessionProfile(Model):
    id: UUID
    name: str | None = None
    group: str | None = None
    expires_at: datetime


class Submission(Model):
    id: UUID
    session_id: UUID
    author_name: str
    author_group: str
    task_id: str
    context: JudgeContext
    sha256: str
    byte_size: int
    source: bytes = Field(exclude=True)
    original_filename: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    status: SubmissionStatus
    result: CheckResult | None = None
    proven_result: CheckResult | None = None
    cache_hit: bool = False
    queue_job_id: UUID | None = None
    state_reason: str | None = None
    queue_position: int | None = None


class QueueJob(Model):
    id: UUID
    submission_id: UUID
    task_id: str
    total_tests: int = Field(ge=1)
    context: JudgeContext
    source: bytes = Field(exclude=True)


class ExecutionHandle(Model):
    execution_id: UUID
    handle: str
    artifact_handle: str | None = None


class StageOutcome(Model):
    status: str
    exit_code: int | None = None
    signal: int | None = None
    wall_seconds: float = 0
    stdout: bytes = Field(default=b"", exclude=True)
    stderr: bytes = Field(default=b"", exclude=True)
    resource_reason: str | None = None
    artifact_handle: str | None = None
