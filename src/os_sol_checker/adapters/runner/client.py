from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
import time
from uuid import UUID

import httpx
from pydantic import ValidationError

from ...config import Settings
from ...domain.models import ExecutionHandle, JudgePolicy, StageOutcome

OUTCOME_BYTES = 98304
STATUSES = frozenset({"completed", "compile_error", "runtime_error", "compile_timeout", "run_timeout",
                      "submission_timeout", "memory_limit", "output_limit", "infrastructure_error"})


class RunnerError(Exception):
    """An unsuccessful or ambiguous runner operation; never automatically retry it."""

    def __init__(self, code: str, message: str = "Runner operation failed", status_code: int = 503):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class RunnerClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._http = httpx.AsyncClient(base_url=settings.runner_url.rstrip("/"), trust_env=False,
                                      follow_redirects=False, headers={"Authorization": f"Bearer {settings.runner_token}"},
                                      timeout=httpx.Timeout(65, connect=3, pool=3),
                                      limits=httpx.Limits(max_connections=settings.max_concurrent_sandboxes + 4))
        self._deadlines: dict[UUID, float] = {}
        self._policies: dict[UUID, JudgePolicy] = {}

    async def _request(self, method: str, path: str, *, limit: int = 8192, timeout: float = 65,
                       headers: dict | None = None, content: bytes | None = None) -> dict | list | None:
        try:
            async with asyncio.timeout(timeout):
                async with self._http.stream(method, path, headers=headers, content=content, timeout=timeout) as response:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > limit:
                            raise RunnerError("protocol", "Runner response exceeds its bound")
                        raw.extend(chunk)
                    if response.status_code >= 300:
                        code = "slotsfull" if response.status_code == 429 else "runner_unavailable"
                        raise RunnerError(code, "Runner rejected operation", response.status_code)
                    if response.status_code == 204:
                        if raw:
                            raise RunnerError("protocol", "Unexpected runner response body")
                        return None
                    if response.headers.get("content-type", "").split(";", 1)[0] != "application/json":
                        raise RunnerError("protocol", "Invalid runner response type")
                    return json.loads(raw)
        except (httpx.HTTPError, ValueError, TimeoutError) as exc:
            raise RunnerError("transport", "Runner response is unavailable or invalid") from exc

    async def policy(self) -> JudgePolicy:
        data = await self._request("GET", "/v1/policy")
        try:
            return JudgePolicy.model_validate(data)
        except ValidationError as exc:
            raise RunnerError("protocol", "Invalid judge policy") from exc

    @staticmethod
    def _handle(data: dict) -> ExecutionHandle:
        try:
            handle = ExecutionHandle(execution_id=data["execution_id"], handle=data["handle"])
            if not 32 <= len(handle.handle) <= 128 or not handle.handle.isascii():
                raise ValueError("invalid handle")
            return handle
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise RunnerError("protocol", "Invalid execution handle") from exc

    async def open(self, submission_id: UUID, policy: JudgePolicy, remaining_seconds: float) -> ExecutionHandle:
        if not math.isfinite(remaining_seconds) or not 0 < remaining_seconds <= 60:
            raise RunnerError("protocol", "Invalid remaining budget", 400)
        # Delivery is itself wall-bounded; subtract its entire bound before fixing the remote TTL.
        delivery = min(5.0, remaining_seconds / 4)
        budget = remaining_seconds - delivery
        body = json.dumps({"submission_id": str(submission_id), "policy_id": policy.digest,
                           "remaining_seconds": budget}, separators=(",", ":")).encode()
        started = time.monotonic()
        try:
            data = await self._request("POST", "/v1/executions", content=body,
                                       headers={"Content-Type": "application/json"}, timeout=delivery)
            if not isinstance(data, dict):
                raise RunnerError("protocol", "Invalid reservation response")
            handle = self._handle(data)
            if data.get("submission_id") != str(submission_id) or data.get("policy_id") != policy.digest:
                raise RunnerError("policy_mismatch", "Runner confirmed a different context")
            confirmed = JudgePolicy.model_validate(data.get("policy"))
            ttl = data.get("remaining_seconds")
            if confirmed != policy or not isinstance(ttl, (float, int)) or not math.isfinite(ttl) or not 0 < ttl <= budget:
                raise RunnerError("policy_mismatch", "Runner confirmed a different policy or budget")
            self._deadlines[handle.execution_id] = started + budget
            self._policies[handle.execution_id] = policy
            return handle
        except (RunnerError, ValidationError, asyncio.CancelledError) as exc:
            # A lost POST response is ambiguous. Reconcile by submission UUID, never re-POST.
            # If reconciliation also fails the error propagates and the runner's fixed TTL remains authoritative.
            async def reconcile():
                rows = await self._request("GET", "/v1/executions",
                                           limit=4096 + self.settings.max_concurrent_sandboxes * 8192, timeout=5)
                if isinstance(rows, list):
                    for row in rows:
                        if isinstance(row, dict) and row.get("submission_id") == str(submission_id):
                            await self.destroy(self._handle(row))

            try:
                await asyncio.shield(asyncio.wait_for(reconcile(), 8))
            except (RunnerError, TimeoutError, asyncio.CancelledError):
                pass
            if isinstance(exc, ValidationError):
                raise RunnerError("protocol", "Invalid confirmed judge policy") from exc
            raise

    def _headers(self, handle: ExecutionHandle) -> dict[str, str]:
        return {"X-Execution-Handle": handle.handle, "Content-Type": "application/octet-stream"}

    async def _stage(self, handle: ExecutionHandle, stage: str, body: bytes, headers: dict) -> StageOutcome:
        policy = self._policies.get(handle.execution_id)
        if policy is None:
            raise RunnerError("protocol", "Execution was not opened by this client")
        maximum = policy.limits.source_bytes if stage == "compile" else policy.limits.test_stdin_bytes
        if len(body) > maximum:
            raise RunnerError("body_limit", "Runner request exceeds policy", 413)
        remaining = self._deadlines[handle.execution_id] - time.monotonic()
        if remaining <= 0:
            raise RunnerError("submission_timeout", "Submission budget exhausted")
        data = await self._request("POST", f"/v1/executions/{handle.execution_id}/{stage}", content=body,
                                   headers=headers, limit=OUTCOME_BYTES, timeout=remaining + 5)
        try:
            if not isinstance(data, dict):
                raise ValueError("not an object")
            raw = dict(data)
            stdout = base64.b64decode(raw.pop("stdout_base64"), validate=True)
            stderr = base64.b64decode(raw.pop("stderr_base64"), validate=True)
            if len(stdout) + len(stderr) > policy.limits.output_bytes:
                raise ValueError("output bound")
            outcome = StageOutcome(**raw, stdout=stdout, stderr=stderr)
            if outcome.status not in STATUSES or not math.isfinite(outcome.wall_seconds) or outcome.wall_seconds < 0:
                raise ValueError("invalid outcome")
            if outcome.resource_reason not in {None, "memory", "pids", "output", "artifact", "timeout", "cancelled"}:
                raise ValueError("invalid resource reason")
            if outcome.artifact_handle is not None and (not 32 <= len(outcome.artifact_handle) <= 128
                                                        or not outcome.artifact_handle.isascii()):
                raise ValueError("invalid artifact")
            if outcome.status == "completed" and (outcome.exit_code != 0 or outcome.signal is not None):
                raise ValueError("inconsistent successful outcome")
            return outcome
        except (KeyError, TypeError, ValueError, binascii.Error, ValidationError) as exc:
            raise RunnerError("protocol", "Invalid bounded stage outcome") from exc

    async def compile(self, handle: ExecutionHandle, source: bytes) -> StageOutcome:
        return await self._stage(handle, "compile", source, self._headers(handle))

    async def run(self, handle: ExecutionHandle, artifact: str, stdin: bytes) -> StageOutcome:
        headers = self._headers(handle)
        headers["X-Artifact-Handle"] = artifact
        return await self._stage(handle, "run", stdin, headers)

    async def destroy(self, handle: ExecutionHandle) -> None:
        await self._request("DELETE", f"/v1/executions/{handle.execution_id}",
                            headers={"X-Execution-Handle": handle.handle}, timeout=8)
        self._deadlines.pop(handle.execution_id, None)
        self._policies.pop(handle.execution_id, None)

    async def executions(self) -> list[ExecutionHandle]:
        rows = await self._request("GET", "/v1/executions",
                                   limit=4096 + self.settings.max_concurrent_sandboxes * 8192)
        if not isinstance(rows, list) or len(rows) > self.settings.max_concurrent_sandboxes:
            raise RunnerError("protocol", "Invalid active execution list")
        return [self._handle(row) for row in rows]

    async def close(self) -> None:
        await self._http.aclose()
