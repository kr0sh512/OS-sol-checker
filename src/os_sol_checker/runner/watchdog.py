from __future__ import annotations

import argparse
import json
import math
import os
import re
import time
from pathlib import Path

from .cgroups import counters, stop_reason


def watch(scratch: Path, cgroup_root: Path, heartbeat_fd: int, slots: int) -> None:
    control = scratch / "control"
    last_heartbeat = 0.0
    while True:
        now = time.monotonic()
        files = list(control.glob("*.json"))
        if len(files) > slots:
            raise RuntimeError("Watchdog registry exceeds reservation limit")
        for entry in files:
            if not re.fullmatch(r"[0-9a-f]{32}\.json", entry.name):
                raise RuntimeError("Invalid watchdog registry entry")
            try:
                data = json.loads(entry.read_bytes())
                deadline = data["deadline"]
                if not isinstance(deadline, (float, int)) or not math.isfinite(deadline):
                    raise RuntimeError("Invalid watchdog deadline")
                execution = scratch / "executions" / entry.stem
                if now >= deadline:
                    stop_reason(execution / "expired", "submission_timeout")
                stage = data.get("stage")
                if stage is None:
                    continue
                stage_id = stage["id"]
                if not re.fullmatch(r"[0-9a-f]{32}", stage_id):
                    raise RuntimeError("Invalid watchdog stage identity")
                cgroup = cgroup_root / ("stage-" + stage_id)
                reason = execution / (stage_id + ".reason")
                if now >= deadline:
                    stop_reason(reason, "submission_timeout")
                elif stage.get("deadline") is not None and now >= stage["deadline"]:
                    stop_reason(reason, stage["kind"] + "_timeout")
                elif counters(cgroup / "memory.events").get("oom_kill", 0):
                    stop_reason(reason, "memory_limit")
                if reason.exists():
                    (cgroup / "cgroup.kill").write_text("1")
            except FileNotFoundError:
                # API unpublishes registry entries before removing stage directories.
                continue
        if now - last_heartbeat >= 0.25:
            os.write(heartbeat_fd, b"W")
            last_heartbeat = now
        time.sleep(0.01)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--cgroup-root", type=Path, required=True)
    parser.add_argument("--heartbeat-fd", type=int, required=True)
    parser.add_argument("--slots", type=int, required=True)
    args = parser.parse_args()
    watch(args.scratch, args.cgroup_root, args.heartbeat_fd, args.slots)


if __name__ == "__main__":
    main()
