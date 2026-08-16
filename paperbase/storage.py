"""Object-store protocol, local filesystem implementation and disk quotas.

Cold PDFs and backups are written through an ObjectStore. The default
filesystem implementation keeps development dependency-free; a COS/S3
implementation is added by Agent C without changing this interface.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path


class StorageError(RuntimeError):
    pass


class FilesystemObjectStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        # Keys are flat sanitized strings such as "pdf/2026.acl-long.1.pdf".
        p = (self.root / key).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            raise StorageError(f"illegal object key: {key!r}")
        return p

    def put(self, key: str, local_path: str) -> None:
        src, dst = Path(local_path), self._path(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(dst.suffix + ".part")
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)

    def get(self, key: str, local_path: str) -> None:
        dst = Path(local_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(dst.suffix + ".part")
        shutil.copyfile(self._path(key), tmp)
        os.replace(tmp, dst)

    def delete(self, key: str) -> None:
        p = self._path(key)
        if p.exists():
            p.unlink()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()


class QuotaExceeded(StorageError):
    pass


def dir_size_bytes(path: str | Path) -> int:
    root = Path(path)
    if not root.exists():
        return 0
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file())


def disk_usage_ratio(path: str | Path) -> float:
    usage = shutil.disk_usage(path)
    return usage.used / usage.total


def ensure_capacity(path: str | Path, incoming_bytes: int, quota_bytes: int) -> None:
    """Raise if writing `incoming_bytes` under `path` would exceed the quota."""
    if quota_bytes <= 0:
        return
    if dir_size_bytes(path) + int(incoming_bytes) > int(quota_bytes):
        raise QuotaExceeded(
            f"quota exceeded for {path}: "
            f"{dir_size_bytes(path)} + {incoming_bytes} > {quota_bytes} bytes"
        )


def prune_cache(path: str | Path, quota_bytes: int) -> int:
    """Delete oldest files until the directory fits `quota_bytes`."""
    if quota_bytes <= 0:
        return 0
    root = Path(path)
    if not root.exists():
        return 0
    files = sorted(
        (p for p in root.rglob("*") if p.is_file()),
        key=lambda p: (p.stat().st_mtime, p.stat().st_atime),
    )
    removed = 0
    total = dir_size_bytes(root)
    for f in files:
        if total <= quota_bytes:
            break
        try:
            total -= f.stat().st_size
            f.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def lru_candidates(path: str | Path, need_free_bytes: int) -> list[Path]:
    """Oldest-accessed files first, until enough bytes would be freed."""
    root = Path(path)
    if not root.exists():
        return []
    files = sorted(
        (p for p in root.rglob("*") if p.is_file()),
        key=lambda p: (p.stat().st_atime, p.stat().st_mtime),
    )
    out: list[Path] = []
    total = 0
    for f in files:
        if total >= need_free_bytes:
            break
        total += f.stat().st_size
        out.append(f)
    return out


__all__ = [
    "StorageError",
    "FilesystemObjectStore",
    "QuotaExceeded",
    "dir_size_bytes",
    "disk_usage_ratio",
    "ensure_capacity",
    "lru_candidates",
    "prune_cache",
]
