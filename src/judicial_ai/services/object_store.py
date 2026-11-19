from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

from ..config import get_settings


class ObjectStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int
    sha256: str
    content_type: str


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _safe_key(key: str) -> str:
    path = PurePosixPath(key)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ObjectStoreError("非法对象键")
    return path.as_posix()


class ObjectStore:
    backend = "unknown"

    def put(self, key: str, content: bytes, content_type: str) -> ObjectInfo:
        raise NotImplementedError

    def get(self, key: str) -> bytes:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        raise NotImplementedError

    def list_keys(self) -> list[str]:
        raise NotImplementedError


class FilesystemObjectStore(ObjectStore):
    backend = "filesystem"

    def __init__(self, root: str | Path | None = None) -> None:
        settings = get_settings()
        self.root = Path(root or settings.object_store_path).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = _safe_key(key)
        target = (self.root / Path(*PurePosixPath(safe).parts)).resolve()
        if target != self.root and self.root not in target.parents:
            raise ObjectStoreError("对象键超出存储根目录")
        return target

    def put(self, key: str, content: bytes, content_type: str) -> ObjectInfo:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_bytes(content)
        temporary.replace(path)
        return ObjectInfo(
            key=_safe_key(key),
            size=len(content),
            sha256=sha256_bytes(content),
            content_type=content_type,
        )

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise ObjectStoreError("对象不存在")
        return path.read_bytes()

    def delete(self, key: str) -> None:
        path = self._path(key)
        path.unlink(missing_ok=True)
        parent = path.parent
        while parent != self.root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def list_keys(self) -> list[str]:
        return sorted(
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file() and not path.name.startswith(".")
        )


class S3ObjectStore(ObjectStore):
    backend = "s3"

    def __init__(self) -> None:
        settings = get_settings()
        try:
            import boto3
        except ImportError as exc:
            raise ObjectStoreError("OBJECT_STORE_BACKEND=s3 需要安装 boto3") from exc
        self.bucket = settings.object_store_bucket
        self.sse = settings.object_store_sse
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.object_store_endpoint_url,
            region_name=settings.object_store_region,
            aws_access_key_id=settings.object_store_access_key,
            aws_secret_access_key=settings.object_store_secret_key,
        )

    def put(self, key: str, content: bytes, content_type: str) -> ObjectInfo:
        safe = _safe_key(key)
        digest = sha256_bytes(content)
        arguments: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": safe,
            "Body": content,
            "ContentType": content_type,
            "Metadata": {"sha256": digest},
        }
        if self.sse:
            arguments["ServerSideEncryption"] = self.sse
        self.client.put_object(**arguments)
        return ObjectInfo(safe, len(content), digest, content_type)

    def get(self, key: str) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=_safe_key(key))
        except Exception as exc:  # noqa: BLE001
            raise ObjectStoreError("对象读取失败") from exc
        return response["Body"].read()

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=_safe_key(key))

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=_safe_key(key))
            return True
        except Exception:  # noqa: BLE001
            return False

    def list_keys(self) -> list[str]:
        keys: list[str] = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket):
            keys.extend(str(item["Key"]) for item in page.get("Contents", []))
        return sorted(keys)


def export_backup(store: ObjectStore, destination: str | Path) -> dict[str, Any]:
    root = Path(destination).resolve()
    objects_dir = root / "objects"
    if root.exists():
        if any(root.iterdir()):
            raise ObjectStoreError("备份目录必须为空")
    objects_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for key in store.list_keys():
        content = store.get(key)
        target = objects_dir / Path(*PurePosixPath(_safe_key(key)).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        entries.append({"key": key, "size": len(content), "sha256": sha256_bytes(content)})
    manifest = {"version": 1, "backend": store.backend, "objects": entries}
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def restore_backup(
    store: ObjectStore, source: str | Path, *, overwrite: bool = False
) -> dict[str, int]:
    root = Path(source).resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise ObjectStoreError("备份清单不存在")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    restored = 0
    skipped = 0
    for entry in manifest.get("objects", []):
        key = _safe_key(str(entry["key"]))
        source_path = root / "objects" / Path(*PurePosixPath(key).parts)
        content = source_path.read_bytes()
        if len(content) != int(entry["size"]) or sha256_bytes(content) != entry["sha256"]:
            raise ObjectStoreError(f"备份对象校验失败: {key}")
        if store.exists(key) and not overwrite:
            skipped += 1
            continue
        store.put(key, content, "application/octet-stream")
        restored += 1
    return {"restored": restored, "skipped": skipped}


def copy_filesystem_store(source: str | Path, destination: str | Path) -> None:
    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    if destination_path.exists():
        raise ObjectStoreError("目标目录已存在")
    shutil.copytree(source_path, destination_path)


def iter_object(content: bytes, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
    for start in range(0, len(content), chunk_size):
        yield content[start : start + chunk_size]


@lru_cache
def get_object_store() -> ObjectStore:
    if get_settings().object_store_backend == "s3":
        return S3ObjectStore()
    return FilesystemObjectStore()
