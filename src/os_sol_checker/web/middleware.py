"""Browser security headers and explicitly process-local, bounded abuse controls."""
from collections import OrderedDict, deque
import time

from ..domain.errors import CheckerError


class LocalRateLimiter:
    """Not a global/multi-replica limit; queue capacity remains transactional in PostgreSQL.

    At most 10000 active key windows. A full table rejects new keys instead of
    evicting active windows (which would let token rotation bypass the limiter).
    """
    def __init__(self, max_keys: int = 10000, clock=time.monotonic):
        self.windows = OrderedDict()
        self.max_keys = max_keys
        self.clock = clock

    def check(self, key: str, limit: int, seconds: float = 60):
        now = self.clock()
        while self.windows:
            first_key, (_, expiry) = next(iter(self.windows.items()))
            if expiry > now:
                break
            del self.windows[first_key]
        entry = self.windows.get(key)
        if entry is None:
            if len(self.windows) >= self.max_keys:
                raise CheckerError("rate_limited", "Слишком много запросов. Попробуйте позднее.", 429)
            hits = deque()
        else:
            hits, _ = entry
            while hits and hits[0] <= now - seconds:
                hits.popleft()
        if len(hits) >= limit:
            raise CheckerError("rate_limited", "Слишком много запросов. Попробуйте через минуту.", 429)
        hits.append(now)
        self.windows[key] = (hits, now + seconds)
        self.windows.move_to_end(key)


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def secure_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend([
                    (b"content-security-policy", b"default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                ])
                if not scope.get("path", "").startswith("/static/"):
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, secure_send)
