"""Offline management login regressions; isolated config/media, no real accounts."""
import asyncio
import base64
from http.cookies import SimpleCookie
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


PASSWORD = "fixture-management-password"
API_KEY = "fixture-business-key"
LAN_HOST = "192.168.3.5:18610"
PUBLIC_HOST = "muse2api.example.test"


class AdminLoginTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.TemporaryDirectory(prefix="muse-admin-login-")
        cls.env = patch.dict(os.environ, {
            "MUSE2API_HOME": cls.home.name,
            "MUSE2API_PROFILE_ROOT": cls.home.name,
            "MUSE2API_HOME_DIR": cls.home.name,
            "MUSE2API_KEY": API_KEY,
            "MUSE2API_ADMIN_PASSWORD": PASSWORD,
            "MUSE2API_ADMIN_LOGIN_REQUIRED": "1",
            "MUSE2API_PUBLIC_BASE": "https://" + PUBLIC_HOST,
        })
        cls.env.start()
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root))
        for name in ("config", "store", "engine", "cdp", "proxy", "admin_auth"):
            sys.modules.pop(name, None)
        spec = importlib.util.spec_from_file_location("admin_login_target", root / "app.py")
        cls.app = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.app
        spec.loader.exec_module(cls.app)
        cls.cookie_name = sys.modules["admin_auth"].COOKIE_NAME
        cls.webp = base64.b64decode("UklGRhoAAABXRUJQVlA4TA0AAAAvAAAAEAcQERGIiP4HAA==")
        (Path(cls.app.CFG.media_dir) / "public.webp").write_bytes(cls.webp)

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.home.cleanup()

    def setUp(self):
        login = self.app.ADMIN_LOGIN
        login._sessions.clear()
        login._failures.clear()
        login._clock = time.monotonic
        self.config = patch.multiple(self.app.CFG, admin_password=PASSWORD,
                                     admin_login_required=True, admin_session_ttl=28800,
                                     public_base="https://" + PUBLIC_HOST)
        self.config.start()
        self.addCleanup(self.config.stop)

    def request(self, method, path, body=None, *, cookie=None, bearer=None,
                origin=None, csrf=None, headers=None, host=LAN_HOST, scheme="http",
                client="192.0.2.1", content_type="application/json", raw_body=None):
        async def invoke():
            data = raw_body if raw_body is not None else (json.dumps(body).encode() if body is not None else b"")
            fields = [(b"host", host.encode()), (b"content-type", content_type.encode())]
            if cookie:
                fields.append((b"cookie", cookie.encode()))
            if bearer is not None:
                fields.append((b"authorization", ("Bearer " + bearer).encode()))
            if origin is not None:
                fields.append((b"origin", origin.encode()))
            if csrf is not None:
                fields.append((b"x-muse-csrf", csrf.encode()))
            fields.extend((name.lower().encode(), value.encode()) for name, value in (headers or []))
            scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                     "http_version": "1.1", "method": method, "scheme": scheme,
                     "path": path, "raw_path": path.encode(), "query_string": b"",
                     "root_path": "", "headers": fields, "server": ("test", 18610),
                     "client": (client, 12000)}
            messages = []
            sent = False
            complete = asyncio.Event()

            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": data, "more_body": False}
                await complete.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                messages.append(message)
                if message["type"] == "http.response.body" and not message.get("more_body", False):
                    complete.set()

            await self.app.app(scope, receive, send)
            start = next(message for message in messages if message["type"] == "http.response.start")
            result_headers = {name.decode().lower(): value.decode() for name, value in start["headers"]}
            content = b"".join(message.get("body", b"") for message in messages)
            return start["status"], result_headers, content

        return asyncio.run(invoke())

    def login(self, password=PASSWORD, **kwargs):
        host = kwargs.get("host", LAN_HOST)
        scheme = kwargs.get("scheme", "http")
        origin = kwargs.pop("origin", ("https://" if host == PUBLIC_HOST else scheme + "://") + host)
        return self.request("POST", "/admin/login", {"password": password}, origin=origin, **kwargs)

    def credentials(self, **kwargs):
        status, headers, body = self.login(**kwargs)
        self.assertEqual(status, 200)
        cookie = SimpleCookie()
        cookie.load(headers["set-cookie"])
        token = cookie[self.cookie_name].value
        return self.cookie_name + "=" + token, json.loads(body)["csrf_token"], headers

    def assert_private(self, headers):
        self.assertEqual(headers["cache-control"], "private, no-store")
        self.assertIn("cookie", headers["vary"].lower())
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertEqual(headers["referrer-policy"], "no-referrer")

    def test_unauthenticated_admin_and_bearer_do_not_bypass_login(self):
        pages = ("/", "/admin", "/admin/", "/docs", "/redoc")
        apis = ("/admin/accounts", "/admin/status", "/admin/tasks", "/admin/apikey",
                "/admin/session", "/admin/extension", "/admin/extension/files",
                "/admin/cookie-helper", "/admin/update/check", "/openapi.json",
                "/docs/oauth2-redirect", "/admin/login/")
        for path in pages + apis:
            for bearer in (None, API_KEY):
                with self.subTest(path=path, bearer=bool(bearer)):
                    status, headers, body = self.request("GET", path, bearer=bearer)
                    self.assertEqual(status, 303 if path in pages else 401)
                    self.assert_private(headers)
                    self.assertNotIn(PASSWORD.encode(), body)
                    self.assertNotIn(API_KEY.encode(), body)
                    if status == 303:
                        self.assertEqual(headers["location"], "/admin/login")

    def test_login_session_cookie_is_opaque_and_admin_uses_session(self):
        status, headers, body = self.login("wrong-fixture")
        self.assertEqual(status, 401)
        self.assert_private(headers)
        self.assertNotIn("set-cookie", headers)
        cookie, csrf, headers = self.credentials()
        self.assertIn("HttpOnly", headers["set-cookie"])
        self.assertIn("SameSite=strict", headers["set-cookie"])
        self.assertIn("Path=/", headers["set-cookie"])
        self.assertNotIn("Secure", headers["set-cookie"])
        self.assertNotIn(PASSWORD, cookie)
        self.assertNotIn(API_KEY, cookie)
        self.assert_private(headers)
        status, headers, body = self.request("GET", "/admin/session", cookie=cookie, bearer="wrong-key")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"authenticated": True, "csrf_token": csrf})
        self.assertNotIn(cookie.split("=", 1)[1].encode(), body)
        self.assert_private(headers)
        status, headers, body = self.request("GET", "/admin", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn(b'data-admin-login-required="true"', body)
        self.assert_private(headers)
        for path in ("/admin/accounts", "/admin/status", "/openapi.json", "/docs"):
            self.assertEqual(self.request("GET", path, cookie=cookie)[0], 200)
        status, headers, _body = self.request("GET", "/admin/login", cookie=cookie)
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/admin")

    def test_login_json_body_and_exact_origin_are_required_without_echo(self):
        cases = [dict(origin=None), dict(origin="https://evil.example"), dict(origin="null"),
                 dict(origin="http://" + LAN_HOST + "/"),
                 dict(origin="http://" + LAN_HOST, headers=[("Origin", "http://" + LAN_HOST)])]
        for args in cases:
            with self.subTest(args=args):
                status, headers, body = self.request("POST", "/admin/login", {"password": PASSWORD}, **args)
                self.assertEqual(status, 403)
                self.assert_private(headers)
                self.assertNotIn(PASSWORD.encode(), body)
        for raw, mime, expected in ((b"not json", "application/json", 400),
                                    (b"password=fixture", "application/x-www-form-urlencoded", 400),
                                    (b"x" * 5000, "application/json", 413),
                                    (b'{"password":12}', "application/json", 400)):
            status, headers, body = self.request("POST", "/admin/login", raw_body=raw,
                content_type=mime, origin="http://" + LAN_HOST)
            self.assertEqual(status, expected)
            self.assert_private(headers)
            self.assertNotIn(raw, body)

    def test_unsafe_admin_cookie_requires_origin_and_csrf_and_logout_revokes(self):
        cookie, csrf, _headers = self.credentials()
        for args in ({}, {"csrf": csrf}, {"origin": "http://" + LAN_HOST},
                     {"origin": "http://" + LAN_HOST, "csrf": "wrong"},
                     {"origin": "https://evil.example", "csrf": csrf}):
            with self.subTest(args=args):
                status, headers, _body = self.request("POST", "/admin/logout", cookie=cookie, **args)
                self.assertEqual(status, 403)
                self.assert_private(headers)
        status, headers, body = self.request("POST", "/admin/logout", cookie=cookie,
                                             csrf=csrf, origin="http://" + LAN_HOST)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertIn("Max-Age=0", headers["set-cookie"])
        self.assert_private(headers)
        self.assertEqual(self.request("GET", "/admin/session", cookie=cookie)[0], 401)
        self.assertEqual(self.request("GET", "/admin", cookie=cookie)[0], 303)

    def test_expired_tampered_and_password_changed_sessions_are_rejected(self):
        cookie, _csrf, _headers = self.credentials()
        self.assertEqual(self.request("GET", "/admin/session", cookie=cookie + "x")[0], 401)
        self.assertEqual(self.request("GET", "/admin/session", cookie=self.cookie_name + "=invalid")[0], 401)
        now = self.app.ADMIN_LOGIN._clock()
        with patch.object(self.app.ADMIN_LOGIN, "_clock", return_value=now + 28801):
            self.assertEqual(self.request("GET", "/admin/session", cookie=cookie)[0], 401)
        cookie, _csrf, _headers = self.credentials()
        with patch.object(self.app.CFG, "admin_password", "changed-fixture-password"):
            self.assertEqual(self.request("GET", "/admin/session", cookie=cookie)[0], 401)

    def test_api_bearer_and_cookie_ui_flows_remain_separate(self):
        cookie, csrf, _headers = self.credentials()
        self.assertTrue(self.app.auth("Bearer " + API_KEY))
        self.assertEqual(self.request("GET", "/v1/models")[0], 401)
        self.assertEqual(self.request("GET", "/v1/models", bearer=API_KEY)[0], 200)
        self.assertEqual(self.request("GET", "/v1/models", cookie=cookie)[0], 200)
        self.assertEqual(self.request("GET", "/v1/models", cookie=cookie, bearer="wrong-key")[0], 401)
        self.assertEqual(self.request("GET", "/v1/models", cookie=cookie,
                                      headers=[("Authorization", "")])[0], 401)
        response = self.app.JSONResponse({"id": "fixture"}, status_code=202)
        with patch.object(self.app, "_queue_image", return_value=response) as queue:
            for args in ({}, {"csrf": csrf}, {"origin": "http://" + LAN_HOST},
                         {"origin": "https://evil.example", "csrf": csrf}):
                self.assertEqual(self.request("POST", "/v1/images/tasks", {"prompt": "fixture"},
                                              cookie=cookie, **args)[0], 403)
            queue.assert_not_called()
            self.assertEqual(self.request("POST", "/v1/images/tasks", {"prompt": "fixture"},
                cookie=cookie, csrf=csrf, origin="http://" + LAN_HOST)[0], 202)
            self.assertEqual(self.request("POST", "/v1/images/tasks", {"prompt": "fixture"},
                                          bearer=API_KEY)[0], 202)
            self.assertEqual(queue.call_count, 2)

    def test_public_https_origin_and_http_lan_do_not_trust_forwarded_headers(self):
        _cookie, _csrf, headers = self.credentials(host=PUBLIC_HOST, scheme="http")
        self.assertIn("Secure", headers["set-cookie"])
        status, headers, _body = self.login(host=LAN_HOST, origin="http://" + LAN_HOST,
            headers=[("X-Forwarded-Proto", "https"), ("X-Forwarded-Host", PUBLIC_HOST)])
        self.assertEqual(status, 200)
        self.assertNotIn("Secure", headers["set-cookie"])
        self.assertEqual(self.login(host=LAN_HOST, origin="https://" + PUBLIC_HOST,
            headers=[("X-Forwarded-Proto", "https"), ("X-Forwarded-Host", PUBLIC_HOST)])[0], 403)
        self.assertEqual(self.login(host=PUBLIC_HOST, origin="http://" + PUBLIC_HOST)[0], 403)
        self.assertEqual(self.login(host=PUBLIC_HOST, origin="https://" + PUBLIC_HOST + ":0")[0], 403)

    def test_rate_limit_is_bounded_and_ignores_spoofed_client_ip_headers(self):
        for attempt in range(5):
            self.assertEqual(self.login("wrong-fixture", headers=[
                ("X-Forwarded-For", "198.51.100." + str(attempt)),
                ("CF-Connecting-IP", "198.51.100." + str(attempt))])[0], 401)
        status, headers, _body = self.login()
        self.assertEqual(status, 429)
        self.assertGreater(int(headers["retry-after"]), 0)
        now = self.app.ADMIN_LOGIN._clock()
        with patch.object(self.app.ADMIN_LOGIN, "_clock", return_value=now + 901):
            self.assertEqual(self.login()[0], 200)
        self.app.ADMIN_LOGIN._failures.clear()
        with patch.object(self.app.ADMIN_LOGIN, "failure_clients_limit", 3):
            for index in range(4):
                self.assertEqual(self.login("wrong-fixture", client="192.0.2." + str(index))[0], 401)
            self.assertEqual(len(self.app.ADMIN_LOGIN._failures), 3)
        with patch.object(self.app.ADMIN_LOGIN, "session_limit", 2):
            cookies = [self.credentials(client="203.0.113." + str(index))[0] for index in range(3)]
            self.assertEqual(len(self.app.ADMIN_LOGIN._sessions), 2)
            self.assertEqual(self.request("GET", "/admin/session", cookie=cookies[0])[0], 401)
            self.assertEqual(self.request("GET", "/admin/session", cookie=cookies[-1])[0], 200)

    def test_missing_password_fails_closed_and_legacy_mode_is_compatible(self):
        with patch.object(self.app.CFG, "admin_password", ""):
            self.assertEqual(self.request("GET", "/admin", bearer=API_KEY)[0], 503)
            self.assertEqual(self.request("GET", "/admin/accounts", bearer=API_KEY)[0], 503)
            self.assertEqual(self.login()[0], 503)
            self.assertEqual(self.request("GET", "/healthz")[0], 200)
            self.assertEqual(self.request("GET", "/v1/models", bearer=API_KEY)[0], 200)
            with patch.object(self.app.CFG, "admin_login_required", False):
                self.assertEqual(self.request("GET", "/admin")[0], 200)
                self.assertEqual(self.request("GET", "/admin/accounts")[0], 401)
                self.assertEqual(self.request("GET", "/admin/accounts", bearer=API_KEY)[0], 200)
        status, _headers, body = self.request("GET", "/readyz")
        self.assertEqual(status, 200)
        self.assertEqual(set(json.loads(body)), {"status"})
        status, headers, body = self.request("GET", "/v1/media/public.webp")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "image/webp")
        self.assertEqual(body, self.webp)


if __name__ == "__main__":
    with patch("requests.sessions.Session.request", side_effect=AssertionError("unexpected network")):
        unittest.main(verbosity=2)
