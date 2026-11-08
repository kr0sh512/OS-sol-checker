from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from starlette.responses import JSONResponse

from ..application.sessions import COOKIE_NAME, csrf_token, resolve_session, valid_csrf, validate_profile
from ..application.submissions import accept_submission, submission_dto
from ..domain.errors import CheckerError
from ..domain.verdicts import SubmissionStatus
from .uploads import media_type, read_bounded, read_upload
from .views import page, session_cookie

router = APIRouter()


def get_store(request: Request):
    """Override the store factory before lifespan, or this dependency in isolated tests."""
    return request.app.state.store


def rate(request: Request, category: str, key: str, limit: int):
    request.app.state.rate_limiter.check(f"{category}:{key}", limit)


def ip(request: Request):
    # Deliberately do not trust client-supplied Forwarded/X-Forwarded-For.
    return request.client.host if request.client else "unknown"


async def session(request: Request, store, *, create: bool):
    return await resolve_session(store, request.cookies.get(COOKIE_NAME), create=create)


async def mutation_session(request: Request, store):
    origins = request.headers.getlist("origin")
    tokens = request.headers.getlist("x-csrf-token")
    if origins != [request.app.state.settings.public_origin] or len(tokens) != 1:
        raise CheckerError("csrf_failed", "Проверка безопасности запроса не пройдена.", 403)
    profile, _ = await session(request, store, create=False)
    if profile is None or not valid_csrf(request.app.state.settings.csrf_secret, profile.id, tokens[0]):
        raise CheckerError("csrf_failed", "Проверка безопасности запроса не пройдена.", 403)
    return profile


async def profile_dto(request, store, profile):
    pointers = await store.last_submissions(profile.id)
    return {
        "profile": {"name": profile.name, "group": profile.group},
        "csrf_token": csrf_token(request.app.state.settings.csrf_secret, profile.id),
        "last_submissions": [{"task_id": item["task_id"], "id": str(item["id"]),
                              "available": bool(item["available"])} for item in pointers],
    }


async def catalog(store):
    snapshot = await store.current_catalog()
    if snapshot is None:
        raise CheckerError("catalog_unavailable", "Каталог заданий пока недоступен.", 503)
    return snapshot


@router.get("/api/catalog")
async def api_catalog(store=Depends(get_store)):
    snapshot = await catalog(store)
    return {"snapshot_id": snapshot.snapshot_id, "generation": snapshot.generation,
            "topics": [{"id": topic.id, "title": topic.title,
                        "tasks": [{"id": task.id, "title": task.title, "availability": task.availability,
                                   "reason": task.reason} for task in topic.tasks]} for topic in snapshot.topics]}


@router.get("/api/profile")
async def get_profile(request: Request, store=Depends(get_store)):
    rate(request, "profile-ip", ip(request), 120)
    profile, token = await session(request, store, create=True)
    return session_cookie(JSONResponse(await profile_dto(request, store, profile)), token)


@router.put("/api/profile")
async def put_profile(request: Request, store=Depends(get_store)):
    profile = await mutation_session(request, store)
    rate(request, "profile-write-ip", ip(request), 60)
    rate(request, "profile-write-session", str(profile.id), 30)
    if media_type(request) != b"application/json":
        raise CheckerError("unsupported_media_type", "Ожидается application/json.", 415)
    body = await read_bounded(request, min(request.app.state.settings.max_request_bytes, 131072))
    try:
        def object_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = value
            return result
        value = json.loads(body, object_pairs_hook=object_pairs)
    except (ValueError, UnicodeError, RecursionError):
        raise CheckerError("invalid_profile", "Некорректный JSON профиля.", 422) from None
    name, group = validate_profile(value)
    profile = await store.update_profile(profile.id, name, group)
    return await profile_dto(request, store, profile)


@router.post("/api/tasks/{task_id}/submissions")
async def submit(task_id: str, request: Request, store=Depends(get_store)):
    profile = await mutation_session(request, store)
    rate(request, "upload-ip", ip(request), 60)
    rate(request, "upload-session", str(profile.id), 10)
    settings = request.app.state.settings
    filename, source = await read_upload(request, request_limit=min(settings.max_request_bytes, 131072),
                                         source_limit=min(settings.max_source_bytes, 102400))
    submission = await accept_submission(store, settings, profile, task_id, source, filename)
    snapshot, policy = await store.current_catalog(), await store.current_policy()
    code = 200 if submission.cache_hit and submission.status == SubmissionStatus.FINISHED else 202
    return JSONResponse(submission_dto(submission, snapshot, policy), status_code=code,
                        headers={"Location": f"/api/submissions/{submission.id}"})


@router.get("/api/submissions/{submission_id}")
async def get_submission(submission_id: str, request: Request, store=Depends(get_store)):
    rate(request, "read-ip", ip(request), 600)
    try:
        parsed_id = UUID(submission_id)
    except ValueError:
        raise CheckerError("submission_not_found", "История недоступна.", 404) from None
    profile, _ = await session(request, store, create=False)
    submission = await store.get_submission(profile.id, parsed_id) if profile else None
    if submission is None:
        raise CheckerError("submission_not_found", "История недоступна.", 404)
    return submission_dto(submission, await store.current_catalog(), await store.current_policy())


async def html_page(request, store, kind, entity_id=None):
    rate(request, "page-ip", ip(request), 120)
    profile, token = await session(request, store, create=True)
    snapshot = await store.current_catalog()
    context = {"profile": profile, "csrf_token": csrf_token(request.app.state.settings.csrf_secret, profile.id),
               "snapshot": snapshot, "task": None, "topic": None}
    if snapshot is None:
        return session_cookie(page(request, "error.html", status_code=503, title="Каталог недоступен",
                                   message="Каталог заданий пока недоступен. Попробуйте позднее.", **context), token)
    if kind == "topic":
        context["topic"] = next((topic for topic in snapshot.topics if topic.id == entity_id), None)
        if context["topic"] is None:
            return session_cookie(page(request, "error.html", status_code=404, title="Тема не найдена",
                                       message="Такой темы нет в каталоге.", **context), token)
    elif kind == "task":
        context["task"] = snapshot.task(entity_id)
        if context["task"] is None:
            return session_cookie(page(request, "error.html", status_code=404, title="Задание не найдено",
                                       message="Такого задания нет в каталоге.", **context), token)
        context["topic"] = next((topic for topic in snapshot.topics if topic.id == context["task"].topic_id), None)
    return session_cookie(page(request, f"{kind if kind != 'home' else 'index'}.html", **context), token)


@router.get("/")
async def home(request: Request, store=Depends(get_store)):
    return await html_page(request, store, "home")


@router.get("/topics/{topic_id}")
async def topic_page(topic_id: str, request: Request, store=Depends(get_store)):
    return await html_page(request, store, "topic", topic_id)


@router.get("/tasks/{task_id}")
async def task_page(task_id: str, request: Request, store=Depends(get_store)):
    return await html_page(request, store, "task", task_id)


@router.get("/health/live")
async def live():
    return {"status": "alive"}


@router.get("/health/ready")
async def ready(store=Depends(get_store)):
    try:
        healthy = (await store.health() and await store.current_catalog() is not None
                   and await store.current_policy() is not None and await store.worker_alive())
    except Exception:
        healthy = False
    return JSONResponse({"status": "ready" if healthy else "not_ready"}, status_code=200 if healthy else 503)
