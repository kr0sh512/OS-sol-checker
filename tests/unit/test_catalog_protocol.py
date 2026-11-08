import subprocess
import sys
import time
from hashlib import sha1

import pytest

from os_sol_checker.adapters.catalog.git import _Git, _import_lock, _ObjectReader, _publisher_lease
from os_sol_checker.adapters.catalog.tree import CatalogError


class ProtocolGit(_Git):
    """A deliberately broken external protocol peer, only in parser tests."""
    def __init__(self, root, payload, exit_code=0):
        super().__init__(root, time.monotonic() + 5)
        self.payload = payload
        self.exit_code = exit_code

    def start(self, arguments):
        program = ("import sys; sys.stdin.buffer.readline(); "
                   "sys.stdout.buffer.write(bytes.fromhex(sys.argv[1])); sys.stdout.buffer.flush(); "
                   "raise SystemExit(int(sys.argv[2]))")
        return subprocess.Popen([sys.executable, "-I", "-c", program, self.payload.hex(), str(self.exit_code)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True, bufsize=0)


def blob_id(data):
    return sha1(b"blob " + str(len(data)).encode() + b"\x00" + data).hexdigest()


@pytest.mark.parametrize("payload", [b"", b"x" * 129, b"missing\n", b"0" * 40 + b" blob 0\n\n",
                                     b"{oid} tree 0\n\n", b"{oid} blob -1\n", b"{oid} blob 999999999999\n",
                                     b"{oid} blob 2\nx", b"{oid} blob 1\nx!", b"{oid} blob 1\ny\n"])
def test_protocol_never_treats_truncation_type_size_or_digest_errors_as_empty_file(tmp_path, payload):
    oid = blob_id(b"x")
    reader = _ObjectReader(ProtocolGit(tmp_path, payload.replace(b"{oid}", oid.encode())))
    try:
        with pytest.raises(CatalogError):
            reader.get(oid, "blob")
    finally:
        reader.close()


def test_protocol_zero_bytes_are_valid_only_with_complete_framing(tmp_path):
    oid = blob_id(b"")
    reader = _ObjectReader(ProtocolGit(tmp_path, oid.encode() + b" blob 0\n\n"))
    try:
        assert reader.get(oid, "blob") == b""
        reader.finish()
    finally:
        reader.close()


def test_protocol_child_error_and_unrequested_trailing_output_reject_import(tmp_path):
    oid = blob_id(b"")
    for suffix, code in [(b"", 1), (b"unexpected", 0)]:
        reader = _ObjectReader(ProtocolGit(tmp_path, oid.encode() + b" blob 0\n\n" + suffix, code))
        try:
            assert reader.get(oid, "blob") == b""
            with pytest.raises(CatalogError):
                reader.finish()
        finally:
            reader.close()


def test_import_lock_serializes_independent_instances(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    entered = threading.Event()
    release = threading.Event()
    order = []

    def first():
        with _import_lock(tmp_path):
            order.append("first")
            entered.set()
            assert release.wait(5)
            order.append("first done")

    def second():
        assert entered.wait(5)
        with _import_lock(tmp_path):
            order.append("second")
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(first)
        two = pool.submit(second)
        assert entered.wait(5)
        release.set()
        one.result(timeout=5)
        two.result(timeout=5)
    assert order == ["first", "first done", "second"]


def test_publisher_lease_prevents_double_sync_and_releases_after_failure(tmp_path):
    with pytest.raises(RuntimeError, match="publication failure"), _publisher_lease(tmp_path):
        with pytest.raises(CatalogError), _publisher_lease(tmp_path):
            pytest.fail("second publisher acquired a live lease")
        raise RuntimeError("publication failure")
    with _publisher_lease(tmp_path):
        pass
