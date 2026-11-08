from hashlib import sha256

import pytest

from os_sol_checker.adapters.catalog.fingerprint import TestsFingerprint
from os_sol_checker.adapters.catalog.tree import (
    MAX_BLOB_BYTES,
    MAX_COMPONENT_BYTES,
    MAX_DEPTH,
    MAX_ENTRIES,
    MAX_MARKDOWN_BYTES,
    MAX_PATH_BYTES,
    CatalogError,
    TreeEntry,
    validate_path,
    validate_tree,
)


def test_canonical_fingerprint_includes_directory_mode_path_length_and_bytes():
    entries = [TreeEntry("tests", "040000"), TreeEntry("tests/1.in", "100755", b"a\x00b"),
               TreeEntry("task.md", "100644", b"not included")]
    canonical = b"os-sol-checker-tests-v1\x00"
    for path, mode, data in [(b"tests", b"040000", b""), (b"tests/1.in", b"100755", b"a\x00b")]:
        canonical += len(path).to_bytes(8, "big") + path + mode + len(data).to_bytes(8, "big") + data
    assert TestsFingerprint.calculate(entries) == sha256(canonical).hexdigest()
    assert TestsFingerprint.calculate(list(reversed(entries))) == sha256(canonical).hexdigest()
    entries[-1] = TreeEntry("task.md", "100644", b"changed description")
    assert TestsFingerprint.calculate(entries) == sha256(canonical).hexdigest()


def test_empty_trees_files_placeholders_and_every_global_tests_subtree_affect_digest():
    empty = TestsFingerprint.calculate([])
    directory = [TreeEntry("tests", "040000")]
    with_file = directory + [TreeEntry("tests/.gitkeep", "100644")]
    nested = with_file + [TreeEntry("tests/tests", "040000"), TreeEntry("tests/tests/empty", "100644")]
    outside_task = [TreeEntry("misc", "040000"), TreeEntry("misc/tests", "040000")]
    variants = [empty, TestsFingerprint.calculate(directory), TestsFingerprint.calculate(with_file),
                TestsFingerprint.calculate(nested), TestsFingerprint.calculate(outside_task)]
    assert len(set(variants)) == len(variants)
    # Nested tests entries are not included twice.
    digest = sha256(b"os-sol-checker-tests-v1\x00")
    for entry in sorted(nested, key=lambda item: item.path.encode()):
        path = entry.path.encode()
        digest.update(len(path).to_bytes(8, "big") + path + entry.mode.encode()
                      + len(entry.data).to_bytes(8, "big") + entry.data)
    assert TestsFingerprint.calculate(nested) == digest.hexdigest()


def test_mode_bytes_and_rename_each_invalidate_fingerprint():
    variants = []
    for name, mode, data in [("a", "100644", b""), ("a", "100755", b""),
                              ("b", "100644", b""), ("a", "100644", b"\x00")]:
        variants.append(TestsFingerprint.calculate([TreeEntry("tests", "040000"),
                                                    TreeEntry("tests/" + name, mode, data)]))
    assert len(set(variants)) == 4


@pytest.mark.parametrize("path", ["", "/absolute", "a/../b", "./a", "a//b", "a/", "a\\b", "a\x00b",
                                   "a/" + "x" * (MAX_COMPONENT_BYTES + 1),
                                   "/".join(["a"] * (MAX_DEPTH + 1)),
                                   "/".join(["x" * 255] * 5), "\udcff"])
def test_unsafe_or_overlong_paths_are_rejected(path):
    with pytest.raises(CatalogError):
        validate_path(path)


def test_path_boundaries_accept_exact_lengths():
    validate_path("x" * MAX_COMPONENT_BYTES)
    validate_path("/".join(["a"] * MAX_DEPTH))
    path = "/".join(["x" * 255] * 3 + ["x" * 254, "y"])
    assert len(path.encode()) == MAX_PATH_BYTES
    validate_path(path)
    with pytest.raises(CatalogError):
        validate_path(path + "x")


@pytest.mark.parametrize("mode", ["120000", "160000", "100664", "040755"])
def test_forbidden_modes_reject_entire_tree(mode):
    with pytest.raises(CatalogError):
        TestsFingerprint.calculate([TreeEntry("entry", mode)])


def test_duplicate_paths_and_missing_or_file_parents_are_rejected():
    for entries in [[TreeEntry("a", "100644"), TreeEntry("a", "100644")],
                    [TreeEntry("a/b", "100644")],
                    [TreeEntry("a", "100644"), TreeEntry("a/b", "100644")]]:
        with pytest.raises(CatalogError):
            validate_tree(entries)


def test_blob_markdown_and_entry_count_limits(monkeypatch):
    # Reduced limits exercise exact boundaries without allocating hundreds of MiB.
    from os_sol_checker.adapters.catalog import tree
    monkeypatch.setattr(tree, "MAX_BLOB_BYTES", 8)
    monkeypatch.setattr(tree, "MAX_MARKDOWN_BYTES", 4)
    monkeypatch.setattr(tree, "MAX_TOTAL_BYTES", 10)
    monkeypatch.setattr(tree, "MAX_ENTRIES", 2)
    validate_tree([TreeEntry("blob", "100644", b"x" * 8)])
    with pytest.raises(CatalogError):
        validate_tree([TreeEntry("blob", "100644", b"x" * 9)])
    validate_tree([TreeEntry("task.md", "100644", b"x" * 4)])
    with pytest.raises(CatalogError):
        validate_tree([TreeEntry("task.md", "100644", b"x" * 5)])
    validate_tree([TreeEntry("a", "100644", b"x" * 5), TreeEntry("b", "100644", b"x" * 5)])
    with pytest.raises(CatalogError):
        validate_tree([TreeEntry("a", "100644", b"x" * 6), TreeEntry("b", "100644", b"x" * 5)])
    with pytest.raises(CatalogError):
        validate_tree([TreeEntry(str(number), "100644") for number in range(3)])
    assert MAX_BLOB_BYTES == 8 * 1024 * 1024
    assert MAX_MARKDOWN_BYTES == 1024 * 1024
    assert MAX_ENTRIES == 10000
