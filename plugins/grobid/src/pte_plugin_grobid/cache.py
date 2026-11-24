"""
Cache utilities for GROBID training data.

An entry holds the training package of one extraction: the TEI files and the feature files GROBID
computed from the PDF's text layer. The key contains the document id, GROBID revision, flavor and
the PDF's content hash (SHA-256, the editor's `file_id`), since text recognition replaces a PDF
while keeping its `stable_id` and `doc_id`: the entry of the old PDF must neither be served for an
extraction of the new one, nor be overwritten, since the versions extracted from the old PDF still
need its feature file (layout, sync check, training download). Every extraction records the hash in
the `pdf-hash` label of its encodingDesc; documents extracted before have no such label and use the
entry without hash.
"""

import hashlib
import json
import os
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path


def get_cache_dir() -> Path:
    """Get the cache directory for GROBID extractions."""
    from fastapi_app.config import get_settings
    settings = get_settings()
    return settings.plugins_data_dir / "grobid" / "extractions"


def _safe_doc_id(doc_id: str) -> str:
    """Return a filesystem-safe version of doc_id (replace path separators)."""
    return doc_id.replace("/", "_").replace("\\", "_")


def _safe_flavor(flavor: str) -> str:
    """Return a filesystem-safe version of flavor (replace path separators)."""
    return flavor.replace("/", "-").replace("\\", "-")


# Characters of the PDF hash used in the cache key
PDF_HASH_KEY_LENGTH = 16


def pdf_content_hash(pdf_path: str | Path) -> str:
    """SHA-256 of the PDF's content, as the editor's `file_id` of the PDF."""
    digest = hashlib.sha256()
    with open(pdf_path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def get_cache_key(doc_id: str, revision: str, flavor: str, pdf_hash: str | None = None) -> str:
    """
    Build the cache key for a (doc_id, revision, flavor, pdf_hash) combination.

    The flavor is part of the key because different flavors make GROBID emit
    different feature columns; reusing a cache entry across flavors would
    silently serve a feature file missing the other flavor's columns (#480).
    The PDF hash is part of it because text recognition replaces the PDF of a
    document (see the module doc); without a hash, the key of documents
    extracted before the hash was recorded is returned.
    """
    safe_id = _safe_doc_id(doc_id)
    safe_flavor = _safe_flavor(flavor)
    key = f"{safe_id}_{revision}" if revision != "unknown" else safe_id
    if safe_flavor != "default":
        key = f"{key}_{safe_flavor}"
    return f"{key}_pdf-{pdf_hash[:PDF_HASH_KEY_LENGTH]}" if pdf_hash else key


def get_cache_zip(doc_id: str, labels: dict[str, str]) -> Path | None:
    """
    The cached training package of the extraction a document comes from, from the labels of its
    encodingDesc (`revision`, `flavor`, `pdf-hash`, see sync.parse_encoding_labels()); None if
    the document has no revision label. The file may not exist.
    """
    revision = labels.get("revision")
    if not revision:
        return None
    key = get_cache_key(doc_id, revision, labels.get("flavor", "default"), labels.get("pdf-hash"))
    return get_cache_dir() / key / "training.zip"


def check_cache(
    doc_id: str, revision: str, flavor: str = "default", force_refresh: bool = False, pdf_hash: str | None = None
) -> dict | None:
    """
    Check if cached training data exists for doc_id, revision, flavor and PDF.

    Args:
        doc_id: Document ID
        revision: GROBID revision string
        flavor: GROBID processing flavor used for the extraction
        force_refresh: If True, ignore cache
        pdf_hash: Content hash of the PDF (pdf_content_hash()); None for the entry without hash

    Returns:
        Dict with temp_dir and files if cache hit, None otherwise
    """
    if force_refresh:
        return None

    cache_dir = get_cache_dir()
    cache_key = get_cache_key(doc_id, revision, flavor, pdf_hash)
    cache_path = cache_dir / cache_key

    zip_path = cache_path / "training.zip"
    if not zip_path.exists():
        return None

    # Extract to temp location
    temp_dir = tempfile.mkdtemp(prefix=f"grobid_cache_{_safe_doc_id(doc_id)}_")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(temp_dir)

    files = [f for f in os.listdir(temp_dir) if f != "training.zip"]
    return {"temp_dir": temp_dir, "files": files}


def package_lookups(
    revision: str, current_pdf_hash: str, labels: dict[str, str]
) -> tuple[list[tuple[str, str | None]], bool]:
    """
    Where to look for the training package that matches a stored document, for the training
    download (the current GROBID revision first), from the labels of its encodingDesc.

    Returns:
        ((revision, pdf hash) cache keys to try in order, whether the package may be fetched
        from the current PDF if none is cached). A document extracted from a PDF that has since
        been replaced (its `pdf-hash` differs from the current PDF's) can only use the cache: the
        current PDF's feature files do not match its text. A document without `pdf-hash` tries
        the entry without hash first, the cache of its time.
    """
    recorded_hash = labels.get("pdf-hash")
    revisions = [revision]
    if labels.get("revision") and labels["revision"] != revision:
        revisions.append(labels["revision"])
    if recorded_hash and recorded_hash != current_pdf_hash:
        return [(r, recorded_hash) for r in revisions], False
    if recorded_hash:
        # The recorded revision after the current one: the package of the extraction itself, or the
        # feature files of an import (#68)
        return [(r, current_pdf_hash) for r in revisions], True
    return [(revision, None), (revision, current_pdf_hash)], True


def cache_training_data(
    doc_id: str, revision: str, flavor: str, temp_dir: str, files: list[str], pdf_hash: str | None = None
) -> None:
    """
    Cache training data for a document.

    Args:
        doc_id: Document ID
        revision: GROBID revision string
        flavor: GROBID processing flavor used for the extraction
        temp_dir: Temp directory with training files
        files: List of files in temp_dir
        pdf_hash: Content hash of the PDF the data was computed from
    """
    cache_dir = get_cache_dir()
    cache_key = get_cache_key(doc_id, revision, flavor, pdf_hash)
    cache_path = cache_dir / cache_key
    cache_path.mkdir(parents=True, exist_ok=True)

    # Create ZIP
    zip_path = cache_path / "training.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename in files:
            src_path = os.path.join(temp_dir, filename)
            if os.path.exists(src_path):
                zf.write(src_path, filename)

    # Save metadata
    metadata = {
        "doc_id": doc_id,
        "grobid_revision": revision,
        "flavor": flavor,
        "pdf_hash": pdf_hash,
        "files": files,
        "cached_at": datetime.now().isoformat()
    }
    with open(cache_path / "metadata.json", "w") as f:
        json.dump(metadata, f, indent=2)


def add_to_cache(doc_id: str, revision: str, flavor: str, pdf_hash: str, files: dict[str, bytes]) -> Path:
    """
    Add imported feature files to the cache entry of (doc_id, revision, flavor, pdf_hash), keeping
    the files the entry already has (#68).

    Args:
        doc_id: Document ID
        revision: Revision label of the imported documents
        flavor: GROBID processing flavor
        pdf_hash: Content hash of the PDF the files were computed from
        files: File name (as GROBID's createTraining names it) -> content

    Returns:
        Path of the entry's training.zip
    """
    cache_path = get_cache_dir() / get_cache_key(doc_id, revision, flavor, pdf_hash)
    cache_path.mkdir(parents=True, exist_ok=True)
    zip_path = cache_path / "training.zip"
    existing: set[str] = set()
    if zip_path.exists():
        with zipfile.ZipFile(zip_path, "r") as zf:
            existing = set(zf.namelist())
    with zipfile.ZipFile(zip_path, "a", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            if name not in existing:
                zf.writestr(name, content)
        names = zf.namelist()
    metadata = {
        "doc_id": doc_id,
        "grobid_revision": revision,
        "flavor": flavor,
        "pdf_hash": pdf_hash,
        "files": names,
        "source": "import",
        "cached_at": datetime.now().isoformat(),
    }
    with open(cache_path / "metadata.json", "w") as f:
        json.dump(metadata, f, indent=2)
    return zip_path


def delete_cache_for_doc(doc_id: str) -> bool:
    """
    Delete all cached training data for a document (all revisions).

    Args:
        doc_id: Document ID

    Returns:
        True if any cache entries were deleted, False otherwise
    """
    import shutil

    cache_dir = get_cache_dir()
    safe_id = _safe_doc_id(doc_id)
    deleted = False

    # Find all cache entries for this doc_id (any revision)
    # Pattern: {safe_id} or {safe_id}_{revision}
    for cache_path in cache_dir.glob(f"{safe_id}*"):
        if cache_path.is_dir():
            # Verify it's actually for this doc_id (not a prefix match of another doc)
            folder_name = cache_path.name
            if folder_name == safe_id or folder_name.startswith(f"{safe_id}_"):
                shutil.rmtree(cache_path, ignore_errors=True)
                deleted = True

    return deleted
