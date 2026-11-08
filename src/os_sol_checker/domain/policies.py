import unicodedata
from hashlib import sha256

from .models import Preview

GCC_FLAGS = (
    "-std=gnu17", "-O2", "-pipe", "-Wall", "-Wextra", "-Wformat=2",
    "-Werror=format-security", "-fstack-protector-strong", "-D_FORTIFY_SOURCE=2",
    "-fPIE", "-pie", "-Wl,-z,relro,-z,now",
)


def identify(source: bytes) -> tuple[str, int]:
    return sha256(source).hexdigest(), len(source)


def flags_digest() -> str:
    return sha256(b"\0".join(arg.encode() for arg in (*GCC_FLAGS, "-lm", "-pthread"))).hexdigest()


def compare(expected: bytes, actual: bytes) -> bool:
    return expected == actual


def preview(value: bytes, limit: int = 4096, *, truncated: bool = False) -> Preview:
    text = value[:limit].decode("utf-8", errors="replace")
    safe = "".join(
        f"\\u{ord(char):04x}" if char not in "\n\t" and unicodedata.category(char) in {"Cc", "Cf"} else char
        for char in text
    )
    return Preview(text=safe, truncated=truncated or len(value) > limit,
                   byte_size=None if truncated else len(value))
