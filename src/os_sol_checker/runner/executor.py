from __future__ import annotations

import asyncio
import ctypes
import hmac
import json
import logging
import os
import secrets
import shutil
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

from ..config import Settings
from ..domain.models import JudgePolicy, StageOutcome
from ..domain.policies import GCC_FLAGS
from .cgroups import Cgroups, counters, stop_reason
from .manifest import verified_policy

log = logging.getLogger(__name__)


class ProtocolError(Exception):
    def __init__(self, status: int, code: str):
        self.status = status
        self.code = code
        super().__init__(code)


@dataclass
class Stage:
    id: str
    kind: str
    cgroup: Path
    work: Path
    reason: Path
    deadline: float | None = None
    process: asyncio.subprocess.Process | None = None
    mounted: bool = False


@dataclass
class Execution:
    id: UUID
    submission_id: UUID
    handle: str
    deadline: float
    root: Path
    state: str = "reserved"
    compiled: bool = False
    artifact: str | None = None
    busy: bool = False
    closing: bool = False
    stage: Stage | None = None
    operation: asyncio.Task | None = None
    request: asyncio.Task | None = None
    cleanup: asyncio.Task | None = None
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)



class RunnerExecutor:
    """One process, one authoritative non-waiting reservation semaphore, no native fallback."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.policy: JudgePolicy | None = None
        self.ready = False
        self.executions: dict[UUID, Execution] = {}
        self.cgroups = Cgroups(settings.nsjail_cgroup_root)
        self.control = settings.runner_scratch / "control"
        self.data = settings.runner_scratch / "executions"
        self._reaper: asyncio.Task | None = None
        self._spawn_lock = asyncio.Lock()
        self._watched_children: set[int] = set()

    def _publish(self, execution: Execution) -> None:
        stage = execution.stage
        value = {"deadline": execution.deadline, "stage": None if stage is None else
                 {"id": stage.id, "kind": stage.kind, "deadline": stage.deadline}}
        target = self.control / (execution.id.hex + ".json")
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, separators=(",", ":")))
        os.replace(temporary, target)

    async def start(self) -> None:
        try:
            self.settings.require("runner")
            if len(self.settings.runner_token.encode()) < 32:
                raise ValueError("Runner token needs at least 32 bytes")
            if os.environ.get("CHECKER_SUPERVISED") != "1" or not os.environ.get("CHECKER_HEARTBEAT_FD"):
                raise ValueError("The independent PID1 supervisor/watchdog is required")
            if os.getuid() != 0:
                raise ValueError("Runner requires root in its outer container, not host user namespace remapping")
            for mapping in ("uid_map", "gid_map"):
                if Path("/proc/self", mapping).read_text().split() != ["0", "0", "4294967295"]:
                    raise ValueError("Outer Docker userns-remap is unsupported; jail gets its own user namespace")
            security = Path("/proc/self/status").read_text()
            if "Seccomp:\t2" not in security or "NoNewPrivs:\t1" not in security:
                raise ValueError("Outer seccomp and no_new_privs must be active")
            apparmor = Path("/proc/self/attr/current").read_text().strip()
            if apparmor != "checker-runner (enforce)":
                raise ValueError("The enforcing checker-runner AppArmor profile is required")
            if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
                raise OSError(ctypes.get_errno(), "Cannot enable subreaping")
            self.settings.runner_scratch.mkdir(mode=0o711, parents=True, exist_ok=True)
            if not any(line.split(" - ")[1].split()[0] == "tmpfs" and
                       line.split()[4] == str(self.settings.runner_scratch)
                       for line in Path("/proc/self/mountinfo").read_text().splitlines()):
                raise ValueError("Runner scratch must be a dedicated bounded tmpfs")
            self.control.mkdir(mode=0o700, exist_ok=True)
            # nsjail resolves these bind sources after switching to its mapped UID.
            # Search-only ancestors expose no directory listing; control/artifacts stay private.
            self.settings.runner_scratch.chmod(0o711)
            self.data.mkdir(mode=0o711, exist_ok=True)
            self.data.chmod(0o711)
            staging = Path("/run/user/0/nsjail")
            staging.mkdir(mode=0o700, parents=True, exist_ok=True)
            for path in (staging, staging / "root", staging / "tmp"):
                path.mkdir(mode=0o700, exist_ok=True)
                os.chown(path, 65534, 65534)
            self.cgroups.prepare(self.settings.limits(), self.settings.max_concurrent_sandboxes)
            if any(self.control.iterdir()) or any(self.data.iterdir()):
                raise ValueError("Runner scratch must be empty at startup")
            self.policy = verified_policy(self.settings)
            # A real trusted executable exercises BOTH installed profiles before readiness.
            for kind in ("compile", "run"):
                execution = self._reserve(uuid4(), min(10, self.policy.limits.submission_deadline_seconds))
                try:
                    namespaces = [str(os.stat(f"/proc/self/ns/{name}").st_ino)
                                  for name in ("net", "user", "pid", "mnt", "ipc", "uts", "cgroup")]
                    outcome = await self._stage(execution, kind, b"", probe=namespaces)
                    if outcome.status != "completed" or outcome.stdout != b"CHECKER_JAIL_READY\n":
                        raise RuntimeError("Jail readiness probe failed")
                finally:
                    await self.destroy(execution)
            self.ready = True
            self._reaper = asyncio.create_task(self._expire())
        except (OSError, ValueError, RuntimeError, ProtocolError) as exc:
            self.ready = False
            log.error("Runner readiness failed (%s); execution disabled", type(exc).__name__)

    def _reserve(self, submission_id: UUID, budget: float) -> Execution:
        # No await between checking capacity and inserting. Busy callers get 429, never wait in a second queue.
        if len(self.executions) >= self.settings.max_concurrent_sandboxes:
            raise ProtocolError(429, "slotsfull")
        if any(value.submission_id == submission_id for value in self.executions.values()):
            raise ProtocolError(409, "duplicate_submission")
        execution_id = uuid4()
        root = self.data / execution_id.hex
        try:
            execution = Execution(execution_id, submission_id, secrets.token_urlsafe(32),
                                  time.monotonic() + budget, root)
            root.mkdir(mode=0o711)
            self.executions[execution_id] = execution
            self._publish(execution)
            return execution
        except OSError as exc:
            self.ready = False
            # A partially published reservation stays recoverable; no new admission is safe.
            raise ProtocolError(503, "reservation_storage_failed") from exc

    def reserve(self, submission_id: UUID, policy_id: str, budget: float) -> Execution:
        if not self.ready or self.policy is None:
            raise ProtocolError(503, "not_ready")
        if policy_id != self.policy.digest:
            raise ProtocolError(503, "policy_mismatch")
        if budget > self.policy.limits.submission_deadline_seconds:
            raise ProtocolError(400, "invalid_budget")
        return self._reserve(submission_id, budget)

    def lookup(self, execution_id: UUID, handle: str | None) -> Execution:
        execution = self.executions.get(execution_id)
        if execution is None or handle is None or not hmac.compare_digest(execution.handle, handle):
            raise ProtocolError(404, "unknown_execution")
        return execution

    def describe(self, execution: Execution) -> dict:
        assert self.policy is not None
        return {"execution_id": str(execution.id), "submission_id": str(execution.submission_id),
                "handle": execution.handle, "policy_id": self.policy.digest,
                "policy": self.policy.model_dump(mode="json"), "state": execution.state,
                "remaining_seconds": max(0, execution.deadline - time.monotonic())}

    def begin(self, execution: Execution, kind: str, artifact: str | None = None) -> None:
        if not self.ready:
            raise ProtocolError(503, "not_ready")
        if execution.busy or execution.closing or time.monotonic() >= execution.deadline:
            raise ProtocolError(409, "execution_busy_or_expired")
        if kind == "compile":
            if execution.compiled:
                raise ProtocolError(409, "already_compiled")
            execution.compiled = True
        elif execution.artifact is None:
            raise ProtocolError(409, "compile_required")
        elif artifact is None or not hmac.compare_digest(execution.artifact, artifact):
            raise ProtocolError(409, "artifact_mismatch")
        execution.busy = True
        execution.state = kind
        execution.request = asyncio.current_task()

    async def execute(self, execution: Execution, kind: str, body: bytes) -> StageOutcome:
        execution.request = None
        execution.operation = asyncio.create_task(self._stage(execution, kind, body))
        return await asyncio.shield(execution.operation)

    async def _spawn(self, *argv: str, **options) -> asyncio.subprocess.Process:
        # Never let the orphan reaper steal a child while asyncio registers its watcher.
        async with self._spawn_lock:
            creation = asyncio.create_task(asyncio.create_subprocess_exec(*argv, **options))
            cancelled = False
            while True:
                try:
                    process = await asyncio.shield(creation)
                    break
                except asyncio.CancelledError:
                    if creation.cancelled():
                        raise
                    cancelled = True
            self._watched_children.add(process.pid)
            if cancelled:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                waiting = asyncio.create_task(self._wait_process(process))
                while True:
                    try:
                        await asyncio.shield(waiting)
                        break
                    except asyncio.CancelledError:
                        if waiting.cancelled():
                            raise
                        continue
                raise asyncio.CancelledError
            return process

    async def _wait_process(self, process: asyncio.subprocess.Process) -> int:
        try:
            return await process.wait()
        finally:
            if process.returncode is not None:
                self._watched_children.discard(process.pid)

    async def _reap_adopted(self) -> None:
        deadline = time.monotonic() + 3
        while True:
            async with self._spawn_lock:
                children: set[int] = set()
                for task in Path("/proc/self/task").iterdir():
                    try:
                        children.update(map(int, (task / "children").read_text().split()))
                    except FileNotFoundError:
                        continue
                adopted = children - self._watched_children
                if not adopted:
                    return
                for pid in adopted:
                    try:
                        await asyncio.to_thread(os.waitpid, pid, os.WNOHANG)
                    except ChildProcessError:
                        pass
            if time.monotonic() >= deadline:
                raise RuntimeError("An adopted stage process was not reaped")
            await asyncio.sleep(0.01)

    async def _command(self, *argv: str, timeout: float = 3) -> None:
        proc = await self._spawn(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL, close_fds=True,
                                                    env={"PATH": "/usr/bin:/bin"}, start_new_session=True)
        try:
            await asyncio.wait_for(self._wait_process(proc), max(0.01, timeout))
        except BaseException:
            proc.kill()
            await asyncio.wait_for(self._wait_process(proc), 3)
            raise
        if proc.returncode != 0:
            raise RuntimeError("Trusted mount operation failed")

    async def _cleanup_stage(self, execution: Execution, stage: Stage,
                             readers: list[asyncio.Task] | None = None) -> None:
        # First stop every writer and reap descendants; then read artifacts or remove writable storage.
        await self.cgroups.kill(stage.cgroup)
        if stage.process is not None:
            await asyncio.wait_for(self._wait_process(stage.process), 3)
        # A watchdog/OOM may already have removed zombies from cgroup.procs.
        # Reap all adopted children, excluding asyncio-owned direct subprocesses.
        await self._reap_adopted()
        # Drainers may still observe buffered overflow and signal the same cgroup.
        # Keep its control files until every pipe is closed, not merely until nsjail exits.
        if readers:
            await asyncio.wait_for(asyncio.gather(*readers), 3)
        # Remove registry reference before removing the cgroup, avoiding watchdog races.
        execution.stage = None
        self._publish(execution)
        stage.cgroup.rmdir()

    @staticmethod
    def _read_artifact(work: Path, target: Path, maximum: int) -> None:
        directory = os.open(work, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        descriptor = None
        try:
            descriptor = os.open("main", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                                 dir_fd=directory)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum or info.st_size == 0:
                raise ValueError("Invalid artifact")
            # Its cgroup is empty: no surviving child can mutate the inode during this bounded copy.
            value = bytearray()
            while len(value) <= maximum:
                block = os.read(descriptor, min(65536, maximum + 1 - len(value)))
                if not block:
                    break
                value.extend(block)
            if len(value) != info.st_size or len(value) > maximum:
                raise ValueError("Artifact exceeds policy")
            with target.open("xb") as stream:
                stream.write(value)
            target.chmod(0o500)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(directory)

    async def _stage(self, execution: Execution, kind: str, body: bytes,
                     probe: list[str] | None = None) -> StageOutcome:
        assert self.policy is not None
        limits = self.policy.limits
        stage_id = uuid4().hex
        work = execution.root / (stage_id + ".work")
        stage: Stage | None = None
        started = time.monotonic()
        stdout, stderr = bytearray(), bytearray()
        readers: list[asyncio.Task] = []
        status_reader = None
        status_transport = None
        status_write = None
        cleaned = False
        outcome = StageOutcome(status="infrastructure_error")
        try:
            group = self.cgroups.create(stage_id, limits)
            stage = Stage(stage_id, kind, group, work, execution.root / (stage_id + ".reason"))
            execution.stage = stage
            self._publish(execution)
            work.mkdir(mode=0o700)
            await self._command("/usr/bin/mount", "-t", "tmpfs", "-o",
                                f"size={limits.tmp_mib * 1048576},mode=0700,nosuid,nodev", "tmpfs", str(work),
                                timeout=min(3, execution.deadline - time.monotonic()))
            stage.mounted = True
            os.chown(work, 65534, 65534)
            (work / "tmp").mkdir(mode=0o700)
            os.chown(work / "tmp", 65534, 65534)
            if probe is None and kind == "compile":
                (work / "main.c").write_bytes(body)
                (work / "main.c").chmod(0o444)
            elif probe is None:
                shutil.copyfile(execution.root / "artifact", work / "main", follow_symlinks=False)
                (work / "main").chmod(0o555)
            if execution.cancelled.is_set():
                outcome = StageOutcome(status="infrastructure_error", resource_reason="cancelled")
                return outcome
            if time.monotonic() >= execution.deadline:
                outcome = StageOutcome(status="submission_timeout", resource_reason="timeout")
                return outcome
            status_read, status_write = os.pipe2(os.O_CLOEXEC)
            os.set_inheritable(status_write, True)
            status_reader = asyncio.StreamReader(limit=512)
            protocol = asyncio.StreamReaderProtocol(status_reader)
            status_transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                lambda: protocol, os.fdopen(status_read, "rb", buffering=0))
            argv = [self.settings.nsjail_binary, "--config", str(self.settings.jail_profiles_root / (kind + ".cfg")),
                    "--chroot", str(self.settings.jail_rootfs_root / kind), "--bindmount", str(work) + ":/work",
                    "--rlimit_as", "hard", "--rlimit_fsize", "hard", "--time_limit", "0",
                    "--log", "/dev/null", "--pass_fd", str(status_write)]
            if kind == "run" and probe is None:
                argv += ["--bindmount_ro", str(work / "main") + ":/work/main"]
            command = (["/checker-probe", *probe] if probe is not None else
                       ["/usr/bin/gcc", *GCC_FLAGS, "/work/main.c", "-o", "/work/main", "-lm", "-pthread"]
                       if kind == "compile" else ["/work/main"])
            argv += ["--", "/checker-init", "--status-fd", str(status_write), "--", *command]
            local_timeout = limits.compile_timeout_seconds if kind == "compile" else limits.run_timeout_seconds
            stage.deadline = min(execution.deadline, time.monotonic() + local_timeout)
            self._publish(execution)
            stage.process = await self._spawn(
                sys.executable, str(Path(__file__).with_name("stage.py")), str(group),
                str(limits.tmp_mib * 1048576), *argv,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                close_fds=True, pass_fds=(status_write,), start_new_session=True,
                env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"})
            os.close(status_write)
            status_write = None

            async def drain(stream: asyncio.StreamReader, destination: bytearray) -> None:
                while block := await stream.read(4096):
                    available = max(0, limits.output_bytes - len(stdout) - len(stderr))
                    destination.extend(block[:available])
                    if len(block) > available:
                        stop_reason(stage.reason, "output_limit")
                        (stage.cgroup / "cgroup.kill").write_text("1")

            async def feed() -> None:
                try:
                    stage.process.stdin.write(body if kind == "run" and probe is None else b"")
                    await stage.process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    stage.process.stdin.close()

            readers = [asyncio.create_task(drain(stage.process.stdout, stdout)),
                       asyncio.create_task(drain(stage.process.stderr, stderr)), asyncio.create_task(feed())]
            await stage.process.wait()  # Watchdog is independent of this event loop and HTTP connection.
            memory = counters(group / "memory.events")
            pid_events = counters(group / "pids.events")
            await self._cleanup_stage(execution, stage, readers)
            cleaned = True
            report = await asyncio.wait_for(status_reader.read(257), 3)
            reason = stage.reason.read_text() if stage.reason.exists() else None
            if reason is None and memory.get("oom_kill", 0):
                reason = "memory_limit"
            code, signum = None, None
            lines = report.splitlines()
            if len(lines) == 2 and lines[0] == b"START" and lines[1].startswith(b"EXIT "):
                _, code_raw, signal_raw = lines[1].split()
                code = int(code_raw)
                signum = int(signal_raw) or None
                if code < -1 or code > 255 or (signum is not None and not 1 <= signum <= 64):
                    raise RuntimeError("Invalid trusted init report")
                code = None if signum is not None else code
            elif len(lines) == 1 and lines[0].startswith(b"EXEC_ERROR ") and kind == "run" and probe is None:
                number = int(lines[0].split()[1])
                if not 1 <= number <= 4095:
                    raise RuntimeError("Invalid trusted exec failure")
                code = 126  # Invalid/unloadable submitted artifact is a runtime error, not infrastructure.
            elif reason is None:
                raise RuntimeError("Jail did not report trusted child completion")
            if reason is not None:
                result = reason if reason in {"compile_timeout", "run_timeout", "submission_timeout",
                                               "memory_limit", "output_limit"} else "infrastructure_error"
            else:
                result = "completed" if code == 0 and signum is None else kind + "_error"
            resource = ({"memory_limit": "memory", "output_limit": "output"}.get(result)
                        or ("timeout" if result.endswith("timeout") else None))
            if reason == "cancelled":
                resource = "cancelled"
            if result in {"compile_error", "run_error"} and pid_events.get("max", 0):
                resource = "pids"
            if result == "run_error":
                result = "runtime_error"
            outcome = StageOutcome(status=result, exit_code=code, signal=signum, stdout=bytes(stdout),
                                   stderr=bytes(stderr), resource_reason=resource,
                                   wall_seconds=time.monotonic() - started)
            if result == "completed" and kind == "compile" and probe is None:
                try:
                    self._read_artifact(work, execution.root / "artifact", limits.artifact_bytes)
                    execution.artifact = secrets.token_urlsafe(32)
                    outcome.artifact_handle = execution.artifact
                except (OSError, ValueError):
                    outcome.status = "compile_error"
                    outcome.resource_reason = "artifact"
            return outcome
        except (OSError, ValueError, RuntimeError, ProtocolError) as exc:
            self.ready = False
            log.error("Stage infrastructure failure (%s)", type(exc).__name__)
            outcome = StageOutcome(status="infrastructure_error", stdout=bytes(stdout), stderr=bytes(stderr),
                                   wall_seconds=time.monotonic() - started)
            return outcome
        finally:
            try:
                if stage is not None and not cleaned:
                    await self._cleanup_stage(execution, stage, readers)
                for task in readers:
                    if not task.done():
                        task.cancel()
                if readers:
                    await asyncio.gather(*readers, return_exceptions=True)
                if status_transport is not None:
                    status_transport.close()
                if status_write is not None:
                    os.close(status_write)
                if stage is not None and stage.mounted:
                    await self._command("/usr/bin/umount", str(work))
                    stage.mounted = False
                if work.exists():
                    shutil.rmtree(work)
                if stage is not None:
                    stage.reason.unlink(missing_ok=True)
                execution.busy = False
                execution.state = "ready" if execution.artifact is not None else "compiled"
                outcome.wall_seconds = time.monotonic() - started
                if outcome.status == "completed" and time.monotonic() >= execution.deadline:
                    outcome.status = "submission_timeout"
                    outcome.resource_reason = "timeout"
                    outcome.artifact_handle = None
            except (OSError, ValueError, RuntimeError, ProtocolError) as exc:
                # Isolate the slot. Do not make room or report cleanup success.
                self.ready = False
                execution.closing = True
                outcome.status = "infrastructure_error"
                outcome.artifact_handle = None
                execution.artifact = None
                log.error("Stage cleanup failure (%s); slot quarantined", type(exc).__name__)

    async def destroy(self, execution: Execution) -> None:
        if execution.cleanup is None:
            execution.cleanup = asyncio.create_task(self._destroy(execution))
        await asyncio.shield(execution.cleanup)

    async def _destroy(self, execution: Execution) -> None:
        execution.closing = True
        execution.cancelled.set()
        try:
            if execution.request is not None and execution.request is not asyncio.current_task():
                execution.request.cancel()
                await asyncio.gather(execution.request, return_exceptions=True)
            if execution.stage is not None:
                stop_reason(execution.stage.reason, "cancelled")
                (execution.stage.cgroup / "cgroup.kill").write_text("1")
            if execution.operation is not None and execution.operation is not asyncio.current_task():
                await asyncio.wait_for(asyncio.shield(execution.operation), 8)
            if execution.stage is not None:
                raise RuntimeError("Stage cleanup is incomplete")
            # Busy without a stage means a failed/quarantined cleanup, never a reusable slot.
            if execution.busy:
                raise RuntimeError("Execution cleanup is incomplete")
            (self.control / (execution.id.hex + ".json")).unlink(missing_ok=True)
            if any(os.path.ismount(path) for path in execution.root.iterdir()):
                raise RuntimeError("Execution still owns a mounted filesystem")
            shutil.rmtree(execution.root)
            self.executions.pop(execution.id, None)
        except Exception as exc:
            self.ready = False
            raise ProtocolError(503, "cleanup_failed") from exc

    async def _expire(self) -> None:
        while True:
            for execution in list(self.executions.values()):
                if time.monotonic() >= execution.deadline and not execution.closing:
                    if execution.operation is not None and not execution.operation.done():
                        continue  # Watchdog has killed it; preserve its terminal response until stage cleanup.
                    try:
                        await self.destroy(execution)
                    except ProtocolError:
                        pass
            await asyncio.sleep(0.025)

    async def close(self) -> None:
        self.ready = False
        if self._reaper is not None:
            self._reaper.cancel()
            await asyncio.gather(self._reaper, return_exceptions=True)
        await asyncio.gather(*(self.destroy(e) for e in list(self.executions.values())))
