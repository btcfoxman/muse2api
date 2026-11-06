"""Password-only management login, separate from the public API bearer key.

Sessions are opaque, bounded and process-local. A restart or password change
invalidates them; neither the password nor the API key is stored in a cookie.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
import hashlib
import hmac
import json
import math
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse


COOKIE_NAME = "muse2api_admin_session"
CSRF_HEADER = "x-muse-csrf"
_SAFE_METHODS = frozenset(("GET", "HEAD", "OPTIONS"))
_COOKIE_SHAPE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_PRIVATE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
    "Vary": "Cookie",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
}


def constant_time_equal(left: str, right: str) -> bool:
    try:
        return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
    except (AttributeError, UnicodeError):
        return False


def _parsed_origin(value: str, *, allow_root_path: bool = False):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme.lower() not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in (("", "/") if allow_root_path else ("",))
                or parsed.query or parsed.fragment):
            return None
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            return None
        return (parsed.scheme.lower(), parsed.hostname.lower(),
                port if port is not None else (443 if parsed.scheme.lower() == "https" else 80))
    except (ValueError, UnicodeError):
        return None


@dataclass(repr=False)
class AdminSession:
    csrf_token: str
    expires_at: float
    password_fingerprint: bytes


class AdminLogin:
    session_limit = 64
    failure_limit = 5
    failure_window = 15 * 60
    failure_clients_limit = 1024

    def __init__(self, config):
        self._config = config
        self._lock = threading.RLock()
        self._sessions: OrderedDict[str, AdminSession] = OrderedDict()
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()
        self._clock = time.monotonic

    @property
    def config(self):
        return self._config()

    @property
    def password(self) -> str:
        return getattr(self.config, "admin_password", "") or ""

    @property
    def enabled(self) -> bool:
        return bool(self.password or getattr(self.config, "admin_login_required", False))

    @property
    def ttl(self) -> int:
        return max(60, min(86400, int(getattr(self.config, "admin_session_ttl", 28800))))

    def _fingerprint(self) -> bytes:
        return hashlib.sha256(self.password.encode("utf-8")).digest()

    def effective_origin(self, request: Request):
        """Trust a configured public origin only for its exact Host, not X-Forwarded-*.

        Cloudflared may reach Uvicorn through a Docker gateway outside its proxy
        allowlist. The server-owned public URL supplies HTTPS in that case,
        while an HTTP LAN Host keeps its own origin and non-Secure cookie.
        """
        public = _parsed_origin(getattr(self.config, "public_base", "") or "", allow_root_path=True)
        host = request.headers.get("host", "")
        try:
            authority = urlsplit("http://" + host)
            if (public and authority.hostname and authority.username is None
                    and authority.password is None and not authority.path
                    and not authority.query and not authority.fragment
                    and authority.hostname.lower() == public[1]
                    and (authority.port is None or authority.port == public[2])):
                return public
        except (ValueError, UnicodeError):
            pass
        return _parsed_origin(str(request.base_url), allow_root_path=True)

    def same_origin(self, request: Request) -> bool:
        origins = request.headers.getlist("origin")
        return (len(origins) == 1 and _parsed_origin(origins[0]) is not None
                and _parsed_origin(origins[0]) == self.effective_origin(request))

    def secure_cookie(self, request: Request) -> bool:
        origin = self.effective_origin(request)
        return bool(origin and origin[0] == "https")

    def _prune_sessions(self, now: float):
        current = self._fingerprint()
        for digest, item in list(self._sessions.items()):
            if item.expires_at <= now or not hmac.compare_digest(item.password_fingerprint, current):
                self._sessions.pop(digest, None)

    def session(self, request: Request) -> AdminSession | None:
        if not self.enabled or not self.password:
            return None
        token = request.cookies.get(COOKIE_NAME, "")
        if not _COOKIE_SHAPE.fullmatch(token):
            return None
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        with self._lock:
            self._prune_sessions(self._clock())
            return self._sessions.get(digest)

    def revoke(self, request: Request):
        token = request.cookies.get(COOKIE_NAME, "")
        if _COOKIE_SHAPE.fullmatch(token):
            digest = hashlib.sha256(token.encode("ascii")).hexdigest()
            with self._lock:
                self._sessions.pop(digest, None)

    def csrf_ok(self, request: Request, session: AdminSession) -> bool:
        tokens = request.headers.getlist(CSRF_HEADER)
        return (self.same_origin(request) and len(tokens) == 1
                and constant_time_equal(tokens[0], session.csrf_token))

    def _client(self, request: Request) -> str:
        # Uvicorn's configured trusted-proxy handling is authoritative. Do not
        # accept X-Forwarded-For / CF-Connecting-IP from an arbitrary client.
        return request.client.host if request.client else "unknown"

    def _retry_after(self, client: str, now: float) -> int:
        for key, entries in list(self._failures.items()):
            while entries and entries[0] <= now - self.failure_window:
                entries.popleft()
            if not entries:
                self._failures.pop(key, None)
        entries = self._failures.get(client)
        if entries and len(entries) >= self.failure_limit:
            return max(1, math.ceil(entries[0] + self.failure_window - now))
        return 0

    def _failed(self, client: str, now: float):
        if client not in self._failures:
            while len(self._failures) >= self.failure_clients_limit:
                self._failures.popitem(last=False)
            self._failures[client] = deque()
        self._failures[client].append(now)
        self._failures.move_to_end(client)

    async def login(self, request: Request):
        if not self.enabled or not self.password:
            return private_error(503, "管理登录尚未配置")
        if not self.same_origin(request):
            return private_error(403, "请求来源不符合要求")
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return private_error(400, "登录请求格式不正确")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 4096:
                return private_error(413, "登录请求过大")
            body.extend(chunk)
        try:
            payload = json.loads(body)
            password = payload.get("password") if isinstance(payload, dict) else None
            if not isinstance(password, str) or not 1 <= len(password) <= 1024:
                return private_error(400, "登录请求格式不正确")
            candidate = hashlib.sha256(password.encode("utf-8")).digest()
        except (ValueError, UnicodeError):
            return private_error(400, "登录请求格式不正确")
        client = self._client(request)
        with self._lock:
            now = self._clock()
            retry = self._retry_after(client, now)
            if retry:
                return private_error(429, "登录尝试过于频繁，请稍后再试", {"Retry-After": str(retry)})
            if not hmac.compare_digest(candidate, self._fingerprint()):
                self._failed(client, now)
                return private_error(401, "管理密码不正确")
            self._failures.pop(client, None)
            self.revoke(request)
            self._prune_sessions(now)
            while len(self._sessions) >= self.session_limit:
                self._sessions.popitem(last=False)
            token = secrets.token_urlsafe(32)
            session = AdminSession(secrets.token_urlsafe(32), now + self.ttl, self._fingerprint())
            self._sessions[hashlib.sha256(token.encode("ascii")).hexdigest()] = session
        response = JSONResponse({"ok": True, "csrf_token": session.csrf_token}, headers=_PRIVATE_HEADERS)
        response.set_cookie(COOKIE_NAME, token, max_age=self.ttl, path="/",
                            secure=self.secure_cookie(request), httponly=True, samesite="strict")
        return response


def private_error(status: int, message: str, headers=None):
    return JSONResponse({"detail": message}, status_code=status,
                        headers={**_PRIVATE_HEADERS, **(headers or {})})


class AdminLoginMiddleware:
    def __init__(self, app, login: AdminLogin):
        self.app = app
        self.login = login

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path = scope.get("path", "")
        management = (path in ("/", "/admin", "/redoc", "/openapi.json", "/docs")
                      or path.startswith("/admin/") or path.startswith("/docs/")
                      or path.startswith("/redoc/"))
        login_route = path == "/admin/login" and request.method in ("GET", "POST", "HEAD")
        private_response = management
        if self.login.enabled:
            if management and not login_route and request.method != "OPTIONS":
                if not self.login.password:
                    await private_error(503, "管理登录尚未配置")(scope, receive, send)
                    return
                session = self.login.session(request)
                if not session:
                    if path in ("/", "/admin", "/admin/", "/docs", "/redoc"):
                        response = RedirectResponse("/admin/login", status_code=303, headers=_PRIVATE_HEADERS)
                    else:
                        response = private_error(401, "请先登录管理端")
                    await response(scope, receive, send)
                    return
                if request.method not in _SAFE_METHODS and not self.login.csrf_ok(request, session):
                    await private_error(403, "登录校验已失效，请刷新页面后重试")(scope, receive, send)
                    return
                scope["muse_admin_session"] = session
            elif (path.startswith("/v1/") and not path.startswith("/v1/media/")
                  and "authorization" not in request.headers):
                session = self.login.session(request)
                if session:
                    if request.method not in _SAFE_METHODS and not self.login.csrf_ok(request, session):
                        await private_error(403, "登录校验已失效，请刷新页面后重试")(scope, receive, send)
                        return
                    scope["muse_admin_session"] = session
                    private_response = True
        if not private_response:
            await self.app(scope, receive, send)
            return
        started = False

        async def private_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = list(message.get("headers", []))
                old_vary = next((value.decode("latin-1") for name, value in headers
                                 if name.lower() == b"vary"), "")
                blocked = {name.lower().encode("ascii") for name in _PRIVATE_HEADERS}
                headers = [(name, value) for name, value in headers if name.lower() not in blocked]
                values = [value.strip() for value in old_vary.split(",") if value.strip()]
                if not any(value.lower() == "cookie" for value in values):
                    values.append("Cookie")
                for name, value in _PRIVATE_HEADERS.items():
                    headers.append((name.lower().encode("ascii"),
                                    (", ".join(values) if name == "Vary" else value).encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, private_send)
        except Exception:
            if started:
                raise
            await private_error(500, "管理请求未能完成，请稍后重试")(scope, receive, send)
