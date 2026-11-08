from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from ..config import Settings
from ..domain.models import JudgePolicy
from ..domain.policies import flags_digest

NSJAIL_COMMIT = "079d70dda4aa1edd9512cfd25ff1e47e316dc355"


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def manifest_entries(settings: Settings) -> list[dict]:
    for directory in (settings.jail_rootfs_root, settings.jail_profiles_root,
                      settings.jail_rootfs_root / "compile", settings.jail_rootfs_root / "run"):
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError("Jail roots and profile roots must be actual immutable directories")
    entries = []
    roots = {"rootfs": settings.jail_rootfs_root, "profiles": settings.jail_profiles_root,
             "nsjail": Path(settings.nsjail_binary)}
    for prefix, root in sorted(roots.items()):
        paths = [root]
        if root.is_dir():
            paths.extend(sorted(root.rglob("*")))
        for path in paths:
            info = path.lstat()
            name = prefix if path == root else prefix + "/" + path.relative_to(root).as_posix()
            mode = stat.S_IMODE(info.st_mode)
            row = {"path": name, "mode": mode, "uid": info.st_uid, "gid": info.st_gid}
            if mode & 0o6000 or (not stat.S_ISLNK(info.st_mode) and mode & 0o022):
                raise ValueError("Unsafe immutable runtime permissions")
            if stat.S_ISREG(info.st_mode):
                row.update(type="file", size=info.st_size, sha256=_file_hash(path))
            elif stat.S_ISDIR(info.st_mode):
                row.update(type="directory")
            elif stat.S_ISLNK(info.st_mode):
                target = os.readlink(path)
                # A rootfs symlink may be absolute inside that root, never point outside it.
                if prefix != "rootfs":
                    raise ValueError("Symlink outside the isolated rootfs")
                jail = root / path.relative_to(root).parts[0]
                resolved = (jail / target.lstrip("/") if target.startswith("/") else path.parent / target).resolve()
                if not resolved.is_relative_to(jail.resolve()):
                    raise ValueError("Escaping rootfs symlink")
                row.update(type="symlink", target=target)
            else:
                raise ValueError("Special file in immutable runtime")
            entries.append(row)
    return entries


def fingerprint(entries: list[dict]) -> str:
    return hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def verified_policy(settings: Settings) -> JudgePolicy:
    settings.require("runner")
    match = re.fullmatch(r"[^\s@]+@sha256:([0-9a-f]{64})", settings.runner_image)
    if match is None:
        raise ValueError("RUNNER_IMAGE must contain an immutable sha256 digest")
    if settings.runner_manifest.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("Runtime manifest is oversized")
    document = json.loads(settings.runner_manifest.read_bytes())
    entries = manifest_entries(settings)
    digest = fingerprint(entries)
    if document != {"version": 1, "nsjail_commit": NSJAIL_COMMIT, "fingerprint": digest, "entries": entries}:
        raise ValueError("Runtime manifest differs from installed profiles, rootfs or nsjail")
    return JudgePolicy(runner_image_digest=match.group(1), sandbox_profile_digest=digest,
                       flags_digest=flags_digest(), limits=settings.limits())
