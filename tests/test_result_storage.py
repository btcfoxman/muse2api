"""Original-byte S3 storage tests; isolated files and fake HTTP, no live calls."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
import result_storage as storage_module
from result_storage import MAX_SINGLE_UPLOAD_BYTES, ResultStorage, ResultStorageError, _upload_headers


class Response:
    def __init__(self, value=None, status=200):
        self.value, self.status_code = value, status
        self.closed = False

    def json(self):
        if isinstance(self.value, Exception):
            raise self.value
        return copy.deepcopy(self.value)

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, server):
        self.server = server
        self.trust_env = True
        self.headers = {"Default": "must be cleared"}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.cookies.set("ambient", "must-not-leak")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def post(self, url, **kwargs):
        return self.server.request(self, "POST", url, kwargs)

    def put(self, url, **kwargs):
        return self.server.request(self, "PUT", url, kwargs)


class Server:
    """Emulates create/PUT/commit/presign while checking credential separation."""
    def __init__(self):
        self.calls = []
        self.responses = {}
        self.upload_id = "du_" + "a" * 32
        self.key = "aicontent/ai-muse/task_123/original-0.webp"
        self.original = b"RIFF\x00\x00\x00\x00WEBPoriginal-exact-bytes\x00\xff"
        self.expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.created_status = "url_issued"
        self.seen_idempotency = []
        self.session_ids = {"POST": set(), "PUT": set()}
        self.grants = []
        self.before_post = None
        self.committed = False
        self.reject_committed_create = False
        self.sessions = []

    def session(self):
        session = FakeSession(self)
        self.sessions.append(session)
        return session

    def request(self, session, method, url, kwargs):
        assert session.trust_env is False
        assert session.headers == {}
        assert not session.cookies
        assert kwargs["allow_redirects"] is False
        self.session_ids[method].add(id(session))
        route = "PUT" if method == "PUT" else url.split("https://storage.example.test")[1]
        saved = {**kwargs}
        if method == "PUT":
            assert "Authorization" not in kwargs["headers"]
            assert "Cookie" not in kwargs["headers"]
            saved["data"] = kwargs["data"].read()
            assert saved["data"] == self.original
        else:
            assert kwargs["headers"]["Authorization"] == "Bearer test-secret-token"
            if self.before_post:
                self.before_post(route)
        self.calls.append((method, route, saved))
        queued = self.responses.get(route)
        if queued:
            value = queued.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        size = len(self.original)
        if route == "/v1/uploads":
            if self.committed and self.reject_committed_create:
                return Response({"error": {"code": "session_conflict"}}, status=409)
            self.seen_idempotency.append(kwargs["headers"]["Idempotency-Key"])
            assert kwargs["json"] == {"bucket": "assets", "key": self.key, "size": size,
                                      "content_type": "image/webp"}
            response = {"id": self.upload_id, "status": self.created_status,
                        "declared_bytes": size, "logical_key": "assets/" + self.key, "expires_at": self.expiry,
                        "upload": {"method": "PUT", "url": "https://object.example.test/private?signature=secret",
                                   "headers": {"Content-Type": ["image/webp"], "x-amz-meta-kind": ["one", "two"]},
                                   "expires_at": self.expiry}}
        elif route == "PUT":
            return Response(status=200)
        elif route.endswith("/complete"):
            self.committed = True
            response = {"id": self.upload_id, "status": "committed", "declared_bytes": size,
                        "logical_key": "assets/" + self.key}
        elif route == "/v1/objects/presign":
            assert kwargs["json"] == {"bucket": "assets", "key": self.key, "method": "GET", "expires_seconds": 0}
            response = {"bucket": "assets", "key": self.key, "size_bytes": size,
                        "request": {"method": "GET", "url": "https://download.example.test/file?signature=public",
                                    "headers": {}, "expires_at": self.expiry}}
            self.grants.append(response)
        else:
            raise AssertionError(f"unexpected fake route {route}")
        return Response(response)


class ResultStorageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="muse-result-storage-test-")
        self.cfg = SimpleNamespace(data_dir=self.temp.name, result_s3_enabled=True,
                                   result_s3_center_api_base="https://storage.example.test",
                                   result_s3_center_token="test-secret-token", result_s3_bucket="assets",
                                   result_s3_key_prefix="aicontent/ai-muse")
        self.storage = ResultStorage(self.cfg)
        self.server = Server()
        self.path = Path(self.temp.name) / "source.wrong-extension"
        self.path.write_bytes(self.server.original)
        self.network = patch("result_storage.requests.Session", side_effect=self.server.session)
        self.network.start()
        self.sleep = patch("result_storage.time.sleep")
        self.sleep.start()

    def tearDown(self):
        self.network.stop()
        self.sleep.stop()
        self.temp.cleanup()

    def transfer(self):
        return self.storage.store_original(self.path, "task_123", content_type="image/webp")

    def metadata(self):
        return {"bucket": "assets", "key": self.server.key,
                "size_bytes": len(self.server.original), "sha256": "preserve-this"}

    def test_exact_original_bytes_and_control_auth_only(self):
        url, metadata = self.transfer()
        self.assertTrue(url.startswith("https://download.example.test/"))
        self.assertEqual(metadata["sha256"], hashlib.sha256(self.server.original).hexdigest())
        self.assertTrue(metadata["original_bytes"])
        self.assertEqual(metadata["content_type"], "image/webp")
        self.assertEqual(metadata["expires_at"], self.server.expiry)
        self.assertEqual([route for _, route, _ in self.server.calls],
                         ["/v1/uploads", "PUT", f"/v1/uploads/{self.server.upload_id}/complete", "/v1/objects/presign"])
        put = self.server.calls[1][2]
        self.assertEqual(put["headers"], {"Content-Type": "image/webp", "x-amz-meta-kind": "one,two"})
        self.assertEqual(put["data"], self.server.original)
        self.assertTrue(self.server.session_ids["POST"].isdisjoint(self.server.session_ids["PUT"]))
        expected = "muse2api:" + hashlib.sha256(
            f"assets:{self.server.key}:{metadata['sha256']}".encode()).hexdigest()
        self.assertEqual(self.server.seen_idempotency, [expected])

    def test_committed_identity_is_reused_without_upload(self):
        self.server.created_status = "committed"
        self.transfer()
        self.assertEqual([route for _, route, _ in self.server.calls], ["/v1/uploads", "/v1/objects/presign"])

    def test_official_create_response_without_logical_key_is_supported(self):
        self.server.responses["/v1/uploads"] = [Response({
            "id": self.server.upload_id, "status": "url_issued", "declared_bytes": len(self.server.original),
            "expires_at": self.server.expiry,
            "upload": {"method": "PUT", "url": "https://object.example.test/file", "headers": {},
                       "expires_at": self.server.expiry}})]
        _, metadata = self.transfer()
        self.assertEqual(metadata["key"], self.server.key)

    def test_repeated_original_has_same_idempotency(self):
        self.transfer()
        self.transfer()
        self.assertEqual(len(self.server.seen_idempotency), 1)
        self.assertEqual(len([route for _, route, _ in self.server.calls if route == "PUT"]), 1)
        self.assertEqual(len([route for _, route, _ in self.server.calls if route == "/v1/objects/presign"]), 2)

    def test_settings_private_persistent_and_secret_never_returned(self):
        public = self.storage.update_settings({"result_s3_key_prefix": "aicontent/custom",
                                              "result_s3_center_token": " replacement-secret "})
        self.assertEqual(public["result_s3_center_token"], "")
        self.assertTrue(public["result_s3_center_token_configured"])
        self.storage.update_settings({"result_s3_center_token": "   "})
        reread = ResultStorage(self.cfg)
        self.assertEqual(reread.settings(False)["result_s3_center_token"], "replacement-secret")
        self.assertEqual(reread.settings()["result_s3_key_prefix"], "aicontent/custom")
        saved_path = Path(self.temp.name) / "result_storage.json"
        saved = json.loads(saved_path.read_text(encoding="utf-8"))
        self.assertNotIn("result_s3_center_token_configured", saved)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(saved_path.stat().st_mode), 0o600)
        self.assertFalse(list(Path(self.temp.name).glob(".result-storage-*")))
        public["result_s3_key_prefix"] = "mutated-copy"
        self.assertEqual(self.storage.settings()["result_s3_key_prefix"], "aicontent/custom")

    def test_disabled_partial_configuration_then_enable_validation(self):
        self.cfg.result_s3_enabled = False
        self.cfg.result_s3_center_api_base = ""
        self.cfg.result_s3_center_token = ""
        disabled = ResultStorage(self.cfg)
        self.assertFalse(disabled.enabled())
        with self.assertRaises(ResultStorageError):
            disabled.update_settings({"result_s3_enabled": True})
        with self.assertRaisesRegex(ResultStorageError, "disabled"):
            disabled.store_original(self.path, "task_123", content_type="image/webp")
        self.assertFalse(self.server.calls)

    def test_invalid_settings_are_atomic_and_do_not_echo_secret(self):
        for values in ({"result_s3_enabled": "true"}, {"result_s3_backend": "boto"},
                       {"result_s3_center_api_base": "https://user:secret@storage.example.test"},
                       {"result_s3_center_api_base": "https://storage.example.test/?secret=present"},
                       {"result_s3_center_token": "secret\r\nvalue"},
                       {"result_s3_bucket": "invalid/path"}, {"result_s3_key_prefix": "a/../b"},
                       {"result_s3_key_prefix": "a//b"}, {"result_s3_bucket": None}):
            with self.subTest(values=values), self.assertRaises(ResultStorageError) as caught:
                self.storage.update_settings(values)
            self.assertNotIn("secret", str(caught.exception))
            self.assertEqual(self.storage.settings(False)["result_s3_center_token"], "test-secret-token")

    def test_file_limits_format_and_identity_rejected_before_network(self):
        empty = Path(self.temp.name) / "empty.webp"
        empty.touch()
        for path, identity, index, mime in ((empty, "task_123", 0, "image/webp"),
                (self.path, "../escape", 0, "image/webp"), (self.path, "task_123", -1, "image/webp"),
                (self.path, "task_123", True, "image/webp"), (self.path, "task_123", 0, "text/html"),
                (Path(self.temp.name), "task_123", 0, "image/webp")):
            with self.subTest(identity=identity, index=index, mime=mime), self.assertRaises(ResultStorageError):
                self.storage.store_original(path, identity, index, mime)
        with patch.object(storage_module, "MAX_SINGLE_UPLOAD_BYTES", len(self.server.original) - 1):
            with self.assertRaises(ResultStorageError):
                self.transfer()
        self.assertEqual(MAX_SINGLE_UPLOAD_BYTES, 256 * 1024 * 1024)
        self.assertFalse(self.server.calls)

    def test_header_lists_and_header_injection_rejected(self):
        self.assertEqual(_upload_headers({"x-custom": ["one", "two"]}), {"x-custom": "one,two"})
        for value in (None, {"bad:name": "value"}, {"bad\r\n": "value"}, {"valid": "value\nnext"},
                      {"valid": []}, {"valid": [1]}, {"valid": " leading"},
                      {"Authorization": "Bearer secret"}, {"Cookie": "secret=value"},
                      {"x-test": "one", "X-Test": "two"}, {"Content-Length": "999"}):
            with self.subTest(value=value), self.assertRaises(ResultStorageError):
                _upload_headers(value, len(self.server.original))

    def test_create_response_identity_status_size_method_and_url_validation(self):
        valid = {"id": self.server.upload_id, "status": "url_issued", "declared_bytes": len(self.server.original),
                 "logical_key": "assets/" + self.server.key, "expires_at": self.server.expiry,
                 "upload": {"method": "PUT", "url": "https://object.example.test/file", "headers": {},
                            "expires_at": self.server.expiry}}
        mutations = [({"id": "invalid"}), ({"status": "unknown"}), ({"declared_bytes": True}),
                     ({"declared_bytes": len(self.server.original) + 1}), ({"logical_key": "other/bucket"}),
                     ({"bucket": "wrong"}), ({"key": "wrong"}), ({"expires_at": "invalid"}),
                     ({"upload": {"method": "POST", "url": "https://object.example.test/file", "headers": {}}}),
                     ({"upload": {"method": "PUT", "url": "http://object.example.test/file", "headers": {}}}),
                     ({"upload": {"method": "PUT", "url": "https://user:secret@object.example.test/file", "headers": {}}}),
                     ({"upload": {"method": "PUT", "url": "https://object.example.test/file\n", "headers": {}}})]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ResultStorageError):
                self.server.responses["/v1/uploads"] = [Response({**valid, **mutation})]
                self.transfer()
        self.assertFalse(any(method == "PUT" for method, _, _ in self.server.calls))

    def test_committed_reuse_requires_matching_identity_and_object(self):
        for extra in ({"id": "bad"}, {"logical_key": "other/key"}, {"declared_bytes": 0}):
            value = {"id": self.server.upload_id, "status": "committed", "declared_bytes": len(self.server.original),
                     "logical_key": "assets/" + self.server.key, **extra}
            with self.subTest(extra=extra), self.assertRaises(ResultStorageError):
                self.server.responses["/v1/uploads"] = [Response(value)]
                self.transfer()
        self.assertFalse(any(method == "PUT" for method, _, _ in self.server.calls))

    def test_complete_response_must_match_upload(self):
        complete = f"/v1/uploads/{self.server.upload_id}/complete"
        self.server.responses[complete] = [Response({"id": "du_" + "b" * 32, "status": "committed",
                                                   "logical_key": "assets/" + self.server.key,
                                                   "declared_bytes": len(self.server.original)})]
        with self.assertRaisesRegex(ResultStorageError, "does not match"):
            self.transfer()
        self.assertFalse(any(route == "/v1/objects/presign" for _, route, _ in self.server.calls))

    def test_presign_rejects_mismatches_nonpublic_grants_and_redirects(self):
        valid = {"bucket": "assets", "key": self.server.key, "size_bytes": len(self.server.original),
                 "request": {"method": "GET", "url": "https://download.example.test/file", "headers": {},
                             "expires_at": self.server.expiry}}
        for mutation in ({"bucket": "wrong"}, {"key": "wrong"}, {"size_bytes": 0},
                         {"request": {"method": "POST", "url": "https://download.example.test/file"}},
                         {"request": {"method": "GET", "url": "http://download.example.test/file"}},
                         {"request": {"method": "GET", "url": "https://download.example.test/file",
                                      "headers": {"Authorization": "Bearer required"}}}):
            with self.subTest(mutation=mutation), self.assertRaises(ResultStorageError):
                self.server.responses["/v1/objects/presign"] = [Response({**valid, **mutation})]
                self.storage.presign(self.metadata())
        for bad in ({**self.metadata(), "bucket": "wrong"}, {**self.metadata(), "key": "prefix/../escape"}):
            with self.assertRaises(ResultStorageError):
                self.storage.presign(bad)

    def test_expiry_renewal_retains_metadata(self):
        for expires_at in ("", "invalid", "2026-01-01T00:00:00", (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()):
            self.assertTrue(self.storage.expires_soon({"expires_at": expires_at}))
        metadata = {**self.metadata(), "expires_at": "invalid"}
        _, renewed = self.storage.presign(metadata)
        self.assertFalse(self.storage.expires_soon(renewed))
        self.assertEqual(renewed["sha256"], "preserve-this")
        self.assertEqual(metadata["expires_at"], "invalid")

    def test_transient_create_retries_are_bounded_with_stable_identity(self):
        self.server.responses["/v1/uploads"] = [Response(status=503), requests.ConnectionError("secret URL")]
        self.transfer()
        creates = [call for call in self.server.calls if call[1] == "/v1/uploads"]
        self.assertEqual(len(creates), 3)
        self.assertEqual(len({call[2]["headers"]["Idempotency-Key"] for call in creates}), 1)

    def test_transport_errors_and_raw_responses_are_not_leaked(self):
        self.server.responses["/v1/uploads"] = [requests.ConnectionError("https://secret?token=leaked")] * 3
        with self.assertRaises(ResultStorageError) as caught:
            self.transfer()
        self.assertEqual(len(self.server.calls), 3)
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("leaked", str(caught.exception))
        self.server.responses["/v1/uploads"] = [Response(ValueError("secret raw response"))]
        with self.assertRaisesRegex(ResultStorageError, "invalid JSON") as caught:
            self.transfer()
        self.assertNotIn("secret", str(caught.exception))

    def test_http_redirect_and_permanent_rejection_are_not_retried(self):
        for status in (301, 302, 307, 400, 401, 403, 409):
            self.server.calls.clear()
            self.server.responses["/v1/uploads"] = [Response({"secret": "raw response"}, status)]
            with self.subTest(status=status), self.assertRaisesRegex(ResultStorageError, f"HTTP {status}"):
                self.transfer()
            self.assertEqual(len(self.server.calls), 1)

    def test_put_retry_reopens_original_stream_and_redirect_never_commits(self):
        self.server.responses["PUT"] = [Response(status=503), requests.ConnectionError("secret signed URL")]
        self.transfer()
        self.assertEqual(len([route for _, route, _ in self.server.calls if route == "PUT"]), 3)
        self.server.calls.clear()
        for receipt in (Path(self.temp.name) / "result_storage_receipts").glob("*.json"):
            receipt.unlink()
        self.server.responses["PUT"] = [Response(status=307)]
        with self.assertRaisesRegex(ResultStorageError, "upload HTTP 307"):
            self.transfer()
        self.assertEqual([route for _, route, _ in self.server.calls], ["/v1/uploads", "PUT"])

    def test_settings_updates_do_not_wait_for_network_operation(self):
        updated = threading.Event()
        def callback(route):
            if route == "/v1/uploads":
                worker = threading.Thread(target=lambda: (self.storage.update_settings({"result_s3_enabled": False}), updated.set()))
                worker.start()
                worker.join(timeout=2)
                self.assertTrue(updated.is_set(), "network must not hold the settings lock")
        self.server.before_post = callback
        self.transfer()
        self.assertFalse(self.storage.enabled())

    def test_restart_after_presign_failure_reuses_durable_committed_receipt(self):
        self.server.reject_committed_create = True
        self.server.responses["/v1/objects/presign"] = [Response(status=503)] * 3
        with self.assertRaisesRegex(ResultStorageError, "presign HTTP 503"):
            self.transfer()
        receipt_path = next((Path(self.temp.name) / "result_storage_receipts").glob("*.json"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["stage"], "committed")
        self.assertNotIn("upload_url", receipt)
        self.assertNotIn("upload_headers", receipt)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(receipt_path.stat().st_mode), 0o600)
        self.storage = ResultStorage(self.cfg)
        _, metadata = self.transfer()
        self.assertEqual(metadata["sha256"], hashlib.sha256(self.server.original).hexdigest())
        self.assertEqual(len([route for _, route, _ in self.server.calls if route == "/v1/uploads"]), 1)
        self.assertEqual(len([route for _, route, _ in self.server.calls if route == "PUT"]), 1)
        self.assertEqual(len([route for _, route, _ in self.server.calls if route.endswith("/complete")]), 1)

    def test_restart_after_uncertain_complete_retries_complete_without_reupload(self):
        complete = f"/v1/uploads/{self.server.upload_id}/complete"
        self.server.responses[complete] = [requests.ConnectionError("response lost")] * 3
        with self.assertRaisesRegex(ResultStorageError, "complete transport failure"):
            self.transfer()
        receipt_path = next((Path(self.temp.name) / "result_storage_receipts").glob("*.json"))
        self.assertEqual(json.loads(receipt_path.read_text(encoding="utf-8"))["stage"], "uploaded")
        self.storage = ResultStorage(self.cfg)
        self.transfer()
        self.assertEqual(len([route for _, route, _ in self.server.calls if route == "/v1/uploads"]), 1)
        self.assertEqual(len([route for _, route, _ in self.server.calls if route == "PUT"]), 1)
        self.assertEqual(len([route for _, route, _ in self.server.calls if route == complete]), 4)

    def test_restart_after_put_failure_reuses_grant_and_rewinds_exact_bytes(self):
        self.server.responses["PUT"] = [requests.ConnectionError("partial upload")] * 3
        with self.assertRaisesRegex(ResultStorageError, "upload transport failure"):
            self.transfer()
        self.storage = ResultStorage(self.cfg)
        self.transfer()
        self.assertEqual(len([route for _, route, _ in self.server.calls if route == "/v1/uploads"]), 1)
        self.assertEqual(len([route for _, route, _ in self.server.calls if route == "PUT"]), 4)

    def test_tampered_receipt_is_not_used_for_another_object(self):
        self.server.responses["PUT"] = [Response(status=403)]
        with self.assertRaises(ResultStorageError):
            self.transfer()
        receipt_path = next((Path(self.temp.name) / "result_storage_receipts").glob("*.json"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["bucket"] = "another-bucket"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        count = len(self.server.calls)
        with self.assertRaisesRegex(ResultStorageError, "receipt does not match"):
            self.transfer()
        self.assertEqual(len(self.server.calls), count)

    def test_invalid_or_expired_presign_expiry_is_not_delivered(self):
        for expiry in ("", "garbage", "2020-01-01T00:00:00+00:00", "2099-01-01T00:00:00"):
            self.server.expiry = expiry
            with self.subTest(expiry=expiry), self.assertRaisesRegex(ResultStorageError, "expiration"):
                self.storage.presign(self.metadata())


if __name__ == "__main__":
    unittest.main(verbosity=2)
