"""Versioned fingerprint of every tests subtree, including directory entries."""
from hashlib import sha256

from .tree import TreeEntry, validate_tree


class TestsFingerprint:
    @staticmethod
    def calculate(entries: list[TreeEntry]) -> str:
        tree = validate_tree(entries)
        roots = {path for path, entry in tree.items()
                 if entry.is_directory and path.rsplit("/", 1)[-1] == "tests"}
        digest = sha256(b"os-sol-checker-tests-v1\x00")
        for path in sorted(tree, key=lambda value: value.encode("utf-8")):
            parts = path.split("/")
            if not any("/".join(parts[:end]) in roots for end in range(1, len(parts) + 1)):
                continue
            entry = tree[path]
            encoded = path.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            digest.update(entry.mode.encode("ascii"))
            digest.update(len(entry.data).to_bytes(8, "big"))
            digest.update(entry.data)
        return digest.hexdigest()
