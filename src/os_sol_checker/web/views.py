"""Jinja views consume already-sanitized catalog HTML, never source Markdown."""
from pathlib import Path

from starlette.templating import Jinja2Templates

from ..application.sessions import COOKIE_MAX_AGE, COOKIE_NAME

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def session_cookie(response, token: str):
    response.set_cookie(COOKIE_NAME, token, max_age=COOKIE_MAX_AGE, secure=True,
                        httponly=True, samesite="lax", path="/")
    return response


def page(request, template, *, status_code=200, **context):
    return templates.TemplateResponse(request=request, name=template, context=context, status_code=status_code)
