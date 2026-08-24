"""Storage adapters implementing :class:`gdap.core.ports.StorageBackend`.

The core never opens a file path directly: it asks the backend for a key. That is what makes the
move from a laptop to S3/GCS/Azure a configuration change (ADR-005).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from gdap.core.errors import StorageError
from gdap.observability.logging import get_logger

log = get_logger(__name__)


def _safe_key(key: str) -> str:
    """Reject traversal and absolute keys — storage keys are relative, always."""
    cleaned = key.strip().lstrip("/")
    if not cleaned or ".." in Path(cleaned).parts:
        raise StorageError(f"unsafe storage key: {key!r}")
    return cleaned


class LocalFileStorage:
    """POSIX filesystem backend rooted at a single directory."""

    scheme = "file"

    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / _safe_key(key)).resolve()
        if not str(path).startswith(str(self.root)):
            raise StorageError(f"key escapes storage root: {key!r}")
        return path

    def write_bytes(self, key: str, payload: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(payload)
        tmp.replace(path)  # atomic publish
        return self.uri(key)

    def read_bytes(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise StorageError(f"object not found: {key}")
        return path.read_bytes()

    def write_file(self, key: str, path: Path) -> str:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        return self.uri(key)

    def local_path(self, key: str) -> Path:
        return self._path(key)

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()

    def list(self, prefix: str = "") -> list[str]:
        base = self._path(prefix) if prefix else self.root
        if not base.exists():
            return []
        if base.is_file():
            return [str(base.relative_to(self.root))]
        return sorted(str(p.relative_to(self.root)) for p in base.rglob("*") if p.is_file())

    def uri(self, key: str) -> str:
        return f"file://{self._path(key)}"

    def size(self, key: str) -> int:
        path = self._path(key)
        if path.is_file():
            return path.stat().st_size
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


class ObjectStorageBackend:
    """S3/GCS/Azure adapter placeholder.

    Deliberately *not* implemented with a half-working SDK call (rule §63: never fake a module).
    The class exists to pin the contract and to fail loudly with an actionable message until the
    ``gdap[object-storage]`` extra and credentials are configured.
    """

    scheme = "s3"

    def __init__(self, bucket: str, prefix: str = "", **options: str) -> None:
        self.bucket = bucket
        self.prefix = prefix
        self.options = options
        raise StorageError(
            "object storage backend is not enabled in this build — "
            "install the 'object-storage' extra and configure credentials, "
            "or keep paths.backend=local (see docs/DEPLOYMENT.md)"
        )
