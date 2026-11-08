"""Trusted launcher: enter the limited cgroup before nsjail can create any children."""
from __future__ import annotations

import os
import resource
import sys
from pathlib import Path


def main() -> None:
    cgroup, maximum_file, binary, *arguments = sys.argv[1:]
    # The controller is the only caller; no request-controlled path or argv is accepted.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (int(maximum_file), int(maximum_file)))
    inherited_hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    descriptors = 128 if inherited_hard == resource.RLIM_INFINITY else min(128, inherited_hard)
    resource.setrlimit(resource.RLIMIT_NOFILE, (descriptors, descriptors))
    Path(cgroup, "cgroup.procs").write_text(str(os.getpid()))
    if str(os.getpid()) not in Path(cgroup, "cgroup.procs").read_text().split():
        raise RuntimeError("Kernel did not place stage PID in its expected delegated leaf")
    os.execve(binary, [binary, *arguments], {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"})


if __name__ == "__main__":
    main()
