"""Run: python tests/test_media_response.py; temporary media, no browser/network/accounts."""
import asyncio
import base64
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


class MediaResponseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.TemporaryDirectory(prefix="muse-media-response-test-")
        cls.env = patch.dict(os.environ, {
            "MUSE2API_HOME": cls.home.name, "MUSE2API_PROFILE_ROOT": cls.home.name,
            "MUSE2API_KEY": "test-only",
        })
        cls.env.start()
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root))
        for name in ("config", "store", "engine", "cdp", "proxy"):
            sys.modules.pop(name, None)
        spec = importlib.util.spec_from_file_location("media_response_target", root / "app.py")
        cls.app = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.app
        spec.loader.exec_module(cls.app)
        webp = base64.b64decode("UklGRhoAAABXRUJQVlA4TA0AAAAvAAAAEAcQERGIiP4HAA==")
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
        # File delivery needs only the ISO BMFF signature, not a playable video fixture.
        mp4 = b"\x00\x00\x00\x14ftypisom\x00\x00\x00\x00isom\x00\x00\x00\x10mdatfixture!"
        cls.fixtures = {
            "generated.webp": (webp, "image/webp"),
            "generated.png": (png, "image/png"),
            "generated.mp4": (mp4, "video/mp4"),
            "renamed.png": (webp, "image/webp"),
            "unknown.webp": (b"unrecognized content must not be advertised as an image", "application/octet-stream"),
        }
        for name, (data, _mime) in cls.fixtures.items():
            (Path(cls.app.CFG.media_dir) / name).write_bytes(data)

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.home.cleanup()

    def request(self, name, headers=None, asgi_app=None):
        async def send_request():
            path = "/v1/media/" + name
            scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                     "method": "GET", "scheme": "http", "path": path,
                     "raw_path": path.encode(), "query_string": b"", "root_path": "",
                     "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
                     "server": ("test", 80), "client": ("test", 1)}
            messages = []
            request_sent = False
            response_completed = asyncio.Event()

            async def receive():
                nonlocal request_sent
                if not request_sent:
                    request_sent = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                await response_completed.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                messages.append(message)
                if message["type"] == "http.response.body" and not message.get("more_body", False):
                    response_completed.set()

            await (asgi_app or self.app.app)(scope, receive, send)
            start = next(m for m in messages if m["type"] == "http.response.start")
            response_headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
            return start["status"], response_headers, b"".join(m.get("body", b"") for m in messages)

        return asyncio.run(send_request())

    def test_transport_waits_for_final_body_before_disconnect(self):
        async def listening_app(scope, receive, send):
            self.assertEqual((await receive())["type"], "http.request")

            async def listen_for_disconnect():
                # A framework may concurrently listen while streaming the response.
                message = await receive()
                self.assertEqual(message["type"], "http.disconnect", "request body must not be repeated")

            listener = asyncio.create_task(listen_for_disconnect())
            try:
                await asyncio.sleep(0)
                self.assertFalse(listener.done(), "disconnect must wait for the response")
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"first", "more_body": True})
                await asyncio.sleep(0)
                self.assertFalse(listener.done(), "partial response must not signal disconnect")
                await send({"type": "http.response.body", "body": b"last", "more_body": False})
                await asyncio.wait_for(listener, timeout=1)
            finally:
                if not listener.done():
                    listener.cancel()
                await asyncio.gather(listener, return_exceptions=True)

        status, _headers, body = self.request("transport-fixture", asgi_app=listening_app)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"firstlast")

    def test_public_delivery_uses_magic_without_system_mime_database(self):
        with patch("starlette.responses.guess_type", return_value=(None, None)):
            for name, (data, mime) in self.fixtures.items():
                with self.subTest(name=name):
                    status, headers, body = self.request(name)
                    self.assertEqual(status, 200)
                    self.assertEqual(headers["content-type"], mime)
                    self.assertEqual(int(headers["content-length"]), len(data))
                    self.assertEqual(body, data)

    def test_range_delivery_preserves_headers_and_exact_bytes(self):
        for name in ("generated.webp", "generated.png", "generated.mp4"):
            with self.subTest(name=name):
                data, mime = self.fixtures[name]
                status, headers, body = self.request(name, {"Range": "bytes=3-17"})
                self.assertEqual(status, 206)
                self.assertEqual(headers["content-type"], mime)
                self.assertEqual(headers["accept-ranges"], "bytes")
                self.assertEqual(headers["content-range"], f"bytes 3-17/{len(data)}")
                self.assertEqual(body, data[3:18])

    def test_missing_and_unsafe_paths_remain_rejected_before_sniffing(self):
        with patch.object(self.app, "_sniff_mime") as sniff:
            self.assertEqual(self.request("missing.webp")[0], 404)
            for name in ("..", "..\\outside.webp", "../outside.webp"):
                with self.subTest(name=name), self.assertRaises(self.app.HTTPException) as error:
                    self.app.get_media(name)
                self.assertEqual(error.exception.status_code, 400)
            sniff.assert_not_called()


if __name__ == "__main__":
    with patch("requests.sessions.Session.request", side_effect=AssertionError("unexpected network")):
        unittest.main(verbosity=2)
