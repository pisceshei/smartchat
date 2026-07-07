"""MinIO-backed media store for the channel layer.

Inbound channel media is copied into MinIO at ingest (WhatsApp media URLs
expire in ~5 minutes) and recorded in the `files` table with per-workspace
sha256 dedup. Outbound adapters reference files by the public URL
`{assets_base_url}/api/v1/files/{file_id}` (served by the inbox/files module).
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import mimetypes
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.messaging import File
from ..settings import get_settings

log = logging.getLogger("smartchat.channels.media")


def file_public_url(file_id: uuid.UUID | str) -> str:
    return f"{get_settings().assets_base_url.rstrip('/')}/api/v1/files/{file_id}"


def guess_extension(mime: str | None, fallback: str = "bin") -> str:
    if mime:
        ext = mimetypes.guess_extension(mime.split(";")[0].strip())
        if ext:
            return ext.lstrip(".")
    return fallback


class MediaStore:
    """Thin async wrapper over the (sync) MinIO SDK. Object writes run in a
    thread; File rows join the caller's transaction."""

    def __init__(self) -> None:
        self._client: Any = None
        self._bucket_checked = False

    def _get_client(self) -> Any:
        if self._client is None:
            from minio import Minio  # local import: keeps module import cheap

            s = get_settings()
            self._client = Minio(
                s.minio_endpoint,
                access_key=s.minio_root_user,
                secret_key=s.minio_root_password,
                secure=s.minio_secure,
            )
        return self._client

    def _ensure_bucket_sync(self) -> None:
        client = self._get_client()
        bucket = get_settings().minio_bucket
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)

    def _put_sync(self, key: str, data: bytes, mime: str | None) -> None:
        if not self._bucket_checked:
            self._ensure_bucket_sync()
            self._bucket_checked = True
        self._get_client().put_object(
            get_settings().minio_bucket,
            key,
            io.BytesIO(data),
            length=len(data),
            content_type=mime or "application/octet-stream",
        )

    def _get_sync(self, key: str) -> bytes:
        resp = self._get_client().get_object(get_settings().minio_bucket, key)
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()

    async def store_bytes(
        self,
        session: AsyncSession,
        *,
        workspace_id: uuid.UUID,
        data: bytes,
        mime: str | None = None,
        filename: str | None = None,
        created_by_type: str | None = None,
        created_by_id: uuid.UUID | None = None,
    ) -> File:
        """Store bytes; returns the (possibly pre-existing) File row. The row
        is added to the caller's session/transaction."""
        sha = hashlib.sha256(data).hexdigest()
        existing = (
            await session.execute(
                select(File).where(File.workspace_id == workspace_id, File.sha256 == sha)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        safe_name = (filename or f"file.{guess_extension(mime)}").replace("/", "_")[:120]
        key = f"{workspace_id}/{sha[:2]}/{sha}/{safe_name}"
        await asyncio.to_thread(self._put_sync, key, data, mime)
        row = File(
            workspace_id=workspace_id,
            sha256=sha,
            storage_key=key,
            mime=mime,
            size=len(data),
            original_name=filename,
            created_by_type=created_by_type,
            created_by_id=created_by_id,
        )
        session.add(row)
        await session.flush()
        return row

    async def load_bytes(self, storage_key: str) -> bytes:
        return await asyncio.to_thread(self._get_sync, storage_key)


_store: MediaStore | None = None


def get_media_store() -> MediaStore:
    global _store
    if _store is None:
        _store = MediaStore()
    return _store
