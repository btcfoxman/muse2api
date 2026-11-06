"""Persist generated originals independently of generation and account retries."""
from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import threading

from engine import _sniff_mime, reference_kind
from result_storage import ResultStorageError
from store import _read, _write


class ResultDelivery:
    def __init__(self, cfg, storage, local_url):
        self.cfg, self.storage, self.local_url = cfg, storage, local_url
        self.path = os.path.join(cfg.data_dir, "result_assets.json")
        self._records = _read(self.path, {})
        if not isinstance(self._records, dict):
            raise ValueError("Invalid result asset manifest")
        self._lock = threading.RLock()
        self._files = {}

    def _file_lock(self, name):
        with self._lock:
            return self._files.setdefault(name, threading.RLock())

    def _save(self, name, record):
        with self._lock:
            self._records[name] = copy.deepcopy(record)
            _write(self.path, self._records)

    def original(self, name):
        if not isinstance(name, str) or not name or any(c in name for c in ("/", "\\", "..", "\x00")):
            raise ValueError("非法文件名")
        root = Path(self.cfg.media_dir).resolve()
        candidate = root / name
        if candidate.is_symlink():
            raise ValueError("不允许转存符号链接")
        path = candidate.resolve()
        if path.parent != root or not path.is_file():
            raise FileNotFoundError("原始成品不存在")
        return path

    def lookup(self, name, *, refresh=False):
        if not refresh:
            # Management lists must not wait behind a multi-minute file upload.
            with self._lock:
                record = copy.deepcopy(self._records.get(name))
            if record and record.get("storage") and self.storage.expires_soon(record["storage"]):
                record["url"] = self.local_url(name)
            return record
        with self._file_lock(name):
            with self._lock:
                record = copy.deepcopy(self._records.get(name))
            if not record:
                return None
            metadata = record.get("storage")
            if metadata and self.storage.expires_soon(metadata):
                if refresh:
                    try:
                        url, metadata = self.storage.presign(metadata)
                        record.update(url=url, storage=metadata, storage_status="stored")
                        record.pop("storage_error", None)
                        self._save(name, record)
                        return record
                    except ResultStorageError:
                        # Keep the committed receipt so a later query only renews its URL.
                        record["storage_error"] = "S3 下载链接暂时无法刷新，已保留原始成品，请稍后重试~"
                record["url"] = self.local_url(name)
            return record

    def store_original(self, result):
        name = result["filename"]
        with self._file_lock(name):
            existing = self.lookup(name, refresh=True)
            if existing and existing.get("storage"):
                return existing
            if not self.storage.enabled():
                return {**result, "url": self.local_url(name), "storage_status": "disabled"}
            path = self.original(name)
            with path.open("rb") as source:
                mime = _sniff_mime(source.read(64))
            record = {"filename": name, "size": path.stat().st_size,
                      "kind": reference_kind(mime) or result.get("kind", "image"),
                      "mime": mime, "url": self.local_url(name),
                      "storage_status": "disabled"}
            record["storage_status"] = "uploading"
            self._save(name, record)
            try:
                # Filename is stable for sync/async, historical retries and restarts.
                identity = "media_" + hashlib.sha256(name.encode()).hexdigest()
                url, metadata = self.storage.store_original(path, identity, content_type=mime)
                record.update(url=url, storage=metadata, storage_status="stored")
            except ResultStorageError:
                record.update(storage_status="failed",
                              storage_error="S3 结果转存暂时失败，已保留原始成品，可单独重试转存~")
            self._save(name, record)
            return record
