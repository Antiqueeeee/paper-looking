"""Disk quotas, LRU eviction to object storage and cold PDF restore."""
from __future__ import annotations

import logging
from pathlib import Path

from paperbase.db import get_paper, set_local_file, set_pdf_status
from paperbase.paths import PaperPaths
from paperbase.storage import FilesystemObjectStore, StorageError, dir_size_bytes

logger = logging.getLogger(__name__)


def make_object_store(config: dict, paths: PaperPaths):
    """Build the configured object store. S3/COS uses boto3 lazily."""
    storage_cfg = config.get("storage", {})
    kind = storage_cfg.get("object_store", "filesystem")
    if kind == "filesystem":
        root = storage_cfg.get("object_root") or str(paths.root / "cold")
        return FilesystemObjectStore(root)
    if kind in ("s3", "cos", "oss"):
        try:
            import boto3
            from botocore.client import Config as BotoConfig
        except ImportError as exc:
            raise StorageError("boto3 is required for s3/cos/oss object storage") from exc
        import os

        force_path_style = bool(storage_cfg.get("force_path_style", kind in ("oss", "cos")))
        client_kwargs = {
            "endpoint_url": storage_cfg.get("endpoint_url") or None,
            "aws_access_key_id": os.environ.get(storage_cfg.get("access_key_env", "S3_ACCESS_KEY"), ""),
            "aws_secret_access_key": os.environ.get(storage_cfg.get("secret_key_env", "S3_SECRET_KEY"), ""),
        }
        if force_path_style:
            client_kwargs["config"] = BotoConfig(s3={"addressing_style": "path"})
        return boto3.client("s3", **client_kwargs)
    raise StorageError(f"unknown object_store: {kind!r}")


class BotoS3Adapter:
    """Wrap a boto3 S3 client so it satisfies the ObjectStore protocol."""

    def __init__(self, client, bucket: str):
        self.client = client
        self.bucket = bucket

    def put(self, key: str, local_path: str) -> None:
        self.client.upload_file(local_path, self.bucket, key)

    def get(self, key: str, local_path: str) -> None:
        self.client.download_file(self.bucket, key, local_path)

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False


def get_object_store(config: dict, paths: PaperPaths):
    store = make_object_store(config, paths)
    if hasattr(store, "put"):
        # Already protocol-shaped (FilesystemObjectStore).
        return store
    return BotoS3Adapter(store, config["storage"].get("bucket", ""))


def evict_hot_pdfs(
    conn,
    paths: PaperPaths,
    store,
    *,
    need_free_bytes: int = 0,
    quota_bytes: int | None = None,
) -> int:
    """Evict parsed PDFs (oldest access first) until enough space is free.

    PDFs that have not finished parsing are never evicted.
    """
    if quota_bytes is None:
        quota_bytes = 0
    candidates = sorted(
        (p for p in paths.pdf_hot_dir.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf"),
        key=lambda p: (p.stat().st_atime, p.stat().st_mtime),
    )
    evicted = 0
    freed = 0
    for pdf in candidates:
        current_size = dir_size_bytes(paths.pdf_hot_dir)
        if need_free_bytes <= 0 and (not quota_bytes or current_size <= quota_bytes):
            break
        paper_id = pdf.stem
        paper = get_paper(conn, paper_id)
        if paper is None or paper["parse_status"] not in ("done", "failed"):
            continue
        key = f"pdf/{paper_id}.pdf"
        try:
            store.put(key, str(pdf))
            if not store.exists(key):
                raise StorageError("object store verification failed")
            set_local_file(conn, paper_id, local_pdf="", object_key=key)
            set_pdf_status(conn, paper_id, "cold")
            pdf_size = pdf.stat().st_size
            pdf.unlink(missing_ok=True)
            evicted += 1
            freed += pdf_size
        except Exception as exc:
            logger.warning("evict %s failed: %s", pdf, exc)
        if need_free_bytes and freed >= need_free_bytes:
            break
    return evicted


def restore_cold_pdf(conn, paths: PaperPaths, store, paper_id: str) -> Path | None:
    paper = get_paper(conn, paper_id)
    if not paper or not paper.get("object_key"):
        return None
    dest = paths.hot_pdf(paper_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    store.get(paper["object_key"], str(dest))
    set_local_file(conn, paper_id, local_pdf=str(dest))
    set_pdf_status(conn, paper_id, "downloaded")
    return dest


__all__ = [
    "make_object_store",
    "get_object_store",
    "BotoS3Adapter",
    "evict_hot_pdfs",
    "restore_cold_pdf",
]
