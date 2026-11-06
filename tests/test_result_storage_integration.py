"""Storage integration uses temporary originals and fake generation, never an account."""
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

from test_result_delivery import FakeStorage, PNG


class StorageIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.TemporaryDirectory(prefix="muse-storage-integration-")
        cls.env = patch.dict(os.environ, {
            "MUSE2API_HOME": cls.home.name, "MUSE2API_PROFILE_ROOT": cls.home.name,
            "MUSE2API_HOME_DIR": cls.home.name, "MUSE2API_KEY": "fixture-business-key",
            "MUSE2API_ADMIN_PASSWORD": "fixture-management-password",
            "MUSE2API_ADMIN_LOGIN_REQUIRED": "1", "MUSE2API_RESULT_S3_ENABLED": "0",
            "MUSE2API_PUBLIC_BASE": "https://muse.example.test",
            "MUSE2API_S3_CENTER_API_BASE": "", "MUSE2API_S3_CENTER_TOKEN": "",
            "MUSE2API_S3_CENTER_BUCKET": "assets", "MUSE2API_S3_CENTER_KEY_PREFIX": "aicontent/ai-muse",
        })
        cls.env.start()
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root))
        for name in ("config", "store", "engine", "cdp", "proxy", "admin_auth", "result_delivery", "result_storage"):
            sys.modules.pop(name, None)
        spec = importlib.util.spec_from_file_location("result_storage_integration_target", root / "app.py")
        cls.target = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.target
        spec.loader.exec_module(cls.target)

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.home.cleanup()

    def setUp(self):
        self.app = self.target
        self.app.store.clear_tasks()
        self.app.ADMIN_LOGIN._sessions.clear()
        self.app.ADMIN_LOGIN._failures.clear()
        self.storage = FakeStorage()
        # FakeStorage is imported before isolated app modules are reloaded.
        # Raise the target's error class so only the storage path handles it.
        def failing_store(path, identity, index=0, content_type=None):
            raise self.app.ResultStorageError("fixture storage unavailable")
        self.failing_store = failing_store
        manifest = Path(self.app.CFG.data_dir) / "result_assets.json"
        manifest.unlink(missing_ok=True)
        delivery = self.app.ResultDelivery(self.app.CFG, self.storage, self.app.media_url)
        self.patches = [patch.object(self.app, "RESULT_STORAGE", self.storage),
                        patch.object(self.app, "RESULT_DELIVERY", delivery)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.name = "integration-" + self._testMethodName + ".png"
        self.path = Path(self.app.CFG.media_dir) / self.name
        self.path.write_bytes(PNG)
        self.result = {"path": str(self.path), "filename": self.name, "size": len(PNG), "kind": "image"}

    def request(self, method, path, body=None, *, bearer=False, cookie=None, csrf=None, origin=None, idempotency=None):
        async def invoke():
            data = json.dumps(body).encode() if body is not None else b""
            headers = [(b"host", b"test:18610"), (b"content-type", b"application/json")]
            if bearer:
                headers.append((b"authorization", b"Bearer fixture-business-key"))
            if idempotency:
                headers.append((b"idempotency-key", idempotency.encode()))
            if cookie:
                headers.append((b"cookie", cookie.encode()))
            if csrf:
                headers.append((b"x-muse-csrf", csrf.encode()))
            if origin:
                headers.append((b"origin", origin.encode()))
            scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                     "http_version": "1.1", "method": method, "scheme": "http", "path": path,
                     "raw_path": path.encode(), "query_string": b"", "root_path": "",
                     "headers": headers, "server": ("test", 18610), "client": ("192.0.2.1", 1)}
            messages, sent, complete = [], False, asyncio.Event()
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
            start = next(m for m in messages if m["type"] == "http.response.start")
            response_headers = {key.decode(): value.decode() for key, value in start["headers"]}
            payload = b"".join(message.get("body", b"") for message in messages)
            return start["status"], response_headers, json.loads(payload) if payload else None
        return asyncio.run(invoke())

    def session(self):
        status, headers, body = self.request("POST", "/admin/login",
            {"password": "fixture-management-password"}, origin="http://test:18610")
        self.assertEqual(status, 200)
        cookies = SimpleCookie()
        cookies.load(headers["set-cookie"])
        name = sys.modules["admin_auth"].COOKIE_NAME
        return {"cookie": name + "=" + cookies[name].value, "csrf": body["csrf_token"],
                "origin": "http://test:18610"}

    def poll(self, kind, tid):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status, _, task = self.request("GET", f"/v1/{kind}/{tid}", bearer=True)
            self.assertEqual(status, 200)
            if task["status"] in ("completed", "failed"):
                return task
            time.sleep(0.01)
        self.fail("fake generation worker did not complete")

    def test_sync_image_returns_s3_original_url_without_additional_generation(self):
        with patch.object(self.app, "_run_generation", return_value=(self.result, "fixture")) as generate:
            status, _, body = self.request("POST", "/v1/images/generations", {"prompt": "fixture"}, bearer=True)
        self.assertEqual(status, 200)
        self.assertTrue(body["data"][0]["url"].startswith("https://files.example.test/"))
        self.assertEqual(body["data"][0]["storage_status"], "stored")
        generate.assert_called_once()
        self.assertEqual(self.storage.calls[0][2], PNG)

    def test_b64_json_keeps_original_bytes_even_with_s3_enabled(self):
        with patch.object(self.app, "_run_generation", return_value=(self.result, "fixture")):
            status, _, body = self.request("POST", "/v1/images/generations",
                {"prompt": "fixture", "response_format": "b64_json"}, bearer=True)
        self.assertEqual(status, 200)
        item = body["data"][0]
        self.assertNotIn("url", item)
        self.assertEqual(base64.b64decode(item["b64_json"]), PNG)

    def test_async_image_poll_uses_s3_url_and_persisted_receipt(self):
        with patch.object(self.app, "_run_generation", return_value=(self.result, "fixture")) as generate:
            status, _, accepted = self.request("POST", "/v1/images/tasks", {"prompt": "fixture"}, bearer=True)
            self.assertEqual(status, 202)
            task = self.poll("images/tasks", accepted["id"])
        self.assertEqual(task["status"], "completed")
        self.assertTrue(task["data"][0]["url"].startswith("https://files.example.test/"))
        self.assertTrue(task["result"]["storage"]["original_bytes"])
        generate.assert_called_once()

    def test_video_poll_uses_s3_url_and_receipt_without_changing_original(self):
        original = b"\x00\x00\x00\x14ftypisom\x00\x00\x00\x00isom\x00\x00\x00\x10mdatfixture!"
        self.path = self.path.with_suffix(".mp4")
        self.path.write_bytes(original)
        result = {"path": str(self.path), "filename": self.path.name, "size": len(original), "kind": "video",
                  "media_metadata": {"width": 1920, "height": 1080, "duration": 6.0, "measured_by": "ffprobe"},
                  "media_metadata_status": "measured"}
        with patch.object(self.app, "_run_generation", return_value=(result, "fixture")) as generate:
            status, _, accepted = self.request("POST", "/v1/videos", {"prompt": "fixture", "duration": 4}, bearer=True)
            self.assertEqual(status, 200)
            task = self.poll("videos", accepted["id"])
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["url"], task["video"]["url"])
        self.assertEqual(task["url"], task["result"]["url"])
        self.assertEqual(task["result"]["bytes"], len(original))
        self.assertEqual(task["result"]["media_metadata"], result["media_metadata"])
        self.assertTrue(task["result"]["storage"]["original_bytes"])
        self.assertEqual(self.path.read_bytes(), original)
        generate.assert_called_once()

    def test_video_submission_idempotency_retains_original_generation(self):
        original = b"\x00\x00\x00\x14ftypisom\x00\x00\x00\x00isom\x00\x00\x00\x10mdatfixture!"
        self.path = self.path.with_suffix(".mp4")
        self.path.write_bytes(original)
        result = {"filename": self.path.name, "size": len(original), "kind": "video"}
        payload = {"model": "muse-video", "prompt": "fixture", "duration": 6}
        with patch.object(self.app, "_run_generation", return_value=(result, "fixture")) as generate:
            status, _, first = self.request("POST", "/v1/videos", payload, bearer=True, idempotency="fixture-unique-video")
            self.assertEqual(status, 200)
            self.poll("videos", first["id"])
            status, _, repeated = self.request("POST", "/v1/videos", payload, bearer=True, idempotency="fixture-unique-video")
            self.assertEqual((status, repeated["id"]), (200, first["id"]))
            status, _, _ = self.request("POST", "/v1/videos", {**payload, "duration": 30}, bearer=True, idempotency="fixture-unique-video")
            self.assertEqual(status, 409)
        generate.assert_called_once()

    def test_video_prompt_preserves_requested_ratio_and_resolution(self):
        for quality, short in (("720p", 720), ("1080p", 1080), ("4k", 2160)):
            for ratio in ("16:9", "9:16", "1:1", "3:4", "4:3"):
                with self.subTest(quality=quality, ratio=ratio):
                    rw, rh = map(int, ratio.split(":"))
                    expected = f"{round(short * rw / min(rw, rh))}x{round(short * rh / min(rw, rh))}"
                    prompt = self.app.build_video_prompt(self.app.VideoRequest(prompt="fixture", aspect_ratio=ratio, resolution=quality, duration=30))
                    self.assertIn(ratio, prompt)
                    self.assertIn(expected, prompt)
                    self.assertIn("30 秒", prompt)
                    if quality != "720p":
                        self.assertNotIn("720x1280", prompt)

    def test_storage_failure_remains_completed_and_does_not_regenerate(self):
        with patch.object(self.storage, "store_original", side_effect=self.failing_store), \
             patch.object(self.app, "_run_generation", return_value=(self.result, "fixture")) as generate:
            status, _, accepted = self.request("POST", "/v1/images/tasks", {"prompt": "fixture"}, bearer=True)
            self.assertEqual(status, 202)
            task = self.poll("images/tasks", accepted["id"])
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["result"]["storage_status"], "failed")
        self.assertEqual(task["data"][0]["url"], self.app.media_url(self.name))
        self.assertEqual(self.path.read_bytes(), PNG)
        generate.assert_called_once()

    def test_historical_transfer_updates_existing_task_and_requires_admin_session(self):
        task = self.app.store.create_task("image", "historical fixture")
        self.app.store.update_task(task["id"], status="completed", url=self.app.media_url(self.name),
            result={"filename": self.name, "size": len(PNG), "kind": "image", "url": self.app.media_url(self.name)})
        payload = {"task_id": task["id"]}
        for method, path, body in (("GET", "/admin/result-storage", None),
                                   ("POST", "/admin/result-storage", {}),
                                   ("POST", "/admin/result-storage/transfer", payload)):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.request(method, path, body, bearer=True)[0], 401)
        credentials = self.session()
        with patch.object(self.app, "_run_generation", side_effect=AssertionError("historical result must not regenerate")):
            status, _, body = self.request("POST", "/admin/result-storage/transfer", payload, **credentials)
            self.assertEqual(status, 200)
            self.assertEqual(body["storage_status"], "stored")
        saved = self.app.store.get_task(task["id"])
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["url"], body["url"])
        self.assertEqual(saved["result"]["storage"], body["storage"])
        self.assertEqual(len(self.app.store.tasks), 1)

    def test_historical_transfer_rejects_invalid_selection_before_storage(self):
        credentials = self.session()
        for body, expected in (({}, 400), ({"filename": self.name, "task_id": "missing"}, 400),
                               ({"filename": "../secret.png"}, 400), ({"filename": "missing.png"}, 404)):
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", "/admin/result-storage/transfer", body, **credentials)[0], expected)
        self.assertEqual(self.storage.calls, [])

    def test_in_progress_poll_cannot_overwrite_a_completing_worker_result(self):
        task = self.app.store.create_task("video", "concurrent fixture")
        partial = {"filename": self.name, "size": len(PNG), "kind": "video"}
        self.app.store.update_task(task["id"], status="processing", result=partial)
        with patch.object(self.app.RESULT_DELIVERY, "lookup", return_value=None) as lookup:
            out = self.app._task_delivery(task, refresh=True)
        self.assertEqual(out["status"], "processing")
        lookup.assert_not_called()

    def test_restart_recovers_saved_original_without_repeating_generation(self):
        task = self.app.store.create_task("image", "interrupted storage fixture")
        self.app.store.update_task(task["id"], status="processing", progress=95,
            result={"filename": self.name, "size": len(PNG), "kind": "image"})
        missing = self.app.store.create_task("video", "interrupted unsaved generation")
        self.app.store.update_task(missing["id"], status="processing")
        async def idle_keepalive():
            return None
        with patch.object(self.app, "_keepalive_loop", side_effect=idle_keepalive), \
             patch.object(self.app, "_run_generation", side_effect=AssertionError("restart must not regenerate")):
            asyncio.run(self.app._startup())
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                recovered = self.app.store.get_task(task["id"])
                if (recovered.get("result") or {}).get("storage_status") == "stored":
                    break
                time.sleep(0.01)
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["result"]["storage_status"], "stored")
        self.assertEqual(self.app.store.get_task(missing["id"])["status"], "failed")
        self.assertEqual(self.storage.calls[0][2], PNG)
        self.assertEqual(self.path.read_bytes(), PNG)


if __name__ == "__main__":
    with patch("requests.sessions.Session.request", side_effect=AssertionError("unexpected network")):
        unittest.main(verbosity=2)
