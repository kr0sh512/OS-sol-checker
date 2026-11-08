import asyncio
import base64
import json
import time
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest

from os_sol_checker.adapters.runner.client import RunnerClient, RunnerError
from os_sol_checker.config import Settings
from os_sol_checker.domain.models import JudgePolicy, StageOutcome
from os_sol_checker.runner.executor import RunnerExecutor
from os_sol_checker.runner.main import create_app


class OrchestrationBoundary(RunnerExecutor):
    """Tests replace only the OS launch boundary, not auth/reservations/protocol state."""

    async def start(self):
        self.control.mkdir(parents=True)
        self.data.mkdir(parents=True)
        self.policy = JudgePolicy(runner_image_digest="a" * 64, sandbox_profile_digest="b" * 64,
                                  flags_digest="c" * 64, limits=self.settings.limits())
        self.ready = True
        self.inputs = []
        self.gate = None

    async def _stage(self, execution, kind, body, probe=None):
        self.inputs.append((kind, body))
        if self.gate is not None:
            await self.gate.wait()
        if kind == "compile":
            execution.artifact = "A" * 43
        execution.busy = False
        return StageOutcome(status="completed", exit_code=0, stdout=body if kind == "run" else b"",
                            stderr=b"\x00\xff", artifact_handle=execution.artifact if kind == "compile" else None)


@asynccontextmanager
async def running_api(tmp_path, slots=2):
    settings = Settings(runner_token="t" * 32, runner_image="r@sha256:" + "a" * 64,
                        runner_scratch=tmp_path / "scratch", runner_url="http://test",
                        max_concurrent_sandboxes=slots)
    engine = OrchestrationBoundary(settings)
    app = create_app(settings, executor=engine)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                          headers={"Authorization": "Bearer " + settings.runner_token}) as http,
    ):
        yield settings, engine, http


async def reserve(engine, http, **changes):
    data = {"submission_id": str(uuid4()), "policy_id": engine.policy.digest, "remaining_seconds": 10}
    data.update(changes)
    return await http.post("/v1/executions", json=data)


def stage_headers(row, artifact=None):
    value = {"X-Execution-Handle": row["handle"], "Content-Type": "application/octet-stream"}
    if artifact is not None:
        value["X-Artifact-Handle"] = artifact
    return value


@pytest.mark.parametrize("path,method", [("/health", "GET"), ("/v1/policy", "GET"),
                                         ("/v1/executions", "GET"), ("/v1/executions/" + str(uuid4()), "DELETE")])
async def test_runner_api_all_routes_require_bearer_before_metadata(tmp_path, path, method):
    async with running_api(tmp_path) as (_, engine, http):
        response = await http.request(method, path, headers={"Authorization": "Bearer wrong"})
        assert response.status_code == 401
        assert response.json() == {"error": "unauthorized"}
        assert engine.executions == {}


async def test_runner_api_authoritative_capacity_no_second_queue(tmp_path):
    async with running_api(tmp_path, slots=2) as (_, engine, http):
        responses = await asyncio.gather(*(reserve(engine, http) for _ in range(3)))
        assert sorted(response.status_code for response in responses) == [201, 201, 429]
        assert len(engine.executions) == 2
        first = next(response.json() for response in responses if response.status_code == 201)
        missing = await http.delete("/v1/executions/" + str(uuid4()))
        assert missing.status_code == 204
        denied = await http.delete("/v1/executions/" + first["execution_id"],
                                   headers={"X-Execution-Handle": "foreign"})
        assert denied.status_code == 404
        removed = await http.delete("/v1/executions/" + first["execution_id"], headers=stage_headers(first))
        assert removed.status_code == 204
        assert (await reserve(engine, http)).status_code == 201


async def test_runner_api_exact_source_bound_binary_roundtrip_and_state(tmp_path):
    async with running_api(tmp_path) as (_, engine, http):
        row = (await reserve(engine, http)).json()
        path = "/v1/executions/" + row["execution_id"]
        early = await http.post(path + "/run", content=b"", headers=stage_headers(row, "x"))
        assert early.status_code == 409
        source = b"x" * 102400
        compiled = await http.post(path + "/compile", content=source, headers=stage_headers(row))
        assert compiled.status_code == 200
        assert engine.inputs == [("compile", source)]
        assert base64.b64decode(compiled.json()["stderr_base64"]) == b"\x00\xff"
        artifact = compiled.json()["artifact_handle"]
        again = await http.post(path + "/compile", content=b"", headers=stage_headers(row))
        assert again.status_code == 409
        foreign = await http.post(path + "/run", content=b"", headers=stage_headers(row, "foreign"))
        assert foreign.status_code == 409
        raw = b"\x00\xff\r\n \x80"
        result = await http.post(path + "/run", content=raw, headers=stage_headers(row, artifact))
        assert base64.b64decode(result.json()["stdout_base64"]) == raw
        assert engine.inputs[-1] == ("run", raw)
        assert len(engine.executions) == 1  # Successful stages do not free a submission's slot.


async def test_runner_api_rejects_streaming_overflow_and_parallel_stage(tmp_path):
    async with running_api(tmp_path) as (_, engine, http):
        row = (await reserve(engine, http)).json()
        path = "/v1/executions/" + row["execution_id"]

        async def oversized():
            yield b"x" * 65536
            yield b"x" * 65536

        response = await http.post(path + "/compile", content=oversized(), headers=stage_headers(row))
        assert response.status_code == 413
        assert engine.inputs == []
        row = (await reserve(engine, http)).json()
        path = "/v1/executions/" + row["execution_id"]
        engine.gate = asyncio.Event()
        first = asyncio.create_task(http.post(path + "/compile", content=b"", headers=stage_headers(row)))
        while not engine.inputs:
            await asyncio.sleep(0)
        second = await http.post(path + "/compile", content=b"", headers=stage_headers(row))
        assert second.status_code == 409
        engine.gate.set()
        assert (await first).status_code == 200


async def test_runner_api_bounds_headers_reservation_schema_policy_and_slow_body(tmp_path):
    async with running_api(tmp_path) as (_, engine, http):
        assert (await http.get("/health", headers={"X-Oversized": "x" * 8192})).status_code == 413
        assert (await reserve(engine, http, policy_id="d" * 64)).status_code == 503
        invalid_budget = json.dumps({"submission_id": str(uuid4()), "policy_id": engine.policy.digest,
                                     "remaining_seconds": float("inf")}).encode()
        assert (await http.post("/v1/executions", content=invalid_budget,
                                headers={"Content-Type": "application/json"})).status_code == 400
        assert (await reserve(engine, http, mounts=["/"])).status_code == 400
        assert (await http.post("/v1/executions", content=b" " * 4097,
                                headers={"Content-Type": "application/json"})).status_code == 413
        row = (await reserve(engine, http, remaining_seconds=0.02)).json()

        async def slow():
            await asyncio.sleep(0.05)
            yield b"x"

        response = await http.post("/v1/executions/" + row["execution_id"] + "/compile",
                                   content=slow(), headers=stage_headers(row))
        assert response.status_code == 503
        assert engine.inputs == []


async def test_runner_client_exact_public_contract_uses_raw_bytes(tmp_path):
    async with running_api(tmp_path) as (settings, _engine, http):
        client = RunnerClient(settings)
        await client._http.aclose()
        client._http = http
        policy = await client.policy()
        handle = await client.open(uuid4(), policy, 10)
        compiled = await client.compile(handle, b"source\x00\xff")
        assert compiled.status == "completed" and compiled.artifact_handle
        actual = await client.run(handle, compiled.artifact_handle, b"\x00\xff\r\n")
        assert actual.stdout == b"\x00\xff\r\n"
        assert actual.stderr == b"\x00\xff"
        assert (await client.executions())[0].execution_id == handle.execution_id
        await client.destroy(handle)
        assert await client.executions() == []


async def test_runner_client_never_retries_ambiguous_compile(tmp_path):
    settings = Settings(runner_token="t" * 32)
    client = RunnerClient(settings)
    await client._http.aclose()
    calls = []

    async def transport(request):
        calls.append(request.method)
        raise httpx.ReadError("lost response", request=request)

    client._http = httpx.AsyncClient(transport=httpx.MockTransport(transport), base_url="http://test")
    from os_sol_checker.domain.models import ExecutionHandle
    handle = ExecutionHandle(execution_id=uuid4(), handle="h" * 43)
    client._deadlines[handle.execution_id] = time.monotonic() + 10
    client._policies[handle.execution_id] = JudgePolicy(runner_image_digest="a" * 64,
                                                      sandbox_profile_digest="b" * 64, flags_digest="c" * 64)
    try:
        with pytest.raises(RunnerError, match="unavailable"):
            await client.compile(handle, b"source")
        assert calls == ["POST"]
    finally:
        await client.close()


async def test_runner_client_lost_reservation_reconciles_and_destroys_without_second_post(tmp_path):
    async with running_api(tmp_path) as (settings, engine, http):
        calls = []

        async def lose_reservation_response(request):
            calls.append(request.method)
            response = await http._transport.handle_async_request(request)
            if request.method == "POST":
                await response.aclose()
                raise httpx.ReadError("reservation reply lost", request=request)
            return response

        client = RunnerClient(settings)
        await client._http.aclose()
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(lose_reservation_response),
                                         base_url="http://test",
                                         headers={"Authorization": "Bearer " + settings.runner_token})
        try:
            with pytest.raises(RunnerError):
                await client.open(uuid4(), engine.policy, 10)
            assert calls == ["POST", "GET", "DELETE"]
            assert engine.executions == {}
        finally:
            await client.close()


@pytest.mark.parametrize("stdout", ["invalid!base64", base64.b64encode(b"x" * 65537).decode()])
async def test_runner_client_rejects_invalid_or_over_budget_binary_outcome(stdout):
    settings = Settings(runner_token="t" * 32)
    client = RunnerClient(settings)
    await client._http.aclose()

    async def transport(request):
        return httpx.Response(200, json={"status": "completed", "exit_code": 0, "signal": None,
                                         "wall_seconds": 0.1, "resource_reason": None, "artifact_handle": None,
                                         "stdout_base64": stdout, "stderr_base64": ""})

    client._http = httpx.AsyncClient(transport=httpx.MockTransport(transport), base_url="http://test")
    from os_sol_checker.domain.models import ExecutionHandle
    handle = ExecutionHandle(execution_id=uuid4(), handle="h" * 43)
    client._deadlines[handle.execution_id] = time.monotonic() + 10
    client._policies[handle.execution_id] = JudgePolicy(runner_image_digest="a" * 64,
                                                      sandbox_profile_digest="b" * 64, flags_digest="c" * 64)
    try:
        with pytest.raises(RunnerError, match="Invalid bounded stage outcome"):
            await client.run(handle, "a" * 43, b"")
    finally:
        await client.close()
