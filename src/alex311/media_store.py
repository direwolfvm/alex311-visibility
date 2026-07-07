"""Media byte storage: local directory for dev, GCS bucket for prod.

Selected via env:
  MEDIA_BUCKET=<bucket name>  -> GCS (requires google-cloud-storage)
  MEDIA_DIR=<path>            -> local filesystem (default ./media_store)
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Protocol


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name or "file")


def object_name(service_request_id: str, media_id: str, file_name: str | None) -> str:
    return f"{service_request_id}/{media_id}_{_safe_name(file_name or 'file')}"


class MediaStore(Protocol):
    def put(self, name: str, data: bytes, content_type: str) -> str: ...
    def get(self, name: str) -> bytes: ...
    def exists(self, name: str) -> bool: ...
    def delete(self, name: str) -> None: ...


class LocalMediaStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, name: str, data: bytes, content_type: str) -> str:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return name

    def get(self, name: str) -> bytes:
        return (self.root / name).read_bytes()

    def exists(self, name: str) -> bool:
        return (self.root / name).exists()

    def delete(self, name: str) -> None:
        (self.root / name).unlink(missing_ok=True)

    def open_path(self, name: str) -> Path:
        return self.root / name


class GcsMediaStore:
    def __init__(self, bucket_name: str, prefix: str = "alex311-media"):
        from google.cloud import storage  # lazy: only needed in prod

        self._client = storage.Client()
        self._bucket = self._client.bucket(bucket_name)
        self.prefix = prefix.strip("/")

    def _blob(self, name: str):
        return self._bucket.blob(f"{self.prefix}/{name}")

    def put(self, name: str, data: bytes, content_type: str) -> str:
        blob = self._blob(name)
        blob.upload_from_string(data, content_type=content_type or "application/octet-stream")
        return name

    def exists(self, name: str) -> bool:
        return self._blob(name).exists()

    def get(self, name: str) -> bytes:
        return self._blob(name).download_as_bytes()

    def delete(self, name: str) -> None:
        blob = self._blob(name)
        if blob.exists():
            blob.delete()


def store_from_env() -> MediaStore:
    bucket = os.environ.get("MEDIA_BUCKET")
    if bucket:
        return GcsMediaStore(bucket, os.environ.get("MEDIA_PREFIX", "alex311-media"))
    return LocalMediaStore(os.environ.get("MEDIA_DIR", "./media_store"))
