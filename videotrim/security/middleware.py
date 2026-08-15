"""Default-deny authentication, wrapped around the whole application.

This is deliberately ASGI middleware around the *outer* app rather than a
decorator per route. Gradio mounts routes this project does not author —
``/gradio_api/file/...``, ``/upload``, ``/queue/join``, ``/config``, ``/info``,
theme assets — each with its own file-serving semantics, and a forgotten
decorator is an open endpoint. Wrapping the inner FastAPI app before the mount
would miss all of them, so this must be applied *after* ``mount_gradio_app``
returns.

Everything is denied unless its path is on a short list of exact matches or
anchored prefixes. Substring tests are never used: ``/anything/login`` must not
inherit ``/login``'s exemption.
"""

import posixpath
from urllib.parse import unquote

from starlette.responses import JSONResponse, RedirectResponse

from .auth import COOKIE_NAME, CSRF_HEADER

# Exact paths reachable without a session: the login page, the login POST, the
# stylesheet that page needs, and a liveness probe.
PUBLIC_EXACT = frozenset({
    "/login",
    "/api/login",
    "/healthz",
    "/favicon.ico",
})

# Anchored prefixes. Only the login-time assets live here; the player's own
# assets are behind authentication with everything else.
PUBLIC_PREFIXES = ("/vt/static/login.",)

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Browsers ask for the page; the player asks with fetch/XHR. Only the first
# should be redirected to the login screen — the second needs a JSON 401 it can
# show in a toast.
_HTML_HINT = "text/html"


def _normalized_path(scope):
    """The request path, percent-decoded and traversal-collapsed.

    Collapsing ``..`` here is what stops ``/vt/static/login.css/../../gradio_api``
    from borrowing the allowlist's exemption.
    """
    raw = scope.get("path", "/") or "/"
    decoded = unquote(raw)
    if not decoded.startswith("/"):
        decoded = "/" + decoded
    collapsed = posixpath.normpath(decoded)
    if decoded.endswith("/") and not collapsed.endswith("/") and collapsed != "/":
        collapsed += "/"
    return collapsed


def is_public(path):
    if path in PUBLIC_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


class AuthenticationMiddleware:
    """Rejects anything that is not authenticated, before it reaches a route."""

    def __init__(self, app, auth_service, host_guard, store=None):
        self.app = app
        self.auth = auth_service
        self.host_guard = host_guard
        self.store = store

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = _normalized_path(scope)
        session = self._session_from(scope)
        # Routes read these instead of re-parsing the cookie, so there is exactly
        # one place that decides whether a request is authenticated.
        scope["vt_session"] = session
        scope["vt_path"] = path

        if is_public(path):
            await self.app(scope, receive, send)
            return

        if session is None:
            await self._deny(scope, receive, send, 401, "Login required")
            return

        if scope.get("type") == "http" and not self._csrf_ok(scope, session):
            await self._deny(
                scope, receive, send, 403,
                "This request is missing its security token. Reload the page and try again.",
            )
            return

        await self.app(scope, receive, send)

    # --- helpers -------------------------------------------------------------
    def _session_from(self, scope):
        token = self._cookie(scope, COOKIE_NAME)
        if not token:
            return None
        try:
            return self.auth.resolve(token)
        except Exception:  # pragma: no cover - a broken store must not 500 every route
            return None

    def _csrf_ok(self, scope, session):
        """Custom-header CSRF on every state change, on top of SameSite.

        A cross-site form post cannot set a custom header, and a GET is never a
        state change here, so this covers the mutations without a token in the
        page body.
        """
        if scope.get("method", "GET").upper() not in _MUTATING_METHODS:
            return True
        presented = self._header(scope, CSRF_HEADER)
        return self.auth.check_csrf(session, presented)

    @staticmethod
    def _header(scope, name):
        wanted = name.lower().encode("latin-1")
        for key, value in scope.get("headers") or ():
            if key.lower() == wanted:
                return value.decode("latin-1")
        return ""

    @classmethod
    def _cookie(cls, scope, name):
        raw = cls._header(scope, "cookie")
        for part in raw.split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return ""

    async def _deny(self, scope, receive, send, status, message):
        if scope.get("type") == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        accept = self._header(scope, "accept")
        wants_page = _HTML_HINT in accept and scope.get("method", "GET").upper() == "GET"
        if wants_page and status == 401:
            response = RedirectResponse("/login", status_code=303)
        else:
            response = JSONResponse({"detail": message}, status_code=status)
        await response(scope, receive, send)
