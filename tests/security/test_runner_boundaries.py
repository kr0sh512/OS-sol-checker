import concurrent.futures
import json
import os

import pytest

from os_sol_checker.config import Settings
from os_sol_checker.domain.models import Limits
from os_sol_checker.runner.cgroups import Cgroups, stop_reason
from os_sol_checker.runner.executor import RunnerExecutor
from os_sol_checker.runner.manifest import NSJAIL_COMMIT, fingerprint, manifest_entries, verified_policy


def test_runner_artifact_exact_limit_and_binary_copy(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    data = bytes(range(256)) * 4096
    (work / "main").write_bytes(data)
    RunnerExecutor._read_artifact(work, tmp_path / "private", len(data))
    assert (tmp_path / "private").read_bytes() == data
    assert (tmp_path / "private").stat().st_mode & 0o777 == 0o500


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory", "oversized", "empty"])
def test_runner_artifact_rejects_unsafe_inode_without_following_or_blocking(tmp_path, kind):
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"private")
    path = work / "main"
    if kind == "symlink":
        path.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, path)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    else:
        path.write_bytes(b"" if kind == "empty" else b"x" * 33)
    with pytest.raises((OSError, ValueError)):
        RunnerExecutor._read_artifact(work, tmp_path / "private", 32)
    assert not (tmp_path / "private").exists()
    assert outside.read_bytes() == b"private"


def runtime_settings(tmp_path):
    rootfs = tmp_path / "rootfs"
    profiles = tmp_path / "profiles"
    (rootfs / "compile").mkdir(parents=True)
    (rootfs / "run").mkdir()
    profiles.mkdir()
    (rootfs / "compile" / "cc").write_bytes(b"compiler bytes")
    (profiles / "run.cfg").write_bytes(b"clone_newnet: true")
    binary = tmp_path / "nsjail"
    binary.write_bytes(b"installed nsjail bytes")
    settings = Settings(runner_token="t" * 32, runner_image="registry/runner@sha256:" + "a" * 64,
                        jail_rootfs_root=rootfs, jail_profiles_root=profiles, nsjail_binary=str(binary),
                        runner_manifest=tmp_path / "manifest.json")
    entries = manifest_entries(settings)
    settings.runner_manifest.write_text(json.dumps({"version": 1, "nsjail_commit": NSJAIL_COMMIT,
                                                   "fingerprint": fingerprint(entries), "entries": entries}))
    return settings


def test_runner_manifest_recomputes_installed_content_not_claimed_hash(tmp_path):
    settings = runtime_settings(tmp_path)
    first = verified_policy(settings)
    assert first.runner_image_digest == "a" * 64
    (settings.jail_rootfs_root / "compile/cc").write_bytes(b"changed compiler")
    with pytest.raises(ValueError, match="differs"):
        verified_policy(settings)


def test_runner_manifest_rejects_extra_files_and_special_types(tmp_path):
    settings = runtime_settings(tmp_path)
    extra = settings.jail_profiles_root / "injected.cfg"
    extra.write_text("keep_caps: true")
    with pytest.raises(ValueError, match="differs"):
        verified_policy(settings)
    extra.unlink()
    os.mkfifo(extra)
    with pytest.raises(ValueError, match="Special file"):
        verified_policy(settings)


def test_runner_stop_reason_first_writer_is_atomic_across_contenders(tmp_path):
    target = tmp_path / "reason"
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda i: stop_reason(target, f"reason-{i}"), range(32)))
    assert len(set(values)) == 1
    assert target.read_text() == values[0]
    assert not list(tmp_path.glob(".stop-*"))


def test_runner_refuses_unbounded_delegated_aggregate_before_enabling(tmp_path):
    values = {"cgroup.controllers": "cpu memory pids", "cgroup.procs": "", "memory.max": "max"}
    for name, value in values.items():
        (tmp_path / name).write_text(value)
    with pytest.raises(ValueError, match="finite aggregate"):
        Cgroups(tmp_path).prepare(Limits(), 4)
    assert not (tmp_path / "cgroup.subtree_control").exists()


def test_runner_fake_control_files_are_not_a_cgroup(tmp_path):
    values = {"cgroup.controllers": "cpu memory pids", "cgroup.procs": "",
              "memory.max": str(4 * 256 * 1048576), "memory.swap.max": "0",
              "pids.max": "256", "cpu.max": "400000 100000"}
    for name, value in values.items():
        (tmp_path / name).write_text(value)
    with pytest.raises(ValueError, match="actual isolated cgroup2 mount"):
        Cgroups(tmp_path).prepare(Limits(), 4)
    assert not (tmp_path / "cgroup.subtree_control").exists()


async def test_runner_missing_supervisor_fails_closed_before_any_launch(tmp_path, monkeypatch):
    monkeypatch.delenv("CHECKER_SUPERVISED", raising=False)
    settings = Settings(runner_token="t" * 32, runner_image="r@sha256:" + "a" * 64,
                        runner_scratch=tmp_path / "scratch", nsjail_binary="/absent/nsjail")
    engine = RunnerExecutor(settings)
    await engine.start()
    assert not engine.ready
    assert engine.policy is None
    assert engine.executions == {}


def test_runner_stage_root_cannot_be_a_symlink_to_an_outside_tree(tmp_path):
    settings = runtime_settings(tmp_path)
    (settings.jail_rootfs_root / "compile/cc").unlink()
    (settings.jail_rootfs_root / "compile").rmdir()
    outside = tmp_path / "outside-root"
    outside.mkdir()
    (settings.jail_rootfs_root / "compile").symlink_to(outside)
    with pytest.raises(ValueError, match="actual immutable directories"):
        verified_policy(settings)
