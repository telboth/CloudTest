from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("cloud_test.storage")


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _safe_filename(filename: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", str(filename or ""))
    cleaned = cleaned.strip("._")
    return cleaned or "attachment.bin"


class AttachmentStorageError(RuntimeError):
    pass


class AttachmentStorageBackend(Protocol):
    backend_name: str

    def store_bytes(self, *, payload: bytes, file_name: str, bug_id: int) -> str:
        ...

    def read_bytes(self, storage_ref: str) -> bytes | None:
        ...

    def delete(self, storage_ref: str) -> bool:
        ...


class FilesystemAttachmentStorage:
    backend_name = "filesystem"

    def __init__(self, root_dir: Path):
        self.root_dir = root_dir.resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_ref(self, storage_ref: str) -> Path:
        raw = str(storage_ref or "").strip()
        if not raw:
            raise AttachmentStorageError("Tom storage-ref.")

        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            return candidate

        resolved = (self.root_dir / candidate).resolve()
        try:
            resolved.relative_to(self.root_dir)
        except ValueError as exc:
            raise AttachmentStorageError("Ugyldig storage-ref (utenfor tillatt område).") from exc
        return resolved

    def store_bytes(self, *, payload: bytes, file_name: str, bug_id: int) -> str:
        if not isinstance(payload, (bytes, bytearray)):
            raise AttachmentStorageError("Payload må være bytes.")
        safe_name = _safe_filename(file_name)
        bug_folder = f"bug-{int(bug_id)}" if int(bug_id) > 0 else "bug-unknown"
        relative = Path(bug_folder) / f"{uuid4().hex}_{safe_name}"
        absolute = (self.root_dir / relative).resolve()
        absolute.parent.mkdir(parents=True, exist_ok=True)
        absolute.write_bytes(bytes(payload))
        return relative.as_posix()

    def read_bytes(self, storage_ref: str) -> bytes | None:
        try:
            path = self._resolve_ref(storage_ref)
        except AttachmentStorageError:
            return None
        if not path.exists() or not path.is_file():
            return None
        return path.read_bytes()

    def delete(self, storage_ref: str) -> bool:
        try:
            path = self._resolve_ref(storage_ref)
        except AttachmentStorageError:
            return False
        if not path.exists():
            return False
        try:
            path.unlink()
            return True
        except OSError:
            return False


def build_attachment_storage() -> AttachmentStorageBackend:
    backend_value = str(os.getenv("ATTACHMENT_STORAGE_BACKEND", "filesystem")).strip().casefold()
    if backend_value in {"filesystem", "file", "local"}:
        return FilesystemAttachmentStorage(settings.attachment_dir)

    logger.warning(
        "Unsupported ATTACHMENT_STORAGE_BACKEND='%s'. Falling back to filesystem backend.",
        backend_value,
    )
    return FilesystemAttachmentStorage(settings.attachment_dir)


def storage_backend_uses_local_files() -> bool:
    if _truthy(os.getenv("STREAMLIT_CLOUD", "")):
        return False
    backend_value = str(os.getenv("ATTACHMENT_STORAGE_BACKEND", "filesystem")).strip().casefold()
    return backend_value in {"filesystem", "file", "local"}
