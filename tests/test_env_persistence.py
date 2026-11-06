"""Run: python tests/test_env_persistence.py; temporary config only, no live accounts."""
import errno
import importlib.util
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch


class EnvPersistenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.TemporaryDirectory(prefix="muse-env-import-test-")
        cls.env = patch.dict(os.environ, {
            "MUSE2API_HOME": cls.home.name,
            "MUSE2API_PROFILE_ROOT": cls.home.name,
            "MUSE2API_KEY": "test-only",
        })
        cls.env.start()
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root))
        for name in ("config", "store", "engine", "cdp", "proxy"):
            sys.modules.pop(name, None)
        spec = importlib.util.spec_from_file_location("env_persistence_target", root / "app.py")
        cls.app = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.app
        spec.loader.exec_module(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.home.cleanup()

    def setUp(self):
        home = tempfile.TemporaryDirectory(prefix="muse-env-write-test-")
        self.addCleanup(home.cleanup)
        cfg = patch.object(self.app.CFG, "base_dir", home.name)
        cfg.start()
        self.addCleanup(cfg.stop)
        self.path = Path(home.name) / ".env"
        self.original = "# preserve this comment\nOTHER=value\nMUSE2API_KEY=" + "old" * 100 + "\n"
        self.path.write_text(self.original, encoding="utf-8")

    def assert_no_temporary_files(self):
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_ordinary_file_still_uses_atomic_replace(self):
        with patch.object(self.app.os, "replace", wraps=os.replace) as replace:
            self.app._persist_env("ADDED", "fixture")
        replace.assert_called_once()
        self.assertEqual(replace.call_args.args[1], str(self.path))
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.original + "ADDED=fixture\n")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assert_no_temporary_files()

    def test_busy_bind_mount_preserves_other_config_and_truncates_old_key(self):
        inode = self.path.stat().st_ino
        with patch.object(self.app.os, "replace", side_effect=OSError(errno.EBUSY, "fixture bind mount")), \
             patch.object(self.app.os, "chmod", wraps=os.chmod) as chmod, \
             patch.object(self.app.os, "fsync", wraps=os.fsync) as fsync:
            self.app._persist_env("MUSE2API_KEY", "short-new-key")
        self.assertEqual(self.path.read_text(encoding="utf-8"),
                         "# preserve this comment\nOTHER=value\nMUSE2API_KEY=short-new-key\n")
        self.assertEqual(self.path.stat().st_ino, inode)
        chmod.assert_called_once_with(str(self.path), 0o600)
        self.assertEqual(fsync.call_count, 2, "both the staged and mounted file must be synced")
        self.assert_no_temporary_files()

    def test_rotation_failure_keeps_current_key_and_authentication(self):
        with patch.object(self.app.CFG, "api_key", "test-only"), \
             patch.object(self.app, "_persist_env", side_effect=OSError(errno.ENOSPC, "fixture full disk")):
            with self.assertRaises(OSError):
                self.app.rotate_apikey()
            self.assertEqual(self.app.CFG.api_key, "test-only")
            self.assertTrue(self.app.auth("Bearer test-only"))
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.original)

    def test_busy_fallback_failure_leaves_config_and_key_unchanged(self):
        with patch.object(self.app.CFG, "api_key", "test-only"), \
             patch.object(self.app.os, "replace", side_effect=OSError(errno.EBUSY, "fixture bind mount")), \
             patch.object(self.app.os, "chmod", side_effect=PermissionError("fixture permission denied")):
            with self.assertRaises(PermissionError):
                self.app.rotate_apikey()
            self.assertEqual(self.app.CFG.api_key, "test-only")
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.original)
        self.assert_no_temporary_files()


if __name__ == "__main__":
    with patch("requests.sessions.Session.request", side_effect=AssertionError("unexpected network")):
        unittest.main()
