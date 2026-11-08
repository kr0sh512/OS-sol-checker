from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .domain.models import Limits


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    tasks_repository_url: str = "https://github.com/kr0sh512/OS-tasks"
    tasks_repository_ref: str = "main"
    catalog_poll_seconds: float = Field(default=60, gt=0)
    catalog_root: Path = Path("var/catalog")
    transient_database_url: str = ""
    archive_database_url: str = ""
    public_origin: str = ""
    csrf_secret: str = ""
    runner_url: str = "http://runner:8081"
    runner_token: str = ""
    runner_image: str = ""
    max_source_bytes: int = Field(default=102400, gt=0, le=102400)
    max_request_bytes: int = Field(default=131072, gt=0)
    max_concurrent_sandboxes: int = Field(default=4, gt=0)
    queue_capacity: int = Field(default=1000, gt=0)
    compile_timeout_seconds: float = Field(default=10, gt=0)
    run_timeout_seconds: float = Field(default=2, gt=0)
    submission_deadline_seconds: float = Field(default=60, gt=0, le=60)
    sandbox_memory_mib: int = Field(default=256, gt=0)
    sandbox_pids_limit: int = Field(default=64, gt=0)
    sandbox_tmp_mib: int = Field(default=32, gt=0)
    max_output_bytes: int = Field(default=65536, gt=0, le=65536)
    max_artifact_bytes: int = Field(default=1048576, gt=0, le=1048576)
    transient_ttl_hours: float = Field(default=24, gt=0)
    session_ttl_days: int = Field(default=30, gt=0)
    runner_scratch: Path = Path("/work/runner")
    nsjail_binary: str = "/usr/local/bin/nsjail"
    nsjail_cgroup_root: Path = Path("/run/checker-cgroup")
    jail_profiles_root: Path = Path("/opt/checker/deploy/nsjail")
    jail_rootfs_root: Path = Path("/opt/checker/rootfs")
    runner_manifest: Path = Path("/opt/checker/runner-manifest.json")
    runner_host: str = "0.0.0.0"
    runner_port: int = 8081

    @model_validator(mode="after")
    def validate_limits(self):
        if max(self.compile_timeout_seconds, self.run_timeout_seconds) > self.submission_deadline_seconds:
            raise ValueError("stage timeout exceeds submission deadline")
        if self.max_request_bytes <= self.max_source_bytes:
            raise ValueError("request body must accommodate multipart overhead")
        return self

    def require(self, role: str) -> None:
        required = {
            "web": ("transient_database_url", "public_origin", "csrf_secret"),
            "worker": ("transient_database_url", "archive_database_url", "runner_token"),
            "catalog": ("transient_database_url",),
            "runner": ("runner_token", "runner_image"),
        }[role]
        for name in required:
            if not getattr(self, name):
                raise ValueError(f"{name.upper()} is required for {role}")
        if role in {"worker", "runner"} and len(self.runner_token.encode()) < 32:
            raise ValueError("RUNNER_TOKEN must contain at least 32 bytes")
        if role == "web" and not self.public_origin.startswith("https://"):
            raise ValueError("PUBLIC_ORIGIN must use HTTPS")
        if role == "runner" and "@sha256:" not in self.runner_image:
            raise ValueError("RUNNER_IMAGE must use an immutable digest")

    def limits(self) -> Limits:
        return Limits(compile_timeout_seconds=self.compile_timeout_seconds,
                      run_timeout_seconds=self.run_timeout_seconds,
                      submission_deadline_seconds=self.submission_deadline_seconds,
                      memory_mib=self.sandbox_memory_mib, pids=self.sandbox_pids_limit,
                      tmp_mib=self.sandbox_tmp_mib, output_bytes=self.max_output_bytes,
                      artifact_bytes=self.max_artifact_bytes, source_bytes=self.max_source_bytes)
