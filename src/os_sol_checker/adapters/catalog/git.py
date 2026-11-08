"""HTTPS-only Git ingestion. Git objects are data, never a checkout or executable."""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from hashlib import sha1, sha256
from pathlib import Path
from urllib.parse import urlsplit

from psycopg import Error as DatabaseError

from ...config import Settings
from ...domain.errors import CheckerError
from ...domain.models import CatalogSnapshot, Task
from .fingerprint import TestsFingerprint
from .parser import PARSER_VERSION, REPO_ID, SORT_UNICODE_VERSION, parse_catalog
from .tree import (
    MAX_BLOB_BYTES,
    MAX_DEPTH,
    MAX_ENTRIES,
    MAX_EXPECTED_OUTPUT_BYTES,
    MAX_MARKDOWN_BYTES,
    MAX_TEST_STDIN_BYTES,
    MAX_TOTAL_BYTES,
    MAX_TREE_BYTES,
    OBJECT_ID,
    CatalogError,
    TreeEntry,
    validate_path,
    validate_tree,
)

FETCH_TIMEOUT_SECONDS = 60
COMMAND_TIMEOUT_SECONDS = 10
IMPORT_TIMEOUT_SECONDS = 120
MAX_GIT_DISK_BYTES = 128 * 1024 * 1024
MAX_GIT_FILE_BYTES = 64 * 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_METADATA_BYTES = 128 * 1024 * 1024
SNAPSHOT_FILENAME = "snapshot.json"
SNAPSHOT_FORMAT = 1
_SNAPSHOT_ID = re.compile(r"[0-9a-f]{64}\Z")
_LOG = logging.getLogger(__name__)


def _source_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise CatalogError("Invalid configured Git source URL") from exc
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or "\\" in value or any(ord(char) <= 32 or ord(char) == 127 for char in value)
            or (port is not None and not 1 <= port <= 65535)):
        raise CatalogError("Configured Git source must be an unauthenticated HTTPS URL")
    return value


def _snapshot_identity(commit: str, unicode_version: str = SORT_UNICODE_VERSION) -> str:
    return sha256(f"{REPO_ID}\x00{commit}\x00{PARSER_VERSION}\x00{unicode_version}".encode()).hexdigest()


def _disk_usage(root: Path) -> int:
    total = 0
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            try:
                info = (Path(directory) / name).lstat()
            except FileNotFoundError:
                continue  # Git atomically renames temporary object files.
            if stat.S_ISLNK(info.st_mode):
                raise CatalogError("Unexpected symlink in service Git storage")
            total += max(info.st_size, info.st_blocks * 512)
    return total


class _Git:
    def __init__(self, repository: Path, deadline: float):
        self.repository = repository
        self.deadline = deadline
        binary = shutil.which("git", path="/usr/bin:/bin")
        if binary is None:
            raise CatalogError("Git executable is required")
        self.prefix = [sys.executable, "-I", str(Path(__file__).with_name("_git_exec.py")), binary,
                       "-c", "core.hooksPath=/dev/null", "-c", "init.templateDir=/dev/null",
                       "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
                       "-c", "credential.helper=", "-c", "core.askPass=/bin/false",
                       "-c", "submodule.recurse=false", "-c", "gc.auto=0",
                       "-c", "maintenance.auto=false", "-c", "fetch.writeCommitGraph=false",
                       "-c", "fetch.fsckObjects=true", "-c", "transfer.fsckObjects=true",
                       "-c", "http.sslVerify=true", "-c", "http.followRedirects=false",
                       "-c", "http.lowSpeedLimit=1024", "-c", "http.lowSpeedTime=20"]
        self.environment = {"PATH": "/usr/bin:/bin", "HOME": str(repository.parent / "home"),
                            "XDG_CONFIG_HOME": str(repository.parent / "home"),
                            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": "/dev/null",
                            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
                            "GIT_ASKPASS": "/bin/false", "GIT_ALLOW_PROTOCOL": "https",
                            "GIT_NO_REPLACE_OBJECTS": "1", "GIT_ATTR_NOSYSTEM": "1"}
        # The deployment may make the catalog network internal and permit GitHub
        # only through an allowlisted HTTPS CONNECT proxy. Preserve just these
        # operator-owned proxy settings, never the full service environment.
        for name in ("HTTPS_PROXY", "https_proxy"):
            if os.environ.get(name):
                self.environment[name] = os.environ[name]

    def start(self, arguments: list[str]) -> subprocess.Popen:
        return subprocess.Popen(self.prefix + arguments, cwd=self.repository,
                                env=self.environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True, bufsize=0)

    @staticmethod
    def stop(process: subprocess.Popen) -> None:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    def guard(self, deadline: float, monitor_disk: bool = False) -> None:
        if time.monotonic() >= min(deadline, self.deadline):
            raise CatalogError("Git operation timed out")
        if monitor_disk:
            if _disk_usage(self.repository) > MAX_GIT_DISK_BYTES:
                raise CatalogError("Git service storage limit exceeded during fetch")
            if shutil.disk_usage(self.repository).free < 2 * MAX_GIT_FILE_BYTES:
                raise CatalogError("Insufficient free space for bounded Git fetch")

    def run(self, arguments: list[str], *, timeout: float = COMMAND_TIMEOUT_SECONDS,
            monitor_disk: bool = False) -> bytes:
        deadline = min(time.monotonic() + timeout, self.deadline)
        self.guard(deadline, monitor_disk)
        process = self.start(arguments)
        chunks: list[bytes] = []
        output_size = 0
        error_size = 0
        try:
            process.stdin.close()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                while selector.get_map():
                    self.guard(deadline, monitor_disk)
                    for key, _ in selector.select(0.025):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        elif key.data == "stdout":
                            output_size += len(chunk)
                            if output_size > MAX_COMMAND_OUTPUT_BYTES:
                                raise CatalogError("Git command output limit exceeded")
                            chunks.append(chunk)
                        else:
                            error_size += len(chunk)
                            if error_size > MAX_COMMAND_OUTPUT_BYTES:
                                raise CatalogError("Git command diagnostic limit exceeded")
            self.guard(deadline, monitor_disk)
            if process.wait(timeout=max(0.001, min(deadline, self.deadline) - time.monotonic())) != 0:
                raise CatalogError("Git command failed: " + arguments[0])
            return b"".join(chunks)
        except subprocess.TimeoutExpired as exc:
            raise CatalogError("Git operation timed out") from exc
        finally:
            self.stop(process)


class _ObjectReader:
    """Bounded cat-file --batch framing, type checks and cryptographic object checks."""
    def __init__(self, git: _Git):
        self.git = git
        self.process = git.start(["cat-file", "--batch"])
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ, "stdout")
        self.selector.register(self.process.stderr, selectors.EVENT_READ, "stderr")
        self.errors = 0
        self.total = 0
        self.deadline = 0.0

    def close(self) -> None:
        self.selector.close()
        self.git.stop(self.process)

    def finish(self) -> None:
        self.process.stdin.close()
        deadline = min(self.git.deadline, time.monotonic() + COMMAND_TIMEOUT_SECONDS)
        while self.selector.get_map():
            self.git.guard(deadline)
            for key, _ in self.selector.select(0.025):
                chunk = os.read(key.fileobj.fileno(), 4096)
                if not chunk:
                    self.selector.unregister(key.fileobj)
                elif key.data == "stdout":
                    raise CatalogError("Unexpected trailing cat-file output")
                else:
                    self.errors += len(chunk)
                    if self.errors > MAX_COMMAND_OUTPUT_BYTES:
                        raise CatalogError("cat-file diagnostic limit exceeded")
        try:
            code = self.process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise CatalogError("cat-file did not exit within its deadline") from exc
        if code != 0:
            raise CatalogError("cat-file exited unsuccessfully")

    def _read(self, size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            self.git.guard(self.deadline)
            for key, _ in self.selector.select(0.025):
                if key.data == "stderr":
                    chunk = os.read(key.fileobj.fileno(), 4096)
                    if not chunk:
                        self.selector.unregister(key.fileobj)
                    self.errors += len(chunk)
                    if self.errors > MAX_COMMAND_OUTPUT_BYTES:
                        raise CatalogError("cat-file diagnostic limit exceeded")
                    continue
                chunk = os.read(key.fileobj.fileno(), min(65536, size - len(result)))
                if not chunk:
                    raise CatalogError("Unexpected EOF in cat-file protocol")
                result.extend(chunk)
        return bytes(result)

    def get(self, object_id: str, kind: str) -> bytes:
        if not OBJECT_ID.fullmatch(object_id):
            raise CatalogError("Invalid Git object ID")
        self.deadline = min(self.git.deadline, time.monotonic() + COMMAND_TIMEOUT_SECONDS)
        try:
            self.process.stdin.write(object_id.encode("ascii") + b"\n")
        except (BrokenPipeError, OSError) as exc:
            raise CatalogError("cat-file request failed") from exc
        header = bytearray()
        while not header.endswith(b"\n"):
            if len(header) >= 128:
                raise CatalogError("Overlong cat-file header")
            header.extend(self._read(1))
        fields = bytes(header[:-1]).split(b" ")
        if (len(fields) != 3 or fields[0] != object_id.encode("ascii")
                or fields[1] != kind.encode("ascii") or not re.fullmatch(rb"[0-9]{1,12}", fields[2])):
            raise CatalogError("Invalid cat-file object header or type")
        size = int(fields[2])
        limit = MAX_TREE_BYTES if kind in {"tree", "commit"} else MAX_BLOB_BYTES
        if size > limit:
            raise CatalogError("Git object size limit exceeded")
        self.total += size
        if self.total > MAX_TOTAL_BYTES:
            raise CatalogError("Aggregate Git object read limit exceeded")
        data = self._read(size)
        if self._read(1) != b"\n":
            raise CatalogError("Invalid cat-file object terminator")
        digest = sha1(kind.encode("ascii") + b" " + str(size).encode("ascii") + b"\x00" + data).hexdigest()
        if digest != object_id:
            raise CatalogError("Git object digest mismatch")
        return data


def _read_tree(reader: _ObjectReader, commit: str) -> list[TreeEntry]:
    commit_data = reader.get(commit, "commit")
    match = re.match(rb"tree ([0-9a-f]{40})\n", commit_data)
    if match is None:
        raise CatalogError("Commit has no valid root tree")
    entries: list[TreeEntry] = []
    seen: set[str] = set()

    def visit(object_id: str, parent: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            raise CatalogError("Git tree depth limit exceeded")
        raw = reader.get(object_id, "tree")
        position = 0
        while position < len(raw):
            space = raw.find(b" ", position)
            nul = raw.find(b"\x00", space + 1) if space != -1 else -1
            if space == -1 or nul == -1 or nul + 21 > len(raw):
                raise CatalogError("Malformed binary Git tree")
            raw_mode = raw[position:space]
            name = raw[space + 1:nul]
            oid = raw[nul + 1:nul + 21].hex()
            position = nul + 21
            if b"/" in name:
                raise CatalogError("Git tree name contains a separator")
            try:
                component = name.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise CatalogError("Git tree name is not UTF-8") from exc
            path = f"{parent}/{component}" if parent else component
            validate_path(path)
            if path in seen:
                raise CatalogError("Duplicate Git tree path")
            seen.add(path)
            if len(seen) > MAX_ENTRIES:
                raise CatalogError("Git tree entry limit exceeded")
            if raw_mode == b"40000":
                entries.append(TreeEntry(path, "040000"))
                visit(oid, path, depth + 1)
            elif raw_mode in (b"100644", b"100755"):
                data = reader.get(oid, "blob")
                if component == "task.md" and len(data) > MAX_MARKDOWN_BYTES:
                    raise CatalogError("Markdown size limit exceeded")
                entries.append(TreeEntry(path, raw_mode.decode("ascii"), data))
            else:
                raise CatalogError("Symlink, submodule or unsupported Git mode")
    visit(match[1].decode("ascii"), "", 0)
    validate_tree(entries)
    return entries


@contextmanager
def _import_lock(root: Path):
    descriptor = os.open(root / ".import.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    deadline = time.monotonic() + IMPORT_TIMEOUT_SECONDS
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise CatalogError("Another catalog import did not finish within the lock deadline")
                time.sleep(0.05)
        yield
    finally:
        os.close(descriptor)


@contextmanager
def _publisher_lease(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(root / ".publisher.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CatalogError("Another catalog publisher already owns this catalog volume") from exc
        yield
    finally:
        os.close(descriptor)


def _discard_abandoned_imports(root: Path) -> None:
    # Called only while holding the cross-process import lock. Published snapshots
    # are deliberately never garbage-collected here: workers may still use them.
    def retry_readonly(function, path, exception):
        target = Path(path)
        target.parent.chmod(0o700)
        if target.is_dir() and not target.is_symlink():
            target.chmod(0o700)
        function(path)

    for entry in root.iterdir():
        if re.fullmatch(r"\.fetch-[a-z0-9_]{8}", entry.name):
            if entry.is_symlink() or not entry.is_dir():
                raise CatalogError("Unexpected abandoned import path type")
            shutil.rmtree(entry, onexc=retry_readonly)


def _bounded_read(root: Path, relative: str, limit: int) -> bytes:
    """Open every component with O_NOFOLLOW; do not trust serialized path strings."""
    validate_path(relative)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor = None
    try:
        parts = relative.split("/")
        for part in parts[:-1]:
            next_directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = next_directory
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise CatalogError("Snapshot file has an invalid type or exceeds its read limit")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, min(65536, limit + 1 - total)):
            total += len(chunk)
            if total > limit:
                raise CatalogError("Snapshot file grew past its read limit")
            chunks.append(chunk)
        if total != info.st_size:
            raise CatalogError("Snapshot file changed during read")
        return b"".join(chunks)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


class GitCatalog:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.catalog_root.absolute()
        self._lock = asyncio.Lock()
        self.last_attempt_commit: str | None = None

    async def import_snapshot(self, ref: str | None = None) -> CatalogSnapshot:
        async with self._lock:
            return await asyncio.to_thread(self._import, ref)

    def _import(self, ref: str | None) -> CatalogSnapshot:
        self.last_attempt_commit = None
        source = _source_url(self.settings.tasks_repository_url)
        selected_ref = ref if ref is not None else self.settings.tasks_repository_ref
        if not selected_ref or selected_ref.startswith("-") or len(selected_ref.encode()) > 1024:
            raise CatalogError("Invalid configured Git ref")
        self.root.mkdir(parents=True, exist_ok=True)
        with _import_lock(self.root):
            _discard_abandoned_imports(self.root)
            snapshots = self.root / "snapshots"
            snapshots.mkdir(exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".fetch-", dir=self.root) as temporary:
                temporary_root = Path(temporary)
                repository = temporary_root / "repository"
                repository.mkdir()
                (temporary_root / "home").mkdir()
                git = _Git(repository, time.monotonic() + IMPORT_TIMEOUT_SECONDS)
                git.run(["init", "--bare", "--object-format=sha1", "--template=", "."])
                git.run(["check-ref-format", "--allow-onelevel", selected_ref])
                git.run(["fetch", "--depth=1", "--no-tags", "--no-recurse-submodules",
                         "--no-auto-maintenance", "--force", "--", source, selected_ref],
                        timeout=FETCH_TIMEOUT_SECONDS, monitor_disk=True)
                commit = git.run(["rev-parse", "--verify", "FETCH_HEAD^{commit}"]).decode("ascii").strip()
                if not OBJECT_ID.fullmatch(commit):
                    raise CatalogError("Git fetch did not resolve to a full commit object ID")
                self.last_attempt_commit = commit
                snapshot_id = _snapshot_identity(commit)
                destination = snapshots / snapshot_id
                if destination.exists():
                    return self.load_snapshot(snapshot_id)
                reader = _ObjectReader(git)
                try:
                    entries = _read_tree(reader, commit)
                    reader.finish()
                finally:
                    reader.close()
                snapshot = CatalogSnapshot(snapshot_id=snapshot_id, commit=commit,
                                           tests_digest=TestsFingerprint.calculate(entries),
                                           root_path=str(destination), topics=parse_catalog(entries))
                self._materialize(temporary_root / "snapshot", destination, entries, snapshot,
                                  deadline=git.deadline)
                return snapshot

    def _materialize(self, staging: Path, destination: Path, entries: list[TreeEntry],
                     snapshot: CatalogSnapshot, *, deadline: float | None = None) -> None:
        staging.mkdir()
        (staging / "tree").mkdir()
        for entry in sorted(entries, key=lambda item: (item.path.count("/"), item.path.encode("utf-8"))):
            if deadline is not None and time.monotonic() >= deadline:
                raise CatalogError("Catalog materialization exceeded its import deadline")
            target = staging / "tree" / entry.path
            if entry.is_directory:
                target.mkdir()  # EEXIST also rejects filesystem case/normalization collisions.
            else:
                with target.open("xb") as stream:
                    stream.write(entry.data)
                    stream.flush()
                    os.fsync(stream.fileno())
                target.chmod(0o444)
        envelope = {"format": SNAPSHOT_FORMAT, "parser_version": PARSER_VERSION,
                    "unicode_version": SORT_UNICODE_VERSION, "snapshot": snapshot.model_dump(mode="json")}
        encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_METADATA_BYTES:
            raise CatalogError("Snapshot metadata size limit exceeded")
        metadata = staging / SNAPSHOT_FILENAME
        with metadata.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        metadata.chmod(0o444)
        # A published tree is append/overwrite protected; only the service owns its parent.
        for directory, _, _ in os.walk(staging, topdown=False):
            if Path(directory) != staging:
                Path(directory).chmod(0o555)
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        staging.rename(destination)
        # Moving across parents updates ".." and needs a writable directory.
        # No catalog pointer is published until this root is sealed and synced.
        destination.chmod(0o555)
        root_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
        descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def load_snapshot(self, snapshot_id: str) -> CatalogSnapshot:
        if not _SNAPSHOT_ID.fullmatch(snapshot_id):
            raise CatalogError("Invalid snapshot ID")
        root = self.root / "snapshots" / snapshot_id
        try:
            envelope = json.loads(_bounded_read(root, SNAPSHOT_FILENAME, MAX_METADATA_BYTES))
            if (envelope["format"] != SNAPSHOT_FORMAT or envelope["parser_version"] != PARSER_VERSION
                    or not isinstance(envelope["unicode_version"], str)):
                raise CatalogError("Unsupported snapshot format")
            snapshot = CatalogSnapshot.model_validate(envelope["snapshot"])
            if (snapshot.snapshot_id != snapshot_id or snapshot.root_path != str(root)
                    or not OBJECT_ID.fullmatch(snapshot.commit)
                    or _snapshot_identity(snapshot.commit, envelope["unicode_version"]) != snapshot_id
                    or not _SNAPSHOT_ID.fullmatch(snapshot.tests_digest) or snapshot.generation != 0):
                raise CatalogError("Snapshot metadata provenance does not match its immutable location")
            return snapshot
        except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
            raise CatalogError("Cannot load complete catalog snapshot") from exc

    def load_test(self, snapshot: CatalogSnapshot, task: Task, index: int) -> tuple[bytes, bytes]:
        stored = self.load_snapshot(snapshot.snapshot_id)
        actual = stored.task(task.id)
        if (snapshot.commit != stored.commit or snapshot.tests_digest != stored.tests_digest
                or snapshot.root_path != stored.root_path or actual is None
                or task != actual or not actual.availability or type(index) is not int
                or index < 0 or index >= len(actual.tests)):
            raise CatalogError("Test does not belong to an available immutable snapshot task")
        test = actual.tests[index]
        expected_prefix = f"tree/{actual.relative_path}/tests/"
        if not test.input_path.startswith(expected_prefix) or not test.expected_path.startswith(expected_prefix):
            raise CatalogError("Test path is outside its task")
        try:
            stdin = _bounded_read(Path(stored.root_path), test.input_path, MAX_TEST_STDIN_BYTES)
            expected = _bounded_read(Path(stored.root_path), test.expected_path, MAX_EXPECTED_OUTPUT_BYTES)
            return stdin, expected
        except OSError as exc:
            raise CatalogError("Cannot read immutable test data") from exc


async def _serve(settings: Settings, *, once: bool, ref: str | None) -> None:
    from ...application.catalog import CatalogSynchronizer
    from ..postgres.transient import TransientStore

    settings.require("catalog")
    # One publisher per shared volume, including the DB publication await. A second
    # daemon/--once must not publish an older import after a newer CLI process.
    with _publisher_lease(settings.catalog_root.absolute()):
        store = TransientStore(settings)
        await store.open()
        try:
            synchronizer = CatalogSynchronizer(GitCatalog(settings), store, settings.catalog_poll_seconds)
            if once:
                await synchronizer.sync(ref)
            else:
                await synchronizer.run(ref)
        finally:
            await store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Import and atomically publish the configured HTTPS task catalog")
    parser.add_argument("--once", action="store_true", help="publish one snapshot and exit nonzero on failure")
    parser.add_argument("--ref", help="override the configured Git branch or full commit ID")
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(_serve(Settings(), once=arguments.once, ref=arguments.ref))
    except KeyboardInterrupt:
        return
    except (OSError, ValueError, RuntimeError, CheckerError, DatabaseError):
        _LOG.exception("Catalog synchronization failed; the last published snapshot is unchanged")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
