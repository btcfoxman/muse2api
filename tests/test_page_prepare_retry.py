"""Single-account, no-network regressions for strictly pre-submit page retries."""
import copy
from contextlib import ExitStack
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MODULE_HOME = tempfile.TemporaryDirectory(prefix="muse-prepare-module-")
os.environ.update(MUSE2API_HOME=MODULE_HOME.name, MUSE2API_PROFILE_ROOT=MODULE_HOME.name,
                  MUSE2API_KEY="offline-prepare-fixture")
spec = importlib.util.spec_from_file_location("prepare_retry_candidate", ROOT / "app.py")
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
spec.loader.exec_module(app)
import engine as engine_module
from store import Store


class Clock:
    def __init__(self):
        self.now = 0.0
    def monotonic(self):
        return self.now
    def time(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


class PrepareRetryTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory(prefix="muse-prepare-case-")
        folder = Path(self.home.name)
        self.store = Store(SimpleNamespace(accounts_file=str(folder / "accounts.json"),
                                           tasks_file=str(folder / "tasks.json")))
        self.account = self.store.add_account({"hatch_sess": "same-fixture-cookie"}, "fixture")
        self.account["ok"] = True
        self.account["proxy"] = {"scheme": "http", "host": "proxy.example.test", "port": 8080}
        self.pool = app.AccountPoolManager(self.store)
        self.engine = engine_module.MuseEngine(SimpleNamespace(profile_dir=self.home.name,
            media_dir=self.home.name, login_wait=0))
        self.clock = Clock()
        self.patches = ExitStack()
        for target, value in (("store", self.store), ("ACCOUNT_POOL", self.pool),
                              ("engine", self.engine), ("time", self.clock)):
            self.patches.enter_context(patch.object(app, target, value))
        self.patches.enter_context(patch.object(engine_module, "time", self.clock))
        self.renew = self.patches.enter_context(patch.object(app, "_renew_and_persist",
            side_effect=lambda ident, **kwargs: self.store.get_account(ident)))
        self.sync = self.patches.enter_context(patch.object(app, "_sync_cookies"))
        self.start = self.patches.enter_context(patch.object(self.engine, "start"))
        self.reset = self.patches.enter_context(patch.object(self.engine, "reset_thread"))
        self.patches.enter_context(patch.object(self.engine, "_scroll_bottom"))
        self.patches.enter_context(patch.object(self.engine, "_clear_attachments"))
        self.upload = self.patches.enter_context(patch.object(self.engine, "_attach_images", return_value=None))
        self.patches.enter_context(patch.object(self.engine, "attachments", return_value=[]))
        self.patches.enter_context(patch.object(self.engine, "_agent_count", return_value=0))
        self.send = self.patches.enter_context(patch.object(self.engine, "_send", return_value="clicked"))
        self.wait = self.patches.enter_context(patch.object(self.engine, "_wait_attachment", return_value={
            "src": "blob:fixture-result", "tid": "image", "w": 64, "h": 64}))
        self.patches.enter_context(patch.object(self.engine, "extract_bytes",
            return_value=(b"\x89PNG\r\n\x1a\nfixture", "image/png", "blob:fixture-result")))
        self.acquire = self.patches.enter_context(patch.object(self.pool, "acquire", wraps=self.pool.acquire))
        self.release = self.patches.enter_context(patch.object(self.pool, "release", wraps=self.pool.release))
        self.generate = self.patches.enter_context(patch.object(self.engine, "generate", wraps=self.engine.generate))

    def tearDown(self):
        self.patches.close()
        self.home.cleanup()

    def run_generation(self, **kwargs):
        return app._run_generation_locked("Exact fixture prompt", "image", 30,
                                          account_id=self.account["id"], **kwargs)

    def test_first_prepare_failure_retries_same_lease_and_real_send_occurs_once(self):
        before = copy.deepcopy(self.account["cookies"])
        attempts = []
        progress = Mock()
        def prepare(cookies, expires=None, account_id=None, **options):
            self.assertIn(account_id, self.pool.busy_accounts)
            self.assertIs(cookies, self.account["cookies"])
            self.assertEqual(self.engine._ensure_local().proxy_snapshot, self.account["proxy"])
            attempts.append(options.get("skip_session_renew", False))
            if len(attempts) == 1:
                self.clock.now += 6
                raise app.MusePageNotReadyError("fixture: no composer before submit")
        with patch.object(self.engine, "ensure_page", side_effect=prepare):
            result, account_id = self.run_generation(reference_images=["same-reference"], on_progress=progress)
        self.assertEqual(account_id, self.account["id"])
        self.assertEqual(Path(result["path"]).read_bytes(), b"\x89PNG\r\n\x1a\nfixture")
        self.assertEqual(attempts, [False, True])
        self.assertEqual([call.kwargs["timeout"] for call in self.generate.call_args_list], [30, 24])
        self.assertEqual([call.kwargs["reference_image"] for call in self.generate.call_args_list],
                         ["same-reference", "same-reference"])
        self.assertTrue(all(call.kwargs["on_progress"] is progress for call in self.generate.call_args_list))
        self.send.assert_called_once_with("Exact fixture prompt")
        self.upload.assert_called_once_with(["same-reference"])
        self.renew.assert_called_once_with(self.account["id"], wake_vm=True, force=False)
        self.start.assert_called_once()
        self.acquire.assert_called_once()
        self.release.assert_called_once_with(self.account["id"])
        self.assertEqual(self.account["cookies"], before)
        self.assertTrue(self.account["ok"])
        self.assertFalse(self.pool.busy_accounts)

    def test_second_prepare_failure_is_terminal_even_when_another_account_exists(self):
        self.store.add_account({"hatch_sess": "other-fixture"}, "other")
        with patch.object(self.engine, "ensure_page", side_effect=app.MusePageNotReadyError("fixture not ready")):
            with self.assertRaises(app.MusePageNotReadyError):
                self.run_generation()
        self.assertEqual(self.generate.call_count, 2)
        self.send.assert_not_called()
        self.upload.assert_not_called()
        self.reset.assert_not_called()
        self.acquire.assert_called_once()
        self.release.assert_called_once_with(self.account["id"])
        self.renew.assert_called_once()
        self.assertTrue(self.account["ok"])
        self.assertFalse(self.pool.busy_accounts)

    def test_exhausted_original_deadline_does_not_start_a_second_prepare(self):
        def consume_budget(*args, **kwargs):
            self.clock.now = 31
            raise app.MusePageNotReadyError("fixture not ready")
        with patch.object(self.engine, "ensure_page", side_effect=consume_budget):
            with self.assertRaises(app.MusePageNotReadyError):
                self.run_generation(deadline=30)
        self.generate.assert_called_once()
        self.send.assert_not_called()
        self.assertFalse(self.pool.busy_accounts)

    def test_post_submit_errors_never_gain_a_same_account_retry(self):
        for error in (app.MuseGenerationError("after-send timeout"),
                      app.MuseModelRefusalError("after-send refusal"),
                      RuntimeError("after-send outcome unknown")):
            with self.subTest(error=type(error).__name__):
                self.generate.reset_mock()
                self.send.reset_mock()
                with (patch.object(self.engine, "ensure_page"),
                      patch.object(self.engine, "_wait_attachment", side_effect=error)):
                    with self.assertRaises(app.MuseGenerationError):
                        self.run_generation()
                self.generate.assert_called_once()
                self.send.assert_called_once()
                self.assertFalse(self.pool.busy_accounts)

    def test_only_final_missing_composer_fallback_has_the_retryable_subtype(self):
        for body, expected in (("loading workspace", app.MusePageNotReadyError),
                               ("Sign in", app.MuseAuthError),
                               ("Connecting...", app.MuseGenerationError),
                               ("ERR_PROXY_CONNECTION_FAILED", app.MuseProxyError)):
            with self.subTest(body=body):
                page = SimpleNamespace(js=lambda script: body, send=Mock(), close=Mock())
                self.engine._ensure_local().proxy_snapshot = self.account["proxy"]
                with (patch.object(self.engine, "_open_page", return_value=page),
                      patch.object(self.engine, "_apply_cookies"),
                      patch.object(self.engine, "_wait_ws_ready", return_value=False),
                      patch.object(self.engine, "renew_session_http") as implicit_renew):
                    with self.assertRaises(expected) as failure:
                        self.engine.ensure_page(self.account["cookies"], account_id=self.account["id"],
                                                skip_session_renew=True)
                self.assertIs(type(failure.exception), expected)
                implicit_renew.assert_not_called()
                self.assertIsNone(self.engine.page)
        self.send.assert_not_called()
        self.upload.assert_not_called()


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        MODULE_HOME.cleanup()
