"""Offline result delivery: originals survive storage failures and restarts."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from result_delivery import ResultDelivery, storage_failure_facts
from result_storage import ResultStorageError


PNG = bytes.fromhex("89504e470d0a1a0a") + b"original-generated-image"


class FakeStorage:
    def __init__(self):
        self.active = True
        self.calls = []
        self.renewals = 0
        self.fail = False
        self.stale = False

    def enabled(self):
        return self.active

    def expires_soon(self, metadata):
        return self.stale

    def store_original(self, path, identity, index=0, content_type=None):
        self.calls.append((identity, content_type, Path(path).read_bytes()))
        if self.fail:
            raise ResultStorageError("fixture transport unavailable")
        time.sleep(0.01)
        return "https://files.example.test/original.png?signed=fixture", {
            "bucket": "assets", "key": "aicontent/ai-muse/fixture/original-0.png",
            "size_bytes": Path(path).stat().st_size,
            "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            "original_bytes": True, "expires_at": "2099-01-01T00:00:00Z",
        }

    def presign(self, metadata):
        self.renewals += 1
        if self.fail:
            raise ResultStorageError("fixture presign unavailable")
        self.stale = False
        return "https://files.example.test/renewed.png?signed=fixture", dict(metadata)


class ResultDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory(prefix="muse-result-delivery-")
        self.addCleanup(self.home.cleanup)
        self.root = Path(self.home.name)
        self.media = self.root / "media"
        self.media.mkdir()
        self.cfg = SimpleNamespace(data_dir=str(self.root), media_dir=str(self.media))
        self.storage = FakeStorage()
        self.local = lambda name: "/v1/media/" + name
        self.delivery = ResultDelivery(self.cfg, self.storage, self.local)
        self.filename = "generated-output.png"
        self.file = self.media / self.filename
        self.file.write_bytes(PNG)
        self.result = {"filename": self.filename, "size": len(PNG), "kind": "image"}

    def test_original_bytes_persisted_receipt_and_filename_dedup_after_restart(self):
        first = self.delivery.store_original(self.result)
        self.assertEqual(first["storage_status"], "stored")
        self.assertEqual(first["storage"]["sha256"], hashlib.sha256(PNG).hexdigest())
        self.assertEqual(self.storage.calls[0][0], "media_" + hashlib.sha256(self.filename.encode()).hexdigest())
        self.assertEqual(self.storage.calls[0][1:], ("image/png", PNG))
        restarted = ResultDelivery(self.cfg, self.storage, self.local)
        self.assertEqual(restarted.store_original(self.result), first)
        self.assertEqual(len(self.storage.calls), 1)
        self.assertEqual(self.file.read_bytes(), PNG)
        manifest = json.loads(Path(self.delivery.path).read_text(encoding="utf-8"))
        self.assertEqual(manifest[self.filename]["storage"]["original_bytes"], True)
        if os.name != "nt":
            self.assertEqual(Path(self.delivery.path).stat().st_mode & 0o777, 0o600)

    def test_expired_stored_link_only_renews_presign_and_retains_original(self):
        first = self.delivery.store_original(self.result)
        self.storage.stale = True
        fallback = self.delivery.lookup(self.filename)
        self.assertEqual(fallback["url"], self.local(self.filename))
        self.assertEqual(self.storage.renewals, 0)
        renewed = self.delivery.lookup(self.filename, refresh=True)
        self.assertIn("/renewed.png", renewed["url"])
        self.assertEqual(self.storage.renewals, 1)
        self.assertEqual(len(self.storage.calls), 1)
        self.assertEqual(renewed["storage"]["sha256"], first["storage"]["sha256"])

    def test_failure_retains_local_original_and_retry_publishes_only_that_file(self):
        self.storage.fail = True
        result = self.delivery.store_original(self.result)
        self.assertEqual(result["storage_status"], "failed")
        self.assertEqual(result["url"], self.local(self.filename))
        self.assertEqual(self.file.read_bytes(), PNG)
        self.storage.fail = False
        result = self.delivery.store_original(self.result)
        self.assertEqual(result["storage_status"], "stored")
        self.assertEqual(len(self.storage.calls), 2)
        self.assertTrue(all(call[2] == PNG for call in self.storage.calls))

    def test_presign_outage_never_reuploads_committed_original(self):
        first = self.delivery.store_original(self.result)
        self.storage.stale = self.storage.fail = True
        result = self.delivery.store_original(self.result)
        self.assertEqual(result["url"], self.local(self.filename))
        self.assertEqual(result["storage"], first["storage"])
        self.assertIn("storage_error", result)
        self.assertEqual(len(self.storage.calls), 1)
        restarted = ResultDelivery(self.cfg, self.storage, self.local)
        self.assertEqual(restarted.lookup(self.filename)["storage"], first["storage"])
        self.assertIn("storage_failure", restarted.lookup(self.filename))
        self.storage.fail = False
        renewed = restarted.lookup(self.filename, refresh=True)
        self.assertEqual(renewed["storage_status"], "stored")
        self.assertNotIn("storage_failure", renewed)
        self.assertEqual(len(self.storage.calls), 1)

    def test_actual_storage_failure_stage_is_saved_without_raw_error(self):
        error = ResultStorageError("s3-center presign HTTP 503")
        with patch.object(self.storage, "store_original", side_effect=error), self.assertLogs("muse2api", level="WARNING") as logs:
            result = self.delivery.store_original(self.result)
        facts = result["storage_failure"]
        self.assertEqual((facts["stage"], facts["code"], facts["http_status"]), ("presign", "HTTP_ERROR", 503))
        self.assertEqual(facts["asset_sha256"], hashlib.sha256(self.filename.encode()).hexdigest())
        self.assertNotIn(self.filename, "".join(logs.output))
        saved = json.loads(Path(self.delivery.path).read_text(encoding="utf-8"))[self.filename]
        self.assertEqual(saved["storage_failure"], facts)
        self.assertEqual(self.file.read_bytes(), PNG)

    def test_storage_diagnostics_allow_only_fixed_types_and_never_raw_request_data(self):
        for message, expected in (("s3-center presign transport failure: ReadTimeout", "TRANSPORT_ERROR"),
                                  ("s3-center presign response does not match object", "INVALID_RESPONSE"),
                                  ("s3-center upload receipt does not match object", "IDENTITY_MISMATCH"),
                                  ("https://user:PRIVATE@objects.example.test/a?signature=PRIVATE", "STORAGE_ERROR")):
            with self.subTest(expected=expected), self.assertLogs("muse2api", level="WARNING") as logs:
                facts = storage_failure_facts(ResultStorageError(message), self.filename)
            self.assertEqual(facts["code"], expected)
            self.assertNotIn("PRIVATE", json.dumps(facts) + "".join(logs.output))
            self.assertNotIn("objects.example.test", json.dumps(facts) + "".join(logs.output))

    def test_simultaneous_historical_and_live_transfers_share_one_upload(self):
        barrier = threading.Barrier(5)
        def transfer(_):
            barrier.wait(timeout=3)
            return self.delivery.store_original(self.result)
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(transfer, range(5)))
        self.assertEqual(len(self.storage.calls), 1)
        self.assertTrue(all(item["url"] == results[0]["url"] for item in results))

    def test_admin_snapshot_does_not_wait_for_an_in_progress_upload(self):
        started, release = threading.Event(), threading.Event()
        original_upload = self.storage.store_original
        def slow_upload(*args, **kwargs):
            started.set()
            if not release.wait(timeout=5):
                raise AssertionError("test upload was not released")
            return original_upload(*args, **kwargs)
        with patch.object(self.storage, "store_original", side_effect=slow_upload), \
             concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            transfer = pool.submit(self.delivery.store_original, self.result)
            try:
                self.assertTrue(started.wait(timeout=3))
                snapshot = pool.submit(self.delivery.lookup, self.filename, refresh=False).result(timeout=2)
                self.assertEqual(snapshot["storage_status"], "uploading")
                self.assertEqual(snapshot["url"], self.local(self.filename))
                self.assertFalse(transfer.done())
                download = pool.submit(self.delivery.lookup, self.filename, refresh=True, blocking=False).result(timeout=2)
                self.assertEqual(download["storage_status"], "uploading")
                self.assertEqual(download["url"], self.local(self.filename))
                self.assertFalse(transfer.done(), "public lookup must not wait for the original upload")
            finally:
                release.set()
            self.assertEqual(transfer.result(timeout=3)["storage_status"], "stored")

    def test_disabled_storage_keeps_existing_local_delivery(self):
        self.storage.active = False
        result = self.delivery.store_original(self.result)
        self.assertEqual(result["storage_status"], "disabled")
        self.assertEqual(result["url"], self.local(self.filename))
        self.assertEqual(self.storage.calls, [])

    def test_public_refresh_misses_do_not_allocate_persistent_file_locks(self):
        for index in range(512):
            self.assertIsNone(self.delivery.lookup("missing-" + str(index), refresh=True, blocking=False))
        self.assertEqual(self.delivery._files, {})
        self.assertEqual(self.storage.calls, [])

    def test_filename_and_symlink_cannot_publish_an_unselected_file(self):
        for name in ("", "..", "../outside.png", "..\\outside.png", "bad\x00.png", None, 5):
            with self.subTest(name=name), self.assertRaises((ValueError, FileNotFoundError)):
                self.delivery.original(name)
        with self.assertRaises(FileNotFoundError):
            self.delivery.original("missing.png")
        link = self.media / "alias.png"
        try:
            link.symlink_to(self.file)
        except OSError:
            self.skipTest("symlinks unavailable to current Windows user")
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.delivery.original(link.name)
        self.assertEqual(self.storage.calls, [])


if __name__ == "__main__":
    with patch("requests.sessions.Session.request", side_effect=AssertionError("unexpected network")):
        unittest.main(verbosity=2)
