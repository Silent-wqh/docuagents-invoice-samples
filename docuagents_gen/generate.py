"""Generate the 30-document main sample set, its split table and its previews (P4-P5).

The command line writes one complete, frozen version of the sample set:

    python -m docuagents_gen.generate --seed 2026 --out datasets/docuagents_v1
    python -m docuagents_gen.generate --seed 2026 --out <tmp> --check \
        --reference datasets/docuagents_v1/manifest.json

What the run produces, for every matrix row:

* ``documents/<doc_id>.<ext>`` -- the rendered file, exactly the bytes the
  upstream generator returns;
* ``gold/<doc_id>.json`` -- the gold record, built from the printed page with
  the real file name, size and SHA-256 (never a placeholder);
* ``text/<doc_id>.p<N>.txt`` -- the printed words of each page, NFC and LF;
* ``bases/<doc_id>-base.json`` -- the image base record of a raster document;
* ``previews/pages/<doc_id>.p<N>.png`` -- one image of each page;
* ``previews/contact_sheet.png`` -- every page in one labelled grid.

and once for the set: ``manifest.json``, ``splits.json``,
``matrix.snapshot.json`` and ``reports/check_summary.json``.

Publication is atomic.  All files go to a temporary directory next to the
output; every check runs there; then one ``os.replace`` moves the directory
to its final name.  A failed run keeps the temporary directory and writes an
``.incomplete`` marker into it, so a half-written tree can never look like a
finished version.

``--check`` regenerates the set in a second temporary directory and compares
it with the reference version.  The reference directory is only read.  A
failed check never rewrites the reference manifest or any stored digest.

Digest rules (``digest_method_version`` 1) are stated here, and again in:
:func:`canonical_bytes`, :func:`pdf_digests` and :func:`image_digests`:

* JSON: UTF-8, sorted keys, compact separators, no BOM, no trailing newline,
  arrays in order, money as strings -> SHA-256;
* text: NFC, LF -> UTF-8 bytes -> SHA-256;
* PDF: file SHA-256, plus a semantic page summary (page text, word count and
  the first and last word box, read with the pinned pdfplumber) and a pixel
  summary (144 DPI RGB render with the pinned pypdfium2);
* image: file SHA-256, plus the decoded RGB summary (Pillow, fixed
  orientation).

A semantic or pixel difference is a locator: it never excuses a byte
difference.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

import pdfplumber
import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageFont, ImageOps

from generator.generate import render_document
from generator.layout import build_pages, page_text

from . import build, codes, fixtures, gold, matrix, offline, split

ROOT = Path(__file__).resolve().parents[1]

#: The upstream commit the sample set is built on (pinned by the sample plan).
UPSTREAM_COMMIT = "3f8864d0d96103a279b5320000c69a71068edd14"

SCHEMA_VERSION = 1
CONTRACT_VERSION = codes.CONTRACT_VERSION
DIGEST_METHOD_VERSION = "1"

#: The default output directory, relative to the repository root.
DEFAULT_OUT = ROOT / "datasets" / "docuagents_v1"

#: The fixture subtree inside the output directory.  The fixtures publish with
#: the main set but stay out of the main manifest and the main counts.
FIXTURE_DIR_NAME = "boundary_fixtures"

#: Preview resolution: 144 DPI from the 72-point PDF page.
PREVIEW_SCALE = 2

#: The fixed library versions the pixel and text digests depend on.
DIGEST_VERSIONS = ("python", "reportlab", "pillow", "pypdfium2", "pdfplumber")

MEDIA_TYPES = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
}

CONTACT_COLUMNS = 6
CELL_WIDTH = 248
THUMB_WIDTH = 240
THUMB_HEIGHT = 312
LABEL_HEIGHT = 30
CELL_HEIGHT = THUMB_HEIGHT + LABEL_HEIGHT
SHEET_GAP = 8
SHEET_MARGIN = 8


class GenerateError(RuntimeError):
    """Raised when a generation or a check cannot finish.

    ``tmp_dir`` is the kept temporary directory of a failed generation, or
    ``None`` when the failure happened before an output was written.
    """

    def __init__(self, message: str, tmp_dir: str | None = None) -> None:
        super().__init__(message)
        self.tmp_dir = tmp_dir


# ---------------------------------------------------------------------------
# Canonical forms and digests
# ---------------------------------------------------------------------------


def canonical_bytes(obj: Any) -> bytes:
    """The canonical JSON bytes of ``obj`` (``digest_method_version`` 1).

    Sorted keys, compact separators, UTF-8 without BOM, NFC text, no trailing
    newline and no timestamp.  The same object always gives the same bytes.
    """
    text = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return unicodedata.normalize("NFC", text).encode("utf-8")


def canonical_sha256(obj: Any) -> str:
    """SHA-256 of :func:`canonical_bytes`."""
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


def normalize_text(text: str) -> str:
    """NFC text with LF line ends; the only form written to a ``.txt`` file."""
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))


def text_bytes(text: str) -> bytes:
    """The bytes of one text artifact."""
    return normalize_text(text).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_bytes(path: Path, data: bytes) -> None:
    """Write one artifact.  Every file of a generation goes through here."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _write_json(path: Path, obj: Any) -> None:
    _write_bytes(path, canonical_bytes(obj))


def _write_text(path: Path, text: str) -> None:
    _write_bytes(path, text_bytes(text))


def _read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Environment and source digests
# ---------------------------------------------------------------------------


def _installed_version(name: str) -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(name)
    except PackageNotFoundError:
        return "missing"


def environment_digest() -> Dict[str, Any]:
    """The lock file digest and the versions the digests depend on."""
    lock = ROOT / "environment.lock.json"
    raw = lock.read_bytes()
    versions = {
        "python": platform.python_version(),
        "reportlab": _installed_version("reportlab"),
        "pillow": _installed_version("pillow"),
        "pypdfium2": _installed_version("pypdfium2"),
        "pdfplumber": _installed_version("pdfplumber"),
    }
    payload = {"lockfile_sha256": _sha256(raw), "versions": versions}
    return {
        "lockfile": "environment.lock.json",
        **payload,
        "canonical_sha256": canonical_sha256(payload),
    }


def generator_source_digest() -> Dict[str, Any]:
    """A digest of the generator source and the copied schemas.

    Every ``docuagents_gen/*.py`` and ``schemas/*.json`` file is hashed on its
    own, in path order, and the list is hashed again.  A change in the code
    that writes the set therefore shows in the manifest.
    """
    files = []
    for path in sorted(list((ROOT / "docuagents_gen").glob("*.py")) + list((ROOT / "schemas").glob("*.json"))):
        files.append({"path": path.relative_to(ROOT).as_posix(), "sha256": _sha256(path.read_bytes())})
    return {"files": files, "canonical_sha256": canonical_sha256(files)}


def generator_commit() -> str | None:
    """The HEAD commit of the working copy, or ``None`` when git cannot say."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


# ---------------------------------------------------------------------------
# File digests: semantic pages and pixels
# ---------------------------------------------------------------------------


def _box(word: Mapping[str, Any]) -> List[float]:
    return [
        round(float(word["x0"]), 2),
        round(float(word["top"]), 2),
        round(float(word["x1"]), 2),
        round(float(word["bottom"]), 2),
    ]


def pdf_semantic_pages(data: bytes) -> List[Dict[str, Any]]:
    """Per-page text, word count and first/last word box (pdfplumber, pinned).

    The summary locates a difference.  It never replaces the byte check.
    """
    pages: List[Dict[str, Any]] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for index, page in enumerate(pdf.pages, 1):
            words = page.extract_words()
            text = normalize_text(page.extract_text() or "")
            pages.append(
                {
                    "page": index,
                    "text": text,
                    "word_count": len(words),
                    "first_word_bbox": _box(words[0]) if words else None,
                    "last_word_bbox": _box(words[-1]) if words else None,
                }
            )
    return pages


def raster_pages(data: bytes, *, limit: int | None = None) -> List[Image.Image]:
    """RGB images of a PDF's pages at 144 DPI (pypdfium2, pinned)."""
    images: List[Image.Image] = []
    document = pdfium.PdfDocument(data)
    try:
        for index in range(len(document)):
            if limit is not None and index >= limit:
                break
            bitmap = document[index].render(scale=PREVIEW_SCALE)
            images.append(bitmap.to_pil().convert("RGB"))
    finally:
        document.close()
    return images


def pdf_digests(data: bytes) -> Dict[str, Any]:
    """File digest, semantic page digest and pixel digest of one PDF."""
    semantic = pdf_semantic_pages(data)
    images = raster_pages(data)
    if len(images) != len(semantic):
        raise GenerateError(
            f"the PDF has {len(images)} rendered pages but {len(semantic)} text pages"
        )
    pixel_parts = [{"page": index, "sha256": _sha256(image.tobytes())}
                   for index, image in enumerate(images, 1)]
    return {
        "byte_sha256": _sha256(data),
        "semantic_sha256": canonical_sha256(semantic),
        "semantic_pages": len(semantic),
        "word_count": sum(page["word_count"] for page in semantic),
        "pixel_sha256": canonical_sha256(pixel_parts),
        "width": images[0].width if images else 0,
        "height": images[0].height if images else 0,
    }


def decode_image(data: bytes) -> Image.Image:
    """The processed RGB form of an image file (EXIF orientation applied)."""
    with Image.open(io.BytesIO(data)) as source:
        source.load()
        return ImageOps.exif_transpose(source).convert("RGB")


def image_digests(data: bytes) -> Dict[str, Any]:
    """File digest and processed RGB digest of one image file."""
    image = decode_image(data)
    return {
        "byte_sha256": _sha256(data),
        "pixel_sha256": _sha256(image.tobytes()),
        "width": image.width,
        "height": image.height,
    }


def file_digests(path: Path, media_type: str) -> Dict[str, Any]:
    """The digest entry of one artifact, by media type."""
    data = path.read_bytes()
    if media_type == "application/pdf":
        return pdf_digests(data)
    if media_type in ("image/png", "image/jpeg"):
        return image_digests(data)
    return {"byte_sha256": _sha256(data)}


# ---------------------------------------------------------------------------
# Matrix snapshot
# ---------------------------------------------------------------------------


def matrix_snapshot() -> Dict[str, Any]:
    """The frozen matrix facts, including the A3 candidate evidence."""
    candidates = build.a3_candidates()
    passed = [index + 1 for index, c in enumerate(candidates) if c["passed"]]
    frozen = next(
        candidate for candidate in candidates
        if candidate["rows_per_page"] == matrix.A3_ROWS_PER_PAGE
        and candidate["line_count"] == matrix.A3_LINE_COUNT
        and candidate["passed"]
    )
    documents = []
    fingerprints = build.fingerprints()
    for row in matrix.ROWS:
        documents.append(
            {
                "doc_id": row.doc_id,
                "split": row.split,
                "class": row.klass,
                "subclass": row.subclass,
                "group_id": split.group_id(row),
                "vendor_id": split.vendor_id(row),
                "base_id": split.base_id(row),
                "fingerprint": fingerprints[row.doc_id],
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "seed": matrix.SEED,
        "matrix_digest": canonical_sha256(fingerprints),
        "a3": {
            "rows_per_page": matrix.A3_ROWS_PER_PAGE,
            "line_count": matrix.A3_LINE_COUNT,
            "description_form": frozen["description_form"],
            "candidates_total": len(candidates),
            "passed_count": len(passed),
            "first_pass_index": passed[0] if passed else None,
        },
        "docs": documents,
    }


# ---------------------------------------------------------------------------
# Previews
# ---------------------------------------------------------------------------


def _font(size: int) -> Any:
    """A deterministic label font.

    Pillow's built-in default font is used.  It is part of the pinned Pillow
    wheel, so its raster cannot differ between machines with the same
    versions.  The choice is recorded in ``reports/check_summary.json``.
    """
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - older Pillow without the size argument
        return ImageFont.load_default()


FONT_CHOICE = "PIL.ImageFont.load_default (Pillow 12.3.0, size argument)"


def _page_images(row: matrix.Row, data: bytes) -> List[Image.Image]:
    if row.render_mode == "pdf":
        return raster_pages(data)
    return [decode_image(data)]


def sample_page_counts() -> Dict[str, int]:
    """The printed page count of every document, from the layout engine."""
    return {row.doc_id: len(build_pages(build.build_spec(row))) for row in matrix.ROWS}


def contact_sheet_layout() -> Dict[str, Any]:
    """The cells of the contact sheet, in matrix order and page order.

    Every page of every document has one cell.  A cell carries the document
    id, the page number, the subclass and the prefilled legibility gate of the
    matrix, so the sheet shows what a reader may expect to read.
    """
    page_counts = sample_page_counts()
    cells: List[Dict[str, Any]] = []
    for row in matrix.ROWS:
        for page in range(1, page_counts[row.doc_id] + 1):
            cells.append(
                {
                    "doc_id": row.doc_id,
                    "page": page,
                    "subclass": row.subclass,
                    "legibility": row.legibility,
                    "labels": [f"{row.doc_id}  p{page}", f"{row.subclass}  {row.legibility}"],
                }
            )
    rows = (len(cells) + CONTACT_COLUMNS - 1) // CONTACT_COLUMNS
    width = 2 * SHEET_MARGIN + CONTACT_COLUMNS * CELL_WIDTH + (CONTACT_COLUMNS - 1) * SHEET_GAP
    height = 2 * SHEET_MARGIN + rows * CELL_HEIGHT + (rows - 1) * SHEET_GAP
    for index, cell in enumerate(cells):
        column, line = index % CONTACT_COLUMNS, index // CONTACT_COLUMNS
        cell["box"] = [
            SHEET_MARGIN + column * (CELL_WIDTH + SHEET_GAP),
            SHEET_MARGIN + line * (CELL_HEIGHT + SHEET_GAP),
            CELL_WIDTH,
            CELL_HEIGHT,
        ]
    return {
        "columns": CONTACT_COLUMNS,
        "rows": rows,
        "cells": cells,
        "size": [width, height],
        "font": FONT_CHOICE,
    }


def render_contact_sheet(preview_dir: Path, path: Path) -> None:
    """Draw every page thumbnail with its label into one PNG."""
    layout = contact_sheet_layout()
    sheet = Image.new("RGB", tuple(layout["size"]), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    title_font = _font(14)
    label_font = _font(12)
    for cell in layout["cells"]:
        x, y, width, height = cell["box"]
        draw.rectangle([x, y, x + width - 1, y + height - 1], outline=(200, 200, 200))
        source = preview_dir / f"{cell['doc_id']}.p{cell['page']}.png"
        with Image.open(source) as page_image:
            thumb = page_image.convert("RGB").copy()
        thumb.thumbnail((THUMB_WIDTH, THUMB_HEIGHT), Image.LANCZOS)
        offset_x = x + (width - thumb.width) // 2
        offset_y = y + (THUMB_HEIGHT - thumb.height) // 2
        sheet.paste(thumb, (offset_x, offset_y))
        draw.text((x + 6, y + THUMB_HEIGHT + 1), cell["labels"][0], fill=(0, 0, 0), font=title_font)
        draw.text((x + 6, y + THUMB_HEIGHT + 15), cell["labels"][1], fill=(70, 70, 70), font=label_font)
    buffer = io.BytesIO()
    sheet.save(buffer, "PNG", optimize=False)
    _write_bytes(path, buffer.getvalue())


def preview_bytes(row: matrix.Row, page_image: Image.Image) -> bytes:
    """The PNG bytes of one preview page."""
    buffer = io.BytesIO()
    page_image.convert("RGB").save(buffer, "PNG", optimize=False)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# One document
# ---------------------------------------------------------------------------


def _extension(row: matrix.Row) -> str:
    return "pdf" if row.render_mode == "pdf" else row.render_fmt


def _media_type(ext: str) -> str:
    try:
        return MEDIA_TYPES[ext.lower()]
    except KeyError as exc:
        raise GenerateError(f"unknown file extension {ext!r}") from exc


def base_record(row: matrix.Row, spec: Any, seed: int) -> Dict[str, Any]:
    """The ``bases/<id>.json`` record of one raster document."""
    if row.render_mode != "image":
        raise GenerateError(f"{row.doc_id}: a PDF document has no image base")
    return {
        "base_id": split.base_id(row),
        "doc_id": row.doc_id,
        "spec_fingerprint": build.spec_fingerprint(spec),
        "render": {
            "mode": row.render_mode,
            "fmt": row.render_fmt,
            "dpi": spec.render.dpi,
            "degradations": [
                {"kind": kind, "severity": severity} for kind, severity in row.degradations
            ],
        },
        "seed": seed,
        "upstream_commit": UPSTREAM_COMMIT,
    }


def _check_rendered(row: matrix.Row, data: bytes, ext: str, page_count: int, pages: Sequence[Any]) -> None:
    """The D3 checks between the rendered bytes and the printed pages."""
    if not data:
        raise GenerateError(f"{row.doc_id}: the renderer returned no bytes")
    expected_ext = _extension(row)
    if ext != expected_ext:
        raise GenerateError(f"{row.doc_id}: rendered {ext!r}, expected {expected_ext!r}")
    if row.render_mode == "image" and page_count != 1:
        raise GenerateError(f"{row.doc_id}: an image document must be one page, got {page_count}")
    if len(pages) != page_count:
        raise GenerateError(
            f"{row.doc_id}: the layout has {len(pages)} pages, the render reports {page_count}"
        )


def generate_document(
    row: matrix.Row,
    tmp: Path,
    *,
    seed: int,
    preview_dir: Path,
) -> Dict[str, Any]:
    """Render, write and describe one document.  Returns its file entries."""
    spec = build.build_spec(row)
    data, ext, page_count, _info = render_document(spec, seed)
    pages = build_pages(spec)
    _check_rendered(row, data, ext, page_count, pages)

    document_rel = f"documents/{row.doc_id}.{ext}"
    media_type = _media_type(ext)
    file_meta = {
        "file_name": document_rel,
        "sha256": _sha256(data),
        "media_type": media_type,
        "page_count": len(pages),
    }
    record = gold.build_gold(spec, pages, file_meta)
    if record["file"]["sha256"] != _sha256(data):
        raise GenerateError(f"{row.doc_id}: the gold digest is not the file digest")

    _write_bytes(tmp / document_rel, data)
    written = (tmp / document_rel).read_bytes()
    if len(written) != len(data):
        raise GenerateError(
            f"{row.doc_id}: the written file holds {len(written)} bytes, the render {len(data)}"
        )
    _write_json(tmp / "gold" / f"{row.doc_id}.json", record)

    text_entries: List[Dict[str, Any]] = []
    preview_entries: List[Dict[str, Any]] = []
    for index, page in enumerate(pages, 1):
        text_rel = f"text/{row.doc_id}.p{index}.txt"
        _write_text(tmp / text_rel, page_text(page))
        text_entries.append(
            {
                "path": text_rel,
                "media_type": "text/plain",
                "artifact_role": "generated",
                "doc_id": row.doc_id,
                "page": index,
            }
        )

    page_images = _page_images(row, data)
    if len(page_images) != len(pages):
        raise GenerateError(f"{row.doc_id}: {len(page_images)} preview images for {len(pages)} pages")
    for index, page_image in enumerate(page_images, 1):
        preview_rel = f"previews/pages/{row.doc_id}.p{index}.png"
        preview_data = preview_bytes(row, page_image)
        _write_bytes(preview_dir / f"{row.doc_id}.p{index}.png", preview_data)
        preview_entries.append(
            {
                "path": preview_rel,
                "media_type": "image/png",
                "artifact_role": "generated",
                "doc_id": row.doc_id,
                "page": index,
                "digests": image_digests(preview_data),
            }
        )

    common = {
        "doc_id": row.doc_id,
        "page_count": len(pages),
        "split": row.split,
        "class": row.klass,
        "subclass": row.subclass,
        "group_id": split.group_id(row),
        "vendor_id": split.vendor_id(row),
        "base_id": split.base_id(row),
    }
    document_entry = {
        "path": document_rel,
        "media_type": media_type,
        "artifact_role": "generated",
        **common,
        "digests": file_digests(tmp / document_rel, media_type),
    }
    gold_entry = {
        "path": f"gold/{row.doc_id}.json",
        "media_type": "application/json",
        "artifact_role": "generated",
        **common,
    }
    entries = [document_entry, gold_entry, *text_entries, *preview_entries]

    if row.render_mode == "image":
        base = base_record(row, spec, seed)
        base_rel = f"bases/{base['base_id']}.json"
        _write_json(tmp / base_rel, base)
        entries.append(
            {
                "path": base_rel,
                "media_type": "application/json",
                "artifact_role": "generated",
                "doc_id": row.doc_id,
                "base_id": base["base_id"],
            }
        )
    return {"entries": entries, "pages": len(pages), "bytes": len(data)}


# ---------------------------------------------------------------------------
# Reference integrity
# ---------------------------------------------------------------------------


def verify_references(out_dir: Path) -> Dict[str, Any]:
    """Prove that every document has its artifacts and its gold is honest.

    Returns ``{"documents", "missing_references", "details"}``.  A missing
    artifact, a wrong file digest, a wrong page count or an evidence page
    outside the file is one missing reference.
    """
    out = Path(out_dir)
    details: List[str] = []
    for row in matrix.ROWS:
        gold_path = out / "gold" / f"{row.doc_id}.json"
        if not gold_path.is_file():
            details.append(f"{row.doc_id}: no gold record")
            continue
        record = _read_json(gold_path)
        if record.get("doc_id") != row.doc_id:
            details.append(f"{row.doc_id}: the gold file names {record.get('doc_id')!r}")
        if record.get("split") != row.split:
            details.append(f"{row.doc_id}: gold.split {record.get('split')!r} is not {row.split!r}")
        if record.get("class") != row.klass or record.get("subclass") != row.subclass:
            details.append(f"{row.doc_id}: the gold class/subclass does not match the matrix")
        file_name = record["file"]["file_name"]
        document = out / file_name
        if not document.is_file():
            details.append(f"{row.doc_id}: no document {file_name}")
        elif _sha256(document.read_bytes()) != record["file"]["sha256"]:
            details.append(f"{row.doc_id}: the gold digest does not match the file")
        page_count = record["file"]["page_count"]
        for index in range(1, page_count + 1):
            if not (out / "text" / f"{row.doc_id}.p{index}.txt").is_file():
                details.append(f"{row.doc_id}: no text for page {index}")
            if not (out / "previews" / "pages" / f"{row.doc_id}.p{index}.png").is_file():
                details.append(f"{row.doc_id}: no preview for page {index}")
        for key, entry in record["evidence"].items():
            page = entry.get("page")
            if page is not None and not (1 <= page <= page_count):
                details.append(f"{row.doc_id}: evidence {key} points at page {page}")
            for searched in entry.get("searched_pages", []):
                if not (1 <= searched <= page_count):
                    details.append(f"{row.doc_id}: evidence {key} searched page {searched}")
        if row.render_mode == "image":
            base = out / "bases" / f"{split.base_id(row)}.json"
            if not base.is_file():
                details.append(f"{row.doc_id}: no image base")
    return {"documents": len(matrix.ROWS), "missing_references": len(details), "details": details}


# ---------------------------------------------------------------------------
# The generation
# ---------------------------------------------------------------------------


def _matrix_order() -> List[str]:
    return [row.doc_id for row in matrix.ROWS]


def _check_order(order: Sequence[str]) -> List[matrix.Row]:
    """The rows in processing order; ``order`` must be a permutation."""
    wanted = _matrix_order()
    if sorted(order) != sorted(wanted) or len(order) != len(set(order)):
        raise GenerateError("the processing order must name every matrix document exactly once")
    return [matrix.BY_ID[doc_id] for doc_id in order]


def _refuse_existing_output(out: Path) -> None:
    """The frozen-version rule: never write over a directory with content."""
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise GenerateError(f"the output directory {out} exists and is not empty")


def counts_of(files: Sequence[Mapping[str, Any]] | None = None) -> Dict[str, Any]:
    """The counts block of the manifest and of the run report."""
    class_counts = {name: sum(1 for row in matrix.ROWS if row.klass == name)
                    for name in sorted(matrix.CLASS_COUNTS)}
    subclass_counts = {name: sum(1 for row in matrix.ROWS if row.subclass == name)
                       for name in sorted(matrix.SUBCLASS_COUNTS)}
    page_counts = sample_page_counts()
    return {
        "total": len(matrix.ROWS),
        "dev": sum(1 for row in matrix.ROWS if row.split == "dev"),
        "test": sum(1 for row in matrix.ROWS if row.split == "test"),
        "pages": sum(page_counts.values()),
        "preview_pages": sum(page_counts.values()),
        "documents_with_base": sum(1 for row in matrix.ROWS if row.render_mode == "image"),
        "class": class_counts,
        "subclass": subclass_counts,
    }


def _entry_with_size(tmp: Path, entry: Mapping[str, Any]) -> Dict[str, Any]:
    """Add ``bytes`` and ``sha256`` from the file on disk."""
    data = (tmp / entry["path"]).read_bytes()
    out = dict(entry)
    out["bytes"] = len(data)
    out["sha256"] = _sha256(data)
    return out


def _stable_manifest_digest(manifest_doc: Mapping[str, Any]) -> str:
    """The digest of the manifest without its runtime report entries.

    The run report is written before the manifest, and the manifest lists the
    report.  Hashing the stable part breaks that cycle and keeps the value
    free of timings, as the digest rules require.
    """
    stable = {
        "schema_version": manifest_doc["schema_version"],
        "contract_version": manifest_doc["contract_version"],
        "seed": manifest_doc["seed"],
        "upstream_commit": manifest_doc["upstream_commit"],
        "generator_source_digest": manifest_doc["generator_source_digest"],
        "matrix_digest": manifest_doc["matrix_digest"],
        "environment_digest": manifest_doc["environment_digest"],
        "digest_method_version": manifest_doc["digest_method_version"],
        "counts": manifest_doc["counts"],
        "files": [entry for entry in manifest_doc["files"] if entry["artifact_role"] != "runtime_report"],
    }
    return canonical_sha256(stable)


def _mark_incomplete(tmp: Path, reason: str) -> None:
    """Leave the temporary directory, but make its state plain."""
    try:
        (tmp / ".incomplete").write_text(
            f"This directory is not a finished sample set.\n{reason}\n", encoding="utf-8", newline="\n"
        )
    except OSError:
        pass


def _publish(tmp: Path, out: Path) -> None:
    """Move the finished temporary directory to its final name.

    ``generate_dataset`` already refused a non-empty output.  An empty
    directory at that path is removed first.  A directory that appeared while
    the run was writing holds data this run did not write, so the move is
    refused rather than replacing it.
    """
    if out.exists():
        if not out.is_dir() or any(out.iterdir()):
            raise GenerateError(f"the output directory {out} appeared during the run")
        out.rmdir()
    try:
        os.replace(tmp, out)
    except OSError as exc:
        raise GenerateError(f"cannot publish {out}: {exc}") from exc


def generate_dataset(
    out_dir: Path,
    *,
    seed: int = matrix.SEED,
    order: Sequence[str] | None = None,
    quiet: bool = False,
) -> Dict[str, Any]:
    """Write one complete sample set to ``out_dir``; return the run summary.

    Args:
        out_dir: the final directory.  It must not exist, or must be empty.
        seed: the generator seed; the matrix seed by default.
        order: the doc ids in processing order (tests use this).  The written
            entries are always in matrix order, so the order cannot change a
            byte.
        quiet: do not print the per-document lines.
    """
    if seed != matrix.SEED:
        raise GenerateError(f"the sample plan fixes the seed at {matrix.SEED}, got {seed}")
    rows = _check_order(list(order) if order is not None else _matrix_order())

    out = Path(out_dir)
    # The frozen-version rule is checked before any work: an existing output
    # is refused, and a temporary directory is never created for it.
    _refuse_existing_output(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    tmp = Path(tempfile.mkdtemp(prefix=".tmp-docuagents-v1-", dir=str(out.parent)))
    fixture_summary: Dict[str, Any] = {}
    try:
        entries: List[Dict[str, Any]] = []
        total_pages = 0
        document_bytes = 0
        (tmp / "previews" / "pages").mkdir(parents=True, exist_ok=True)
        for index, row in enumerate(rows, 1):
            result = generate_document(row, tmp, seed=seed, preview_dir=tmp / "previews" / "pages")
            entries.extend(result["entries"])
            total_pages += result["pages"]
            document_bytes += result["bytes"]
            if not quiet:
                print(f"[{index:3d}/{len(rows)}] {row.doc_id} ({result['pages']}p, {result['bytes']} bytes)", flush=True)

        splits_doc = split.build_splits()
        _write_json(tmp / "splits.json", splits_doc)
        snapshot = matrix_snapshot()
        _write_json(tmp / "matrix.snapshot.json", snapshot)
        entries.append({"path": "splits.json", "media_type": "application/json", "artifact_role": "generated"})
        entries.append({"path": "matrix.snapshot.json", "media_type": "application/json",
                        "artifact_role": "generated"})

        render_contact_sheet(tmp / "previews" / "pages", tmp / "previews" / "contact_sheet.png")
        entries.append({"path": "previews/contact_sheet.png", "media_type": "image/png",
                        "artifact_role": "generated"})

        # The split table must hold now; a broken isolation stops the run.
        split.check_isolation(splits_doc["documents"])
        references = verify_references(tmp)
        if references["missing_references"]:
            raise GenerateError(
                "the generated set has missing references: " + "; ".join(references["details"][:5])
            )

        files = sorted((_entry_with_size(tmp, entry) for entry in entries), key=lambda e: e["path"])

        environment = environment_digest()
        manifest_doc: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "contract_version": CONTRACT_VERSION,
            "seed": seed,
            "upstream_commit": UPSTREAM_COMMIT,
            "generator_commit": generator_commit(),
            "generator_source_digest": generator_source_digest(),
            "matrix_digest": snapshot["matrix_digest"],
            "environment_digest": environment,
            "digest_method_version": DIGEST_METHOD_VERSION,
            "counts": counts_of(),
            "files": [],
        }
        manifest_doc["files"] = files
        manifest_digest = _stable_manifest_digest(manifest_doc)

        report = {
            "schema_version": SCHEMA_VERSION,
            "contract_version": CONTRACT_VERSION,
            "seed": seed,
            "upstream_commit": UPSTREAM_COMMIT,
            "generator_commit": manifest_doc["generator_commit"],
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "duration_seconds": round(time.time() - started, 3),
            "counts": manifest_doc["counts"],
            "manifest_digest": manifest_digest,
            "matrix_digest": manifest_doc["matrix_digest"],
            "environment_digest": environment["canonical_sha256"],
            "environment_lockfile_sha256": environment["lockfile_sha256"],
            "contact_sheet_font": FONT_CHOICE,
            "reference_integrity": {
                "documents": references["documents"],
                "missing_references": references["missing_references"],
            },
            "isolation": split.check_isolation(splits_doc["documents"]),
            "isolation_rule": split.check_rule(),
            "price_policy": {
                "rounding_branch_documents": 0,
                "note": "every main sample prints unit prices with two decimals, so rule A uses the strict branch",
            },
            "documents": len(matrix.ROWS),
            "pages": total_pages,
            "document_bytes": document_bytes,
        }
        _write_json(tmp / "reports" / "check_summary.json", report)
        report_entry = _entry_with_size(
            tmp, {"path": "reports/check_summary.json", "media_type": "application/json",
                  "artifact_role": "runtime_report"}
        )
        files = sorted([*files, report_entry], key=lambda e: e["path"])
        manifest_doc["files"] = files
        _write_json(tmp / "manifest.json", manifest_doc)

        # The boundary and technical fixtures live beside the main set, with
        # their own manifest and their own counts.  They share this temporary
        # directory, so one atomic publish writes both trees.  No fixture path
        # enters the main manifest.
        fixture_summary = fixtures.generate_boundary_fixtures(
            tmp / FIXTURE_DIR_NAME, seed=fixtures.FIXTURE_SEED
        )

        _publish(tmp, out)
    except BaseException as exc:
        _mark_incomplete(tmp, f"{type(exc).__name__}: {exc}")
        if isinstance(exc, GenerateError):
            exc.tmp_dir = exc.tmp_dir or str(tmp)
            raise
        raise GenerateError(str(exc), tmp_dir=str(tmp)) from exc

    return {
        "out": str(out),
        "seed": seed,
        "documents": len(matrix.ROWS),
        "pages": total_pages,
        "files": len(manifest_doc["files"]) + 1,
        "bytes": sum(entry["bytes"] for entry in manifest_doc["files"]),
        "document_bytes": document_bytes,
        "manifest_digest": manifest_digest,
        "matrix_digest": manifest_doc["matrix_digest"],
        "environment_digest": manifest_doc["environment_digest"]["canonical_sha256"],
        "generator_commit": manifest_doc["generator_commit"],
        "duration_seconds": round(time.time() - started, 3),
        "tmp_dir": str(tmp),
        "fixtures": fixture_summary,
    }


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def _directory_paths(root: Path) -> set:
    """Every file in ``root``, minus the fixture subtree.

    The fixtures publish beside the main set and have their own manifest, so
    the main file-set check must not see them as undeclared files.
    """
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
        and not path.relative_to(root).as_posix().startswith(FIXTURE_DIR_NAME + "/")
    }


def _compare_digests(name: str, reference: Path, candidate: Path, kind: str) -> List[str]:
    """Compare the semantic and pixel summaries; name the differing page."""
    reference_data = reference.read_bytes()
    candidate_data = candidate.read_bytes()
    failures: List[str] = []
    if kind == "application/pdf":
        ref_pages = pdf_semantic_pages(reference_data)
        cand_pages = pdf_semantic_pages(candidate_data)
        if canonical_sha256(ref_pages) != canonical_sha256(cand_pages):
            differ = [
                index
                for index, (left, right) in enumerate(zip(ref_pages, cand_pages), 1)
                if left != right
            ]
            if len(ref_pages) != len(cand_pages):
                differ.append("page-count")
            failures.append(f"{name}: the semantic summary differs (pages {differ})")
        ref_pixels = [_sha256(image.tobytes()) for image in raster_pages(reference_data)]
        cand_pixels = [_sha256(image.tobytes()) for image in raster_pages(candidate_data)]
        if ref_pixels != cand_pixels:
            differ = [
                index
                for index, (left, right) in enumerate(zip(ref_pixels, cand_pixels), 1)
                if left != right
            ]
            failures.append(f"{name}: the pixel summary differs (pages {differ})")
        return failures
    ref_image = image_digests(reference_data)
    cand_image = image_digests(candidate_data)
    if ref_image["pixel_sha256"] != cand_image["pixel_sha256"]:
        failures.append(f"{name}: the pixel summary differs")
    return failures


def compare_datasets(candidate_dir: Path, reference_dir: Path) -> Dict[str, Any]:
    """Compare one generated tree with the reference tree; never write.

    Returns ``{"ok", "failures", "checks", "counts"}``.  The reference manifest
    and the reference files are the only baseline; a fresh manifest is never
    used to approve itself.
    """
    candidate = Path(candidate_dir)
    reference = Path(reference_dir)
    failures: List[str] = []
    checks: Dict[str, Any] = {}
    counts = {
        "missing": 0,
        "extra": 0,
        "generated_total": 0,
        "generated_compared": 0,
        "semantic_compared": 0,
        "pdf_pixel_compared": 0,
        "image_pixel_compared": 0,
    }

    reference_manifest_path = reference / "manifest.json"
    candidate_manifest_path = candidate / "manifest.json"
    if not reference_manifest_path.is_file():
        raise GenerateError(f"the reference manifest does not exist: {reference_manifest_path}")
    if not candidate_manifest_path.is_file():
        return {"ok": False, "failures": [f"no manifest in the candidate tree: {candidate_manifest_path}"],
                "checks": checks, "counts": counts}
    reference_manifest = _read_json(reference_manifest_path)
    candidate_manifest = _read_json(candidate_manifest_path)

    # 1. The file set.
    expected = {entry["path"] for entry in reference_manifest["files"]} | {"manifest.json"}
    actual = _directory_paths(candidate)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    counts["missing"], counts["extra"] = len(missing), len(extra)
    if missing:
        failures.append("missing files: " + ", ".join(missing[:10]))
    if extra:
        failures.append("undeclared files: " + ", ".join(extra[:10]))
    checks["file_set"] = {"missing": missing, "extra": extra}

    # 2. Byte equality of every generated artifact, and the roles.
    role_failures: List[str] = []
    runtime_reports = 0
    for entry in sorted(reference_manifest["files"], key=lambda e: e["path"]):
        rel = entry["path"]
        reference_file = reference / rel
        candidate_file = candidate / rel
        role = entry["artifact_role"]
        if not reference_file.is_file():
            role_failures.append(f"{rel}: the reference file is missing")
            continue
        if role == "runtime_report":
            runtime_reports += 1
            if not candidate_file.is_file():
                role_failures.append(f"{rel}: the run report is missing")
            else:
                try:
                    json.loads(candidate_file.read_text(encoding="utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    role_failures.append(f"{rel}: the run report is not readable JSON")
            continue
        counts["generated_total"] += 1
        if _sha256(reference_file.read_bytes()) != entry["sha256"]:
            role_failures.append(f"{rel}: the reference manifest does not match the reference file")
        if not candidate_file.is_file():
            continue
        counts["generated_compared"] += 1
        if _sha256(candidate_file.read_bytes()) != _sha256(reference_file.read_bytes()):
            role_failures.append(f"{rel}: the bytes differ from the reference")
            continue
        media_type = entry.get("media_type")
        if media_type == "application/pdf":
            counts["semantic_compared"] += 1
            counts["pdf_pixel_compared"] += 1
            role_failures.extend(_compare_digests(rel, reference_file, candidate_file, media_type))
        elif media_type in ("image/png", "image/jpeg") and rel.startswith("documents/"):
            counts["image_pixel_compared"] += 1
            role_failures.extend(_compare_digests(rel, reference_file, candidate_file, media_type))
    if runtime_reports != 1:
        role_failures.append(f"the reference must hold exactly one run report, got {runtime_reports}")
    failures.extend(role_failures)
    checks["generated_bytes"] = {"compared": counts["generated_compared"],
                                 "total": counts["generated_total"]}

    # 3. The matrix snapshot and the matrix digest.
    matrix_failures: List[str] = []
    if reference_manifest["matrix_digest"] != candidate_manifest.get("matrix_digest"):
        matrix_failures.append("the matrix digest differs")
    if reference_manifest["matrix_digest"] != canonical_sha256(build.fingerprints()):
        matrix_failures.append("the matrix digest does not match the current matrix")
    failures.extend(matrix_failures)
    checks["matrix"] = {"failures": matrix_failures}

    # 4. The split table, with the isolation and the reference checks rerun.
    split_failures: List[str] = []
    candidate_splits_path = candidate / "splits.json"
    if not candidate_splits_path.is_file():
        split_failures.append("splits.json is missing")
    else:
        try:
            candidate_splits = _read_json(candidate_splits_path)
        except json.JSONDecodeError:
            candidate_splits = None
            split_failures.append("splits.json is not readable JSON")
        if candidate_splits is not None:
            try:
                split.check_isolation(candidate_splits["documents"])
            except (split.SplitError, KeyError) as exc:
                split_failures.append(f"the isolation does not hold: {exc}")
            try:
                split.check_references(candidate_splits["documents"], _document_ids(candidate))
            except (split.SplitError, KeyError) as exc:
                split_failures.append(f"the split table does not agree with the files: {exc}")
    failures.extend(split_failures)
    checks["splits"] = {"failures": split_failures}

    # 5. Reference integrity inside the candidate tree.
    integrity = verify_references(candidate)
    checks["references"] = {"documents": integrity["documents"],
                            "missing_references": integrity["missing_references"]}
    if integrity["missing_references"]:
        failures.append(
            f"{integrity['missing_references']} missing references: "
            + "; ".join(integrity["details"][:5])
        )
    # The summaries must have run for every PDF and every raster document; a
    # short count would silently weaken the check.
    expected_semantic = sum(1 for entry in reference_manifest["files"]
                            if entry.get("media_type") == "application/pdf")
    expected_images = sum(1 for entry in reference_manifest["files"]
                          if entry["path"].startswith("documents/")
                          and entry.get("media_type", "").startswith("image/"))
    if counts["semantic_compared"] != expected_semantic:
        failures.append(
            f"the semantic summary was compared for {counts['semantic_compared']} PDF files, "
            f"expected {expected_semantic}"
        )
    if counts["pdf_pixel_compared"] != expected_semantic:
        failures.append(
            f"the PDF pixel summary was compared for {counts['pdf_pixel_compared']} files, "
            f"expected {expected_semantic}"
        )
    if counts["image_pixel_compared"] != expected_images:
        failures.append(
            f"the image pixel summary was compared for {counts['image_pixel_compared']} files, "
            f"expected {expected_images}"
        )

    # 6. The version fields.
    version_failures: List[str] = []
    for field in ("schema_version", "contract_version", "digest_method_version",
                  "upstream_commit", "generator_source_digest"):
        if reference_manifest.get(field) != candidate_manifest.get(field):
            version_failures.append(f"{field} differs")
    failures.extend(version_failures)
    checks["versions"] = {"failures": version_failures}

    # 7. The boundary and technical fixtures.  They have their own manifest and
    #    their own counts; the reference fixture manifest is the only baseline.
    fixture_result = fixtures.check_fixtures(candidate / FIXTURE_DIR_NAME,
                                             reference / FIXTURE_DIR_NAME / "manifest.json")
    counts["fixture_compared"] = fixture_result["counts"]["generated_compared"]
    counts["fixture_total"] = fixture_result["counts"]["generated_total"]
    counts["fixture_missing"] = fixture_result["counts"]["missing"]
    counts["fixture_extra"] = fixture_result["counts"]["extra"]
    fixture_failures = [f"fixtures: {failure}" for failure in fixture_result["failures"]]
    failures.extend(fixture_failures)
    checks["fixtures"] = {"ok": fixture_result["ok"], "failures": fixture_failures,
                          "counts": fixture_result["counts"]}

    return {"ok": not failures, "failures": failures, "checks": checks, "counts": counts}


def _document_ids(tree: Path) -> set:
    documents = tree / "documents"
    if not documents.is_dir():
        return set()
    return {path.stem for path in documents.iterdir() if path.is_file()}


def check_dataset(
    out_dir: Path,
    *,
    reference: Path,
    seed: int = matrix.SEED,
    quiet: bool = False,
) -> Dict[str, Any]:
    """Generate a fresh copy into ``out_dir`` and compare it with ``reference``.

    The reference directory is only read.  ``out_dir`` must not be the
    reference directory or the same path.
    """
    reference_manifest = Path(reference).resolve()
    reference_dir = reference_manifest.parent
    out = Path(out_dir).resolve()
    if out == reference_dir:
        raise GenerateError("--check needs an output directory other than the reference directory")
    if out == reference_manifest:
        raise GenerateError("--check needs an output directory, not the reference manifest")
    if reference_dir == out or reference_dir.is_relative_to(out):
        raise GenerateError("--check needs an output directory that does not contain the reference")
    _refuse_existing_output(Path(out_dir))
    generated = generate_dataset(out_dir, seed=seed, quiet=quiet)
    result = compare_datasets(out, reference_dir)
    result["generated"] = generated
    result["reference"] = str(reference_manifest)
    return result


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _print_check(result: Mapping[str, Any]) -> None:
    counts = result["counts"]
    print(f"reference: {result['reference']}")
    print(f"files compared: {counts['generated_compared']}/{counts['generated_total']}")
    print(f"fixture files compared: {counts['fixture_compared']}/{counts['fixture_total']}"
          f" (missing {counts['fixture_missing']}, undeclared {counts['fixture_extra']})")
    print(f"missing files: {counts['missing']}, undeclared files: {counts['extra']}")
    print(f"semantic summaries compared: {counts['semantic_compared']}")
    print(f"pixel summaries compared: PDF {counts['pdf_pixel_compared']}, "
          f"image {counts['image_pixel_compared']}")
    references = result["checks"]["references"]
    print(f"reference integrity: {references['documents']} documents, "
          f"{references['missing_references']} missing")
    if result["ok"]:
        print("check passed: the generated set matches the reference")
    else:
        print("check failed:")
        for failure in result["failures"]:
            print(f"  - {failure}")


def main(argv: Sequence[str] | None = None) -> int:
    """The command-line entry point.  Installs the offline guard first."""
    offline.install_network_guard()
    parser = argparse.ArgumentParser(
        prog="python -m docuagents_gen.generate",
        description="Generate the DocuAgentS main sample set.",
    )
    parser.add_argument("--seed", type=int, default=matrix.SEED)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--check", action="store_true",
                        help="regenerate into --out and compare with the reference manifest")
    parser.add_argument("--reference", type=Path, default=None,
                        help="the reference manifest (required with --check)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if args.check and args.reference is None:
        print("error: --check needs --reference <manifest.json>", file=sys.stderr)
        return 2
    try:
        if args.check:
            result = check_dataset(args.out, reference=args.reference, seed=args.seed, quiet=args.quiet)
            _print_check(result)
            return 0 if result["ok"] else 1
        summary = generate_dataset(args.out, seed=args.seed, quiet=args.quiet)
    except GenerateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if exc.tmp_dir:
            print(f"the incomplete output is kept at: {exc.tmp_dir}", file=sys.stderr)
        return 1
    except offline.NetworkBlocked as exc:  # pragma: no cover - defensive
        print(f"error: the run tried to use the network: {exc}", file=sys.stderr)
        return 1
    print(f"{summary['documents']} documents, {summary['pages']} pages -> {summary['out']}")
    print(f"manifest digest: {summary['manifest_digest']}")
    print(f"matrix digest: {summary['matrix_digest']}")
    print(f"generator commit: {summary['generator_commit']}")
    return 0


__all__ = [
    "CONTACT_COLUMNS",
    "DIGEST_METHOD_VERSION",
    "DEFAULT_OUT",
    "GenerateError",
    "SCHEMA_VERSION",
    "UPSTREAM_COMMIT",
    "base_record",
    "canonical_bytes",
    "canonical_sha256",
    "check_dataset",
    "compare_datasets",
    "contact_sheet_layout",
    "counts_of",
    "decode_image",
    "environment_digest",
    "generate_dataset",
    "generator_commit",
    "generator_source_digest",
    "image_digests",
    "main",
    "matrix_snapshot",
    "normalize_text",
    "pdf_digests",
    "pdf_semantic_pages",
    "preview_bytes",
    "render_contact_sheet",
    "text_bytes",
    "verify_references",
]


if __name__ == "__main__":
    sys.exit(main())
