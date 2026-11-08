from __future__ import annotations

import argparse
import asyncio
import base64
import hmac
import json
import logging
import os
import selectors
import signal
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from uvicorn.protocols.http.h11_impl import H11Protocol

from ..config import Settings
from ..domain.models import StageOutcome
from .executor import ProtocolError, RunnerExecutor


class Reservation(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    submission_id: UUID
    policy_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    remaining_seconds: float = Field(gt=0, le=60)


class BoundedH11Protocol(H11Protocol):
    """Bound incomplete headers even when a peer continually dribbles bytes."""

    def _arm_headers(self):
        self._cancel_headers()
        self._header_timer = self.loop.call_later(3, self.transport.close)

    def _cancel_headers(self):
        timer = getattr(self, "_header_timer", None)
        if timer is not None:
            timer.cancel()
            self._header_timer = None

    def connection_made(self, transport):
        super().connection_made(transport)
        self._arm_headers()

    def handle_events(self):
        super().handle_events()
        if self.cycle is not None and not self.cycle.response_complete:
            self._cancel_headers()

    def on_response_complete(self):
        self._arm_headers()
        super().on_response_complete()

    def connection_lost(self, exc):
        self._cancel_headers()
        super().connection_lost(exc)


class SecurityBoundary:
    def __init__(self, app, token: str, maximum_seconds: float):
        self.app = app
        self.expected = ("Bearer " + token).encode()
        self.maximum_seconds = maximum_seconds

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = False

        async def guarded_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            async with asyncio.timeout(self.maximum_seconds):
                return await self._dispatch(scope, receive, guarded_send)
        except TimeoutError:
            if started:
                raise RuntimeError("Runner response deadline exceeded") from None
            response = JSONResponse({"error": "request_deadline"}, 503, headers={"Connection": "close"})
            await asyncio.wait_for(response(scope, receive, send), 1)

    async def _dispatch(self, scope, receive, send):
        headers = scope.get("headers", [])
        if (sum(len(k) + len(v) + 4 for k, v in headers) > 8192
                or len(scope.get("raw_path", b"")) > 2048 or len(scope.get("query_string", b"")) > 1024):
            return await JSONResponse({"error": "header_limit"}, 413)(scope, receive, send)
        tokens = [value for key, value in headers if key == b"authorization"]
        if len(tokens) != 1 or not hmac.compare_digest(tokens[0], self.expected):
            return await JSONResponse({"error": "unauthorized"}, 401)(scope, receive, send)
        return await self.app(scope, receive, send)


async def bounded_body(request: Request, limit: int, timeout: float) -> bytes:
    lengths = request.headers.getlist("content-length")
    if len(lengths) > 1:
        raise ProtocolError(400, "invalid_content_length")
    if lengths:
        try:
            length = int(lengths[0])
        except ValueError as exc:
            raise ProtocolError(400, "invalid_content_length") from exc
        if length < 0:
            raise ProtocolError(400, "invalid_content_length")
        if length > limit:
            raise ProtocolError(413, "body_limit")
    body = bytearray()
    try:
        async with asyncio.timeout(max(0.001, timeout)):
            async for chunk in request.stream():
                if len(body) + len(chunk) > limit:
                    raise ProtocolError(413, "body_limit")
                body.extend(chunk)
    except TimeoutError as exc:
        raise ProtocolError(503, "request_deadline") from exc
    if lengths and len(body) != int(lengths[0]):
        raise ProtocolError(400, "invalid_content_length")
    return bytes(body)


def outcome_json(outcome: StageOutcome) -> dict:
    data = outcome.model_dump(mode="json")
    data.update(stdout_base64=base64.b64encode(outcome.stdout).decode("ascii"),
                stderr_base64=base64.b64encode(outcome.stderr).decode("ascii"))
    if len(json.dumps(data, separators=(",", ":")).encode()) > 98304:
        raise ProtocolError(503, "outcome_limit")
    return data


def opaque_header(request: Request, name: str) -> str | None:
    values = request.headers.getlist(name)
    if not values:
        return None
    value = values[0]
    if len(values) != 1 or len(value) > 128 or not value.isascii():
        raise ProtocolError(413, "invalid_handle_header")
    return value


def create_app(settings: Settings | None = None, *, executor: RunnerExecutor | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.require("runner")
    if len(settings.runner_token.encode()) < 32:
        raise ValueError("RUNNER_TOKEN must contain at least 32 bytes")
    engine = executor if executor is not None else RunnerExecutor(settings)

    @asynccontextmanager
    async def lifespan(app):
        await engine.start()
        heartbeat = None
        if os.environ.get("CHECKER_HEARTBEAT_FD"):
            descriptor = int(os.environ["CHECKER_HEARTBEAT_FD"])

            async def pulse():
                while True:
                    os.write(descriptor, b"A")
                    await asyncio.sleep(0.25)

            heartbeat = asyncio.create_task(pulse())
        try:
            yield
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            await engine.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.executor = engine
    app.add_middleware(SecurityBoundary, token=settings.runner_token,
                       maximum_seconds=settings.submission_deadline_seconds + 10)

    @app.exception_handler(ProtocolError)
    async def protocol_error(request, exc):
        return JSONResponse({"error": exc.code}, exc.status)

    @app.get("/health")
    async def health():
        return JSONResponse({"ready": engine.ready}, 200 if engine.ready else 503)

    @app.get("/v1/policy")
    async def policy():
        if not engine.ready or engine.policy is None:
            raise ProtocolError(503, "not_ready")
        return engine.policy.model_dump(mode="json")

    @app.post("/v1/executions", status_code=201)
    async def reserve(request: Request):
        if request.headers.get("content-type", "").lower() != "application/json":
            raise ProtocolError(415, "content_type")
        raw = await bounded_body(request, 4096, 3)
        try:
            reservation = Reservation.model_validate_json(raw)
        except ValidationError as exc:
            raise ProtocolError(400, "invalid_reservation") from exc
        execution = engine.reserve(reservation.submission_id, reservation.policy_id, reservation.remaining_seconds)
        return JSONResponse(engine.describe(execution), 201)

    @app.get("/v1/executions")
    async def executions():
        return [engine.describe(execution) for execution in engine.executions.values()]

    @app.delete("/v1/executions/{execution_id}", status_code=204)
    async def destroy(execution_id: UUID, request: Request):
        if execution_id in engine.executions:
            execution = engine.lookup(execution_id, opaque_header(request, "X-Execution-Handle"))
            await engine.destroy(execution)
        return Response(status_code=204)

    async def stage(execution_id: UUID, kind: str, request: Request):
        if request.headers.get("content-type", "").lower() != "application/octet-stream":
            raise ProtocolError(415, "content_type")
        execution = engine.lookup(execution_id, opaque_header(request, "X-Execution-Handle"))
        artifact = opaque_header(request, "X-Artifact-Handle") if kind == "run" else None
        engine.begin(execution, kind, artifact)
        launched = False
        try:
            assert engine.policy is not None
            limits = engine.policy.limits
            limit = limits.source_bytes if kind == "compile" else limits.test_stdin_bytes
            body = await bounded_body(request, limit, execution.deadline - time.monotonic())
            launched = True
            outcome = await engine.execute(execution, kind, body)
            return outcome_json(outcome)
        finally:
            if not launched:
                execution.request = None
                execution.busy = False
                execution.state = "compiled" if execution.compiled else "reserved"

    @app.post("/v1/executions/{execution_id}/compile")
    async def compile_source(execution_id: UUID, request: Request):
        return await stage(execution_id, "compile", request)

    @app.post("/v1/executions/{execution_id}/run")
    async def run(execution_id: UUID, request: Request):
        return await stage(execution_id, "run", request)

    return app


def serve() -> None:
    settings = Settings()
    uvicorn.run(create_app(settings), host=settings.runner_host, port=settings.runner_port,
                workers=1, http=BoundedH11Protocol, access_log=False, server_header=False, timeout_keep_alive=2,
                h11_max_incomplete_event_size=8192, limit_concurrency=settings.max_concurrent_sandboxes * 4 + 16,
                timeout_graceful_shutdown=8)


def supervise(settings: Settings) -> None:
    if os.getpid() != 1:
        raise SystemExit("checker-runner must be PID1 in its private container PID namespace")
    settings.require("runner")
    scratch = settings.runner_scratch
    scratch.mkdir(mode=0o711, parents=True, exist_ok=True)
    (scratch / "control").mkdir(mode=0o700, exist_ok=True)
    (scratch / "executions").mkdir(mode=0o711, exist_ok=True)
    stopping = False

    def terminate(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    selector = selectors.DefaultSelector()
    children: list[subprocess.Popen] = []
    try:
        for role in ("watchdog", "api"):
            read_fd, write_fd = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
            environment = dict(os.environ, CHECKER_SUPERVISED="1", CHECKER_HEARTBEAT_FD=str(write_fd))
            if role == "watchdog":
                command = [sys.executable, "-m", "os_sol_checker.runner.watchdog", "--scratch", str(scratch),
                           "--cgroup-root", str(settings.nsjail_cgroup_root), "--heartbeat-fd", str(write_fd),
                           "--slots", str(settings.max_concurrent_sandboxes)]
            else:
                command = [sys.executable, "-m", "os_sol_checker.runner.main", "--api"]
            child = subprocess.Popen(command, env=environment, close_fds=True, pass_fds=(write_fd,))
            children.append(child)
            os.close(write_fd)
            selector.register(read_fd, selectors.EVENT_READ, {"last": time.monotonic(), "role": role})
        began = time.monotonic()
        while not stopping:
            for key, _ in selector.select(0.05):
                if not os.read(key.fd, 4096):
                    stopping = True
                key.data["last"] = time.monotonic()
            if any(child.poll() is not None for child in children):
                stopping = True
            # Startup verifies real namespaces/rootfs and may take a finite 30 seconds.
            threshold = 30 if time.monotonic() - began < 30 else 3
            if any(time.monotonic() - key.data["last"] > threshold for key in selector.get_map().values()):
                stopping = True
            # Reap grandchildren orphaned when a trusted component unexpectedly exits.
            # Popen owns direct children, so do not steal their wait status.
            direct = {child.pid for child in children}
            try:
                adopted = Path(f"/proc/self/task/{os.getpid()}/children").read_text().split()
                for value in adopted:
                    if int(value) not in direct:
                        os.waitpid(int(value), os.WNOHANG)
            except (FileNotFoundError, ChildProcessError):
                pass
    finally:
        # PID1 is in the outer private PID namespace. This cannot target host processes.
        # Kill all descendants, including ones with a new session/process group, then die as PID1.
        for child in children:
            if child.poll() is None:
                child.terminate()
        try:
            os.kill(-1, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for child in children:
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        selector.close()
    raise SystemExit(1)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--health", action="store_true")
    args = parser.parse_args()
    if args.health:
        import urllib.request
        settings = Settings()
        request = urllib.request.Request(f"http://127.0.0.1:{settings.runner_port}/health",
                                         headers={"Authorization": "Bearer " + settings.runner_token})
        with urllib.request.urlopen(request, timeout=2) as response:
            if response.status != 200:
                raise SystemExit(1)
    elif args.api:
        if os.environ.get("CHECKER_SUPERVISED") != "1":
            raise SystemExit("API requires the runner supervisor")
        serve()
    else:
        supervise(Settings())


if __name__ == "__main__":
    main()
