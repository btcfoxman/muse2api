"""Publish original result bytes through the s3-center direct-upload protocol."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests


MAX_SINGLE_UPLOAD_BYTES = 256 * 1024 * 1024
_RETRYABLE_HTTP = {408, 425, 429, 500, 502, 503, 504}
_UPLOAD_ID = re.compile(r"du_[a-f0-9]{32}")
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_CONTENT_TYPES = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
    "image/gif": ".gif", "image/avif": ".avif", "video/mp4": ".mp4",
    "video/quicktime": ".mov", "video/webm": ".webm",
}
_SETTING_FIELDS = (
    "result_s3_enabled", "result_s3_backend", "result_s3_center_api_base",
    "result_s3_center_token", "result_s3_bucket", "result_s3_key_prefix",
)


class ResultStorageError(Exception):
    """A safe storage error containing no credentials or signed URLs."""


# Keep the same exception spelling available to code shared with ijm2api.
ResultTransferError = ResultStorageError


def _integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _url(value: object, *, https_only: bool = True) -> str:
    if not isinstance(value, str) or any(ord(char) < 33 or ord(char) == 127 for char in value):
        raise ResultStorageError("s3-center URL is invalid")
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in ({"https"} if https_only else {"http", "https"})
                 and bool(parsed.hostname) and not parsed.username and not parsed.password
                 and not parsed.fragment and parsed.port != 0)
    except ValueError:
        valid = False
    if not valid:
        raise ResultStorageError("s3-center URL is invalid")
    return value


def _object_key(value: object) -> str:
    if (not isinstance(value, str) or not value or len(value) > 1024
            or value.startswith("/") or "//" in value or "\\" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or any(part in {".", "..", ""} for part in value.split("/"))):
        raise ResultStorageError("s3-center object key is invalid")
    return value


def _future_expiry(value: object) -> bool:
    try:
        expiry = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return expiry.tzinfo is not None and expiry.timestamp() > time.time() + 1
    except (TypeError, ValueError, OverflowError):
        return False


def _write_private_json(path: Path, value: dict, label: str) -> None:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".result-storage-", dir=str(path.parent))
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError:
        raise ResultStorageError(f"s3-center {label} could not be saved") from None
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _upload_headers(value: object, expected_size: int | None = None) -> dict[str, str]:
    """Normalize the direct-upload grant without forwarding application auth."""
    if not isinstance(value, dict):
        raise ResultStorageError("s3-center upload headers are invalid")
    headers: dict[str, str] = {}
    seen: set[str] = set()
    for name, raw in value.items():
        if not isinstance(name, str) or not _HEADER_NAME.fullmatch(name):
            raise ResultStorageError("s3-center upload header name is invalid")
        lowered = name.lower()
        if lowered in seen or lowered in {"authorization", "proxy-authorization", "cookie", "cookie2"}:
            raise ResultStorageError("s3-center upload headers contain forbidden credentials")
        seen.add(lowered)
        if isinstance(raw, str):
            text = raw
        elif isinstance(raw, (list, tuple)) and raw and all(isinstance(item, str) for item in raw):
            text = ",".join(raw)
        else:
            raise ResultStorageError("s3-center upload header value is invalid")
        if (not text or text[:1].isspace()
                or any(ord(char) < 32 or ord(char) == 127 for char in text)):
            raise ResultStorageError("s3-center upload header value is invalid")
        if lowered == "content-length" and _integer(text) != expected_size:
            raise ResultStorageError("s3-center upload content length is invalid")
        headers[name] = text
    return headers


@contextmanager
def _session():
    # No proxy env/netrc, session authorization, cookies, or requests defaults can
    # leak into the separately created presigned-upload connection.
    with requests.Session() as http:
        http.trust_env = False
        http.headers.clear()
        http.cookies.clear()
        yield http


def _post(http, base: str, path: str, token: str, body: dict | None,
          label: str, extra_headers: dict | None = None) -> dict:
    for attempt in range(3):
        response = None
        try:
            http.cookies.clear()
            response = http.post(base + path, json=body,
                                 headers={"Authorization": "Bearer " + token, **(extra_headers or {})},
                                 timeout=(15, 60), allow_redirects=False)
            status = response.status_code
            if status in _RETRYABLE_HTTP and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            if not 200 <= status < 300:
                raise ResultStorageError(f"s3-center {label} HTTP {status}")
            try:
                data = response.json()
            except ValueError:
                raise ResultStorageError(f"s3-center {label} returned invalid JSON") from None
            if not isinstance(data, dict):
                raise ResultStorageError(f"s3-center {label} returned invalid JSON")
            return data
        except requests.RequestException as exc:
            if attempt == 2:
                raise ResultStorageError(f"s3-center {label} transport failure: {type(exc).__name__}") from None
            time.sleep(2 ** attempt)
        finally:
            if response is not None:
                response.close()
    raise ResultStorageError(f"s3-center {label} failed")


class ResultStorage:
    def __init__(self, cfg):
        self._settings_file = Path(cfg.data_dir) / "result_storage.json"
        self._receipts_dir = Path(cfg.data_dir) / "result_storage_receipts"
        self._lock = threading.RLock()
        self._object_locks: dict[str, tuple[threading.Lock, int]] = {}
        self._values = {
            "result_s3_enabled": bool(cfg.result_s3_enabled),
            "result_s3_backend": "s3_center_direct",
            "result_s3_center_api_base": cfg.result_s3_center_api_base,
            "result_s3_center_token": cfg.result_s3_center_token,
            "result_s3_bucket": cfg.result_s3_bucket,
            "result_s3_key_prefix": cfg.result_s3_key_prefix,
        }
        if self._settings_file.exists():
            try:
                saved = json.loads(self._settings_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise ResultStorageError("s3-center saved settings cannot be read") from None
            if not isinstance(saved, dict):
                raise ResultStorageError("s3-center saved settings are invalid")
            self._values.update({key: value for key, value in saved.items() if key in _SETTING_FIELDS})
        self._values = self._validate(self._values)

    @staticmethod
    def _validate(values: dict) -> dict:
        values = dict(values)
        enabled = values["result_s3_enabled"]
        if not isinstance(enabled, bool):
            raise ResultStorageError("s3-center enabled setting must be a boolean")
        if values["result_s3_backend"] != "s3_center_direct":
            raise ResultStorageError("s3-center direct storage backend is required")
        for key in _SETTING_FIELDS[2:]:
            if not isinstance(values[key], str):
                raise ResultStorageError(f"invalid {key}")
            values[key] = values[key].strip()
        base = values["result_s3_center_api_base"].rstrip("/")
        if base:
            _url(base, https_only=False)
            if urlsplit(base).query:
                raise ResultStorageError("s3-center API base must not contain a query")
        token = values["result_s3_center_token"]
        if len(token) > 8192 or any(ord(char) < 32 or ord(char) == 127 for char in token):
            raise ResultStorageError("s3-center token is invalid")
        bucket = values["result_s3_bucket"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}", bucket):
            raise ResultStorageError("s3-center bucket is invalid")
        prefix = values["result_s3_key_prefix"].strip("/")
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/_-]{0,99}", prefix)
                or "//" in prefix):
            raise ResultStorageError("s3-center object key prefix is invalid")
        if enabled and (not base or not token):
            raise ResultStorageError("s3-center API base and token are required when enabled")
        values.update(result_s3_center_api_base=base, result_s3_key_prefix=prefix)
        return values

    def settings(self, public: bool = True) -> dict:
        with self._lock:
            values = dict(self._values)
        if public:
            values["result_s3_center_token_configured"] = bool(values["result_s3_center_token"])
            values["result_s3_center_token"] = ""
        return values

    def update_settings(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ResultStorageError("s3-center settings must be an object")
        with self._lock:
            updates = {key: value for key, value in payload.items() if key in _SETTING_FIELDS}
            if "result_s3_center_token" in updates and updates["result_s3_center_token"] in (None, ""):
                updates.pop("result_s3_center_token")
            if isinstance(updates.get("result_s3_center_token"), str) and not updates["result_s3_center_token"].strip():
                updates.pop("result_s3_center_token")
            candidate = self._validate({**self._values, **updates})
            _write_private_json(self._settings_file, candidate, "settings")
            self._values = candidate
            return self.settings()

    def enabled(self) -> bool:
        return self.settings(public=False)["result_s3_enabled"]

    @contextmanager
    def _object_lock(self, key: str):
        with self._lock:
            lock, count = self._object_locks.get(key, (threading.Lock(), 0))
            self._object_locks[key] = (lock, count + 1)
        try:
            with lock:
                yield
        finally:
            with self._lock:
                _, count = self._object_locks[key]
                if count == 1:
                    del self._object_locks[key]
                else:
                    self._object_locks[key] = (lock, count - 1)

    @staticmethod
    def expires_soon(metadata: dict) -> bool:
        try:
            expiry = datetime.fromisoformat(str(metadata.get("expires_at") or "").replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                return True
            return expiry.timestamp() < datetime.now(timezone.utc).timestamp() + 300
        except (AttributeError, TypeError, ValueError, OverflowError):
            return True

    def _presign(self, metadata: dict, settings: dict) -> tuple[str, dict]:
        bucket = metadata.get("bucket")
        key = _object_key(metadata.get("key"))
        size = _integer(metadata.get("size_bytes"))
        if bucket != settings["result_s3_bucket"] or size is None or not 0 < size <= MAX_SINGLE_UPLOAD_BYTES:
            raise ResultStorageError("s3-center object metadata does not match configuration")
        with _session() as http:
            grant = _post(http, settings["result_s3_center_api_base"], "/v1/objects/presign",
                          settings["result_s3_center_token"],
                          {"bucket": bucket, "key": key, "method": "GET", "expires_seconds": 0}, "presign")
        request = grant.get("request")
        if (grant.get("bucket") != bucket or grant.get("key") != key
                or _integer(grant.get("size_bytes")) != size or not isinstance(request, dict)
                or request.get("method") != "GET"):
            raise ResultStorageError("s3-center presign response does not match object")
        url = _url(request.get("url"))
        # Returned links must work without API authorization, cookies, or headers.
        headers = request.get("headers")
        if headers not in (None, {}):
            raise ResultStorageError("s3-center result URL requires additional headers")
        if not _future_expiry(request.get("expires_at")):
            raise ResultStorageError("s3-center result URL expiration is invalid")
        renewed = {**metadata, "bucket": bucket, "key": key, "size_bytes": size,
                   "expires_at": str(request.get("expires_at") or "")}
        return url, renewed

    def presign(self, metadata: dict) -> tuple[str, dict]:
        settings = self._validate(self.settings(public=False))
        if not settings["result_s3_center_api_base"] or not settings["result_s3_center_token"]:
            raise ResultStorageError("s3-center API base and token are not configured")
        return self._presign(metadata, settings)

    def _receipt(self, path: Path, identity: dict) -> dict | None:
        if not path.exists():
            return None
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ResultStorageError("s3-center upload receipt cannot be read") from None
        if (not isinstance(saved, dict) or any(saved.get(key) != value for key, value in identity.items())
                or saved.get("stage") not in {"url_issued", "uploaded", "committed"}
                or not isinstance(saved.get("upload_id"), str)
                or not _UPLOAD_ID.fullmatch(saved["upload_id"])):
            raise ResultStorageError("s3-center upload receipt does not match object")
        return saved

    @staticmethod
    def _save_receipt(path: Path, receipt: dict) -> None:
        if receipt["stage"] != "url_issued":
            receipt.pop("upload_url", None)
            receipt.pop("upload_headers", None)
        _write_private_json(path, receipt, "upload receipt")

    @staticmethod
    def _complete(http, settings: dict, receipt: dict) -> None:
        upload_id = receipt["upload_id"]
        committed = _post(http, settings["result_s3_center_api_base"],
                          f"/v1/uploads/{upload_id}/complete", settings["result_s3_center_token"],
                          None, "complete")
        if (committed.get("id") != upload_id or committed.get("status") != "committed"
                or committed.get("logical_key") != f"{receipt['bucket']}/{receipt['key']}"
                or _integer(committed.get("declared_bytes")) != receipt["size_bytes"]):
            raise ResultStorageError("s3-center committed object does not match upload")

    def store_original(self, path, identity: str, index: int = 0,
                       content_type: str = "video/mp4") -> tuple[str, dict]:
        settings = self._validate(self.settings(public=False))
        if not settings["result_s3_enabled"]:
            raise ResultStorageError("s3-center result storage is disabled")
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", identity):
            raise ResultStorageError("s3-center result identity is invalid")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 9999:
            raise ResultStorageError("s3-center result index is invalid")
        mime = str(content_type).split(";", 1)[0].strip().lower()
        suffix = _CONTENT_TYPES.get(mime)
        if not suffix:
            raise ResultStorageError("s3-center result format is unsupported")
        source = Path(path)
        try:
            before = source.stat()
            if not source.is_file() or not 0 < before.st_size <= MAX_SINGLE_UPLOAD_BYTES:
                raise ResultStorageError("s3-center result exceeds 256 MiB or is empty")
            digest = hashlib.sha256()
            hashed_size = 0
            with source.open("rb") as contents:
                for chunk in iter(lambda: contents.read(1024 * 1024), b""):
                    hashed_size += len(chunk)
                    if hashed_size > MAX_SINGLE_UPLOAD_BYTES:
                        raise ResultStorageError("s3-center source file changed during transfer")
                    digest.update(chunk)
            if self._file_signature(source) != (before.st_size, before.st_mtime_ns):
                raise ResultStorageError("s3-center source file changed during transfer")
        except OSError:
            raise ResultStorageError("s3-center source result cannot be read") from None
        size = before.st_size
        sha = digest.hexdigest()
        bucket = settings["result_s3_bucket"]
        key = f"{settings['result_s3_key_prefix']}/{identity}/original-{index}{suffix}"
        idempotency = "muse2api:" + hashlib.sha256(f"{bucket}:{key}:{sha}".encode()).hexdigest()
        identity_fields = {"api_base": settings["result_s3_center_api_base"], "bucket": bucket,
                           "key": key, "size_bytes": size, "sha256": sha, "content_type": mime}
        receipt_name = hashlib.sha256(json.dumps(identity_fields, sort_keys=True).encode()).hexdigest()
        receipt_path = self._receipts_dir / f"{receipt_name}.json"
        with self._object_lock(f"{bucket}/{key}"), _session() as http:
            receipt = self._receipt(receipt_path, identity_fields)
            if receipt is None:
                created = _post(http, settings["result_s3_center_api_base"], "/v1/uploads",
                                settings["result_s3_center_token"],
                                {"bucket": bucket, "key": key, "size": size, "content_type": mime},
                                "create", {"Idempotency-Key": idempotency})
                upload_id = created.get("id")
                if not isinstance(upload_id, str) or not _UPLOAD_ID.fullmatch(upload_id):
                    raise ResultStorageError("s3-center create response has invalid upload identity")
                if (_integer(created.get("declared_bytes")) != size
                        or created.get("logical_key") not in (None, f"{bucket}/{key}")
                        or created.get("bucket") not in (None, bucket) or created.get("key") not in (None, key)):
                    raise ResultStorageError("s3-center create response does not match object")
                upload = created.get("upload")
                # Newer s3-center versions may return a committed identity. The
                # local receipt is what guarantees recovery on current servers,
                # which instead return 409 on create after a completed upload.
                if created.get("status") == "committed" and created.get("logical_key") == f"{bucket}/{key}":
                    receipt = {**identity_fields, "upload_id": upload_id, "stage": "committed"}
                elif created.get("status") == "url_issued" and isinstance(upload, dict) and upload.get("method") == "PUT":
                    upload_url = _url(upload.get("url"))
                    headers = _upload_headers(upload.get("headers"), size)
                    if not _future_expiry(upload.get("expires_at")) or not _future_expiry(created.get("expires_at")):
                        raise ResultStorageError("s3-center upload expiration is invalid")
                    receipt = {**identity_fields, "upload_id": upload_id, "stage": "url_issued",
                               "upload_url": upload_url, "upload_headers": headers,
                               "upload_expires_at": str(upload.get("expires_at") or ""),
                               "session_expires_at": str(created.get("expires_at") or "")}
                else:
                    raise ResultStorageError("s3-center create response is invalid")
                self._save_receipt(receipt_path, receipt)
            if receipt["stage"] == "url_issued":
                upload_url = _url(receipt.get("upload_url"))
                headers = _upload_headers(receipt.get("upload_headers"), size)
                # A partial PUT can safely be overwritten with the exact same
                # bytes. An expired grant may have been auto-committed by the
                # server reaper; try idempotent complete first in that case.
                if receipt.get("upload_expires_at") and not _future_expiry(receipt["upload_expires_at"]):
                    self._complete(http, settings, receipt)
                    receipt["stage"] = "committed"
                else:
                    self._upload(source, upload_url, headers, (size, before.st_mtime_ns))
                    receipt["stage"] = "uploaded"
                self._save_receipt(receipt_path, receipt)
            if receipt["stage"] == "uploaded":
                self._complete(http, settings, receipt)
                receipt["stage"] = "committed"
                # Persist before presign: retries and process restarts must not
                # repeat create/PUT after a successful object commitment.
                self._save_receipt(receipt_path, receipt)
            upload_id = receipt["upload_id"]
        return self._presign({"bucket": bucket, "key": key, "size_bytes": size,
                              "upload_id": upload_id, "sha256": sha, "original_bytes": True,
                              "content_type": mime}, settings)

    @staticmethod
    def _file_signature(path: Path) -> tuple[int, int]:
        info = path.stat()
        return info.st_size, info.st_mtime_ns

    def _upload(self, source: Path, url: str, headers: dict, signature: tuple[int, int]) -> None:
        with _session() as upload_http:
            for attempt in range(3):
                response = None
                try:
                    if self._file_signature(source) != signature:
                        raise ResultStorageError("s3-center source file changed during transfer")
                    upload_http.cookies.clear()
                    with source.open("rb") as contents:
                        response = upload_http.put(url, headers=headers, data=contents,
                                                   timeout=(30, 300), allow_redirects=False)
                    if self._file_signature(source) != signature:
                        raise ResultStorageError("s3-center source file changed during transfer")
                    if 200 <= response.status_code < 300:
                        return
                    if response.status_code not in _RETRYABLE_HTTP or attempt == 2:
                        raise ResultStorageError(f"s3-center upload HTTP {response.status_code}")
                except requests.RequestException as exc:
                    if attempt == 2:
                        raise ResultStorageError(f"s3-center upload transport failure: {type(exc).__name__}") from None
                except OSError:
                    raise ResultStorageError("s3-center source result cannot be read") from None
                finally:
                    if response is not None:
                        response.close()
                time.sleep(2 ** attempt)
