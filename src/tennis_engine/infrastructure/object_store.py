"""Immutable object-store implementations for local tests and S3-compatible services."""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from minio import Minio
from minio.error import S3Error


@dataclass(frozen=True)
class ObjectMetadata:
    key: str
    sha256: str
    size_bytes: int


class ImmutableObjectStore(Protocol):
    def put(self, key: str, content: bytes) -> ObjectMetadata: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...


def validate_key(key: str) -> PurePosixPath:
    path = PurePosixPath(key)
    if path.is_absolute() or not path.parts or ".." in path.parts or "\\" in key:
        raise ValueError("Object key must be a relative POSIX path")
    return path


def content_metadata(key: str, content: bytes) -> ObjectMetadata:
    return ObjectMetadata(key, hashlib.sha256(content).hexdigest(), len(content))


class LocalObjectStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        relative = validate_key(key)
        target = self.root.joinpath(*relative.parts).resolve()
        if self.root not in target.parents:
            raise ValueError("Object key escapes the store root")
        return target

    def put(self, key: str, content: bytes) -> ObjectMetadata:
        target = self._path(key)
        metadata = content_metadata(key, content)
        if target.exists():
            existing = target.read_bytes()
            if existing != content:
                raise FileExistsError(
                    f"Immutable object already exists with different bytes: {key}"
                )
            return metadata
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return metadata

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()


class S3ObjectStore:
    def __init__(self, client: Minio, bucket: str):
        self.client = client
        self.bucket = bucket

    def put(self, key: str, content: bytes) -> ObjectMetadata:
        from io import BytesIO

        validate_key(key)
        metadata = content_metadata(key, content)
        try:
            existing = self.client.stat_object(self.bucket, key)
        except S3Error as error:
            if error.code not in {"NoSuchKey", "NoSuchObject"}:
                raise
        else:
            existing_metadata = existing.metadata or {}
            if existing_metadata.get("x-amz-meta-sha256") != metadata.sha256:
                raise FileExistsError(
                    f"Immutable object already exists with different bytes: {key}"
                )
            return metadata
        self.client.put_object(
            self.bucket,
            key,
            BytesIO(content),
            length=len(content),
            metadata={"sha256": metadata.sha256},
        )
        return metadata

    def get(self, key: str) -> bytes:
        validate_key(key)
        response = self.client.get_object(self.bucket, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def exists(self, key: str) -> bool:
        validate_key(key)
        try:
            self.client.stat_object(self.bucket, key)
        except S3Error as error:
            if error.code in {"NoSuchKey", "NoSuchObject"}:
                return False
            raise
        return True
