"""
Garbage collection of the GROBID training-data cache.

A cache entry (see cache.py) is needed as long as a GROBID training document refers to it: the
labels of its encodingDesc (`revision`, `flavor`, `pdf-hash`) give the key of the entry it was
extracted with (`cache.get_cache_zip()`), which the layout, the sync check, the page breaks and
the training download read. Soft-deleted documents count as well, since they can be restored.
Entries no document refers to (documents purged, PDFs replaced and their versions gone, doc_id
changed, extractions never saved) are deleted. Entries written within the last hour are kept, so
that an extraction whose document is not saved yet does not lose its entry.
"""

import logging
import shutil
import time
from pathlib import Path

from fastapi_app.plugin_api import FileRepository, FileStorage, GcContext, GcResult
from pte_plugin_grobid.cache import get_cache_dir, get_cache_zip
from pte_plugin_grobid.sync import parse_encoding_labels

logger = logging.getLogger(__name__)

TRAINING_VARIANT_PREFIX = "grobid.training."

# Entries younger than this (seconds) are kept: their document may not be saved yet
MIN_AGE_SECONDS = 3600


def referenced_cache_entries(file_repo: FileRepository, file_storage: FileStorage) -> set[str]:
    """
    The names of the cache entries (directories) that GROBID training documents refer to.

    Args:
        file_repo: File repository
        file_storage: File storage

    Returns:
        Names of the cache directories to keep
    """
    referenced: set[str] = set()
    for file in file_repo.list_files(file_type="tei", include_deleted=True):
        if not (file.variant or "").startswith(TRAINING_VARIANT_PREFIX):
            continue
        try:
            content = file_storage.read_file(file.id, "tei")
        except Exception as e:  # noqa: BLE001 - an unreadable document refers to nothing
            logger.debug(f"GROBID cache GC: cannot read {file.stable_id}: {e}")
            continue
        if not content:
            continue
        zip_path = get_cache_zip(file.doc_id, parse_encoding_labels(content.decode("utf-8", errors="replace")))
        if zip_path is not None:
            referenced.add(zip_path.parent.name)
    return referenced


def _size(path: Path) -> int:
    """Size of a file or of all files in a directory, in bytes."""
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _age_seconds(path: Path) -> float:
    """Seconds since the last change of a cache entry (its newest file)."""
    mtimes = [path.stat().st_mtime]
    if path.is_dir():
        mtimes += [f.stat().st_mtime for f in path.rglob("*")]
    return time.time() - max(mtimes)


def collect_cache_garbage(ctx: GcContext) -> GcResult:
    """
    Delete the cache entries that no GROBID training document refers to (GC task
    `plugin:grobid`, registered by the plugin). Runs after the core's purge of deleted files.

    Args:
        ctx: Garbage-collection context (repository, storage, dry run)

    Returns:
        Entries deleted (or that would be deleted) and the bytes freed; details
        `entries_deleted`, `entries_kept`
    """
    result = GcResult()
    cache_dir = get_cache_dir()
    if not cache_dir.exists():
        result.details = {"entries_deleted": 0, "entries_kept": 0}
        return result
    referenced = referenced_cache_entries(ctx.file_repo, ctx.file_storage)
    kept = 0
    for entry in sorted(cache_dir.iterdir()):
        try:
            if entry.name in referenced or _age_seconds(entry) < MIN_AGE_SECONDS:
                kept += 1
                continue
            size = _size(entry)
            if not ctx.dry_run:
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
                logger.info(f"Deleted unreferenced GROBID cache entry {entry.name} ({size} bytes)")
        except OSError as e:
            logger.warning(f"Failed to delete GROBID cache entry {entry.name}: {e}")
            result.errors.append(f"{entry.name}: {e}")
            continue
        result.items_deleted += 1
        result.bytes_freed += size
    result.details = {"entries_deleted": result.items_deleted, "entries_kept": kept}
    return result
