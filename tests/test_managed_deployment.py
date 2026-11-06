"""Run: python tests/test_managed_deployment.py; no live accounts/network/browser."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class ManagedDeploymentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.TemporaryDirectory(prefix="muse-managed-test-")
        cls.env = patch.dict(os.environ, {
            "MUSE2API_HOME": cls.home.name,
            "MUSE2API_PROFILE_ROOT": cls.home.name,
            "MUSE2API_KEY": "test-only",
            "MUSE2API_MANAGED_DEPLOYMENT": "true",
        })
        cls.env.start()
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root))
        for name in ("config", "store", "engine", "cdp", "proxy"):
            sys.modules.pop(name, None)
        spec = importlib.util.spec_from_file_location("managed_deployment_target", root / "app.py")
        cls.target = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.target
        spec.loader.exec_module(cls.target)

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.home.cleanup()

    def request(self, method, path, payload=None, authenticated=True):
        async def send_request():
            body = json.dumps(payload or {}).encode()
            headers = [(b"content-type", b"application/json")]
            if authenticated:
                headers.append((b"authorization", b"Bearer test-only"))
            scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                     "method": method, "scheme": "http", "path": path,
                     "raw_path": path.encode(), "query_string": b"", "root_path": "",
                     "headers": headers, "server": ("test", 80), "client": ("test", 1)}
            messages = []
            sent = False

            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                await asyncio.Event().wait()

            async def send(message):
                messages.append(message)

            await self.target.app(scope, receive, send)
            status = next(m["status"] for m in messages if m["type"] == "http.response.start")
            content = b"".join(m.get("body", b"") for m in messages).decode()
            return status, content

        return asyncio.run(send_request())

    def test_managed_routes_reject_before_side_effects(self):
        app = self.target
        with patch.object(app.CFG, "managed_deployment", True), \
             patch.object(app, "_upgrade_from_github_sync") as upgrade, \
             patch.object(app, "_ensure_git_repo") as repo, \
             patch.object(app, "_persist_env") as persist, \
             patch.object(app, "_git") as git, \
             patch.object(app.os, "_exit") as restart:
            for path in ("/admin/update/upgrade", "/admin/repo/pull", "/admin/repo/push"):
                with self.subTest(path=path):
                    status, body = self.request("POST", path, {"github_token": "fixture-token"})
                    self.assertEqual(status, 403)
                    self.assertIn("GitHub Actions", json.loads(body)["detail"])
                    self.assertEqual(self.request("POST", path, authenticated=False)[0], 401)
            for operation in (upgrade, repo, persist, git, restart):
                operation.assert_not_called()
        with patch.object(app.CFG, "managed_deployment", True):
            for operation in (app._upgrade_from_github_sync, app._ensure_git_repo):
                with self.assertRaises(app.HTTPException) as error:
                    operation()
                self.assertEqual(error.exception.status_code, 403)
        self.assertFalse((Path(self.home.name) / ".env").exists())

    def test_managed_check_is_read_only_and_page_hides_writes(self):
        app = self.target
        version = {"version": "1.6.7"}
        responses = [SimpleNamespace(status_code=200, json=lambda: version),
                     SimpleNamespace(status_code=200, json=lambda: [{
                         "sha": "1234567", "commit": {"message": "fixture", "committer": {}}
                     }])]
        with patch.object(app.CFG, "managed_deployment", True), \
             patch.object(app, "BASE_DIR", self.home.name), \
             patch.object(app, "_read_local_version", return_value=version), \
             patch("requests.get", side_effect=responses), \
             patch.object(app, "_git", side_effect=AssertionError("unexpected git")):
            status, body = self.request("GET", "/admin/update/check")
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["managed_deployment"])
            self.assertFalse(Path(app._installed_sha_file()).exists())
        with patch.object(app.CFG, "managed_deployment", True):
            for path in ("/", "/admin"):
                status, page = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertIn('data-managed-deployment="true"', page)
                self.assertNotIn("__MANAGED_DEPLOYMENT__", page)
            self.assertTrue(app.admin_status()["config"]["managed_deployment"])

    def test_default_development_routes_remain_available(self):
        app = self.target
        from config import Config
        with patch.dict(os.environ, {"MUSE2API_MANAGED_DEPLOYMENT": "false"}):
            self.assertFalse(Config().managed_deployment)
        with patch.dict(os.environ, {"MUSE2API_MANAGED_DEPLOYMENT": " YES "}):
            self.assertTrue(Config().managed_deployment)
        with patch.object(app.CFG, "managed_deployment", False), \
             patch.object(app, "_upgrade_from_github_sync", return_value={"ok": True}) as upgrade, \
             patch.object(app, "_ensure_git_repo") as repo, \
             patch.object(app, "_git", return_value=SimpleNamespace(stdout="", stderr="", returncode=0)), \
             patch.object(app, "_check_update_sync", return_value={}):
            for path in ("/admin/update/upgrade", "/admin/repo/pull"):
                self.assertEqual(self.request("POST", path, {"restart": False})[0], 200)
            self.assertEqual(upgrade.call_count, 2)
            self.assertEqual(self.request("POST", "/admin/repo/push")[0], 200)
            repo.assert_called_once()
            self.assertIn('data-managed-deployment="false"', app._admin_html())


if __name__ == "__main__":
    with patch("requests.sessions.Session.request", side_effect=AssertionError("unexpected network")):
        unittest.main()
