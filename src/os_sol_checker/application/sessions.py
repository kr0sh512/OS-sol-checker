"""Opaque, hashed-at-rest sessions and stable, session-bound CSRF tokens."""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import unicodedata
from uuid import UUID

from ..domain.errors import CheckerError

COOKIE_NAME = "__Host-checker_session"
COOKIE_MAX_AGE = 30 * 24 * 60 * 60
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
BIDI_CONTROLS = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def csrf_token(secret: str, session_id: UUID) -> str:
    digest = hmac.digest(secret.encode("utf-8"), f"checker-csrf-v1:{session_id}".encode(), "sha256")
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def valid_csrf(secret: str, session_id: UUID, supplied: str) -> bool:
    return supplied.isascii() and hmac.compare_digest(csrf_token(secret, session_id), supplied)


def validate_profile(value: object) -> tuple[str, str]:
    if not isinstance(value, dict) or set(value) != {"name", "group"}:
        raise CheckerError("invalid_profile", "Укажите имя и группу без дополнительных полей.", 422)
    fields = []
    for key, limit in (("name", 120), ("group", 64)):
        text = value[key]
        if not isinstance(text, str):
            raise CheckerError("invalid_profile", "Имя и группа должны быть строками.", 422)
        # Reject controls even at the edges, before stripping whitespace.
        if any(unicodedata.category(c) in {"Cc", "Cs"} or c in BIDI_CONTROLS for c in text):
            raise CheckerError("invalid_profile", "Управляющие символы в профиле запрещены.", 422)
        text = text.strip()
        if not text or len(text) > limit:
            raise CheckerError("invalid_profile", f"Поле {key}: от 1 до {limit} символов.", 422)
        fields.append(text)
    return fields[0], fields[1]


async def resolve_session(store, raw_token: str | None, *, create: bool):
    if raw_token and TOKEN_PATTERN.fullmatch(raw_token):
        profile = await store.get_session(token_hash(raw_token), 30)
        if profile is not None:
            return profile, raw_token
    if not create:
        return None, None
    raw_token = secrets.token_urlsafe(32)
    return await store.create_session(token_hash(raw_token), 30), raw_token
