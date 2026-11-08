"""HTTP boundary tests with explicitly injected storage, never an alternate runtime backend."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from os_sol_checker.application.sessions import COOKIE_NAME
from os_sol_checker.config import Settings
from os_sol_checker.domain.models import (CatalogSnapshot, CheckResult, Hint, JudgePolicy, SessionProfile,
                                         Submission, Task, TestCase, Topic)
from os_sol_checker.domain.verdicts import SubmissionStatus, Verdict
from os_sol_checker.main import create_app

ORIGIN = "https://checker.example"
TASK_ID = "a" * 64


class IsolatedStore:
    def __init__(self):
        self.opened = False
        self.closed = False
        self.sessions = {}
        self.submissions = {}
        self.pointers = {}
        self.next_cached = False
        self.heartbeat = True
        self.policy = JudgePolicy(runner_image_digest="sha256:" + "b" * 64,
                                  sandbox_profile_digest="c" * 64, flags_digest="d" * 64)
        task = Task(id=TASK_ID, topic_id="topic", title="Сумма <тест>", relative_path="private/path",
                    description_html="<p>Безопасное условие</p>",
                    hints=[Hint(title="Первая", html="<p>Раз</p>"), Hint(title="Вторая", html="<p>Два</p>")],
                    tests=[TestCase(id="1", input_path="tests/input", expected_path="tests/secret")])
        self.snapshot = CatalogSnapshot(snapshot_id="snapshot", commit="e" * 40, tests_digest="f" * 64,
                                        root_path="/private", generation=1,
                                        topics=[Topic(id="topic", title="Ввод и вывод", tasks=[task])])

    async def open(self): self.opened = True
    async def close(self): self.closed = True
    async def health(self): return True
    async def worker_alive(self): return self.heartbeat
    async def current_catalog(self): return self.snapshot
    async def current_policy(self): return self.policy

    async def create_session(self, token_hash, ttl_days):
        profile = SessionProfile(id=uuid4(), expires_at=datetime.now(timezone.utc) + timedelta(days=ttl_days))
        self.sessions[token_hash] = profile
        return profile

    async def get_session(self, token_hash, ttl_days): return self.sessions.get(token_hash)

    async def update_profile(self, session_id, name, group):
        for profile in self.sessions.values():
            if profile.id == session_id:
                profile.name, profile.group = name, group
                return profile
        raise AssertionError("unknown session")

    async def last_submissions(self, session_id): return self.pointers.get(session_id, [])

    async def accept_submission(self, session_id, task, context, source, filename, queue_capacity):
        profile = next(profile for profile in self.sessions.values() if profile.id == session_id)
        cached = self.next_cached
        submission = Submission(id=uuid4(), session_id=session_id, author_name=profile.name,
            author_group=profile.group, task_id=task.id, context=context, sha256=sha256(source).hexdigest(),
            byte_size=len(source), source=source, original_filename=filename, created_at=datetime.now(timezone.utc),
            status=SubmissionStatus.FINISHED if cached else SubmissionStatus.QUEUED, cache_hit=cached,
            result=CheckResult(verdict=Verdict.WRONG_ANSWER, passed_tests=0, total_tests=1) if cached else None,
            queue_position=None if cached else 1)
        self.submissions[submission.id] = submission
        self.pointers[session_id] = [{"task_id": task.id, "id": submission.id, "available": True}]
        return submission

    async def get_submission(self, session_id, submission_id):
        submission = self.submissions.get(submission_id)
        return submission if submission and submission.session_id == session_id else None


@pytest.fixture
def web():
    store = IsolatedStore()
    app = create_app(Settings(transient_database_url="postgresql://test", public_origin=ORIGIN,
                              csrf_secret="test-secret-only"))
    app.state.store_factory = lambda settings: store
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, store
    assert store.opened and store.closed


def authenticate(client, *, name="Иван", group="ИУ-12"):
    data = client.get("/api/profile").json()
    headers = {"Origin": ORIGIN, "X-CSRF-Token": data["csrf_token"]}
    response = client.put("/api/profile", headers=headers, json={"name": name, "group": group})
    assert response.status_code == 200
    return headers


def test_cookie_profile_and_immutable_author_journey(web):
    client, store = web
    response = client.get("/api/profile")
    cookie = response.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "Path=/" in cookie and "Max-Age=2592000" in cookie and "Domain=" not in cookie
    raw_token = client.cookies.get(COOKIE_NAME)
    assert raw_token not in store.sessions
    assert sha256(raw_token.encode()).hexdigest() in store.sessions
    assert response.headers["cache-control"] == "no-store"
    headers = authenticate(client)
    submitted = client.post(f"/api/tasks/{TASK_ID}/submissions", headers=headers,
                            files={"file": ("empty.c", b"", "text/plain")})
    assert submitted.status_code == 202
    data = submitted.json()
    assert submitted.headers["location"] == "/api/submissions/" + data["id"]
    assert data["queue_position"] == 1 and data["verdict"] is None
    assert not ({"source", "session_id", "author_name", "author_group", "sha256", "original_filename"} & data.keys())
    client.put("/api/profile", headers=headers, json={"name": "Новое имя", "group": "Другая"})
    submission = next(iter(store.submissions.values()))
    assert submission.author_name == "Иван" and submission.author_group == "ИУ-12"
    profile = client.get("/api/profile").json()
    assert profile["csrf_token"] == headers["X-CSRF-Token"]
    assert profile["last_submissions"] == [{"task_id": TASK_ID, "id": data["id"], "available": True}]
    assert client.get(submitted.headers["location"]).status_code == 200


def test_foreign_unknown_expired_and_malformed_are_indistinguishable(web):
    client, store = web
    headers = authenticate(client)
    submitted = client.post(f"/api/tasks/{TASK_ID}/submissions", headers=headers, files={"file": ("a.c", b"x")})
    target = submitted.headers["location"]
    client.cookies.clear()
    foreign = client.get(target)
    unknown = client.get("/api/submissions/" + str(uuid4()))
    malformed = client.get("/api/submissions/not-a-uuid")
    assert foreign.status_code == unknown.status_code == malformed.status_code == 404
    assert foreign.json() == unknown.json() == malformed.json()
    assert not client.cookies


@pytest.mark.parametrize("origin", [None, "null", "https://evil.example", ORIGIN + "/"])
def test_origin_is_required_and_exact_before_body_parsing(web, origin):
    client, _ = web
    headers = authenticate(client)
    headers.pop("Origin")
    if origin is not None: headers["Origin"] = origin
    response = client.post(f"/api/tasks/{TASK_ID}/submissions", headers=headers, content=b"bad")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"


def test_csrf_rejects_cross_session_tokens(web):
    client, _ = web
    headers = authenticate(client)
    client.cookies.clear()
    client.get("/api/profile")
    assert client.put("/api/profile", headers=headers, json={"name": "x", "group": "g"}).status_code == 403


def test_cache_status_stale_pointer_and_readiness(web):
    client, store = web
    headers = authenticate(client)
    store.next_cached = True
    response = client.post(f"/api/tasks/{TASK_ID}/submissions", headers=headers, files={"file": ("a.c", b"x")})
    assert response.status_code == 200 and response.json()["cache_hit"]
    assert response.json()["verdict"] == "wrong_answer"
    assert client.get("/health/ready").status_code == 200
    store.heartbeat = False
    assert client.get("/health/ready").status_code == 503
    assert client.get("/health/live").status_code == 200
    pointer = next(iter(store.pointers.values()))[0]
    pointer["available"] = False
    store.submissions.clear()
    assert client.get("/api/profile").json()["last_submissions"][0]["available"] is False
    assert client.get(response.headers["location"]).status_code == 404


def test_catalog_ui_sanitized_fields_hints_and_csp(web):
    client, store = web
    assert client.get("/").status_code == 200
    assert client.get("/topics/topic").status_code == 200
    response = client.get(f"/tasks/{TASK_ID}")
    assert response.status_code == 200
    html = response.text
    assert "<p>Безопасное условие</p>" in html
    assert html.count("<details>") == 2 and "<details open" not in html
    assert "Сумма &lt;тест&gt;" in html
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert "unsafe-inline" not in response.headers["content-security-policy"]
    data = client.get("/api/catalog").json()
    assert "root_path" not in data and "tests" not in data["topics"][0]["tasks"][0]
    assert client.get("/topics/missing").status_code == 404
    store.snapshot = None
    assert client.get("/").status_code == 503
    assert client.get("/api/catalog").json()["error"]["code"] == "catalog_unavailable"


def test_profile_types_extra_fields_and_media_type(web):
    client, _ = web
    headers = authenticate(client)
    for value in [{"name": "x", "group": "g", "role": "admin"}, {"name": 7, "group": "g"}]:
        response = client.put("/api/profile", headers=headers, json=value)
        assert response.status_code == 422 and response.json()["error"]["code"] == "invalid_profile"
    assert client.put("/api/profile", headers=headers, content=b"x").status_code == 415


def test_upload_rate_limit_is_session_bounded(web):
    client, _ = web
    headers = authenticate(client)
    for _ in range(10):
        assert client.post(f"/api/tasks/{TASK_ID}/submissions", headers=headers,
                           files={"file": ("a.c", b"")}).status_code == 202
    response = client.post(f"/api/tasks/{TASK_ID}/submissions", headers=headers, files={"file": ("a.c", b"")})
    assert response.status_code == 429 and "retry-after" in response.headers
