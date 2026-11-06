"""Run: python tests/test_browser_start.py; mocked Chromium/CDP, temporary data only."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class ProbeLock:
    """Observe whether the second caller must wait for Chromium initialization."""
    def __init__(self):
        self.lock = threading.Lock()
        self.second_attempt = threading.Event()
        self.second_blocked = None

    def __enter__(self):
        if threading.current_thread().name == "second-start":
            acquired = self.lock.acquire(blocking=False)
            self.second_blocked = not acquired
            self.second_attempt.set()
            if not acquired:
                self.lock.acquire()
        else:
            self.lock.acquire()

    def __exit__(self, *_args):
        self.lock.release()


class BrowserStartTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory(prefix="muse-start-test-")
        self.addCleanup(self.home.cleanup)
        env = patch.dict(os.environ, {"MUSE2API_HOME": self.home.name,
                                     "MUSE2API_PROFILE_ROOT": self.home.name,
                                     "MUSE2API_KEY": "test-only"})
        env.start()
        self.addCleanup(env.stop)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import engine
        self.module = engine
        self.engine = engine.MuseEngine(SimpleNamespace(
            profile_dir=self.home.name, data_dir=self.home.name, home_dir=self.home.name,
            chromium="fixture-chromium", cdp_port=19210, extra_path=""))
        self.addCleanup(lambda: self.engine._log and self.engine._log.close())

    def test_simultaneous_first_requests_launch_one_browser(self):
        first_spawn = threading.Event()
        finish_spawn = threading.Event()
        lock = ProbeLock()
        self.engine._start_lock = lock
        proc = SimpleNamespace(poll=lambda: None)
        browser = object()

        def spawn(*_args, **_kwargs):
            first_spawn.set()
            self.assertTrue(finish_spawn.wait(3), "test did not release Chromium startup")
            return proc

        def debug_version(*_args, **_kwargs):
            if not self.engine.proc:
                raise ConnectionError("no existing browser")
            return {"webSocketDebuggerUrl": "ws://fixture"}

        def second_start():
            threading.current_thread().name = "second-start"
            self.engine.start()

        with patch.object(self.module.subprocess, "Popen", side_effect=spawn) as launch, \
             patch.object(self.module, "http_json", side_effect=debug_version), \
             patch.object(self.module, "CDP", return_value=browser) as cdp, \
             ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.engine.start)
            try:
                self.assertTrue(first_spawn.wait(3), "first caller never launched Chromium")
                second = executor.submit(second_start)
                self.assertTrue(lock.second_attempt.wait(3), "second caller never attempted startup")
                self.assertTrue(lock.second_blocked, "second caller bypassed startup serialization")
            finally:
                finish_spawn.set()
            first.result(timeout=3)
            second.result(timeout=3)
            launch.assert_called_once()
            cdp.assert_called_once()
            self.assertIs(self.engine.browser, browser)

    def test_existing_cdp_is_reused_without_spawning(self):
        browser = object()
        with patch.object(self.module, "http_json", return_value={"webSocketDebuggerUrl": "ws://fixture"}), \
             patch.object(self.module, "CDP", return_value=browser), \
             patch.object(self.module.subprocess, "Popen") as launch:
            self.engine.start()
            self.assertIs(self.engine.browser, browser)
            launch.assert_not_called()


if __name__ == "__main__":
    with patch("requests.sessions.Session.request", side_effect=AssertionError("unexpected network")):
        unittest.main()
