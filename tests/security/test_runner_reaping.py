"""Exercise Linux adoption/reaping with trusted Python children, never student code."""
import subprocess
import sys

import pytest

SCENARIO = r'''
import asyncio
import ctypes
import os
import sys
from pathlib import Path
import tempfile
import time
from uuid import uuid4

from os_sol_checker.config import Settings
from os_sol_checker.runner.executor import Execution, RunnerExecutor, Stage

class AlreadyEmptyCgroup:
    async def kill(self, path):
        # The watchdog already killed the tree: zombies are absent from cgroup.procs.
        return []

async def main():
    if ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) != 0:
        raise RuntimeError("cannot configure isolated test subreaper")
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        engine = RunnerExecutor(Settings(runner_scratch=root))
        engine.control.mkdir()
        engine.cgroups = AlreadyEmptyCgroup()
        group = root / "group"
        group.mkdir()
        code = "import os; child=os.fork(); os._exit(0)"
        direct = await asyncio.create_subprocess_exec(sys.executable, "-c", code)
        await direct.wait()
        children = Path(f"/proc/self/task/{os.getpid()}/children")
        until = time.monotonic() + 2
        while not children.read_text().strip():
            if time.monotonic() >= until:
                raise AssertionError("child was not adopted")
            await asyncio.sleep(0.01)
        registered = None
        if len(sys.argv) > 1:
            registered = await engine._spawn(
                sys.executable, "-c", "import time; time.sleep(0.2); raise SystemExit(7)")
        execution = Execution(uuid4(), uuid4(), "handle", time.monotonic() + 5, root)
        stage = Stage("stage", "run", group, root / "work", root / "reason", process=direct)
        execution.stage = stage
        try:
            await engine._cleanup_stage(execution, stage)
            if registered is not None:
                assert await engine._wait_process(registered) == 7, "asyncio child watcher lost exit status"
            assert not children.read_text().strip(), "cleanup acknowledged while adopted zombie remains"
        finally:
            until = time.monotonic() + 2
            while children.read_text().strip() and time.monotonic() < until:
                for pid in children.read_text().split():
                    try:
                        os.waitpid(int(pid), os.WNOHANG)
                    except ChildProcessError:
                        pass
                await asyncio.sleep(0.01)

asyncio.run(main())
'''


@pytest.mark.skipif(sys.platform != "linux", reason="Linux subreaper semantics")
def test_cleanup_reaps_children_already_absent_from_cgroup():
    result = subprocess.run([sys.executable, "-c", SCENARIO], check=False, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode(errors="replace")


@pytest.mark.skipif(sys.platform != "linux", reason="Linux subreaper semantics")
def test_orphan_reaping_preserves_asyncio_owned_subprocess_exit_status():
    result = subprocess.run([sys.executable, "-c", SCENARIO, "registered"], check=False, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
