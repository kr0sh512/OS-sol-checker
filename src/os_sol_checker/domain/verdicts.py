from enum import StrEnum


class SubmissionStatus(StrEnum):
    QUEUED = "queued"
    COMPILING = "compiling"
    RUNNING = "running"
    ARCHIVE_PENDING = "archive_pending"
    FINISHED = "finished"
    INVALIDATED = "invalidated"


class Verdict(StrEnum):
    ACCEPTED = "accepted"
    COMPILE_ERROR = "compile_error"
    COMPILE_TIMEOUT = "compile_timeout"
    RUNTIME_ERROR = "runtime_error"
    RUN_TIMEOUT = "run_timeout"
    WRONG_ANSWER = "wrong_answer"
    MEMORY_LIMIT = "memory_limit"
    OUTPUT_LIMIT = "output_limit"
    SUBMISSION_TIMEOUT = "submission_timeout"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
