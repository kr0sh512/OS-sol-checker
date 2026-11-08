"""Public web process. This role never constructs a runner or archive connection."""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.staticfiles import StaticFiles

from .adapters.catalog.git import GitCatalog
from .adapters.postgres.transient import TransientStore
from .config import Settings
from .domain.errors import CheckerError
from .web.middleware import LocalRateLimiter, SecurityHeadersMiddleware
from .web.routes import router
from .web.views import page


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.require("web")
    origin = urlsplit(settings.public_origin)
    if (origin.scheme != "https" or not origin.hostname or origin.username or origin.password
            or origin.path or origin.query or origin.fragment):
        raise ValueError("PUBLIC_ORIGIN must be an exact HTTPS origin without path or credentials")
    try:
        _ = origin.port
    except ValueError:
        raise ValueError("PUBLIC_ORIGIN has an invalid port") from None

    @asynccontextmanager
    async def lifespan(app):
        store = app.state.store_factory(settings)
        app.state.store = store
        try:
            await store.open()
            yield
        finally:
            await store.close()

    app = FastAPI(title="Проверка решений по ОС", lifespan=lifespan, docs_url=None,
                  redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.store_factory = TransientStore
    app.state.catalog = GitCatalog(settings)
    app.state.rate_limiter = LocalRateLimiter()
    app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"), name="static")

    @app.exception_handler(CheckerError)
    async def checker_error(request: Request, exc: CheckerError):
        headers = {"Retry-After": "3"} if exc.code == "queue_full" else (
            {"Retry-After": "60"} if exc.status_code == 429 else {})
        error = {"code": exc.code, "message": exc.message}
        if hasattr(exc, "limit_bytes"):
            error["limit_bytes"] = exc.limit_bytes
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": error, "submission": None}, status_code=exc.status_code, headers=headers)
        return page(request, "error.html", status_code=exc.status_code, title="Запрос не выполнен",
                    message=exc.message, profile=None, task=None, topic=None)

    @app.exception_handler(DatabaseError)
    @app.exception_handler(PoolTimeout)
    async def storage_error(request: Request, exc):
        return await checker_error(request, CheckerError("storage_unavailable", "Хранилище временно недоступно.", 503))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc):
        return await checker_error(request, CheckerError("invalid_request", "Некорректный запрос.", 422))

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc):
        return await checker_error(request, CheckerError("not_found" if exc.status_code == 404 else "invalid_request",
                                                        "Страница не найдена." if exc.status_code == 404
                                                        else "Запрос не выполнен.", exc.status_code))

    return app


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="Public OS solution checker web server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port, proxy_headers=False)


if __name__ == "__main__":
    main()
