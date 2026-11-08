import json
import os
import subprocess
import time
from hashlib import sha1
from pathlib import Path

import pytest

from os_sol_checker.adapters.catalog.fingerprint import TestsFingerprint
from os_sol_checker.adapters.catalog.git import (
    MAX_GIT_DISK_BYTES,
    MAX_GIT_FILE_BYTES,
    GitCatalog,
    _bounded_read,
    _discard_abandoned_imports,
    _Git,
    _ObjectReader,
    _read_tree,
    _snapshot_identity,
    _source_url,
)
from os_sol_checker.adapters.catalog.parser import parse_catalog
from os_sol_checker.adapters.catalog.tree import CatalogError, TreeEntry
from os_sol_checker.config import Settings
from os_sol_checker.domain.models import CatalogSnapshot


def test_git_preserves_only_operator_https_proxy_from_service_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://catalog-proxy:3128")
    monkeypatch.setenv("RUNNER_TOKEN", "not-a-real-token")
    monkeypatch.setenv("TRANSIENT_DATABASE_URL", "must-not-reach-git")
    git = _Git(tmp_path, time.monotonic() + 10)
    assert git.environment["HTTPS_PROXY"] == "http://catalog-proxy:3128"
    assert "RUNNER_TOKEN" not in git.environment
    assert "TRANSIENT_DATABASE_URL" not in git.environment


@pytest.fixture
def object_repository(tmp_path):
    """Local Git is only an object-parser fixture, never a production fetch source."""
    repository = tmp_path / "objects.git"
    environment = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "init", "--bare", "--template=",
                    "--object-format=sha1", str(repository)], env=environment, check=True, capture_output=True)

    def object_id(kind: str, data: bytes) -> str:
        result = subprocess.run(["git", "--git-dir", str(repository), "hash-object", "--literally",
                                 "-t", kind, "-w", "--stdin"], input=data, env=environment,
                                check=True, capture_output=True)
        return result.stdout.decode().strip()

    def commit(raw_tree: bytes) -> str:
        tree = object_id("tree", raw_tree)
        return object_id("commit", (f"tree {tree}\nauthor Fixture <fixture@example.invalid> 0 +0000\n"
                                     "committer Fixture <fixture@example.invalid> 0 +0000\n\nFixture\n").encode())
    return repository, object_id, commit


def read_entries(repository: Path, commit: str):
    git = _Git(repository, time.monotonic() + 10)
    reader = _ObjectReader(git)
    try:
        entries = _read_tree(reader, commit)
        reader.finish()
        return entries
    finally:
        reader.close()


def raw_entry(mode: bytes, name: bytes, object_id: str) -> bytes:
    return mode + b" " + name + b"\x00" + bytes.fromhex(object_id)


def test_real_binary_git_tree_empty_directories_unicode_and_binary_blob(object_repository):
    repository, object_id, commit = object_repository
    empty_tree = object_id("tree", b"")
    binary = object_id("blob", b"\x00\xff\r\n")
    root = raw_entry(b"40000", b"tests", empty_tree) + raw_entry(b"100755", "имя\nс пробелом".encode(), binary)
    entries = read_entries(repository, commit(root))
    assert TreeEntry("tests", "040000") in entries
    assert TreeEntry("имя\nс пробелом", "100755", b"\x00\xff\r\n") in entries
    assert TestsFingerprint.calculate(entries) != TestsFingerprint.calculate([])


@pytest.mark.parametrize("mode", [b"120000", b"160000"])
def test_real_git_symlink_and_submodule_reject_snapshot(object_repository, mode):
    repository, object_id, commit = object_repository
    blob = object_id("blob", b"/etc/passwd")
    with pytest.raises(CatalogError):
        read_entries(repository, commit(raw_entry(mode, b"unsafe", blob)))


@pytest.mark.parametrize("name", [b"..", b"/absolute", b"a/b", b"a\\b", b"\xff"])
def test_real_git_malicious_names_are_rejected(object_repository, name):
    repository, object_id, commit = object_repository
    blob = object_id("blob", b"")
    with pytest.raises(CatalogError):
        read_entries(repository, commit(raw_entry(b"100644", name, blob)))


def test_real_git_duplicate_tree_names_rejected(object_repository):
    repository, object_id, commit = object_repository
    blob = object_id("blob", b"")
    duplicate = raw_entry(b"100644", b"duplicate", blob) * 2
    with pytest.raises(CatalogError):
        read_entries(repository, commit(duplicate))


def test_cat_file_checks_actual_type_instead_of_treating_a_tree_as_blob(object_repository):
    repository, object_id, commit = object_repository
    tree = object_id("tree", b"")
    with pytest.raises(CatalogError, match="header or type"):
        read_entries(repository, commit(raw_entry(b"100644", b"not-a-blob", tree)))


def test_cat_file_enforces_declared_bound_before_reading_blob(object_repository, monkeypatch):
    import os_sol_checker.adapters.catalog.git as adapter
    repository, object_id, commit = object_repository
    blob = object_id("blob", b"12345")
    monkeypatch.setattr(adapter, "MAX_BLOB_BYTES", 4)
    with pytest.raises(CatalogError, match="size limit"):
        read_entries(repository, commit(raw_entry(b"100644", b"large", blob)))
    blob = object_id("blob", b"1234")
    assert read_entries(repository, commit(raw_entry(b"100644", b"boundary", blob)))[0].data == b"1234"


@pytest.mark.parametrize("source", ["file:///tmp/repo", "/tmp/repo", "ssh://example/repo", "git://example/repo",
                                     "ext::sh -c id", "http://example/repo", "https://user:pass@example/repo",
                                     "https://example/repo#fragment", "https://example/repo\n", "https://[bad/repo"])
def test_production_sources_are_https_only(source):
    with pytest.raises(CatalogError):
        _source_url(source)


def test_git_environment_and_configuration_do_not_inherit_executable_controls(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_SSH_COMMAND", "attacker")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/attacker")
    git = _Git(tmp_path, time.monotonic() + 10)
    assert "GIT_SSH_COMMAND" not in git.environment and "GIT_CONFIG_COUNT" not in git.environment
    assert git.environment["GIT_ALLOW_PROTOCOL"] == "https"
    assert "core.hooksPath=/dev/null" in git.prefix
    assert "credential.helper=" in git.prefix
    assert "protocol.allow=never" in git.prefix


def test_git_deadline_and_live_disk_guard_fail_closed(tmp_path, monkeypatch):
    import os_sol_checker.adapters.catalog.git as adapter
    git = _Git(tmp_path, time.monotonic() - 1)
    with pytest.raises(CatalogError, match="timed out"):
        git.guard(time.monotonic() + 1)
    git.deadline = time.monotonic() + 10
    monkeypatch.setattr(adapter, "_disk_usage", lambda root: MAX_GIT_DISK_BYTES + 1)
    with pytest.raises(CatalogError, match="during fetch"):
        git.guard(git.deadline, monitor_disk=True)
    assert MAX_GIT_DISK_BYTES == 128 * 1024 * 1024
    assert MAX_GIT_FILE_BYTES == 64 * 1024 * 1024


@pytest.fixture
def materialized(tmp_path):
    catalog = GitCatalog(Settings(catalog_root=tmp_path / "catalog"))
    snapshots = catalog.root / "snapshots"
    snapshots.mkdir(parents=True)
    entries = [TreeEntry("topic", "040000"), TreeEntry("topic/task", "040000"),
               TreeEntry("topic/task/task.md", "100644", b"Description"),
               TreeEntry("topic/task/tests", "040000"),
               TreeEntry("topic/task/tests/1.in", "100644"),
               TreeEntry("topic/task/tests/1.out", "100644", b"\xff"),
               TreeEntry("snapshot.json", "100644", b"upstream cannot overwrite metadata")]
    commit = sha1(b"fixture commit").hexdigest()
    snapshot_id = _snapshot_identity(commit)
    destination = snapshots / snapshot_id
    snapshot = CatalogSnapshot(snapshot_id=snapshot_id, commit=commit,
                               tests_digest=TestsFingerprint.calculate(entries), root_path=str(destination),
                               topics=parse_catalog(entries))
    catalog._materialize(tmp_path / "stage", destination, entries, snapshot)
    yield catalog, snapshot
    # Restore private fixture write permissions so pytest can remove its own files.
    for directory, _, _ in os.walk(catalog.root):
        Path(directory).chmod(0o755)


def test_metadata_is_outside_task_tree_and_load_preserves_original_bytes(materialized):
    catalog, snapshot = materialized
    assert catalog.load_snapshot(snapshot.snapshot_id) == snapshot
    assert (Path(snapshot.root_path) / "tree" / "snapshot.json").read_bytes().startswith(b"upstream")
    assert json.loads((Path(snapshot.root_path) / "snapshot.json").read_bytes())["snapshot"]["commit"] == snapshot.commit
    assert catalog.load_test(snapshot, snapshot.topics[0].tasks[0], 0) == (b"", b"\xff")
    assert catalog.load_test(snapshot.model_copy(update={"generation": 7}), snapshot.topics[0].tasks[0], 0) == (b"", b"\xff")


def test_untrusted_ids_tasks_indices_and_snapshot_context_cannot_select_paths(materialized):
    catalog, snapshot = materialized
    with pytest.raises(CatalogError):
        catalog.load_snapshot("../outside")
    task = snapshot.topics[0].tasks[0]
    for index in (-1, 1, True):
        with pytest.raises(CatalogError):
            catalog.load_test(snapshot, task, index)
    with pytest.raises(CatalogError):
        catalog.load_test(snapshot.model_copy(update={"commit": "0" * 40}), task, 0)
    forged = task.model_copy(deep=True)
    forged.tests[0].input_path = "../../outside"
    with pytest.raises(CatalogError):
        catalog.load_test(snapshot, forged, 0)


def test_snapshot_reader_never_follows_file_or_directory_symlinks(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"secret")
    (root / "file").symlink_to(outside / "secret")
    (root / "directory").symlink_to(outside, target_is_directory=True)
    for path in ("file", "directory/secret"):
        with pytest.raises(OSError):
            _bounded_read(root, path, 100)
    (root / "empty").write_bytes(b"")
    assert _bounded_read(root, "empty", 0) == b""
    (root / "oversized").write_bytes(b"12345")
    with pytest.raises(CatalogError):
        _bounded_read(root, "oversized", 4)


def test_abandoned_import_cleanup_keeps_published_snapshots_and_unrelated_names(tmp_path):
    abandoned = tmp_path / ".fetch-12345678"
    nested = abandoned / "snapshot" / "tree"
    nested.mkdir(parents=True)
    (nested / "readonly").write_bytes(b"data")
    for directory in (nested, nested.parent):
        directory.chmod(0o555)
    published = tmp_path / "snapshots" / "preserve"
    published.mkdir(parents=True)
    unrelated = tmp_path / ".fetch-not-our-private-temporary-name"
    unrelated.mkdir()
    _discard_abandoned_imports(tmp_path)
    assert not abandoned.exists()
    assert published.is_dir() and unrelated.is_dir()
