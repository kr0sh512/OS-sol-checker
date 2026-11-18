from __future__ import annotations

import base64
import binascii
import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from ..adapters.postgres.admin import AdminArchiveStore
from ..application.admin import admin_dashboard
from .routes import StoreDependency as TransientStoreDependency
from .routes import catalog, ip, rate
from .views import page

router = APIRouter()


def get_admin_store(request: Request):
    return request.app.state.admin_store


StoreDependency = Annotated[AdminArchiveStore, Depends(get_admin_store)]


def valid_admin_credentials(request: Request) -> bool:
    headers = request.headers.getlist("authorization")
    if len(headers) != 1:
        return False
    scheme, _, encoded = headers[0].partition(" ")
    if scheme.lower() != "basic" or not encoded or len(encoded) > 4096:
        return False
    try:
        credentials = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return False
    username, separator, password = credentials.partition(b":")
    expected = request.app.state.settings.admin_password.encode()
    return bool(separator) and hmac.compare_digest(username, b"admin") & hmac.compare_digest(password, expected)


@router.get("/admin")
async def admin_page(request: Request, admin_store: StoreDependency, store: TransientStoreDependency):
    rate(request, "admin-ip", ip(request), 60)
    if not valid_admin_credentials(request):
        return page(
            request, "error.html", status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="os-sol-checker-admin", charset="UTF-8"'},
            title="Требуется авторизация", message="Введите учётные данные администратора.",
            profile=None, task=None, topic=None,
        )
    snapshot = await catalog(store)
    transient = await store.admin_statistics()
    archived = await admin_store.correct_solutions()
    return page(
        request, "admin.html", dashboard=admin_dashboard(snapshot, transient, archived),
        profile=None, task=None, topic=None,
    )
