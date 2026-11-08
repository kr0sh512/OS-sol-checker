"""Build-time only: copy the fixed Debian GCC/glibc allowlist, never a host root mount."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def copy_file(source: Path, root: Path) -> None:
    canonical = source.resolve(strict=True)
    destination = root / str(canonical).lstrip("/")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        shutil.copyfile(canonical, destination)
        destination.chmod(0o555 if os.access(canonical, os.X_OK) else 0o444)
    if source.is_symlink():
        alias = root / str(source).lstrip("/")
        alias.parent.mkdir(parents=True, exist_ok=True)
        if not alias.exists() and not alias.is_symlink():
            alias.symlink_to(os.path.relpath(destination, alias.parent))


def dependencies(binary: Path, root: Path) -> None:
    output = subprocess.check_output(["ldd", str(binary)], text=True)
    for line in output.splitlines():
        match = re.search(r"(?:=>\s+)?(/[^\s]+)", line)
        if match:
            copy_file(Path(match.group(1)), root)


def initialize(root: Path) -> None:
    for directory in ("usr/bin", "usr/lib", "usr/include", "lib64", "etc", "proc", "work"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    (root / "lib").symlink_to("usr/lib")
    (root / "bin").symlink_to("usr/bin")
    (root / "tmp").symlink_to("work/tmp")
    (root / "etc/passwd").write_text("checker:x:65534:65534:checker:/work:/nonexistent\n")
    (root / "etc/group").write_text("checker:x:65534:\n")
    (root / "etc/nsswitch.conf").write_text("passwd: files\ngroup: files\nhosts: files\n")
    (root / "etc/hosts").write_text("")
    for binary in ("checker-init", "checker-probe"):
        shutil.copyfile(Path("/build") / binary, root / binary)
        (root / binary).chmod(0o555)
        dependencies(Path("/build") / binary, root)
    for library in ("libc.so.6", "libm.so.6", "libmvec.so.1", "libgcc_s.so.1"):
        copy_file(Path("/lib/x86_64-linux-gnu") / library, root)
    copy_file(Path("/lib64/ld-linux-x86-64.so.2"), root)


def main() -> None:
    destination = Path(sys.argv[1])
    compile_root = destination / "compile"
    run_root = destination / "run"
    initialize(compile_root)
    initialize(run_root)
    for binary in ("/usr/bin/x86_64-linux-gnu-gcc-12", "/usr/bin/x86_64-linux-gnu-as",
                   "/usr/bin/x86_64-linux-gnu-ld.bfd", "/usr/lib/gcc/x86_64-linux-gnu/12/cc1",
                   "/usr/lib/gcc/x86_64-linux-gnu/12/collect2",
                   "/usr/lib/gcc/x86_64-linux-gnu/12/liblto_plugin.so",
                   "/usr/lib/gcc/x86_64-linux-gnu/12/lto-wrapper"):
        copy_file(Path(binary), compile_root)
        dependencies(Path(binary), compile_root)
    aliases = {"gcc": "x86_64-linux-gnu-gcc-12", "as": "x86_64-linux-gnu-as",
               "ld": "x86_64-linux-gnu-ld.bfd", "x86_64-linux-gnu-ld": "x86_64-linux-gnu-ld.bfd"}
    for alias, target in aliases.items():
        (compile_root / "usr/bin" / alias).symlink_to(target)
    packages = ("libc6-dev", "linux-libc-dev", "libgcc-12-dev", "libcrypt-dev")
    for package in packages:
        paths = subprocess.check_output(["dpkg-query", "-L", package], text=True).splitlines()
        for value in paths:
            path = Path(value)
            allowed = (value.startswith("/usr/include/") or
                       (value.startswith("/usr/lib/x86_64-linux-gnu/") and path.suffix in {".a", ".o", ".so"}) or
                       (value.startswith("/usr/lib/gcc/x86_64-linux-gnu/12/") and
                        ("/include/" in value or path.name.startswith("libgcc") or path.name.startswith("crt"))))
            if allowed and path.is_file():
                copy_file(path, compile_root)
    # No device nodes, setuid files, package manager, shell, Python, CA certificates or secrets.
    for root in (compile_root, run_root):
        for path in [root, *root.rglob("*")]:
            if not path.is_symlink():
                path.chmod(0o555 if path.is_dir() or os.access(path, os.X_OK) else 0o444)


if __name__ == "__main__":
    main()
