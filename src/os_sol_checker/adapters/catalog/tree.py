"""Validated, bounded Git data. No working-tree or checkout semantics."""
from __future__ import annotations

import re
from dataclasses import dataclass

MAX_BLOB_BYTES = 8 * 1024 * 1024
MAX_MARKDOWN_BYTES = 1024 * 1024
MAX_TREE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 10_000
MAX_DEPTH = 16
MAX_PATH_BYTES = 1024
MAX_COMPONENT_BYTES = 255
MAX_TEST_STDIN_BYTES = 1024 * 1024
MAX_EXPECTED_OUTPUT_BYTES = 65536
OBJECT_ID = re.compile(r"[0-9a-f]{40}\Z")


class CatalogError(RuntimeError):
    """An import failed; the previously published snapshot must remain current."""


@dataclass(frozen=True, slots=True)
class TreeEntry:
    path: str
    mode: str
    data: bytes = b""

    @property
    def is_directory(self) -> bool:
        return self.mode == "040000"


def validate_path(path: str) -> None:
    try:
        encoded = path.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise CatalogError("Git path is not UTF-8") from exc
    parts = path.split("/")
    if (not path or len(encoded) > MAX_PATH_BYTES or len(parts) > MAX_DEPTH
            or "\x00" in path or "\\" in path
            or any(part in ("", ".", "..") or len(part.encode("utf-8")) > MAX_COMPONENT_BYTES
                   for part in parts)):
        raise CatalogError("Unsafe or overlong Git path")


def validate_tree(entries: list[TreeEntry]) -> dict[str, TreeEntry]:
    if len(entries) > MAX_ENTRIES:
        raise CatalogError("Git tree entry limit exceeded")
    tree: dict[str, TreeEntry] = {}
    total = 0
    for entry in entries:
        validate_path(entry.path)
        if entry.path in tree:
            raise CatalogError("Duplicate Git path")
        if entry.mode not in ("040000", "100644", "100755"):
            raise CatalogError("Symlinks, submodules and unsupported Git modes are forbidden")
        if entry.is_directory and entry.data:
            raise CatalogError("Directory entry has blob content")
        if len(entry.data) > MAX_BLOB_BYTES:
            raise CatalogError("Git blob size limit exceeded")
        if entry.path.rsplit("/", 1)[-1] == "task.md" and len(entry.data) > MAX_MARKDOWN_BYTES:
            raise CatalogError("Markdown size limit exceeded")
        total += len(entry.data)
        if total > MAX_TOTAL_BYTES:
            raise CatalogError("Git tree byte limit exceeded")
        tree[entry.path] = entry
    for path in tree:
        if "/" in path:
            parent = tree.get(path.rsplit("/", 1)[0])
            if parent is None or not parent.is_directory:
                raise CatalogError("Git tree has a missing or non-directory parent")
    return tree
