"""
Custom routes for Annotation Progress plugin.
"""

import csv
import io
import logging
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from lxml import etree

from fastapi_app.lib.core.dependencies import (
    get_auth_manager,
    get_db,
    get_file_storage,
    get_file_repository,
    get_session_manager,
    require_authenticated_user,
)
from fastapi_app.lib.models import FileMetadata
from fastapi_app.plugin_api import FileRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/plugins/annotation-progress", tags=["annotation-progress"])


@router.get("/view", response_class=HTMLResponse)
async def view_progress(
    collection: str = Query(..., description="Collection ID"),
    variant: str | None = Query(None, description="Variant filter"),
    session_id: str | None = Query(None),
    x_session_id: str | None = Header(None, alias="X-Session-ID"),
    session_manager=Depends(get_session_manager),
    auth_manager=Depends(get_auth_manager),
):
    """
    View annotation progress as an HTML page with sortable table.

    Args:
        collection: Collection ID to view
        variant: Optional variant filter
        session_id: Session ID from query parameter
        x_session_id: Session ID from header
        session_manager: Session manager dependency
        auth_manager: Auth manager dependency

    Returns:
        HTML page with DataTables-powered table
    """
    from fastapi_app.config import get_settings
    from fastapi_app.lib.utils.config_utils import get_config
    from fastapi_app.lib.repository.file_repository import FileRepository
    from fastapi_app.lib.plugins.plugin_tools import generate_datatable_page, escape_html

    # Extract session ID (header takes precedence)
    session_id_value = x_session_id or session_id
    if not session_id_value:
        raise HTTPException(status_code=401, detail="Authentication required")

    # Validate session
    settings = get_settings()
    if not session_manager.is_session_valid(session_id_value, settings.session_timeout):
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    # Get user
    user = auth_manager.get_user_by_session_id(session_id_value, session_manager)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    # Check collection access
    from fastapi_app.lib.permissions.user_utils import user_has_collection_access
    if not user_has_collection_access(user, collection, settings.db_dir):
        raise HTTPException(status_code=403, detail="Access denied to collection")

    try:
        db = get_db()
        file_repo = FileRepository(db)
        file_storage = get_file_storage()

        # Get lifecycle order from config
        config = get_config()
        lifecycle_order = config.get("annotation.lifecycle.order", [])

        from fastapi_app.lib.utils.doi_utils import normalize_legacy_encoding

        # Get all files in the collection
        all_files = file_repo.get_files_by_collection(collection)

        # Get all unique doc_ids from the collection (from PDF and TEI files).
        # Normalize legacy $XX$ encoding so files with the same logical doc_id
        # are not counted as separate documents.
        all_doc_ids = set()
        for f in all_files:
            if f.doc_id:
                all_doc_ids.add(normalize_legacy_encoding(f.doc_id))

        # Filter to TEI files only
        tei_files = [f for f in all_files if f.file_type == "tei"]

        # Filter by variant if specified
        if variant and variant not in ("all", ""):
            tei_files = [
                f for f in tei_files if getattr(f, "variant", None) == variant
            ]

        # Build map of doc_id -> PDF (first PDF found per doc_id)
        pdf_files = [f for f in all_files if f.file_type == "pdf"]
        doc_pdf: dict[str, FileMetadata] = {}
        for f in pdf_files:
            if f.doc_id and f.stable_id:
                normalized = normalize_legacy_encoding(f.doc_id)
                if normalized not in doc_pdf:
                    doc_pdf[normalized] = f

        # Versions that another version was copied from (read-only history)
        frozen_ids = file_repo.get_frozen_stable_ids()
        show_variant = not variant or variant == "all"

        # Group annotations by doc_id
        doc_annotations = defaultdict(list)
        for file_metadata in tei_files:
            try:
                content_bytes = file_storage.read_file(file_metadata.id, "tei")
                if not content_bytes:
                    continue

                xml_content = content_bytes.decode("utf-8")
                annotation_info = _extract_annotation_info(xml_content, file_metadata)

                if annotation_info:
                    doc_id = normalize_legacy_encoding(file_metadata.doc_id or "Unknown")
                    doc_annotations[doc_id].append(annotation_info)

            except Exception as e:
                logger.error(f"Failed to parse TEI file {file_metadata.id}: {e}")
                continue

        # Calculate collection statistics
        total_docs = len(all_doc_ids)
        total_annotations = sum(len(anns) for anns in doc_annotations.values())

        # Count documents by lifecycle stage and calculate progress
        stage_counts = {stage: 0 for stage in lifecycle_order}
        stage_counts["no-status"] = 0
        total_progress_sum = 0

        for doc_id in all_doc_ids:
            annotations = doc_annotations[doc_id]
            if not annotations:
                stage_counts["no-status"] += 1
                continue

            rep = _select_representative_annotation(annotations, lifecycle_order)
            status = rep.get("last_change_status", "") if rep else ""

            if status in lifecycle_order:
                stage_counts[status] += 1
                current_index = lifecycle_order.index(status)
                doc_progress = ((current_index + 1) / len(lifecycle_order)) * 100
                total_progress_sum += doc_progress
            else:
                stage_counts["no-status"] += 1

        # Calculate average progress across all documents
        avg_progress = (total_progress_sum / total_docs) if total_docs > 0 else 0

        # Prepare table data
        headers = ["Document", "Document ID", "Version History", "Last Change", "Last Annotator", "Status", "Date"]
        rows = []

        # One row per version history, sorted by document label. The first row of a document
        # carries its label and id; the cells stay empty in the following rows so that these
        # look indented. Rows stay in this order (see HISTORY_TABLE_JS).
        documents = sorted(
            ((_document_label(doc_pdf.get(doc_id), doc_id), doc_id) for doc_id in all_doc_ids),
            key=lambda item: (item[0].lower(), item[1]),
        )
        for label, doc_id in documents:
            annotations = doc_annotations[doc_id]

            label_cell = f'<span class="doc-label" title="{escape_html(label)}">{escape_html(label)}</span>'
            # Make doc_id clickable if a PDF exists for it
            pdf = doc_pdf.get(doc_id)
            if pdf:
                doc_id_cell = f'<a href="#" onclick="sandbox.load({{pdf:\'{pdf.stable_id}\'}}); return false;" class="version-link">{escape_html(doc_id)}</a>'
            else:
                doc_id_cell = escape_html(doc_id)

            histories = _group_histories(annotations, frozen_ids)
            if not histories:
                rows.append([label_cell, doc_id_cell, "No annotations", "", "", "", ""])

            for index, history in enumerate(histories):
                latest = _latest_annotation(history["leaves"] or history["ancestors"])
                timestamp = latest.get("last_change_timestamp")
                rows.append([
                    label_cell if index == 0 else "",
                    doc_id_cell if index == 0 else "",
                    _format_history_html(history, show_variant),
                    escape_html(latest.get("last_change_desc", "")),
                    escape_html(latest.get("last_annotator", "")),
                    _create_status_indicator(latest.get("last_change_status", ""), lifecycle_order),
                    f'<span class="date">{timestamp.strftime("%Y-%m-%d %H:%M")}</span>' if timestamp else "",
                ])

        # Generate summary card HTML
        summary_html = _create_summary_card(
            total_docs,
            avg_progress,
            total_annotations,
            stage_counts,
            lifecycle_order
        )

        # Generate HTML page
        title = f"Annotation Progress - {collection}"
        if variant and variant != "all":
            title += f" ({variant})"

        html = generate_datatable_page(
            title=title,
            headers=headers,
            rows=rows,
            table_id="annotationProgressTable",
            page_length=50,
            default_sort_col=0,
            default_sort_dir="asc",
            enable_sandbox_client=True,
            custom_css="""
                table { font-size: 0.9em; }
                .summary-card {
                    background: #f8f9fa;
                    border: 1px solid #dee2e6;
                    border-radius: 8px;
                    padding: 20px;
                    margin-bottom: 20px;
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                }
                .summary-stats {
                    display: grid;
                    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                    gap: 15px;
                    margin-bottom: 15px;
                }
                .stat-item {
                    background: white;
                    padding: 12px;
                    border-radius: 6px;
                    border: 1px solid #e9ecef;
                }
                .stat-label {
                    font-size: 0.85em;
                    color: #6c757d;
                    margin-bottom: 4px;
                }
                .stat-value {
                    font-size: 1.5em;
                    font-weight: 600;
                    color: #212529;
                }
                .stage-distribution {
                    margin-top: 15px;
                }
                .stage-list {
                    display: flex;
                    flex-wrap: wrap;
                    gap: 10px;
                    margin-top: 8px;
                }
                .stage-badge {
                    background: white;
                    border: 1px solid #e9ecef;
                    padding: 6px 12px;
                    border-radius: 4px;
                    font-size: 0.9em;
                }
                .stage-badge strong {
                    color: #495057;
                }
                .status-indicator {
                    display: inline-flex;
                    align-items: center;
                    gap: 4px;
                }
                .status-circle {
                    width: 3px;
                    height: 3px;
                    border: 0.5px solid #495057;
                    border-radius: 50%;
                    display: inline-block;
                }
                .status-circle.filled {
                    background-color: #495057;
                }
                .status-label {
                    font-size: 0.75em;
                    font-weight: 600;
                    text-transform: uppercase;
                    letter-spacing: 0.5px;
                    color: #495057;
                    background: #e9ecef;
                    padding: 2px 8px;
                    border-radius: 3px;
                }
                .version-chain {
                    margin: 2px 0;
                    white-space: nowrap;
                }
                .version-link {
                    color: #0066cc;
                    text-decoration: underline;
                }
                .version-link:hover {
                    color: #004499;
                }
                .doc-label {
                    display: block;
                    max-width: 16em;
                    font-weight: 600;
                    white-space: nowrap;
                    overflow: hidden;
                    text-overflow: ellipsis;
                }
                #annotationProgressTable td:nth-child(3) {
                    min-width: 16em;
                }
                #annotationProgressTable td:nth-child(4) {
                    min-width: 10em;
                }
                #annotationProgressTable tbody td {
                    background-color: #fff;
                    box-shadow: none !important;
                    vertical-align: top;
                }
                #annotationProgressTable tbody tr:has(.doc-label) td {
                    border-top: 1px solid #ced4da;
                }
                .date {
                    white-space: nowrap;
                }
                .history-toggle {
                    white-space: nowrap;
                    color: #6c757d;
                    font-size: 0.9em;
                    margin-left: 4px;
                }
                .share-bar {
                    display: flex;
                    justify-content: flex-end;
                    align-items: center;
                    gap: 8px;
                    margin: -10px 0 10px;
                }
                .share-bar button {
                    font: inherit;
                    font-size: 0.9em;
                    padding: 4px 12px;
                    border: 1px solid #ced4da;
                    border-radius: 4px;
                    background: #fff;
                    cursor: pointer;
                }
                .share-bar button:hover {
                    background: #f1f3f5;
                }
                .share-status {
                    font-size: 0.85em;
                    color: #6c757d;
                }
                .variant-suffix {
                    color: #6c757d;
                    font-size: 0.9em;
                }
            """ + LINEAGE_CSS,
            custom_js=HISTORY_TABLE_JS,
            extra_content_before_table=_share_link_html(collection, variant) + summary_html
        )

        return HTMLResponse(content=html)

    except Exception as e:
        logger.error(f"Failed to generate annotation progress view: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/export")
async def export_csv(
    collection: str = Query(..., description="Collection ID"),
    variant: str | None = Query(None, description="Variant filter"),
    session_id: str | None = Query(None),
    x_session_id: str | None = Header(None, alias="X-Session-ID"),
    session_manager=Depends(get_session_manager),
    auth_manager=Depends(get_auth_manager),
):
    """
    Export annotation progress as CSV.

    Args:
        collection: Collection ID to export
        variant: Optional variant filter
        session_id: Session ID from query parameter
        x_session_id: Session ID from header
        session_manager: Session manager dependency
        auth_manager: Auth manager dependency

    Returns:
        CSV file as streaming response
    """
    from fastapi_app.config import get_settings
    from fastapi_app.lib.repository.file_repository import FileRepository
    from fastapi_app.lib.permissions.user_utils import user_has_collection_access

    # Extract session ID (header takes precedence)
    session_id_value = x_session_id or session_id
    if not session_id_value:
        raise HTTPException(status_code=401, detail="Authentication required")

    # Validate session
    settings = get_settings()
    if not session_manager.is_session_valid(session_id_value, settings.session_timeout):
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    # Get user
    user = auth_manager.get_user_by_session_id(session_id_value, session_manager)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    # Check collection access
    if not user_has_collection_access(user, collection, settings.db_dir):
        raise HTTPException(status_code=403, detail="Access denied to collection")

    try:
        db = get_db()
        file_repo = FileRepository(db)
        file_storage = get_file_storage()

        from fastapi_app.lib.utils.doi_utils import normalize_legacy_encoding

        # Get all files in the collection
        all_files = file_repo.get_files_by_collection(collection)

        # Get all unique doc_ids, normalizing legacy $XX$ encoding
        all_doc_ids = set()
        for f in all_files:
            if f.doc_id:
                all_doc_ids.add(normalize_legacy_encoding(f.doc_id))

        # Filter to TEI files only
        tei_files = [f for f in all_files if f.file_type == "tei"]

        # Filter by variant if specified
        if variant and variant not in ("all", ""):
            tei_files = [
                f for f in tei_files if getattr(f, "variant", None) == variant
            ]

        # Group annotations by doc_id
        doc_annotations = defaultdict(list)
        for file_metadata in tei_files:
            try:
                content_bytes = file_storage.read_file(file_metadata.id, "tei")
                if not content_bytes:
                    continue

                xml_content = content_bytes.decode("utf-8")
                annotation_info = _extract_annotation_info(xml_content, file_metadata)

                if annotation_info:
                    doc_id = normalize_legacy_encoding(file_metadata.doc_id or "Unknown")
                    doc_annotations[doc_id].append(annotation_info)

            except Exception as e:
                logger.error(f"Failed to parse TEI file {file_metadata.id}: {e}")
                continue

        # Generate CSV
        output = io.StringIO()
        writer = csv.writer(output)

        # Write header
        writer.writerow(
            ["Document ID", "Annotation Label", "Revision Count", "Last Change", "Last Annotator", "Status", "Date"]
        )

        # Write data - sort by doc_id and include all documents even if they have no annotations
        for doc_id in sorted(all_doc_ids):
            annotations = doc_annotations[doc_id]

            if not annotations:
                # Document with no annotations
                writer.writerow([doc_id, "", "", "", "", "", ""])
            else:
                # Sort annotations by label for consistent output
                annotations.sort(key=lambda x: x["annotation_label"])

                for ann in annotations:
                    # Format timestamp as timezone-aware ISO format with Z suffix
                    date_str = ""
                    timestamp = ann.get("last_change_timestamp")
                    if timestamp:
                        # Assume UTC if no timezone info (since we stripped it during parsing)
                        from datetime import timezone
                        if timestamp.tzinfo is None:
                            timestamp = timestamp.replace(tzinfo=timezone.utc)
                        date_str = timestamp.isoformat().replace("+00:00", "Z")

                    writer.writerow([
                        doc_id,
                        ann["annotation_label"],
                        ann["revision_count"],
                        ann.get("last_change_desc", ""),
                        ann.get("last_annotator", ""),
                        ann.get("last_change_status", ""),
                        date_str,
                    ])

        # Create streaming response
        output.seek(0)
        return StreamingResponse(
            io.BytesIO(output.getvalue().encode("utf-8")),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="annotation-progress-{collection}.csv"'
            },
        )

    except Exception as e:
        logger.error(f"Failed to export annotation progress: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _select_representative_annotation(
    annotations: list[dict], lifecycle_order: list[str]
) -> dict | None:
    """
    Pick the annotation version that represents a document's overall progress.

    Prefers the version whose status is furthest along ``lifecycle_order``,
    disregarding timestamp entirely, so an older approved version is not
    shadowed by a newer draft/regression version of the same document. Ties
    (same status) are broken by the most recent timestamp. Falls back to the
    most recently changed annotation if none carry a status recognized by
    ``lifecycle_order``, so the row still shows something meaningful.

    Args:
        annotations: Annotation info dicts for a single document (as produced
            by `_extract_annotation_info`)
        lifecycle_order: Ordered list of lifecycle stages

    Returns:
        The representative annotation dict, or None if `annotations` is empty
    """
    best = None
    best_index = -1
    for ann in annotations:
        status = ann.get("last_change_status", "")
        if status not in lifecycle_order:
            continue
        index = lifecycle_order.index(status)
        timestamp = ann.get("last_change_timestamp")
        if index > best_index or (
            index == best_index
            and timestamp
            and (not best.get("last_change_timestamp") or timestamp > best["last_change_timestamp"])
        ):
            best = ann
            best_index = index

    if best is not None:
        return best

    newest = None
    for ann in annotations:
        timestamp = ann.get("last_change_timestamp")
        if timestamp and (newest is None or timestamp > newest.get("last_change_timestamp")):
            newest = ann
    return newest


def _extract_annotation_info(xml_content: str, file_metadata) -> dict | None:
    """
    Extract annotation information from TEI document.

    Args:
        xml_content: TEI XML content as string
        file_metadata: File metadata object

    Returns:
        Dictionary with annotation label, revision count, last change info, and change signatures
    """
    try:
        from fastapi_app.lib.utils.tei_utils import (
            get_artifact_label,
            get_annotator_name,
            extract_change_signatures,
        )

        root = etree.fromstring(xml_content.encode("utf-8"))
        ns = {
            "tei": "http://www.tei-c.org/ns/1.0",
        }

        annotation_label = get_artifact_label(root) or "Untitled"

        # Count all change elements (revision count)
        change_elements = root.findall(".//tei:teiHeader/tei:revisionDesc/tei:change", ns)
        revision_count = len(change_elements)

        # Extract change signatures for ancestry tracking
        change_signatures = extract_change_signatures(root)

        # Get the last change element
        last_change = root.find(".//tei:teiHeader/tei:revisionDesc/tei:change[last()]", ns)

        last_change_desc = ""
        last_annotator = ""
        last_change_status = ""
        last_change_timestamp = None

        if last_change is not None:
            # Get description from text content or desc subelement
            desc_elem = last_change.find("tei:desc", ns)
            if desc_elem is not None and desc_elem.text:
                last_change_desc = desc_elem.text.strip()
            elif last_change.text:
                last_change_desc = last_change.text.strip()

            # Get annotator name
            who_id = last_change.get("who", "")
            last_annotator = get_annotator_name(root, who_id)

            # Get status attribute
            last_change_status = last_change.get("status", "")

            # Get timestamp for comparison
            when = last_change.get("when", "")
            if when:
                try:
                    from datetime import datetime
                    last_change_timestamp = datetime.fromisoformat(when.replace("Z", "+00:00"))
                    if last_change_timestamp.tzinfo is not None:
                        last_change_timestamp = last_change_timestamp.replace(tzinfo=None)
                except (ValueError, AttributeError):
                    pass

        return {
            "annotation_label": annotation_label,
            "revision_count": revision_count,
            "stable_id": file_metadata.stable_id,
            "last_change_desc": last_change_desc,
            "last_annotator": last_annotator,
            "last_change_status": last_change_status,
            "last_change_timestamp": last_change_timestamp,
            "change_signatures": change_signatures,
            "is_gold_standard": getattr(file_metadata, "is_gold_standard", False),
            "parent_stable_id": getattr(file_metadata, "parent_stable_id", None),
            "version": getattr(file_metadata, "version", None),
            "variant": getattr(file_metadata, "variant", None),
        }

    except Exception as e:
        logger.error(f"Error extracting annotation info: {e}")
        return None


# Lines connecting superseded versions to the current versions of their history, as in the
# "Target XML" and "Compare with version" menus of the app (app/web/app.css)
LINEAGE_CSS = """
    .lineage {
        --lineage-x: 0.6em;
        --lineage-color: #adb5bd;
    }
    .lineage-ancestor {
        position: relative;
        padding: 1px 0 1px calc(var(--lineage-x) + 1em);
    }
    /* vertical line, ending in the middle of the last ancestor */
    .lineage-ancestor::before {
        content: "";
        position: absolute;
        left: var(--lineage-x);
        top: 0;
        bottom: 0;
        border-left: 1px solid var(--lineage-color);
    }
    .lineage-ancestor:last-child::before {
        bottom: 50%;
    }
    /* horizontal tick towards the label */
    .lineage-ancestor::after {
        content: "";
        position: absolute;
        left: var(--lineage-x);
        top: 50%;
        width: 0.8em;
        border-top: 1px solid var(--lineage-color);
    }
    /* the line's upper end with an arrowhead, below the current versions */
    .lineage-arrow {
        position: relative;
        height: 0.9em;
    }
    .lineage-arrow::before {
        content: "";
        position: absolute;
        left: var(--lineage-x);
        top: 0.15em;
        bottom: 0;
        border-left: 1px solid var(--lineage-color);
    }
    .lineage-arrow::after {
        content: "";
        position: absolute;
        left: calc(var(--lineage-x) - 0.25em + 0.5px);
        top: 0;
        border: 0.25em solid transparent;
        border-top: 0;
        border-bottom: 0.35em solid var(--lineage-color);
    }
"""

# Keeps each document row together with the rows of its version histories: sorting is disabled
# and the search shows a document with all its histories if any of their rows match.
HISTORY_TABLE_JS = """
    window.toggleHistory = function (link) {
        var lineage = link.closest('.history').querySelector('.lineage');
        lineage.hidden = !lineage.hidden;
        link.textContent = lineage.hidden ? '(see history)' : '(hide history)';
    };

    var pageLength = table.page.len();
    table.destroy();
    $('#annotationProgressTable thead th').removeClass('sorting sorting_asc sorting_desc');

    var groupOf = [];
    var group = -1;
    $('#annotationProgressTable tbody tr').each(function () {
        if ($(this).find('.doc-label').length) group++;
        groupOf.push(group);
    });

    var matchedGroups = null;
    $.fn.dataTable.ext.search.push(function (settings, data, dataIndex) {
        return matchedGroups === null || matchedGroups.has(groupOf[dataIndex]);
    });

    table = $('#annotationProgressTable').DataTable({ ordering: false, pageLength: pageLength });

    $('#annotationProgressTable_filter input').off().on('input', function () {
        var query = this.value.trim().toLowerCase();
        if (!query) {
            matchedGroups = null;
        } else {
            matchedGroups = new Set();
            table.rows().every(function (rowIdx) {
                if ($(this.node()).text().toLowerCase().includes(query)) matchedGroups.add(groupOf[rowIdx]);
            });
        }
        table.draw();
    });
"""


def _share_link_html(collection: str, variant: str | None) -> str:
    """
    A "Share link" button that copies a link to the app which opens this view for the
    collection and variant (see the `url_openable` endpoint metadata in plugin.py).

    The link is built from the location of the app window: the parent window when the view is
    shown in the app's dialog, the opener when it is shown in a window of its own.

    Args:
        collection: Collection ID
        variant: Variant filter, if any

    Returns:
        HTML with the button and its script
    """
    import json
    from urllib.parse import urlencode

    hash_params = {"plugin": "annotation-progress/show_progress", "collection": collection}
    if variant:
        hash_params["variant"] = variant
    hash_json = json.dumps("#" + urlencode(hash_params, safe="/")).replace("</", "<\\/")
    return f"""
    <div class="share-bar">
        <span class="share-status" id="shareStatus" aria-live="polite"></span>
        <button type="button" id="shareLinkBtn" title="Copy a link that opens this view">Share link</button>
    </div>
    <script>
        (function () {{
            function appUrl() {{
                for (const win of [window.parent, window.opener]) {{
                    try {{
                        if (win && win !== window) return win.location.origin + win.location.pathname;
                    }} catch (e) {{ /* other origin */ }}
                }}
                return window.location.origin + '/';
            }}
            document.getElementById('shareLinkBtn').addEventListener('click', async function () {{
                const link = appUrl() + {hash_json};
                const status = document.getElementById('shareStatus');
                try {{
                    await navigator.clipboard.writeText(link);
                    status.textContent = 'Link copied to clipboard';
                }} catch (e) {{
                    window.prompt('Copy this link:', link);
                    status.textContent = '';
                }}
                setTimeout(function () {{ status.textContent = ''; }}, 3000);
            }});
        }})();
    </script>
    """


def _document_label(pdf: FileMetadata | None, doc_id: str) -> str:
    """
    Label of a document as shown in the app's PDF menu: the PDF's label, else the title from
    its metadata, else the document id.

    Args:
        pdf: The document's PDF file, if any
        doc_id: The (normalized) document id

    Returns:
        The document label
    """
    if pdf:
        if pdf.label and pdf.label.lower() not in ("untitled", "unknown title"):
            return pdf.label
        title = (pdf.doc_metadata or {}).get("title", "")
        if isinstance(title, str) and title and title.lower() not in ("untitled", "unknown title"):
            return title
    return doc_id


def _group_histories(annotations: list[dict], frozen_ids: set[str]) -> list[dict[str, list[dict]]]:
    """
    Group the TEI files of a document into version histories.

    Mirrors `groupVersionsByLineage` in the app's file-data-utils.js: a history consists of the
    current (non-frozen) versions that were copied from the same version ("leaves"), followed by
    that version and its ancestors, nearest first ("ancestors"). Unlike in the app's menus,
    current versions without a parent each form a history of their own. Each gold standard
    forms a history of its own and ends the ancestor chain of the versions copied from it.
    Frozen versions that are no ancestor of a current version form a last history without
    leaves, so that no version is lost.

    Args:
        annotations: Annotation info dicts of one document (as produced by `_extract_annotation_info`)
        frozen_ids: Stable ids of the files that another file was copied from

    Returns:
        List of {"leaves": [...], "ancestors": [...]} dicts, gold standards first
    """
    by_id = {ann["stable_id"]: ann for ann in annotations}

    def by_version(ann: dict) -> tuple[int, str]:
        return (ann.get("version") or 0, ann.get("annotation_label", ""))

    def ancestors_of(parent_id: str | None) -> list[dict]:
        ancestors: list[dict] = []
        seen: set[str] = set()
        while parent_id and parent_id in by_id and parent_id not in seen and not by_id[parent_id].get("is_gold_standard"):
            seen.add(parent_id)
            ancestors.append(by_id[parent_id])
            parent_id = by_id[parent_id].get("parent_stable_id")
        return ancestors

    histories: list[dict[str, list[dict]]] = []
    for gold in sorted((a for a in annotations if a.get("is_gold_standard")), key=by_version):
        histories.append({"leaves": [gold], "ancestors": ancestors_of(gold.get("parent_stable_id"))})

    leaves_by_parent: dict[str, list[dict]] = {}
    for leaf in sorted(annotations, key=by_version):
        if leaf.get("is_gold_standard") or leaf["stable_id"] in frozen_ids:
            continue
        parent_id = leaf.get("parent_stable_id")
        has_parent = parent_id in by_id and not by_id[parent_id].get("is_gold_standard")
        leaves_by_parent.setdefault(parent_id if has_parent else leaf["stable_id"], []).append(leaf)

    for key, leaves in leaves_by_parent.items():
        parent_id = leaves[0].get("parent_stable_id") if key != leaves[0]["stable_id"] else None
        histories.append({"leaves": leaves, "ancestors": ancestors_of(parent_id)})

    shown = {ann["stable_id"] for history in histories for ann in history["leaves"] + history["ancestors"]}
    rest = sorted((a for a in annotations if a["stable_id"] not in shown), key=by_version)
    if rest:
        histories.append({"leaves": [], "ancestors": rest})
    return histories


def _latest_annotation(annotations: list[dict]) -> dict:
    """
    The most recently changed of a non-empty list of annotations; the first one if none has a timestamp.
    """
    timestamped = [a for a in annotations if a.get("last_change_timestamp")]
    if not timestamped:
        return annotations[0]
    return max(timestamped, key=lambda a: a["last_change_timestamp"])


def _format_history_html(history: dict[str, list[dict]], show_variant: bool) -> str:
    """
    Format a version history: the labels of its current versions, followed by a "(see history)"
    link that expands the superseded versions below them. All labels open the respective version.
    The gold standard is marked with 🥇.

    Args:
        history: {"leaves": [...], "ancestors": [...]} as returned by `_group_histories`
        show_variant: Whether to append the variant to the labels (when not filtering by variant)

    Returns:
        HTML string
    """
    from fastapi_app.lib.plugins.plugin_tools import escape_html

    def link(ann: dict) -> str:
        prefix = "🥇 " if ann.get("is_gold_standard") else ""
        html = f'<a href="#" onclick="sandbox.openDocument(\'{ann["stable_id"]}\'); return false;" class="version-link">{prefix}{escape_html(ann["annotation_label"])}</a>'
        if show_variant and ann.get("variant"):
            # last segment of dotted variant names only, to save space
            short_variant = ann["variant"].rsplit(".", 1)[-1]
            html += f' <span class="variant-suffix" title="{escape_html(ann["variant"])}">({escape_html(short_variant)})</span>'
        return html

    leaves, ancestors = history["leaves"], history["ancestors"]
    if not leaves:
        items = "".join(f'<div class="lineage-ancestor">{link(ann)}</div>' for ann in ancestors)
        return f'<div class="history"><div class="lineage">{items}</div></div>'

    leaf_htmls = [link(ann) for ann in leaves]
    if not ancestors:
        return '<div class="history">' + "".join(f'<div class="history-leaf">{html}</div>' for html in leaf_htmls) + "</div>"

    leaf_htmls[-1] += ' <a href="#" class="history-toggle" onclick="toggleHistory(this); return false;">(see history)</a>'
    items = "".join(f'<div class="lineage-ancestor">{link(ann)}</div>' for ann in ancestors)
    return (
        '<div class="history">'
        + "".join(f'<div class="history-leaf">{html}</div>' for html in leaf_htmls)
        + f'<div class="lineage" hidden><div class="lineage-arrow" aria-hidden="true"></div>{items}</div>'
        + "</div>"
    )


def _get_stage_color(stage_index: int, total_stages: int) -> tuple[str, str]:
    """
    Get background and text color for a lifecycle stage using cool-to-warm gradient.

    Args:
        stage_index: Zero-based index of the stage
        total_stages: Total number of stages

    Returns:
        Tuple of (background_color, text_color)
    """
    # Cool-to-warm gradient: blue → purple → orange
    # Colors chosen for good contrast and accessibility
    colors = [
        ("#E3F2FD", "#1565C0"),  # Light blue bg, dark blue text
        ("#BBDEFB", "#0D47A1"),  # Medium light blue
        ("#D1C4E9", "#4527A0"),  # Light purple bg, dark purple text
        ("#B39DDB", "#311B92"),  # Medium purple
        ("#CE93D8", "#6A1B9A"),  # Light violet bg, dark violet text
        ("#FFE0B2", "#E65100"),  # Light orange bg, dark orange text
        ("#FFCC80", "#E65100"),  # Medium orange
        ("#FFB74D", "#E65100"),  # Darker orange
    ]

    # If we have more stages than colors, interpolate
    if total_stages <= len(colors):
        # Use subset of colors
        step = len(colors) / total_stages
        color_index = int(stage_index * step)
        return colors[color_index]
    else:
        # Use all colors and repeat last one
        if stage_index < len(colors):
            return colors[stage_index]
        return colors[-1]


def _create_status_indicator(status: str, lifecycle_order: list[str]) -> str:
    """
    Create a visual status indicator with filled circles based on lifecycle position.

    Args:
        status: Current status value
        lifecycle_order: Ordered list of lifecycle stages

    Returns:
        HTML string with status indicator
    """
    from fastapi_app.lib.plugins.plugin_tools import escape_html

    if not status or not lifecycle_order:
        return '<span class="status-text">—</span>'

    # Find position in lifecycle
    try:
        current_index = lifecycle_order.index(status)
        total_stages = len(lifecycle_order)
        filled_count = current_index + 1
    except ValueError:
        # Status not in lifecycle order
        return f'<span class="status-label">{escape_html(status.upper())}</span>'

    # Get color for this stage
    bg_color, text_color = _get_stage_color(current_index, total_stages)

    # Create circles (filled for completed stages, empty for remaining)
    circles = []
    for i in range(total_stages):
        filled_class = "filled" if i < filled_count else ""
        circles.append(f'<span class="status-circle {filled_class}"></span>')

    circles_html = "".join(circles)
    status_text = escape_html(status)
    status_display = escape_html(status.upper())

    return f'<span class="status-indicator" title="{status_text} ({filled_count}/{total_stages})">{circles_html} <span class="status-label" style="background: {bg_color}; color: {text_color};">{status_display}</span></span>'


def _create_summary_card(
    total_docs: int,
    avg_progress: float,
    total_annotations: int,
    stage_counts: dict[str, int],
    lifecycle_order: list[str]
) -> str:
    """
    Create HTML summary card showing collection-level statistics.

    Args:
        total_docs: Total number of documents
        avg_progress: Average lifecycle progress percentage across all documents
        total_annotations: Total number of annotations
        stage_counts: Dictionary mapping lifecycle stages to document counts
        lifecycle_order: Ordered list of lifecycle stages

    Returns:
        HTML string for summary card
    """
    # Build stage distribution HTML
    stage_badges = []
    for stage in lifecycle_order:
        count = stage_counts.get(stage, 0)
        if count > 0:
            stage_badges.append(f'<span class="stage-badge"><strong>{count}</strong> {stage}</span>')

    # Add no-status count if any
    no_status_count = stage_counts.get("no-status", 0)
    if no_status_count > 0:
        stage_badges.append(f'<span class="stage-badge"><strong>{no_status_count}</strong> no status</span>')

    stage_badges_html = "\n".join(stage_badges) if stage_badges else '<span class="stage-badge">No data</span>'

    return f"""
    <div class="summary-card">
        <div class="summary-stats">
            <div class="stat-item">
                <div class="stat-label">Total Documents</div>
                <div class="stat-value">{total_docs}</div>
            </div>
            <div class="stat-item">
                <div class="stat-label">Total Progress</div>
                <div class="stat-value">{avg_progress:.1f}%</div>
            </div>
            <div class="stat-item">
                <div class="stat-label">Total Annotations</div>
                <div class="stat-value">{total_annotations}</div>
            </div>
        </div>
        <div class="stage-distribution">
            <div class="stat-label">Distribution by Lifecycle Stage</div>
            <div class="stage-list">
                {stage_badges_html}
            </div>
        </div>
    </div>
    """


def _summarize_collection(files: list[FileMetadata], lifecycle_order: list[str], threshold: str) -> tuple[int, dict[str, int]]:
    """Count the documents among `files` and, per TEI variant, those whose TEI status is at or after `threshold`.

    Returns:
        (number of documents, {variant: number of documents of that variant that reached the threshold}),
        listing every variant that has a TEI file, even if no document reached the threshold.
    """
    from fastapi_app.lib.utils.doi_utils import normalize_legacy_encoding

    threshold_index = lifecycle_order.index(threshold) if threshold in lifecycle_order else len(lifecycle_order) - 1
    doc_ids: set[str] = set()
    done: dict[str, set[str]] = {}
    for f in files:
        if not f.doc_id:
            continue
        doc_id = normalize_legacy_encoding(f.doc_id)
        doc_ids.add(doc_id)
        if f.file_type != "tei" or not f.variant:
            continue
        variant_done = done.setdefault(f.variant, set())
        if f.status in lifecycle_order and lifecycle_order.index(f.status) >= threshold_index:
            variant_done.add(doc_id)
    return len(doc_ids), {variant: len(docs) for variant, docs in sorted(done.items())}


@router.get("/dashboard-summary")
async def dashboard_summary(
    user: dict[str, Any] = Depends(require_authenticated_user),
    file_repo: FileRepository = Depends(get_file_repository),
) -> dict[str, Any]:
    """Per project the user can see and per collection: number of documents and, per variant, the number that reached the dashboard threshold status.

    The threshold is `plugin.annotation-progress.dashboard-threshold` (a status from
    `annotation.lifecycle.order`). Each collection is listed once, under the most specific
    project containing it (fewest collections, wildcard projects last). Collections that belong
    only to archived projects, collections without documents and projects without collections
    are omitted.
    """
    from fastapi_app.config import get_settings
    from fastapi_app.lib.utils.collection_utils import get_collections_with_details
    from fastapi_app.lib.utils.config_utils import get_config
    from fastapi_app.lib.utils.project_utils import get_archived_collection_ids, get_visible_projects
    from fastapi_app.plugins.dashboard.documents import project_specificity

    settings = get_settings()
    config = get_config()
    lifecycle_order: list[str] = config.get("annotation.lifecycle.order", default=[]) or []
    threshold: str = config.get("plugin.annotation-progress.dashboard-threshold", default="approved")
    if threshold not in lifecycle_order and lifecycle_order:
        threshold = lifecycle_order[-1]

    try:
        collection_names = {c["id"]: c.get("name") or c["id"] for c in get_collections_with_details(settings.db_dir) or []}
    except (FileNotFoundError, ValueError):
        collection_names = {}
    archived = get_archived_collection_ids(settings.db_dir)
    seen: set[str] = set()

    projects: list[dict[str, Any]] = []
    for project in sorted(get_visible_projects(user, settings.db_dir), key=project_specificity):
        collections = project.get("collections", [])
        if "*" in collections:
            collections = list(collection_names)
        batches: list[dict[str, Any]] = []
        for collection in collections:
            if collection in seen or collection in archived:
                continue
            seen.add(collection)
            total, variants = _summarize_collection(file_repo.get_files_by_collection(collection), lifecycle_order, threshold)
            if total:
                batches.append({
                    "id": collection,
                    "name": collection_names.get(collection, collection),
                    "total": total,
                    "variants": [{"variant": variant, "done": done} for variant, done in variants.items()],
                })
        if batches:
            batches.sort(key=lambda batch: batch["name"].lower())
            projects.append({"id": project["id"], "name": project.get("name") or project["id"], "batches": batches})

    projects.sort(key=lambda project: project["name"].lower())
    return {"threshold": threshold, "projects": projects}
