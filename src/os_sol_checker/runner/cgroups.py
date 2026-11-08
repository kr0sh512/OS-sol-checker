from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import time
from uuid import uuid4

from ..domain.models import Limits


def counters(path: Path) -> dict[str, int]:
    return {key: int(value) for key, value in (line.split() for line in path.read_text().splitlines())}


def stop_reason(path: Path, reason: str) -> str:
    """First trusted stop event wins, across API and independent watchdog processes."""
    temporary = path.with_name(".stop-" + uuid4().hex)
    try:
        temporary.write_text(reason)
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        return path.read_text()
    finally:
        temporary.unlink(missing_ok=True)


class Cgroups:
    def __init__(self, root: Path):
        self.root = root

    def prepare(self, limits: Limits, slots: int) -> None:
        if self.root == Path("/sys/fs/cgroup") or self.root.is_symlink():
            raise ValueError("Only an explicitly delegated cgroup subtree is allowed")
        required = {"cpu", "memory", "pids"}
        if not required <= set((self.root / "cgroup.controllers").read_text().split()):
            raise ValueError("Delegated memory/pids/cpu controllers are missing")
        if (self.root / "cgroup.procs").read_text().strip():
            raise ValueError("Delegated stage root must have no internal processes")
        expected = {"memory.max": str(limits.memory_mib * 1048576 * slots),
                    "memory.swap.max": "0", "pids.max": str(limits.pids * slots),
                    "cpu.max": f"{slots * 100000} 100000"}
        for name, value in expected.items():
            if (self.root / name).read_text().strip() != value:
                raise ValueError("Delegated subtree lacks the configured finite aggregate limits")
        if not any(line.split()[4] == str(self.root) and line.split(" - ")[1].split()[0] == "cgroup2"
                   for line in Path("/proc/self/mountinfo").read_text().splitlines()):
            raise ValueError("Delegated root must be an actual isolated cgroup2 mount")
        (self.root / "cgroup.subtree_control").write_text("+cpu +memory +pids")
        if not required <= set((self.root / "cgroup.subtree_control").read_text().split()):
            raise ValueError("Delegated controllers could not be enabled")
        leftovers = [p for p in self.root.iterdir() if p.is_dir()]
        for path in leftovers:
            if not re.fullmatch(r"stage-[0-9a-f]{32}", path.name):
                raise ValueError("Unexpected child in delegated cgroup root")
            # Never admit requests while an old execution may still exist.
            (path / "cgroup.kill").write_text("1")
            if counters(path / "cgroup.events").get("populated") != 0:
                raise ValueError("Previous stage has not terminated")
            path.rmdir()

    def create(self, stage_id: str, limits: Limits) -> Path:
        path = self.root / ("stage-" + stage_id)
        path.mkdir(mode=0o700)
        try:
            values = {"memory.max": str(limits.memory_mib * 1048576), "memory.swap.max": "0",
                      "memory.oom.group": "1", "pids.max": str(limits.pids), "cpu.max": "100000 100000"}
            for name, value in values.items():
                (path / name).write_text(value)
                if (path / name).read_text().strip() != value:
                    raise ValueError("Cgroup limit did not take effect")
            if not (path / "cgroup.kill").exists():
                raise ValueError("cgroup.kill is required")
            return path
        except BaseException:
            path.rmdir()
            raise

    async def kill(self, path: Path, timeout: float = 3) -> set[int]:
        pids = {int(value) for value in (path / "cgroup.procs").read_text().split()}
        (path / "cgroup.kill").write_text("1")
        end = time.monotonic() + timeout
        while counters(path / "cgroup.events").get("populated") != 0:
            if time.monotonic() >= end:
                raise RuntimeError("Stage cgroup remains populated after kill")
            await asyncio.sleep(0.01)
        return pids
