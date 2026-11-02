"""Single-user password auth: scrypt hashes, a session flag, login/logout routes.

Run `python -m app.core.auth` to print a hash for HOMEBASE_PASSWORD_HASH.
"""

import asyncio
import base64
import hashlib
import hmac
import os
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from app.core.web import render

SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}
PUBLIC_PATHS = {"/login", "/healthz"}

router = APIRouter()


def hash_password(password: str) -> str:
    """Return `scrypt:<salt>:<hash>` (no `$`, so .env/compose don't interpolate it)."""
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
    return f"scrypt:{_b64(salt)}:{_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt, digest = stored.split(":")
        if scheme != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=base64.urlsafe_b64decode(salt), **SCRYPT)
        return hmac.compare_digest(actual, base64.urlsafe_b64decode(digest))
    except ValueError:
        return False


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode()


def _safe_next(path: str | None) -> str:
    """Only allow local redirects."""
    return path if path and path.startswith("/") and not path.startswith("//") else "/"


async def require_login(request: Request, call_next) -> Response:
    """Middleware: redirect unauthenticated requests to /login (htmx gets HX-Redirect)."""
    path = request.url.path
    if not request.app.state.settings.auth_enabled:
        return await call_next(request)
    if path in PUBLIC_PATHS or path.startswith("/static/") or request.session.get("auth"):
        return await call_next(request)
    if request.headers.get("HX-Request"):
        return Response(status_code=401, headers={"HX-Redirect": "/login"})
    return RedirectResponse(f"/login?next={quote(path)}", status_code=303)


@router.get("/login")
def login_form(request: Request, next: str = "/"):
    if not request.app.state.settings.auth_enabled:
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", next=_safe_next(next))


@router.post("/login")
async def login(
    request: Request, password: Annotated[str, Form()], next: Annotated[str, Form()] = "/"
):
    settings = request.app.state.settings
    if not settings.auth_enabled or verify_password(password, settings.password_hash):
        request.session["auth"] = True
        return RedirectResponse(_safe_next(next), status_code=303)
    await asyncio.sleep(1)  # slow down guessing
    return render(request, "login.html", next=_safe_next(next), error="Wrong password", status=401)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


if __name__ == "__main__":
    import getpass

    pw = getpass.getpass("Password: ")
    if pw != getpass.getpass("Repeat: "):
        raise SystemExit("Passwords don't match")
    print(hash_password(pw))
