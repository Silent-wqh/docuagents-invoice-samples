"""The T17 data gate: machine pre-check, annotation lock and frozen review.

The module answers three separate questions and never mixes them:

* :func:`machine_precheck` reads the published originals, their gold records
  and their previews, and proves the mechanical facts: file digests, page
  counts, extracted text, evidence pages and boxes, and the deliberate
  conflicts of class C.  An image document has no text layer, so its printed
  values are marked for human visual review, never counted as a difference.
* :func:`build_annotation_lock` writes the reviewed decisions (missing,
  unreadable, ambiguous, readability and routing) with the final file and
  gold digests.  The lock holds no printed amount and no constructed truth.
* :func:`build_annotation_review` writes the frozen review that a person
  signs.  Its machine part comes from the pre-check; its human fields stay
  empty with ``pending_human_confirmation``.

The freeze is deterministic: two builds from the same tree give the same
bytes.  There is no clock, no random value and no absolute path in the
stable documents.

The generated artifacts stay out of the lock and the review.  The main
generator writes the same 243 manifest files as T16; this module adds the
three frozen files (``annotations.lock.json``, ``reports/annotation_review.json``
and ``README.md``) in a controlled re-freeze.

Commands::

    python -m docuagents_gen.review --precheck
    python -m docuagents_gen.review --freeze
    python -m docuagents_gen.review --verify
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import pdfplumber

from . import codes, fixtures, generate, matrix, split

LOCK_NAME = "annotations.lock.json"
REVIEW_NAME = "reports/annotation_review.json"
README_NAME = "README.md"
RELEASE_DRAFT_NAME = "release_record_draft.json"

LOCK_SCHEMA_VERSION = 1
REVIEW_SCHEMA_VERSION = 1

REQUIRED_FIELDS = ("vendor_name", "invoice_number", "invoice_date", "currency", "total_amount")
AMOUNT_FIELDS = ("subtotal_amount", "tax_amount", "total_amount")
INVOICE_SCORED_FIELDS = (
    "invoice_number", "invoice_date", "due_date", "vendor_name", "customer_name",
    "currency", "document_type", "subtotal_amount", "tax_amount", "total_amount",
    "purchase_order_number",
)
LINE_ITEM_SCORED_FIELDS = ("description", "quantity", "unit_price", "amount")
CHARGE_SCORED_FIELDS = ("label", "amount", "kind")
LINE_ITEM_PATTERN = re.compile(r"^line_items\[(?P<index>\d+)\]\.(?P<part>description|quantity|unit_price|amount)$")
MONEY_PATTERN = re.compile(r"^-?\$?(?P<number>\d+(?:\.\d{1,2})?)$")

ROUTING_RULE = (
    "needs_review when the gold scope status is not SUPPORTED, when gold records "
    "an expected issue, when the readability gate is not clean_text or "
    "image_readable, when a required field (vendor_name, invoice_number, "
    "invoice_date, currency, total_amount) has no observed value, or when a "
    "field is unreadable; auto_accepted otherwise. The rule is derived from the "
    "frozen gold record only."
)

COMPLETION_CRITERIA = (
    ("C1", "The machine pre-check covers every page of all 30 main samples and finds no substantive difference.",
     "The pre-check reads 30 documents and 35 pages, compares every observed evidence value with the extracted text or the gold decision, and records image-only fields as pending human visual review."),
    ("C2", "annotations.lock.json binds the final source files and gold records and records the reviewed decisions.",
     "Every lock entry carries the source byte SHA-256, the gold SHA-256, the missing, unreadable, ambiguous and routing decisions, and the pending human status."),
    ("C3", "reports/annotation_review.json covers every page of all 30 main samples and the boundary fixtures, and records zero disputes.",
     "The report lists every page with its preview and its machine result, covers the 18 content and 4 technical fixtures, the 4 object-level cases and da-fx-unread, and separates pending human items from disputes."),
    ("C4", "datasets/docuagents_v1/README.md states every P7 item, including the scoring range, isolation, commands, seeds and limitations.",
     "The README has the required sections: purpose, matrix, fixtures, gold and codes, scoring, readability, isolation, commands, seeds, upstream, environment, freeze process, model input restrictions, limitations, licence and test visibility."),
    ("C5", "The licence and attribution statements are separate and complete.",
     "NOTICE and the README state the upstream MIT code licence, the upstream CC BY 4.0 data licence, the new MIT code licence and the new CC BY 4.0 data licence separately; the upstream files are unchanged."),
    ("C6", "The three frozen files are in the main manifest and the end-to-end check passes on the final version.",
     "The manifest declares annotations.lock.json, reports/annotation_review.json and README.md with the frozen_review role; the acceptance run is recorded in the task completion report."),
    ("C7", "The controlled re-freeze leaves all 142 main, 64 fixture and 100 injection artifacts byte-identical.",
     "The manifest changes are limited to the three new entries, the generator fields and the run report; the acceptance run compares the generated bytes with the reference."),
    ("C8", "The existing and new tests pass offline with no skip and no xfail.",
     "The task completion report records the pytest command, the passed count and the absence of skip and xfail."),
    ("C9", "The human sign-off, the tag and the formal release record are completed by the Project Owner.",
     "This report stays pending_human_confirmation. The tag docuagents-v1.0, the signed review and the formal release record are not created by this task."),
)

README_SECTIONS = (
    "# DocuAgentS invoice sample set",
    "## Purpose",
    "## Contents",
    "## Main matrix",
    "## Boundary and fault fixtures",
    "## Gold, evidence and codes",
    "## Field scoring",
    "## Readability",
    "## Split isolation",
    "## Commands",
    "## Seeds, upstream and environment",
    "## Freeze process",
    "## Model input restrictions",
    "## Limitations",
    "## Licence and attribution",
    "## Test visibility",
    "## Human confirmation status",
    "## Release status",
)


class ReviewError(RuntimeError):
    """Raised when the pre-check finds a difference or a freeze cannot finish."""


# ---------------------------------------------------------------------------
# Small deterministic helpers
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _norm(text: str) -> str:
    """NFC, LF and single spaces: the comparison form of one printed value."""
    return " ".join(generate.normalize_text(text).split())


def _printed_date(value: str) -> str:
    parsed = date.fromisoformat(value)
    return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"


def _sorted_doc_ids() -> List[str]:
    return [row.doc_id for row in matrix.ROWS]


# ---------------------------------------------------------------------------
# Reading an original
# ---------------------------------------------------------------------------


def _pdf_pages(path: Path) -> List[Dict[str, Any]]:
    with pdfplumber.open(str(path)) as pdf:
        pages = []
        for page in pdf.pages:
            pages.append(
                {
                    "text": generate.normalize_text(page.extract_text() or ""),
                    "words": page.extract_words(),
                    "bbox": [float(value) for value in page.bbox],
                }
            )
        return pages


def _locate(text: str, page_text: str) -> bool:
    return _norm(text) in _norm(page_text)


def _token_present(text: str, words: Sequence[Mapping[str, Any]]) -> bool:
    return any(_norm(word["text"]) == _norm(text) for word in words)


def _box_is_inside(box: Sequence[float], page_box: Sequence[float], tolerance: float = 1.0) -> bool:
    return (
        len(box) == 4
        and box[0] < box[2]
        and box[1] < box[3]
        and box[0] >= page_box[0] - tolerance
        and box[1] >= page_box[1] - tolerance
        and box[2] <= page_box[2] + tolerance
        and box[3] <= page_box[3] + tolerance
    )


def _words_in_box(words: Sequence[Mapping[str, Any]], box: Sequence[float]) -> str:
    # A printed word box and the recorded evidence box can differ by a fraction
    # of a point (font ascent and descent).  A 0.75 pt margin absorbs that
    # difference without reaching a neighbouring column.
    inside = [
        word for word in words
        if word["x0"] >= box[0] - 0.75
        and word["x1"] <= box[2] + 0.75
        and word["top"] >= box[1] - 0.75
        and word["bottom"] <= box[3] + 0.75
    ]
    return _norm(" ".join(word["text"] for word in inside))


def _decimal_value(text: Any) -> Optional[Decimal]:
    """Read a printed amount, with or without a symbol and grouping commas."""
    cleaned = str(text).replace("$", "").replace(",", "").strip()
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _expected_decimal(field: str, invoice: Optional[Mapping[str, Any]]) -> Optional[Decimal]:
    """The numeric gold value a money or quantity field must show."""
    if invoice is None:
        return None
    if field in AMOUNT_FIELDS:
        value = invoice.get(field)
        return None if value is None else Decimal(str(value))
    match = LINE_ITEM_PATTERN.match(field)
    if match and match.group("part") in ("quantity", "unit_price", "amount"):
        index = int(match.group("index"))
        part = match.group("part")
        for item in invoice.get("line_items", []):
            if item.get("index") == index:
                value = item.get(part)
                return None if value is None else Decimal(str(value))
    return None


def _expected_evidence_text(field: str, invoice: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The printed text a gold value must show, or ``None`` when not applicable.

    Money and quantity fields are compared as numbers, because the page may
    print a grouping comma that the gold value does not hold.
    """
    if invoice is None:
        return None
    if field in ("vendor_name", "customer_name", "currency", "invoice_number", "purchase_order_number"):
        value = invoice.get(field)
        return None if value is None else str(value)
    if field in ("invoice_date", "due_date"):
        value = invoice.get(field)
        return None if value is None else _printed_date(str(value))
    if field in AMOUNT_FIELDS:
        return None
    match = LINE_ITEM_PATTERN.match(field)
    if match:
        part = match.group("part")
        if part in ("quantity", "unit_price", "amount"):
            return None
        index = int(match.group("index"))
        for item in invoice.get("line_items", []):
            if item.get("index") == index:
                value = item.get(part)
                return None if value is None else str(value)
    return None


def scoring_range(tree: Path) -> Dict[str, Any]:
    """The scoring denominator, computed from the frozen gold records.

    Every invoice-level field, every line-item field and every charge field is
    one scored decision.  An explicit gold ``null`` is a decision too: the
    reader must return the same absence.  B3 is excluded.  The invoice-level
    fields of the multi-invoice file are excluded because that file has no
    single invoice.
    """
    tree = Path(tree)
    scored = nulls = 0
    b3_slots = 0
    multi_invoice_slots = 0
    counted_documents = 0
    for row in matrix.ROWS:
        gold = _read_json(tree / "gold" / f"{row.doc_id}.json")
        invoice = gold.get("invoice")
        slots = 0
        null_count = 0
        if invoice is not None:
            slots += len(INVOICE_SCORED_FIELDS)
            null_count += sum(1 for name in INVOICE_SCORED_FIELDS if invoice.get(name) is None)
            for item in invoice.get("line_items", []):
                slots += len(LINE_ITEM_SCORED_FIELDS)
                null_count += sum(1 for name in LINE_ITEM_SCORED_FIELDS if item.get(name) is None)
            for charge in invoice.get("charges", []):
                slots += len(CHARGE_SCORED_FIELDS)
                null_count += sum(1 for name in CHARGE_SCORED_FIELDS if charge.get(name) is None)
        if row.subclass == "B3":
            b3_slots += slots
            continue
        scored += slots
        nulls += null_count
        counted_documents += 1
        if invoice is None:
            multi_invoice_slots += len(INVOICE_SCORED_FIELDS)
    return {
        "fields_scored": scored,
        "explicit_null_decisions": nulls,
        "non_null_decisions": scored - nulls,
        "documents": counted_documents,
        "excluded": {
            "b3_slots": b3_slots,
            "multi_invoice_level_slots": multi_invoice_slots,
        },
        "rule": (
            "One decision per invoice field, line-item field and charge field. "
            "An explicit gold null counts as a decision. B3 is excluded. The "
            "invoice-level fields of the multi-invoice file are excluded."
        ),
    }


def _route_of(gold: Mapping[str, Any]) -> Dict[str, Any]:
    unreadable = sorted(
        key for key, entry in gold["evidence"].items()
        if entry.get("status") == "unreadable" and not key.startswith("scope_signals.")
    )
    not_found = sorted(
        key for key, entry in gold["evidence"].items()
        if entry.get("status") == "not_found" and not key.startswith("scope_signals.")
    )
    ambiguous = sorted(
        key for key, entry in gold["evidence"].items()
        if entry.get("status") == "ambiguous" and not key.startswith("scope_signals.")
    )
    scope = gold["scope"]
    readability = gold["readability"]["gate"]
    missing_required = [name for name in REQUIRED_FIELDS if name in not_found]
    review_reasons = []
    if scope["status"] != "SUPPORTED":
        review_reasons.append(f"scope:{scope['status']}")
    if gold["expected_issues"]:
        review_reasons.append("issues:" + ",".join(gold["expected_issues"]))
    if readability not in ("clean_text", "image_readable"):
        review_reasons.append(f"readability:{readability}")
    if missing_required:
        review_reasons.append("missing_required:" + ",".join(missing_required))
    if unreadable:
        review_reasons.append("unreadable:" + ",".join(unreadable))
    route = "needs_review" if review_reasons else "auto_accepted"
    return {
        "unreadable_fields": unreadable,
        "not_found_fields": not_found,
        "ambiguous_fields": ambiguous,
        "scope_status": scope["status"],
        "scope_reasons": list(scope["reasons"]),
        "expected_issues": list(gold["expected_issues"]),
        "upstream_validate": gold["acceptance_policy"]["upstream_validate"],
        "readability_gate": readability,
        "route": route,
        "route_reasons": review_reasons,
        "must_not_auto_accept": route == "needs_review",
    }


# ---------------------------------------------------------------------------
# The machine pre-check
# ---------------------------------------------------------------------------


def _check_manifest_integrity(tree: Path) -> Dict[str, Any]:
    manifest = _read_json(tree / "manifest.json")
    failures: List[str] = []
    counts = {"main_generated": 0, "main_verified": 0, "injection": 0, "injection_verified": 0,
              "frozen_declared": 0, "frozen_verified": 0, "runtime_reports": 0}
    for entry in manifest["files"]:
        path = tree / entry["path"]
        if entry["artifact_role"] == "runtime_report":
            counts["runtime_reports"] += 1
            if not path.is_file():
                failures.append(f"{entry['path']}: the runtime report is missing")
            else:
                try:
                    json.loads(path.read_text(encoding="utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    failures.append(f"{entry['path']}: the runtime report is not readable JSON")
            continue
        if entry["artifact_role"] == "frozen_review":
            counts["frozen_declared"] += 1
            if not path.is_file():
                failures.append(f"{entry['path']}: the frozen file is missing")
                continue
            data = path.read_bytes()
            if _sha256(data) != entry["sha256"] or len(data) != entry["bytes"]:
                failures.append(f"{entry['path']}: the manifest does not match the frozen file")
                continue
            counts["frozen_verified"] += 1
            continue
        bucket = "injection" if entry["path"].startswith("fault_injection/") else "main_generated"
        counts[bucket] += 1
        if not path.is_file():
            failures.append(f"{entry['path']}: the file is missing")
            continue
        data = path.read_bytes()
        if _sha256(data) != entry["sha256"]:
            failures.append(f"{entry['path']}: the manifest digest does not match the file")
            continue
        if len(data) != entry["bytes"]:
            failures.append(f"{entry['path']}: the manifest byte count does not match the file")
            continue
        counts["main_verified" if bucket == "main_generated" else "injection_verified"] += 1
    return {"failures": failures, "counts": counts, "manifest": manifest}


def _check_evidence(
    doc_id: str,
    gold: Mapping[str, Any],
    pages: Sequence[Mapping[str, Any]],
    *,
    is_image: bool,
    page_count: int,
) -> Tuple[List[str], List[str], Dict[str, int]]:
    """Check every evidence record against the printed page.

    Returns ``(differences, manual_notes, counts)``.  A difference is a
    substantive contradiction; a manual note is a value that only a person
    can confirm because the original has no text layer.
    """
    differences: List[str] = []
    manual: List[str] = []
    counts = {"observed": 0, "located": 0, "boxes": 0, "not_found": 0,
              "unreadable": 0, "ambiguous": 0}
    invoice = gold.get("invoice")

    for field in sorted(gold["evidence"]):
        entry = gold["evidence"][field]
        status = entry.get("status")
        page = entry.get("page")
        text = entry.get("text")
        searched = entry.get("searched_pages", [])
        if page is not None and not (1 <= int(page) <= page_count):
            differences.append(f"{doc_id}: evidence {field} points at page {page}, the file has {page_count}")
        for number in searched:
            if not (1 <= int(number) <= page_count):
                differences.append(f"{doc_id}: evidence {field} searched page {number}, the file has {page_count}")

        if status == "observed":
            counts["observed"] += 1
            if page is None:
                differences.append(f"{doc_id}: observed evidence {field} has no page")
                continue
            if text is None:
                differences.append(f"{doc_id}: observed evidence {field} has no text")
                continue
            expected_number = _expected_decimal(field, invoice)
            expected = _expected_evidence_text(field, invoice)
            if field.startswith("scope_signals."):
                expected = None
            if expected_number is not None:
                printed_number = _decimal_value(text)
                if printed_number != expected_number:
                    differences.append(
                        f"{doc_id}: evidence {field} prints {text!r}, gold holds {expected_number}"
                    )
            elif expected is not None and _norm(str(expected)) != _norm(str(text)):
                differences.append(
                    f"{doc_id}: evidence {field} prints {text!r}, gold holds {expected!r}"
                )
            if is_image:
                if "bbox_pt" in entry:
                    differences.append(f"{doc_id}: image evidence {field} must not carry a PDF box")
                if (
                    not LINE_ITEM_PATTERN.match(field)
                    and not field.startswith("scope_signals.")
                    and entry.get("bbox_precision") != "unavailable"
                ):
                    differences.append(
                        f"{doc_id}: image evidence {field} must say bbox_precision=unavailable"
                    )
                continue
            page_data = pages[int(page) - 1]
            has_box = "bbox_pt" in entry
            if field.startswith("scope_signals.") and not has_box:
                # Some signals are computed labels ("Rows observed: 4"), not
                # printed values.  A signal with a box is a printed value and
                # is checked like any other observed value.
                counts["located"] += 1
            else:
                if not _locate(str(text), page_data["text"]):
                    differences.append(f"{doc_id}: evidence {field} text {text!r} is not on page {page}")
                else:
                    counts["located"] += 1
                if LINE_ITEM_PATTERN.match(field) and not _token_present(str(text), page_data["words"]):
                    differences.append(
                        f"{doc_id}: evidence {field} value {text!r} is not a separate word on page {page}"
                    )
            if has_box:
                box = entry["bbox_pt"]
                if entry.get("bbox_precision") != "approximate":
                    differences.append(
                        f"{doc_id}: evidence {field} has a box but bbox_precision is "
                        f"{entry.get('bbox_precision')!r}"
                    )
                if not isinstance(box, list) or not _box_is_inside(box, page_data["bbox"]):
                    differences.append(f"{doc_id}: evidence {field} box is outside page {page}")
                else:
                    counts["boxes"] += 1
                    joined = _words_in_box(page_data["words"], box)
                    # A signal box may cover only the value ("USD"), while the
                    # signal text also holds its label ("Currency: USD").
                    if not field.startswith("scope_signals.") and _norm(joined) != _norm(str(text)):
                        differences.append(
                            f"{doc_id}: evidence {field} box words {joined!r} do not match {text!r}"
                        )
            elif not LINE_ITEM_PATTERN.match(field) and not field.startswith("scope_signals."):
                differences.append(
                    f"{doc_id}: PDF evidence {field} has no approximate box marker"
                )
        elif status == "not_found":
            counts["not_found"] += 1
            if page is not None or text is not None:
                differences.append(f"{doc_id}: not_found evidence {field} carries a page or text")
            if not searched:
                differences.append(f"{doc_id}: not_found evidence {field} has no searched_pages")
            if not field.startswith("scope_signals."):
                actual = None if invoice is None else invoice.get(field)
                if field in AMOUNT_FIELDS or field in (
                    "invoice_number", "invoice_date", "due_date", "purchase_order_number",
                    "vendor_name", "customer_name", "currency",
                ):
                    if actual is not None:
                        differences.append(
                            f"{doc_id}: evidence {field} is not_found but gold holds {actual!r}"
                        )
        elif status == "unreadable":
            counts["unreadable"] += 1
            if text is not None:
                differences.append(f"{doc_id}: unreadable evidence {field} carries text")
        elif status == "ambiguous":
            counts["ambiguous"] += 1
        else:
            differences.append(f"{doc_id}: evidence {field} has unknown status {status!r}")

    return differences, manual, counts


def _check_document(
    tree: Path,
    row: matrix.Row,
    manifest_by_path: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    doc_id = row.doc_id
    gold_path = tree / "gold" / f"{doc_id}.json"
    gold = _read_json(gold_path)
    file_name = gold["file"]["file_name"]
    document_path = tree / file_name
    differences: List[str] = []
    manual: List[str] = []

    if not document_path.is_file():
        return {"doc_id": doc_id, "differences": [f"{doc_id}: the source file is missing"], "manual": []}
    data = document_path.read_bytes()
    byte_sha = _sha256(data)

    if byte_sha != gold["file"]["sha256"]:
        differences.append(f"{doc_id}: the gold file digest does not match the source file")
    gold_sha = _sha256(gold_path.read_bytes())

    entry = manifest_by_path.get(file_name)
    if entry is None:
        differences.append(f"{doc_id}: the manifest does not declare {file_name}")
    else:
        if entry.get("sha256") != byte_sha:
            differences.append(f"{doc_id}: the manifest digest does not match the source file")
        if entry.get("page_count") != gold["file"]["page_count"]:
            differences.append(f"{doc_id}: the manifest page count differs from gold")

    is_image = gold["file"]["media_type"].startswith("image/")
    if is_image:
        page_count = 1
        pages: List[Dict[str, Any]] = []
    else:
        pages = _pdf_pages(document_path)
        page_count = len(pages)
        if page_count == 0:
            differences.append(f"{doc_id}: the PDF has no page")
    if page_count != gold["file"]["page_count"]:
        differences.append(
            f"{doc_id}: the file has {page_count} pages, gold says {gold['file']['page_count']}"
        )
    if gold.get("doc_id") != doc_id or gold.get("split") != row.split:
        differences.append(f"{doc_id}: the gold header does not match the matrix")
    if gold.get("class") != row.klass or gold.get("subclass") != row.subclass:
        differences.append(f"{doc_id}: the gold class does not match the matrix")

    previews = sorted((tree / "previews" / "pages").glob(f"{doc_id}.p*.png"))
    texts = sorted((tree / "text").glob(f"{doc_id}.p*.txt"))
    if len(previews) != page_count:
        differences.append(f"{doc_id}: {len(previews)} previews for {page_count} pages")
    if len(texts) != page_count:
        differences.append(f"{doc_id}: {len(texts)} text files for {page_count} pages")
    for index in range(1, page_count + 1):
        if not (tree / "previews" / "pages" / f"{doc_id}.p{index}.png").is_file():
            differences.append(f"{doc_id}: preview page {index} is missing")
        if not (tree / "text" / f"{doc_id}.p{index}.txt").is_file():
            differences.append(f"{doc_id}: text page {index} is missing")

    if not is_image:
        for index, page in enumerate(pages, 1):
            artifact = (tree / "text" / f"{doc_id}.p{index}.txt").read_text(encoding="utf-8")
            if _norm(artifact) != _norm(page["text"]):
                differences.append(
                    f"{doc_id}: the text artifact of page {index} differs from the extracted text"
                )

    evidence_diffs, evidence_manual, evidence_counts = _check_evidence(
        doc_id, gold, pages, is_image=is_image, page_count=page_count
    )
    differences.extend(evidence_diffs)
    manual.extend(evidence_manual)

    # -- deliberate construction facts -------------------------------------
    if row.subclass == "A3":
        if page_count != 2:
            differences.append(f"{doc_id}: A3 must have two pages, got {page_count}")
        summary_fields = ("subtotal_amount", "tax_amount", "total_amount")
        for field in summary_fields:
            evidence = gold["evidence"].get(field)
            if evidence and evidence.get("status") == "observed" and evidence.get("page") != 2:
                differences.append(f"{doc_id}: {field} evidence must be on page 2")
        for key, evidence in gold["evidence"].items():
            if key.startswith("line_items[") and evidence.get("status") == "observed":
                if evidence.get("page") != 1:
                    differences.append(f"{doc_id}: {key} evidence must be on page 1")
    if row.subclass == "A4":
        if gold["invoice"]["tax_amount"] != "0.00":
            differences.append(f"{doc_id}: A4 must print a zero tax")
        tax_evidence = gold["evidence"].get("tax_amount", {})
        if tax_evidence.get("status") != "observed" or tax_evidence.get("text") != "$0.00":
            differences.append(f"{doc_id}: A4 tax evidence must print $0.00")
    if row.subclass == "D1":
        invoice = gold["invoice"]
        if invoice["tax_amount"] is not None:
            differences.append(f"{doc_id}: D1 must not hold a tax amount")
        tax_evidence = gold["evidence"].get("tax_amount", {})
        if tax_evidence.get("status") != "not_found":
            differences.append(f"{doc_id}: D1 tax evidence must be not_found")
        if not tax_evidence.get("searched_pages"):
            differences.append(f"{doc_id}: D1 tax evidence must record searched_pages")
    if row.subclass == "D2":
        invoice = gold["invoice"]
        if invoice["invoice_number"] is not None:
            differences.append(f"{doc_id}: D2 must not use the PO number as the invoice number")
        if not invoice.get("purchase_order_number"):
            differences.append(f"{doc_id}: D2 must hold the printed PO number")
        number_evidence = gold["evidence"].get("invoice_number", {})
        if number_evidence.get("status") != "not_found":
            differences.append(f"{doc_id}: D2 invoice number evidence must be not_found")
    if row.klass == "C":
        expected_issue = {"C1": "LINE_AMOUNT_MISMATCH", "C2": "SUBTOTAL_MISMATCH",
                          "C3": "TOTAL_MISMATCH"}[row.subclass]
        if expected_issue not in gold["expected_issues"]:
            differences.append(f"{doc_id}: the expected issue {expected_issue} is missing")
        if gold["acceptance_policy"]["upstream_validate"] != "SKIPPED_DELIBERATE_CONFLICT":
            differences.append(f"{doc_id}: a class C document must skip upstream validation")
        arithmetic = gold["arithmetic"]
        if row.subclass == "C1" and not any(
            line["delta"] not in (None, "0.00", "-0.00") for line in arithmetic["lines"]
        ):
            differences.append(f"{doc_id}: C1 has no printed line amount conflict")
        if row.subclass == "C2" and arithmetic["delta_subtotal"] in (None, "0.00", "-0.00"):
            differences.append(f"{doc_id}: C2 has no printed subtotal conflict")
        if row.subclass == "C3" and arithmetic["delta_total"] in (None, "0.00", "-0.00"):
            differences.append(f"{doc_id}: C3 has no printed total conflict")
    if row.klass == "E":
        if gold["scope"]["status"] != "UNSUPPORTED":
            differences.append(f"{doc_id}: an E document must be UNSUPPORTED")
        if gold["invoice"] is None:
            # The multi-invoice file has no single invoice-level value.
            for field in ("invoice_number", "invoice_date", "due_date",
                          "purchase_order_number", "subtotal_amount", "tax_amount",
                          "total_amount"):
                if field in gold["evidence"] and gold["evidence"][field].get("status") != "not_found":
                    differences.append(f"{doc_id}: multi-invoice evidence {field} must be not_found")
    if row.subclass == "B3":
        if gold["readability"]["gate"] != "image_degraded_unverified":
            differences.append(f"{doc_id}: B3 must carry the degraded readability gate")
    if gold["readability"]["verified"] is not False:
        differences.append(f"{doc_id}: the readability gate is already marked verified")

    # -- per-page view ------------------------------------------------------
    page_entries: List[Dict[str, Any]] = []
    for index in range(1, page_count + 1):
        observed = sorted(
            key for key, value in gold["evidence"].items()
            if value.get("status") == "observed"
            and value.get("page") == index
            and not key.startswith("line_items[")
            and not key.startswith("scope_signals.")
        )
        line_items = sum(
            1 for key, value in gold["evidence"].items()
            if value.get("status") == "observed"
            and value.get("page") == index
            and key.startswith("line_items[")
        )
        signals = sum(
            1 for key, value in gold["evidence"].items()
            if value.get("status") == "observed"
            and value.get("page") == index
            and key.startswith("scope_signals.")
        )
        page_entries.append({
            "page": index,
            "preview": f"previews/pages/{doc_id}.p{index}.png",
            "text": f"text/{doc_id}.p{index}.txt",
            "text_layer": "absent_image" if is_image else "extracted",
            "observed_fields": observed,
            "observed_line_item_fields": line_items,
            "observed_signal_fields": signals,
            "status": "requires_human_visual_check" if is_image else "pass",
        })

    if is_image:
        manual.append(
            f"{doc_id}: no PDF text layer; confirm the printed values in the page previews."
        )
    if row.subclass == "B3":
        manual.append(
            f"{doc_id}: the image is degraded; confirm that the printed values stay legible enough for the declared gate."
        )
    if row.subclass == "C1" and row.render_mode == "image":
        manual.append(
            f"{doc_id}: scanned class C document; confirm that the printed conflict is visible and is not a scan artifact."
        )
    if row.subclass in ("C2", "C3"):
        manual.append(
            f"{doc_id}: two printed line amounts are equal; confirm that both rows are intended."
        )
    if row.klass in ("C", "D", "E") or row.subclass == "B3":
        manual.append(
            f"{doc_id}: confirm the expected route ({_route_of(gold)['route']}) and the printed evidence."
        )
    if page_count > 1:
        manual.append(
            f"{doc_id}: multi-page document; confirm that every page preview belongs to the same original."
        )

    return {
        "doc_id": doc_id,
        "split": row.split,
        "class": row.klass,
        "subclass": row.subclass,
        "source_family": row.source_family,
        "file": file_name,
        "media_type": gold["file"]["media_type"],
        "byte_sha256": byte_sha,
        "gold_sha256": gold_sha,
        "page_count": page_count,
        "text_layer": "absent_image" if is_image else "extracted",
        "pages": page_entries,
        "evidence_counts": evidence_counts,
        "differences": differences,
        "manual_items": manual,
        "route": _route_of(gold)["route"],
    }


def _check_fixtures(tree: Path) -> Dict[str, Any]:
    fixture_dir = tree / generate.FIXTURE_DIR_NAME
    manifest = _read_json(fixture_dir / "manifest.json")
    failures: List[str] = []
    verified = 0
    for entry in manifest["files"]:
        path = fixture_dir / entry["path"]
        if not path.is_file():
            failures.append(f"fixtures: {entry['path']} is missing")
            continue
        data = path.read_bytes()
        if _sha256(data) != entry["sha256"]:
            failures.append(f"fixtures: {entry['path']} does not match the fixture manifest")
            continue
        if len(data) != entry["bytes"]:
            failures.append(f"fixtures: {entry['path']} byte count differs")
            continue
        verified += 1

    records: Dict[str, Dict[str, Any]] = {}
    content = 0
    technical = 0
    for row in fixtures.FIXTURES:
        doc_id = row["doc_id"]
        if row["fixture_kind"] == "technical":
            technical += 1
            path = fixture_dir / "technical" / f"{doc_id}.pdf"
            entry = next((e for e in manifest["files"] if e["path"] == f"technical/{doc_id}.pdf"), None)
            if not path.is_file():
                failures.append(f"fixtures: {doc_id} technical file is missing")
            elif entry is None:
                failures.append(f"fixtures: {doc_id} technical file is not declared")
            elif entry.get("expected", {}).get("technical_error") != row["expected_technical_error"]:
                failures.append(f"fixtures: {doc_id} technical error differs from the declaration")
            continue
        content += 1
        document = fixture_dir / "documents" / f"{doc_id}.pdf"
        gold_path = fixture_dir / "gold" / f"{doc_id}.json"
        if not document.is_file() or not gold_path.is_file():
            failures.append(f"fixtures: {doc_id} document or gold is missing")
            continue
        gold = _read_json(gold_path)
        data = document.read_bytes()
        if _sha256(data) != gold["file"]["sha256"]:
            failures.append(f"fixtures: {doc_id} file digest differs from gold")
        previews = sorted((fixture_dir / "previews" / "pages").glob(f"{doc_id}.p*.png"))
        if len(previews) != gold["file"]["page_count"]:
            failures.append(f"fixtures: {doc_id} preview count differs from the page count")
        records[doc_id] = gold

    review = _read_json(fixture_dir / "reports" / "legibility_review.json")
    review_documents = {item["doc_id"]: item for item in review["documents"]}
    if len(review_documents) != content:
        failures.append(
            f"fixtures: the legibility review covers {len(review_documents)} of {content} content documents"
        )
    for doc_id, entry in review_documents.items():
        gold = records.get(doc_id)
        if gold is None:
            failures.append(f"fixtures: {doc_id} has a review entry but no gold")
            continue
        if entry["file_sha256"] != gold["file"]["sha256"]:
            failures.append(f"fixtures: {doc_id} review digest differs from gold")
        if entry["page_count"] != gold["file"]["page_count"]:
            failures.append(f"fixtures: {doc_id} review page count differs from gold")

    unreadable = review_documents.get("da-fx-unread")
    if unreadable is None:
        failures.append("fixtures: da-fx-unread is not in the legibility review")
    else:
        fields = {item["field"]: item for item in unreadable["fields"]}
        for name in ("subtotal_amount", "tax_amount", "total_amount"):
            item = fields.get(name)
            if item is None or item.get("status") != "unreadable":
                failures.append(f"fixtures: da-fx-unread field {name} is not unreadable")
            elif item.get("page") is not None or item.get("text") is not None:
                failures.append(f"fixtures: da-fx-unread field {name} carries a value")
        gold = records.get("da-fx-unread")
        if gold is not None:
            for name in ("subtotal_amount", "tax_amount", "total_amount"):
                if gold["invoice"][name] is not None:
                    failures.append(f"fixtures: da-fx-unread gold holds {name}")
                evidence = gold["evidence"].get(name, {})
                if evidence.get("status") != "unreadable":
                    failures.append(f"fixtures: da-fx-unread evidence {name} is not unreadable")
            if gold["acceptance_policy"]["decisions"] != ["UNREADABLE_FIELD"]:
                failures.append("fixtures: da-fx-unread acceptance decision is not UNREADABLE_FIELD")

    object_cases = fixtures.object_level_cases()
    object_differences: List[str] = []
    for case in object_cases:
        computed = matrix.round_money(
            Decimal(str(case["quantity"])) * Decimal(str(case["unit_price"]))
        )
        if f"{computed:.2f}" != case["expected_line_amount"]:
            object_differences.append(
                f"object-level {case['case_id']}: {computed} is not {case['expected_line_amount']}"
            )

    return {
        "manifest_files": len(manifest["files"]),
        "manifest_verified": verified,
        "content": content,
        "technical": technical,
        "object_level": len(object_cases),
        "object_level_failures": object_differences,
        "failures": failures,
        "legibility_review": {
            "path": "boundary_fixtures/reports/legibility_review.json",
            "status": review["status"],
            "reviewer": review.get("reviewer"),
            "documents": len(review["documents"]),
        },
        "unreadable_fixture": "da-fx-unread" if unreadable is not None else None,
        "records": records,
    }


def _manual_items(precheck: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Group the non-mechanical checks for the human reviewer."""
    documents = {item["doc_id"]: item for item in precheck["documents_detail"]}
    groups: List[Dict[str, Any]] = []

    def add(category: str, reason: str, doc_ids: Sequence[str]) -> None:
        selected = sorted(set(doc_ids))
        if not selected:
            return
        groups.append({
            "category": category,
            "doc_ids": selected,
            "reason": reason,
            "status": "pending_human_confirmation",
            "previews": [
                f"previews/pages/{doc_id}.p{page}.png"
                for doc_id in selected
                for page in range(1, documents[doc_id]["page_count"] + 1)
            ],
        })

    add(
        "image_samples",
        "The original has no PDF text layer. Confirm the printed values and the evidence pages in the previews.",
        [item["doc_id"] for item in precheck["documents_detail"]
         if item["text_layer"] == "absent_image"],
    )
    add(
        "b3_degraded_readability",
        "Confirm that the severe degradation still permits the declared B3 readability gate.",
        [item["doc_id"] for item in precheck["documents_detail"] if item["subclass"] == "B3"],
    )
    add(
        "c1_scanned_conflict",
        "Confirm that the printed line amount conflict is visible in the scanned original.",
        [item["doc_id"] for item in precheck["documents_detail"]
         if item["subclass"] == "C1" and item["text_layer"] == "absent_image"],
    )
    add(
        "routing_and_scope",
        "Confirm the expected route and the scope decision for the C, D and E classes.",
        [item["doc_id"] for item in precheck["documents_detail"]
         if item["class"] in ("C", "D", "E")],
    )
    add(
        "multi_page",
        "Confirm that every page preview belongs to the same original and that the summary page is correct.",
        [item["doc_id"] for item in precheck["documents_detail"] if item["page_count"] > 1],
    )
    add(
        "duplicate_amount_lines",
        "Two printed line amounts are equal. Confirm that both rows are intended and are not a duplicate entry.",
        [item["doc_id"] for item in precheck["documents_detail"] if item["subclass"] in ("C2", "C3")],
    )
    return groups


def machine_precheck(tree: Path) -> Dict[str, Any]:
    """Run the complete mechanical pre-check on one published tree."""
    tree = Path(tree)
    if not (tree / "manifest.json").is_file():
        raise ReviewError(f"{tree} is not a published tree: manifest.json is missing")
    integrity = _check_manifest_integrity(tree)
    manifest_by_path = {entry["path"]: entry for entry in integrity["manifest"]["files"]}
    differences: List[str] = list(integrity["failures"])
    documents = []
    observed = located = boxes = not_found = unreadable = ambiguous = 0
    for row in matrix.ROWS:
        detail = _check_document(tree, row, manifest_by_path)
        documents.append(detail)
        differences.extend(detail["differences"])
        counts = detail.get("evidence_counts", {})
        observed += counts.get("observed", 0)
        located += counts.get("located", 0)
        boxes += counts.get("boxes", 0)
        not_found += counts.get("not_found", 0)
        unreadable += counts.get("unreadable", 0)
        ambiguous += counts.get("ambiguous", 0)
    fixture_result = _check_fixtures(tree)
    differences.extend(fixture_result["failures"])
    differences.extend(fixture_result["object_level_failures"])

    manual = _manual_items({"documents_detail": documents})
    if fixture_result["unreadable_fixture"]:
        manual.append({
            "category": "unreadable_fixture",
            "doc_ids": ["da-fx-unread"],
            "reason": (
                "The white rectangle covers the totals block. Confirm that this is a valid "
                "unreadable form and that the three null values with unreadable evidence are correct."
            ),
            "status": "pending_human_confirmation",
            "previews": ["boundary_fixtures/previews/pages/da-fx-unread.p1.png"],
        })

    pages = sum(item["page_count"] for item in documents)
    summary = {
        "schema_version": 1,
        "contract_version": codes.CONTRACT_VERSION,
        "seed": matrix.SEED,
        "documents": len(documents),
        "pages": pages,
        "split": {
            "dev": sum(1 for row in matrix.ROWS if row.split == "dev"),
            "test": sum(1 for row in matrix.ROWS if row.split == "test"),
        },
        "evidence": {
            "observed": observed,
            "located": located,
            "boxes_checked": boxes,
            "not_found": not_found,
            "unreadable": unreadable,
            "ambiguous": ambiguous,
        },
        "scoring": scoring_range(tree),
        "manifest": integrity["counts"],
        "fixtures": {
            "manifest_files": fixture_result["manifest_files"],
            "manifest_verified": fixture_result["manifest_verified"],
            "content": fixture_result["content"],
            "technical": fixture_result["technical"],
            "object_level": fixture_result["object_level"],
            "legibility_review": fixture_result["legibility_review"],
            "unreadable_fixture": fixture_result["unreadable_fixture"],
        },
        "differences": differences,
        "manual_items": manual,
        "documents_detail": documents,
    }
    return summary


# ---------------------------------------------------------------------------
# The annotation lock
# ---------------------------------------------------------------------------


def build_annotation_lock(tree: Path) -> Dict[str, Any]:
    """The deterministic reviewed decisions of the 30 main samples."""
    tree = Path(tree)
    entries = []
    for row in matrix.ROWS:
        gold_path = tree / "gold" / f"{row.doc_id}.json"
        gold = _read_json(gold_path)
        document = tree / gold["file"]["file_name"]
        data = document.read_bytes()
        decisions = _route_of(gold)
        entries.append({
            "doc_id": row.doc_id,
            "split": row.split,
            "class": row.klass,
            "subclass": row.subclass,
            "source_family": row.source_family,
            "file": {
                "path": gold["file"]["file_name"],
                "media_type": gold["file"]["media_type"],
                "bytes": len(data),
                "byte_sha256": _sha256(data),
                "page_count": gold["file"]["page_count"],
            },
            "gold": {
                "path": f"gold/{row.doc_id}.json",
                "bytes": len(gold_path.read_bytes()),
                "sha256": _sha256(gold_path.read_bytes()),
            },
            "decisions": decisions,
            "readability": {
                "gate": gold["readability"]["gate"],
                "verified": False,
            },
            "annotation_status": "pending_human_confirmation",
        })
    return {
        "schema_version": LOCK_SCHEMA_VERSION,
        "contract_version": codes.CONTRACT_VERSION,
        "seed": matrix.SEED,
        "digest_method_version": generate.DIGEST_METHOD_VERSION,
        "status": "pending_human_confirmation",
        "reviewed_by": None,
        "reviewed_at": None,
        "routing_rule": ROUTING_RULE,
        "entries": entries,
    }


def lock_bytes(document: Mapping[str, Any]) -> bytes:
    return generate.canonical_bytes(document)


def verify_annotation_lock(tree: Path) -> Dict[str, Any]:
    """Verify the lock against the final sources and gold; never rewrite."""
    tree = Path(tree)
    failures: List[str] = []
    lock_path = tree / LOCK_NAME
    if not lock_path.is_file():
        return {"ok": False, "failures": [f"{LOCK_NAME} is missing"], "counts": {"entries": 0, "verified": 0}}
    lock = _read_json(lock_path)
    if lock.get("schema_version") != LOCK_SCHEMA_VERSION:
        failures.append("the lock schema version is not supported")
    status = lock.get("status")
    if status not in ("pending_human_confirmation", "confirmed"):
        failures.append(f"the lock status {status!r} is not a supported status")
    if status == "pending_human_confirmation":
        if lock.get("reviewed_by") is not None or lock.get("reviewed_at") is not None:
            failures.append("a pending lock must leave reviewed_by and reviewed_at empty")
    elif status == "confirmed":
        if not lock.get("reviewed_by") or not lock.get("reviewed_at"):
            failures.append("a confirmed lock must name the reviewer and the time")
    entries = lock.get("entries", [])
    expected_ids = [row.doc_id for row in matrix.ROWS]
    if [entry.get("doc_id") for entry in entries] != expected_ids:
        failures.append("the lock must hold the 30 matrix documents in matrix order")
    verified = 0
    for entry in entries:
        doc_id = entry.get("doc_id")
        gold_path = tree / "gold" / f"{doc_id}.json"
        if not gold_path.is_file():
            failures.append(f"{doc_id}: the gold record is missing")
            continue
        gold = _read_json(gold_path)
        document = tree / gold["file"]["file_name"]
        if not document.is_file():
            failures.append(f"{doc_id}: the source file is missing")
            continue
        data = document.read_bytes()
        if entry.get("file", {}).get("byte_sha256") != _sha256(data):
            failures.append(f"{doc_id}: the lock is not bound to the final source file")
            continue
        if entry.get("file", {}).get("bytes") != len(data):
            failures.append(f"{doc_id}: the lock byte count is stale")
            continue
        gold_bytes = gold_path.read_bytes()
        if entry.get("gold", {}).get("sha256") != _sha256(gold_bytes):
            failures.append(f"{doc_id}: the lock is not bound to the final gold record")
            continue
        if entry.get("gold", {}).get("bytes") != len(gold_bytes):
            failures.append(f"{doc_id}: the lock gold byte count is stale")
            continue
        if entry.get("split") != gold.get("split") or entry.get("class") != gold.get("class"):
            failures.append(f"{doc_id}: the lock labels differ from gold")
            continue
        if entry.get("decisions") != _route_of(gold):
            failures.append(f"{doc_id}: the lock decisions differ from gold")
            continue
        verified += 1
    return {
        "ok": not failures,
        "failures": failures,
        "counts": {"entries": len(entries), "verified": verified},
    }


# ---------------------------------------------------------------------------
# The frozen review
# ---------------------------------------------------------------------------


def build_annotation_review(
    tree: Path,
    *,
    lock_sha256: Optional[str] = None,
    readme_sha256: Optional[str] = None,
    precheck: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """The deterministic frozen review; the human fields stay empty."""
    tree = Path(tree)
    check = dict(precheck) if precheck is not None else machine_precheck(tree)
    lock_path = tree / LOCK_NAME
    readme_path = tree / README_NAME
    if lock_sha256 is None:
        lock_sha256 = _sha256(lock_path.read_bytes())
    if readme_sha256 is None:
        readme_sha256 = _sha256(readme_path.read_bytes())
    manual = list(check["manual_items"])
    completion = [
        {
            "id": item[0],
            "criterion": item[1],
            "status": (
                "PENDING_HUMAN_CONFIRMATION" if item[0] == "C9"
                else "PENDING_FINAL_ACCEPTANCE_RUN" if item[0] == "C8"
                else "PASS"
            ),
            "evidence": item[2],
        }
        for item in COMPLETION_CRITERIA
    ]
    fixture_detail = _check_fixtures(tree)
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "contract_version": codes.CONTRACT_VERSION,
        "artifact_role": "frozen_review",
        "status": "pending_human_confirmation",
        "reviewer": "Project Owner",
        "reviewed_by": None,
        "reviewed_at": None,
        "confirmation_path": (
            "The Project Owner reviews every page preview and the listed manual items, "
            "signs this report, and updates the lock status. Until then this report and "
            "the lock stay pending_human_confirmation: an agent cannot sign them."
        ),
        "machine_precheck": {key: value for key, value in check.items() if key != "documents_detail"},
        "documents": check["documents_detail"],
        "fixtures": {
            "manifest": f"{generate.FIXTURE_DIR_NAME}/manifest.json",
            "manifest_files": fixture_detail["manifest_files"],
            "manifest_verified": fixture_detail["manifest_verified"],
            "content": fixture_detail["content"],
            "technical": fixture_detail["technical"],
            "object_level": fixture_detail["object_level"],
            "legibility_review": fixture_detail["legibility_review"],
            "unreadable_fixture": fixture_detail["unreadable_fixture"],
            "documents": [
                {
                    "fixture_id": doc_id,
                    "page_count": record["file"]["page_count"],
                    "file_sha256": record["file"]["sha256"],
                    "gold_sha256": _sha256((tree / generate.FIXTURE_DIR_NAME / "gold" / f"{doc_id}.json").read_bytes()),
                    "previews": [
                        f"boundary_fixtures/previews/pages/{doc_id}.p{page}.png"
                        for page in range(1, record["file"]["page_count"] + 1)
                    ],
                }
                for doc_id, record in sorted(fixture_detail["records"].items())
            ],
            "object_level_cases": [
                {
                    "case_id": case["case_id"],
                    "quantity": case["quantity"],
                    "unit_price": case["unit_price"],
                    "expected_line_amount": case["expected_line_amount"],
                }
                for case in fixtures.object_level_cases()
            ],
        },
        "differences": list(check["differences"]),
        "manual_check_items": manual,
        "manual_check_items_status": "pending_human_confirmation",
        "disputes": [],
        "dispute_resolution": (
            "No substantive dispute was found by the machine pre-check. The manual items "
            "are pending human confirmation; they are not counted as resolved or disputed."
        ),
        "completion_criteria": completion,
        "completion_criteria_source": (
            "These nine criteria are derived from the T17 acceptance clauses and the "
            "D1-D10 deliverables in the task instruction. The Notion section §8.1 text "
            "is not in this repository, so no Notion text is quoted or invented here."
        ),
        "manual_checklist": {
            "signing_steps": [
                "Read README.md and the main matrix.",
                "Open every page preview listed in the preview index. Do not use previews/contact_sheet.png in place of the page review.",
                "Check each manual item and write the conclusion.",
                "Set status to confirmed, then fill reviewed_by and reviewed_at in annotations.lock.json and in this report.",
                "Update the manifest digests with the frozen-file procedure and run the check again.",
            ],
            "contact_sheet_note": (
                "previews/contact_sheet.png is an index only. It does not replace the full-page review."
            ),
            "focus_items": [
                "Image samples B1, B2 and B3: confirm the printed values in the previews.",
                "C1 scanned original: confirm that the printed conflict is visible.",
                "da-fx-unread: confirm the white rectangle as a valid unreadable form.",
                "C, D and E routing: confirm the expected route and scope.",
                "Multi-page A3, E-04 and E-06: confirm every page and the summary page.",
                "C2 and C3: confirm the two equal line amounts are intended.",
                "Ambiguous dates and approximate boxes: none are open; every date is the unambiguous long form and every PDF box is marked approximate.",
            ],
            "preview_index": {
                item["doc_id"]: [page["preview"] for page in item["pages"]]
                for item in check["documents_detail"]
            },
        },
        "t15_carryover": {
            "fixture_id": "da-fx-unread",
            "decision_context": (
                "At T15 close the Project Owner recorded that the white rectangle over the "
                "totals block is a valid unreadable form and meets the core test expectation. "
                "This report carries that background; it does not claim a new human confirmation."
            ),
            "legibility_review": "boundary_fixtures/reports/legibility_review.json",
            "gold": "boundary_fixtures/gold/da-fx-unread.json",
            "preview": "boundary_fixtures/previews/pages/da-fx-unread.p1.png",
            "fields": ["subtotal_amount", "tax_amount", "total_amount"],
            "gold_values": {"subtotal_amount": None, "tax_amount": None, "total_amount": None},
            "evidence_status": "unreadable",
            "must_not_auto_accept": True,
            "status": "pending_human_confirmation",
        },
        "annotation_lock": {"path": LOCK_NAME, "sha256": lock_sha256},
        "dataset_readme": {"path": README_NAME, "sha256": readme_sha256},
        "verification": {
            "commands": [
                "python -m pytest -q",
                "python -m docuagents_gen.generate --seed 2026 --out <fresh-dir> --check --reference datasets/docuagents_v1/manifest.json --quiet",
                "python -m docuagents_gen.review --verify",
            ],
            "note": (
                "The acceptance results are recorded in the task completion report. "
                "This frozen report does not claim a human signature."
            ),
        },
    }


def review_bytes(document: Mapping[str, Any]) -> bytes:
    return generate.canonical_bytes(document)


def verify_annotation_review(tree: Path) -> Dict[str, Any]:
    """Verify the frozen review against the current originals and lock."""
    tree = Path(tree)
    failures: List[str] = []
    review_path = tree / REVIEW_NAME
    if not review_path.is_file():
        return {"ok": False, "failures": [f"{REVIEW_NAME} is missing"], "counts": {"documents": 0, "pages": 0}}
    review = _read_json(review_path)
    if review.get("schema_version") != REVIEW_SCHEMA_VERSION:
        failures.append("the review schema version is not supported")
    if review.get("artifact_role") != "frozen_review":
        failures.append("the review must carry artifact_role=frozen_review")
    status = review.get("status")
    if status not in ("pending_human_confirmation", "confirmed"):
        failures.append(f"the review status {status!r} is not a supported status")
    if status == "pending_human_confirmation":
        if review.get("reviewed_by") is not None or review.get("reviewed_at") is not None:
            failures.append("a pending review must leave reviewed_by and reviewed_at empty")
    elif status == "confirmed":
        if not review.get("reviewed_by") or not review.get("reviewed_at"):
            failures.append("a confirmed review must name the reviewer and the time")
    if review.get("reviewer") != "Project Owner":
        failures.append("the review must name the Project Owner as the reviewer")

    check = machine_precheck(tree)
    stored = review.get("machine_precheck", {})
    for key in ("documents", "pages", "evidence", "scoring", "manifest", "fixtures"):
        if stored.get(key) != check[key]:
            failures.append(f"the stored machine pre-check field {key} is stale")
    if review.get("differences") != []:
        failures.append("the frozen review must record zero differences")
    if review.get("disputes") != []:
        failures.append("the frozen review must record zero disputes")

    documents = review.get("documents", [])
    if [item.get("doc_id") for item in documents] != _sorted_doc_ids():
        failures.append("the review must cover the 30 matrix documents in matrix order")
    expected_pages = {item["doc_id"]: item["pages"] for item in check["documents_detail"]}
    for item in documents:
        doc_id = item.get("doc_id")
        if doc_id not in expected_pages:
            continue
        if item.get("pages") != expected_pages[doc_id]:
            failures.append(f"{doc_id}: the per-page review does not match the current pages")
    if sum(len(item.get("pages", [])) for item in documents) != check["pages"]:
        failures.append("the review does not list every page")

    criteria = review.get("completion_criteria", [])
    if len(criteria) != 9 or [item.get("id") for item in criteria] != [f"C{i}" for i in range(1, 10)]:
        failures.append("the review must carry the nine completion criteria")
    carry = review.get("t15_carryover", {})
    if carry.get("fixture_id") != "da-fx-unread":
        failures.append("the T15 carryover entry is missing")
    elif carry.get("evidence_status") != "unreadable" or carry.get("must_not_auto_accept") is not True:
        failures.append("the T15 carryover entry does not record the unreadable decision")

    lock_path = tree / LOCK_NAME
    readme_path = tree / README_NAME
    if not lock_path.is_file() or review.get("annotation_lock", {}).get("sha256") != _sha256(lock_path.read_bytes()):
        failures.append("the review is not bound to the current annotation lock")
    if not readme_path.is_file() or review.get("dataset_readme", {}).get("sha256") != _sha256(readme_path.read_bytes()):
        failures.append("the review is not bound to the current README")

    return {
        "ok": not failures,
        "failures": failures,
        "counts": {"documents": len(documents), "pages": sum(len(item.get("pages", [])) for item in documents)},
    }


def verify_readme(tree: Path) -> Dict[str, Any]:
    """Verify the required README sections; the digest is checked by the manifest."""
    path = Path(tree) / README_NAME
    if not path.is_file():
        return {"ok": False, "failures": [f"{README_NAME} is missing"], "counts": {"sections": 0}}
    text = path.read_text(encoding="utf-8")
    missing = [heading for heading in README_SECTIONS if heading not in text]
    return {
        "ok": not missing,
        "failures": [f"{README_NAME}: the section {heading!r} is missing" for heading in missing],
        "counts": {"sections": len(README_SECTIONS) - len(missing)},
    }


# ---------------------------------------------------------------------------
# Check integration
# ---------------------------------------------------------------------------


def check_frozen_artifacts(
    candidate: Path,
    reference: Path,
    reference_manifest: Mapping[str, Any],
) -> Dict[str, Any]:
    """Verify the frozen files; never regenerate them.

    The reference manifest and the reference files are the baseline.  If the
    candidate holds a copy of a frozen file, the copy must match the
    reference byte for byte.  A freshly generated candidate does not hold the
    frozen files at all, and that is valid: the check only proves that the
    frozen report still describes the audited originals.
    """
    candidate = Path(candidate)
    reference = Path(reference)
    failures: List[str] = []
    counts = {
        "frozen_total": 0,
        "frozen_verified": 0,
        "frozen_compared": 0,
        "lock_total": 0,
        "lock_verified": 0,
        "review_total": 0,
        "review_verified": 0,
        "readme_total": 0,
        "readme_verified": 0,
    }
    for entry in reference_manifest["files"]:
        if entry["artifact_role"] != "frozen_review":
            continue
        counts["frozen_total"] += 1
        rel = entry["path"]
        reference_file = reference / rel
        if not reference_file.is_file():
            failures.append(f"{rel}: the frozen file is missing")
            continue
        data = reference_file.read_bytes()
        if _sha256(data) != entry["sha256"]:
            failures.append(f"{rel}: the reference manifest does not match the frozen file")
            continue
        if len(data) != entry["bytes"]:
            failures.append(f"{rel}: the frozen byte count is stale")
            continue
        counts["frozen_verified"] += 1
        candidate_file = candidate / rel
        if candidate_file.is_file():
            if candidate_file.read_bytes() != data:
                failures.append(f"{rel}: the candidate copy differs from the frozen file")
                continue
            counts["frozen_compared"] += 1

        if rel == LOCK_NAME:
            counts["lock_total"] += 1
            result = verify_annotation_lock(reference)
            counts["lock_verified"] += 1 if result["ok"] else 0
            failures.extend(f"lock: {failure}" for failure in result["failures"])
        elif rel == REVIEW_NAME:
            counts["review_total"] += 1
            result = verify_annotation_review(reference)
            counts["review_verified"] += 1 if result["ok"] else 0
            failures.extend(f"review: {failure}" for failure in result["failures"])
        elif rel == README_NAME:
            counts["readme_total"] += 1
            result = verify_readme(reference)
            counts["readme_verified"] += 1 if result["ok"] else 0
            failures.extend(result["failures"])
        else:
            failures.append(f"{rel}: an unknown frozen artifact")
    return {
        "ok": not failures,
        "failures": failures,
        "counts": counts,
        "checks": {
            "frozen": counts["frozen_verified"],
            "lock": counts["lock_verified"],
            "review": counts["review_verified"],
            "readme": counts["readme_verified"],
        },
    }


# ---------------------------------------------------------------------------
# Freeze
# ---------------------------------------------------------------------------


def _entry(tree: Path, rel: str, media_type: str, frozen_kind: str) -> Dict[str, Any]:
    data = (tree / rel).read_bytes()
    return {
        "path": rel,
        "media_type": media_type,
        "artifact_role": "frozen_review",
        "frozen_kind": frozen_kind,
        "bytes": len(data),
        "sha256": _sha256(data),
    }


def _write_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(generate.canonical_bytes(document))


def freeze(tree: Path, *, release_draft: bool = True) -> Dict[str, Any]:
    """Write the lock and the review, then update the manifest and run report.

    This is the controlled re-freeze of T17.  It does not regenerate the
    142 main artifacts, the 64 fixture artifacts or the 100 injection files.
    """
    tree = Path(tree)
    manifest_path = tree / "manifest.json"
    if not manifest_path.is_file():
        raise ReviewError(f"{tree} is not a published tree: manifest.json is missing")
    precheck = machine_precheck(tree)
    if precheck["differences"]:
        raise ReviewError(
            "the machine pre-check found substantive differences: "
            + "; ".join(precheck["differences"][:10])
        )
    if not (tree / README_NAME).is_file():
        raise ReviewError(f"{README_NAME} must exist before the freeze")

    lock_document = build_annotation_lock(tree)
    lock_data = lock_bytes(lock_document)
    (tree / LOCK_NAME).write_bytes(lock_data)
    if _sha256((tree / LOCK_NAME).read_bytes()) != _sha256(lock_data):
        raise ReviewError("the lock was not written as built")

    review_document = build_annotation_review(
        tree,
        lock_sha256=_sha256(lock_data),
        readme_sha256=_sha256((tree / README_NAME).read_bytes()),
        precheck=precheck,
    )
    review_data = review_bytes(review_document)
    (tree / REVIEW_NAME).write_bytes(review_data)
    if _sha256((tree / REVIEW_NAME).read_bytes()) != _sha256(review_data):
        raise ReviewError("the review was not written as built")

    manifest = _read_json(manifest_path)
    frozen_paths = {LOCK_NAME, REVIEW_NAME, README_NAME}
    files = [entry for entry in manifest["files"] if entry["path"] not in frozen_paths]
    files.extend([
        _entry(tree, LOCK_NAME, "application/json", "annotation_lock"),
        _entry(tree, REVIEW_NAME, "application/json", "annotation_review"),
        _entry(tree, README_NAME, "text/markdown", "dataset_readme"),
    ])
    manifest["files"] = sorted(files, key=lambda entry: entry["path"])
    manifest["generator_commit"] = generate.generator_commit()
    manifest["generator_source_digest"] = generate.generator_source_digest()
    _write_json(manifest_path, manifest)

    # The review stores the manifest summary.  That summary changes when the
    # three frozen entries join the manifest, so the review is built a second
    # time from the final manifest, and its entry digest is updated.
    final_precheck = machine_precheck(tree)
    if final_precheck["differences"]:
        raise ReviewError(
            "the machine pre-check failed after the manifest update: "
            + "; ".join(final_precheck["differences"][:10])
        )
    review_document = build_annotation_review(
        tree,
        lock_sha256=_sha256((tree / LOCK_NAME).read_bytes()),
        readme_sha256=_sha256((tree / README_NAME).read_bytes()),
        precheck=final_precheck,
    )
    review_data = review_bytes(review_document)
    (tree / REVIEW_NAME).write_bytes(review_data)
    review_entry = next(entry for entry in manifest["files"] if entry["path"] == REVIEW_NAME)
    review_entry["bytes"] = len(review_data)
    review_entry["sha256"] = _sha256(review_data)
    manifest["files"] = sorted(manifest["files"], key=lambda entry: entry["path"])
    stable_digest = generate._stable_manifest_digest(manifest)

    report_path = tree / "reports" / "check_summary.json"
    report = _read_json(report_path)
    report["manifest_digest"] = stable_digest
    report["generator_commit"] = manifest["generator_commit"]
    report["annotation_review"] = {
        "lock": LOCK_NAME,
        "lock_sha256": _sha256((tree / LOCK_NAME).read_bytes()),
        "review": REVIEW_NAME,
        "review_sha256": _sha256((tree / REVIEW_NAME).read_bytes()),
        "readme": README_NAME,
        "readme_sha256": _sha256((tree / README_NAME).read_bytes()),
        "documents": precheck["documents"],
        "pages": precheck["pages"],
        "differences": len(precheck["differences"]),
        "manual_check_items": len(precheck["manual_items"]),
        "status": "pending_human_confirmation",
        "note": "Added by the T17 controlled re-freeze. The generated artifacts did not change.",
    }
    _write_json(report_path, report)
    report_entry = next(entry for entry in manifest["files"] if entry["path"] == "reports/check_summary.json")
    report_data = report_path.read_bytes()
    report_entry["bytes"] = len(report_data)
    report_entry["sha256"] = _sha256(report_data)
    manifest["files"] = sorted(manifest["files"], key=lambda entry: entry["path"])
    _write_json(manifest_path, manifest)

    manifest_sha = _sha256(manifest_path.read_bytes())
    release: Optional[Dict[str, Any]] = None
    if release_draft:
        release = {
            "schema_version": 1,
            "status": "draft_pending_human_confirmation",
            "tag": "docuagents-v1.0",
            "tag_created": False,
            "release_record_finalized": False,
            "base": {
                "branch": "docuagents-v1",
                "commit": "47e8ce45d60ccebc91546ffb38d19b059148d736",
            },
            "manifest": {
                "path": "datasets/docuagents_v1/manifest.json",
                "sha256": manifest_sha,
                "stable_digest": stable_digest,
                "files": len(manifest["files"]),
            },
            "locked_components": {
                "generator_commit": manifest["generator_commit"],
                "generator_source_digest": manifest["generator_source_digest"]["canonical_sha256"],
                "matrix_digest": manifest["matrix_digest"],
                "environment_digest": manifest["environment_digest"]["canonical_sha256"],
                "split": {
                    "path": "splits.json",
                    "sha256": _sha256((tree / "splits.json").read_bytes()),
                },
                "gold": {
                    "records": len(matrix.ROWS),
                    "sha256": generate.canonical_sha256([
                        {"path": f"gold/{row.doc_id}.json",
                         "sha256": _sha256((tree / "gold" / f"{row.doc_id}.json").read_bytes())}
                        for row in matrix.ROWS
                    ]),
                },
                "seed": manifest["seed"],
                "counts": manifest["counts"],
                "annotations": {
                    "lock": LOCK_NAME,
                    "lock_sha256": _sha256((tree / LOCK_NAME).read_bytes()),
                    "review": REVIEW_NAME,
                    "review_sha256": _sha256((tree / REVIEW_NAME).read_bytes()),
                    "readme": README_NAME,
                    "readme_sha256": _sha256((tree / README_NAME).read_bytes()),
                    "documents": precheck["documents"],
                    "pages": precheck["pages"],
                    "fixtures": precheck["fixtures"]["content"] + precheck["fixtures"]["technical"],
                    "object_level_cases": precheck["fixtures"]["object_level"],
                    "injection_cases": manifest["counts"]["fault_injection"]["cases"],
                },
            },
            "not_done": [
                "The tag is not created.",
                "The formal release record is not finalized.",
                "The Project Owner has not signed the review.",
            ],
        }
        _write_json(generate.ROOT / "reports" / RELEASE_DRAFT_NAME, release)

    lock_result = verify_annotation_lock(tree)
    review_result = verify_annotation_review(tree)
    if not lock_result["ok"]:
        raise ReviewError("the lock does not verify after the freeze: " + "; ".join(lock_result["failures"]))
    if not review_result["ok"]:
        raise ReviewError("the review does not verify after the freeze: " + "; ".join(review_result["failures"]))
    return {
        "ok": True,
        "tree": str(tree),
        "manifest_sha256": manifest_sha,
        "manifest_digest": stable_digest,
        "files": len(manifest["files"]),
        "lock_sha256": _sha256((tree / LOCK_NAME).read_bytes()),
        "review_sha256": _sha256((tree / REVIEW_NAME).read_bytes()),
        "readme_sha256": _sha256((tree / README_NAME).read_bytes()),
        "precheck": {key: value for key, value in precheck.items() if key != "documents_detail"},
        "release_draft": release is not None,
    }


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _print_precheck(precheck: Mapping[str, Any]) -> None:
    print(f"documents: {precheck['documents']}, pages: {precheck['pages']}")
    print(f"evidence observed: {precheck['evidence']['observed']}, "
          f"located: {precheck['evidence']['located']}, "
          f"boxes checked: {precheck['evidence']['boxes_checked']}")
    print(f"not_found: {precheck['evidence']['not_found']}, "
          f"unreadable: {precheck['evidence']['unreadable']}, "
          f"ambiguous: {precheck['evidence']['ambiguous']}")
    print(f"manifest files: {precheck['manifest']['main_generated']} main + "
          f"{precheck['manifest']['injection']} injection + "
          f"{precheck['manifest']['runtime_reports']} report")
    print(f"fixtures: {precheck['fixtures']['content']} content + "
          f"{precheck['fixtures']['technical']} technical + "
          f"{precheck['fixtures']['object_level']} object-level")
    print(f"differences: {len(precheck['differences'])}, "
          f"manual items: {len(precheck['manual_items'])}")
    for failure in precheck["differences"]:
        print(f"  - {failure}")


def main(argv: Sequence[str] | None = None) -> int:
    """The command-line entry point.  Installs the offline guard first."""
    generate.offline.install_network_guard()
    parser = argparse.ArgumentParser(
        prog="python -m docuagents_gen.review",
        description="Machine pre-check, annotation lock and frozen review (T17).",
    )
    parser.add_argument("--tree", type=Path, default=generate.DEFAULT_OUT)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--precheck", action="store_true")
    mode.add_argument("--freeze", action="store_true")
    mode.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.precheck:
            precheck = machine_precheck(args.tree)
            _print_precheck(precheck)
            return 0 if not precheck["differences"] else 1
        if args.freeze:
            summary = freeze(args.tree)
            print(f"frozen: {summary['files']} manifest files")
            print(f"manifest sha256: {summary['manifest_sha256']}")
            print(f"lock sha256: {summary['lock_sha256']}")
            print(f"review sha256: {summary['review_sha256']}")
            return 0
        lock_result = verify_annotation_lock(args.tree)
        review_result = verify_annotation_review(args.tree)
        readme_result = verify_readme(args.tree)
        for line in lock_result["failures"] + review_result["failures"] + readme_result["failures"]:
            print(f"  - {line}")
        print(f"lock: {lock_result['counts']['verified']}/{lock_result['counts']['entries']}")
        print(f"review: {review_result['counts']['documents']} documents, "
              f"{review_result['counts']['pages']} pages")
        print(f"readme sections: {readme_result['counts']['sections']}")
        return 0 if lock_result["ok"] and review_result["ok"] and readme_result["ok"] else 1
    except ReviewError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


__all__ = [
    "LOCK_NAME",
    "README_NAME",
    "REVIEW_NAME",
    "ReviewError",
    "build_annotation_lock",
    "build_annotation_review",
    "check_frozen_artifacts",
    "freeze",
    "lock_bytes",
    "machine_precheck",
    "review_bytes",
    "verify_annotation_lock",
    "verify_annotation_review",
    "verify_readme",
]


if __name__ == "__main__":
    sys.exit(main())
