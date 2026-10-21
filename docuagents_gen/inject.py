"""Fault injection suite for the class-A gold records (sample plan §7 P6).

The suite turns ten clean gold annotations into fifty first-candidate fault
cases.  Each case has two files:

* ``fault_injection/<case_id>.json`` -- the evaluation envelope.  It names the
  base document, the injected fault, the original and injected value, the
  neutral candidate file and the expected behaviour.  Only an evaluation
  process reads this file.
* ``fault_injection/candidates/inj-NNNN.json`` -- a complete runtime
  ``ExtractionEnvelope`` (design §6.2) built as a *simulated first read*.
  The name, ``document_id`` and page ids carry no class, fault or routing
  meaning (design §12.5, QA28), so a pipeline cannot recognise the case from
  the input.

Rules this module keeps:

* No randomness and no clock.  The case table, every target row and the
  neutral numbering are written out; two runs give the same bytes.
* Every fault is real.  A value change is compared as ``Decimal``; a
  transposition must keep the digit multiset and change the value.  A
  registered alternate row exists for the transposition, and an impossible
  case raises :class:`InjectError` instead of writing a no-op.
* Evidence is rebuilt at the runtime level.  The candidate never copies the
  gold ``generator_recorded`` / ``text_verified`` / ``human_reviewed`` marks,
  a precise bounding box or a source reference; every record is
  ``model_cited`` with ``context_binding=unverified``.
* Structural faults rebuild the item indexes, the evidence paths and the row
  references.  A missing row leaves no stale row behind; a duplicated row
  keeps both rows, which is the point of the fault.
* The gold records and the documents are read only.  Their SHA-256 values are
  the same before and after an injection.

Design basis: MVP v1.1 §12.5 (P6 paragraph, neutral aliases, QA28), §8.7,
§6.2, §6.4, §7.5; sample plan §7 P6, §3, §5.1, §5.3, §6.3.
"""

from __future__ import annotations

import copy
import hashlib
import json
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import matrix, split

#: The subtree that holds the suite inside the dataset directory.
DIR_NAME = "fault_injection"

#: The subtree that holds the neutral runtime candidates.
CANDIDATE_DIR_NAME = "candidates"

#: Evaluation envelope structure version (unrelated to the runtime envelope).
SCHEMA_VERSION = 1

#: The injection derivation seed, registered explicitly.  No draw uses it:
#: the table below is closed, so the value documents the fixed environment.
INJECTION_SEED = 2028

#: The five faults, in the order that fixes the neutral numbering.
FAULT_TYPES: Tuple[str, ...] = (
    "unit_decimal_shift",
    "line_digit_transpose",
    "missing_row",
    "duplicate_row",
    "tax_null",
)

#: Structural faults change the row count; scalar faults change one value.
STRUCTURAL_FAULTS = frozenset({"missing_row", "duplicate_row"})

#: The ten class-A documents, in matrix order.
A_DOC_IDS: Tuple[str, ...] = (
    "da-a1-01", "da-a1-02", "da-a1-03", "da-a1-04", "da-a1-05",
    "da-a1-06", "da-a2-01", "da-a3-01", "da-a3-02", "da-a4-01",
)

#: Registered target row per document and fault.  The transposition targets
#: avoid a leading-zero result (for example ``69.00 -> 96.00``, not
#: ``60.00 -> 06.00``); every other fault uses row zero.
TARGET_ROWS: Mapping[str, Mapping[str, int]] = {
    "da-a1-01": {"unit_decimal_shift": 0, "line_digit_transpose": 0,
                 "missing_row": 0, "duplicate_row": 0},
    "da-a1-02": {"unit_decimal_shift": 0, "line_digit_transpose": 0,
                 "missing_row": 0, "duplicate_row": 0},
    "da-a1-03": {"unit_decimal_shift": 0, "line_digit_transpose": 0,
                 "missing_row": 0, "duplicate_row": 0},
    "da-a1-04": {"unit_decimal_shift": 0, "line_digit_transpose": 1,
                 "missing_row": 0, "duplicate_row": 0},
    "da-a1-05": {"unit_decimal_shift": 0, "line_digit_transpose": 0,
                 "missing_row": 0, "duplicate_row": 0},
    "da-a1-06": {"unit_decimal_shift": 0, "line_digit_transpose": 2,
                 "missing_row": 0, "duplicate_row": 0},
    "da-a2-01": {"unit_decimal_shift": 0, "line_digit_transpose": 0,
                 "missing_row": 0, "duplicate_row": 0},
    "da-a3-01": {"unit_decimal_shift": 0, "line_digit_transpose": 0,
                 "missing_row": 0, "duplicate_row": 0},
    "da-a3-02": {"unit_decimal_shift": 0, "line_digit_transpose": 0,
                 "missing_row": 0, "duplicate_row": 0},
    "da-a4-01": {"unit_decimal_shift": 0, "line_digit_transpose": 0,
                 "missing_row": 0, "duplicate_row": 0},
}

#: Registered alternate row for the transposition.  It is used only when the
#: primary row cannot produce a different value; a failure of both raises.
ALTERNATE_ROWS: Mapping[str, int] = {
    "da-a1-01": 1,
    "da-a1-02": 1,
    "da-a1-03": 1,
    "da-a1-04": 2,
    "da-a1-05": 1,
    "da-a1-06": 3,
    "da-a2-01": 1,
    "da-a3-01": 1,
    "da-a3-02": 1,
    "da-a4-01": 1,
}

#: The operation name recorded in the evaluation envelope.
OPERATIONS: Mapping[str, str] = {
    "unit_decimal_shift": "multiply_unit_price_by_ten",
    "line_digit_transpose": "swap_adjacent_digits_in_line_amount",
    "missing_row": "delete_line_item_row",
    "duplicate_row": "duplicate_line_item_row",
    "tax_null": "set_tax_amount_unreadable",
}

#: The four expected-behaviour keys.  The values follow design §8.7: a scalar
#: misread can be patched and re-verified; a structure change goes to a human
#: review; a tax amount that was not read can be found again in the original.
EXPECTED_BEHAVIOUR: Mapping[str, Mapping[str, Any]] = {
    "unit_decimal_shift": {
        "repairable": True,
        "expected_route": "patch_and_reverify",
        "requires_human_review": False,
        "must_not_auto_accept": True,
    },
    "line_digit_transpose": {
        "repairable": True,
        "expected_route": "patch_and_reverify",
        "requires_human_review": False,
        "must_not_auto_accept": True,
    },
    "missing_row": {
        "repairable": False,
        "expected_route": "human_review",
        "requires_human_review": True,
        "must_not_auto_accept": True,
    },
    "duplicate_row": {
        "repairable": False,
        "expected_route": "human_review",
        "requires_human_review": True,
        "must_not_auto_accept": True,
    },
    "tax_null": {
        "repairable": True,
        "expected_route": "patch_and_reverify",
        "requires_human_review": False,
        "must_not_auto_accept": True,
    },
}

NOTES: Mapping[str, str] = {
    "unit_decimal_shift": (
        "The candidate reads the unit price ten times too large; the line "
        "amount, the subtotal and the total stay as printed."
    ),
    "line_digit_transpose": (
        "The candidate transposes two adjacent digits of one line amount; "
        "every other printed value stays as printed."
    ),
    "missing_row": (
        "The candidate omits one printed row; the later indexes and row "
        "references are rebuilt.  The totals are not adjusted."
    ),
    "duplicate_row": (
        "The candidate reports one printed row twice; the later indexes and "
        "row references are rebuilt.  The totals are not adjusted."
    ),
    "tax_null": (
        "The candidate does not find the printed tax amount.  The printed "
        "value stays only in this envelope, never in the candidate."
    ),
}

#: The invoice-level gold evidence keys and their runtime envelope paths.
INVOICE_EVIDENCE: Tuple[Tuple[str, str], ...] = (
    ("invoice_number", "invoice_number"),
    ("invoice_date", "invoice_date"),
    ("vendor_name", "vendor_name"),
    ("currency", "currency"),
    ("purchase_order_number", "extras.po_number"),
    ("due_date", "extras.due_date"),
    ("subtotal_amount", "subtotal"),
    ("tax_amount", "tax_amount"),
    ("total_amount", "invoice_total"),
)

#: The line-item gold evidence suffixes and their runtime field names.
LINE_EVIDENCE: Tuple[Tuple[str, str], ...] = (
    ("quantity", "quantity"),
    ("unit_price", "unit_price"),
    ("amount", "line_net_amount"),
)

#: Runtime line-item fields, in the order used by the row references.
LINE_FIELDS: Tuple[str, ...] = ("quantity", "unit_price", "line_net_amount")

#: Runtime envelope keys that every candidate carries, including empty ones.
ENVELOPE_KEYS: Tuple[str, ...] = (
    "schema_version", "document_id", "invoice", "raw_fields", "scope_status",
    "scope_signals", "out_of_scope_reasons", "evidence", "row_references",
    "uncertainties", "extras",
)


class InjectError(RuntimeError):
    """Raised when an injection cannot be built or checked."""


# ---------------------------------------------------------------------------
# The registered case table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InjectionCase:
    """One registered fault case.  Every field is fixed at import time."""

    case_id: str
    base_doc_id: str
    fault_type: str
    split: str
    group_id: str
    source_family: str
    candidate_id: str
    target_row: Optional[int]
    alternate_row: Optional[int]
    target_path: str
    operation: str
    notes: str


def _target_path(doc_id: str, fault: str, row: Optional[int]) -> str:
    if fault == "unit_decimal_shift":
        return f"items[{row}].unit_price"
    if fault == "line_digit_transpose":
        return f"items[{row}].line_net_amount"
    if fault in STRUCTURAL_FAULTS:
        return f"items[{row}]"
    return "tax_amount"


def _build_plan() -> Tuple[InjectionCase, ...]:
    """The closed 50-case table, in matrix order and fixed fault order."""
    cases: List[InjectionCase] = []
    number = 0
    for doc_id in A_DOC_IDS:
        row = matrix.BY_ID.get(doc_id)
        if row is None or row.klass != "A":
            raise InjectError(f"{doc_id} is not a class-A matrix document")
        for fault in FAULT_TYPES:
            number += 1
            target_row = None if fault == "tax_null" else TARGET_ROWS[doc_id][fault]
            alternate = None if fault == "tax_null" else ALTERNATE_ROWS[doc_id]
            cases.append(
                InjectionCase(
                    case_id=f"{doc_id}__{fault}",
                    base_doc_id=doc_id,
                    fault_type=fault,
                    split=row.split,
                    group_id=split.group_id(row),
                    source_family=row.source_family,
                    candidate_id=f"inj-{number:04d}",
                    target_row=target_row,
                    alternate_row=alternate,
                    target_path=_target_path(doc_id, fault, target_row),
                    operation=OPERATIONS[fault],
                    notes=NOTES[fault],
                )
            )
    return tuple(cases)


#: The complete registered plan.  Import-time construction keeps the table
#: and the matrix in agreement; a mismatch stops the module.
INJECTION_PLAN: Tuple[InjectionCase, ...] = _build_plan()

#: Case lookup by ``case_id``.
CASE_BY_ID: Mapping[str, InjectionCase] = {
    case.case_id: case for case in INJECTION_PLAN
}


def plan_counts() -> Dict[str, int]:
    """The manifest counts of the suite; 50 cases are not 50 invoices."""
    return {
        "cases": len(INJECTION_PLAN),
        "dev": sum(1 for case in INJECTION_PLAN if case.split == "dev"),
        "test": sum(1 for case in INJECTION_PLAN if case.split == "test"),
        "envelopes": len(INJECTION_PLAN),
        "candidates": len(INJECTION_PLAN),
    }


# ---------------------------------------------------------------------------
# Canonical JSON, identical to the dataset rule (digest_method_version 1)
# ---------------------------------------------------------------------------


def canonical_bytes(obj: Any) -> bytes:
    """Sorted keys, compact separators, UTF-8, NFC, no trailing newline."""
    text = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return unicodedata.normalize("NFC", text).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(obj))


def _read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Deterministic value transforms
# ---------------------------------------------------------------------------


def shift_unit_price(text: str) -> str:
    """Move the decimal point one position right: ``4.25 -> 42.50``.

    The input is a plain amount string.  The result keeps two decimals, so
    the printed form stays plausible.  An input without decimals is handled
    by the same rule: ``5 -> 50.00``.
    """
    shifted = (Decimal(text) * Decimal(10)).quantize(Decimal("0.01"))
    result = f"{shifted:.2f}"
    if Decimal(result) == Decimal(text):
        raise InjectError(f"the unit price shift would be a no-op: {text}")
    return result


def transpose_adjacent_digits(text: str) -> str:
    """Swap the first adjacent, different digit pair of ``text``.

    The pair must be adjacent in the string itself, so the decimal point is
    never crossed.  The result must parse to a different value; otherwise
    the scan continues.  ``None`` means that the string has no such pair.
    """
    for index in range(len(text) - 1):
        first, second = text[index], text[index + 1]
        if not (first.isdigit() and second.isdigit()) or first == second:
            continue
        swapped = text[:index] + second + first + text[index + 2:]
        try:
            changed = Decimal(swapped) != Decimal(text)
        except ArithmeticError:  # pragma: no cover - plain amounts only
            continue
        if changed:
            return swapped
    return None


# ---------------------------------------------------------------------------
# Evidence records (design §6.4, runtime level only)
# ---------------------------------------------------------------------------


def _runtime_evidence(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Convert one gold evidence record to a runtime-level record.

    The gold verification marks, the exact bounding box and the precision of
    that box are dropped.  The page, the text and the searched pages stay:
    they are what a first read would cite.
    """
    status = record.get("status")
    if status == "observed":
        return {
            "status": "observed",
            "page": record.get("page"),
            "text": record.get("text"),
            "searched_pages": list(record.get("searched_pages") or []),
            "bbox_precision": "unavailable",
            "verification": "model_cited",
            "context_binding": "unverified",
        }
    if status == "not_found":
        return {
            "status": "not_found",
            "page": None,
            "text": None,
            "searched_pages": list(record.get("searched_pages") or []),
            "bbox_precision": "unavailable",
            "verification": "model_cited",
            "context_binding": "unverified",
        }
    raise InjectError(f"unsupported gold evidence status: {status!r}")


def _not_found_evidence(record: Mapping[str, Any]) -> Dict[str, Any]:
    """A runtime ``not_found`` record that keeps the searched pages."""
    return {
        "status": "not_found",
        "page": None,
        "text": None,
        "searched_pages": list(record.get("searched_pages") or []),
        "bbox_precision": "unavailable",
        "verification": "model_cited",
        "context_binding": "unverified",
    }


# ---------------------------------------------------------------------------
# Candidate construction
# ---------------------------------------------------------------------------


def _gold_items(invoice: Mapping[str, Any]) -> List[Dict[str, Any]]:
    return [
        {
            "item_description": item["description"],
            "quantity": item["quantity"],
            "unit_price": item["unit_price"],
            "line_net_amount": item["amount"],
        }
        for item in invoice["line_items"]
    ]


def _gold_row_evidence(gold: Mapping[str, Any]) -> List[Dict[str, Any]]:
    records = gold["evidence"]
    rows: List[Dict[str, Any]] = []
    for index in range(len(gold["invoice"]["line_items"])):
        row: Dict[str, Any] = {}
        for gold_field, runtime_field in LINE_EVIDENCE:
            record = records.get(f"line_items[{index}].{gold_field}")
            if record is None:
                raise InjectError(
                    f"{gold['doc_id']}: missing evidence for line {index} {gold_field}"
                )
            row[runtime_field] = _runtime_evidence(record)
        rows.append(row)
    return rows


def _apply_scalar_fault(
    case: InjectionCase,
    items: List[Dict[str, Any]],
    row_evidence: List[Dict[str, Any]],
) -> int:
    """Apply a scalar fault in place and return the row that it changed."""
    row = case.target_row
    if row is None or not 0 <= row < len(items):
        raise InjectError(f"{case.case_id}: target row {row} is out of range")
    if case.fault_type == "unit_decimal_shift":
        original = items[row]["unit_price"]
        injected = shift_unit_price(original)
        items[row]["unit_price"] = injected
        row_evidence[row]["unit_price"]["text"] = injected
        return row
    if case.fault_type == "line_digit_transpose":
        original = items[row]["line_net_amount"]
        injected = transpose_adjacent_digits(original)
        if injected is None:
            alternate = case.alternate_row
            if alternate is None or alternate == row or not 0 <= alternate < len(items):
                raise InjectError(f"{case.case_id}: the transposition has no target")
            original = items[alternate]["line_net_amount"]
            injected = transpose_adjacent_digits(original)
            if injected is None:
                raise InjectError(f"{case.case_id}: the alternate row is a no-op too")
            items[alternate]["line_net_amount"] = injected
            row_evidence[alternate]["line_net_amount"]["text"] = injected
            return alternate
        items[row]["line_net_amount"] = injected
        row_evidence[row]["line_net_amount"]["text"] = injected
        return row
    raise InjectError(f"{case.case_id}: {case.fault_type} is not a scalar fault")


def _build_candidate(
    gold: Mapping[str, Any], case: InjectionCase
) -> Tuple[Dict[str, Any], Optional[int]]:
    """Build the complete runtime envelope and name the row that changed."""
    invoice = gold["invoice"]
    items = _gold_items(invoice)
    row_evidence = _gold_row_evidence(gold)
    raw_gold = gold["evidence"]

    actual_row: Optional[int] = case.target_row
    if case.fault_type in ("unit_decimal_shift", "line_digit_transpose"):
        actual_row = _apply_scalar_fault(case, items, row_evidence)

    elif case.fault_type == "missing_row":
        row = case.target_row
        if row is None or not 0 <= row < len(items):
            raise InjectError(f"{case.case_id}: target row {row} is out of range")
        del items[row]
        del row_evidence[row]

    elif case.fault_type == "duplicate_row":
        row = case.target_row
        if row is None or not 0 <= row < len(items):
            raise InjectError(f"{case.case_id}: target row {row} is out of range")
        items.insert(row + 1, copy.deepcopy(items[row]))
        row_evidence.insert(row + 1, copy.deepcopy(row_evidence[row]))

    elif case.fault_type == "tax_null":
        if case.target_row is not None:
            raise InjectError(f"{case.case_id}: tax_null must not name a row")
    else:  # pragma: no cover - the table is closed
        raise InjectError(f"{case.case_id}: unknown fault {case.fault_type}")

    # Invoice payload: parsed values only, money stays a string.
    candidate_invoice: Dict[str, Any] = {
        "invoice_number": invoice["invoice_number"],
        "invoice_date": invoice["invoice_date"],
        "vendor_name": invoice["vendor_name"],
        "currency": invoice["currency"],
        "items": items,
        "subtotal": invoice["subtotal_amount"],
        "tax_amount": None if case.fault_type == "tax_null" else invoice["tax_amount"],
        "invoice_total": invoice["total_amount"],
    }

    # Evidence: invoice fields, rebuilt rows, then scope signals.
    evidence: Dict[str, Any] = {}
    for gold_key, runtime_path in INVOICE_EVIDENCE:
        record = raw_gold.get(gold_key)
        if record is None:
            raise InjectError(f"{gold['doc_id']}: missing evidence for {gold_key}")
        if runtime_path == "tax_amount" and case.fault_type == "tax_null":
            evidence[runtime_path] = _not_found_evidence(record)
        else:
            evidence[runtime_path] = _runtime_evidence(record)
    for index, row_record in enumerate(row_evidence):
        for field in LINE_FIELDS:
            evidence[f"items[{index}].{field}"] = row_record[field]
    for key, record in raw_gold.items():
        if key.startswith("scope_signals."):
            evidence[key] = _runtime_evidence(record)

    scope_signals = copy.deepcopy(gold["scope"]["signals"])
    if case.fault_type in STRUCTURAL_FAULTS:
        # The simulated read sees the row count it reports.
        scope_signals["observed_row_count"] = len(items)
        evidence["scope_signals.observed_row_count"]["text"] = f"Rows observed: {len(items)}"

    # Raw fields: the text a first read would keep for audit.
    raw_fields: Dict[str, Any] = {}
    for runtime_path in (
        "invoice_number", "invoice_date", "vendor_name", "currency",
        "extras.po_number", "extras.due_date",
        "subtotal", "tax_amount", "invoice_total",
    ):
        raw_fields[runtime_path] = evidence[runtime_path]["text"]
    for index, item in enumerate(items):
        raw_fields[f"items[{index}].item_description"] = item["item_description"]
        for field in LINE_FIELDS:
            raw_fields[f"items[{index}].{field}"] = evidence[f"items[{index}].{field}"]["text"]

    # Row references: one per item, with a valid neutral page id.
    row_references: List[Dict[str, Any]] = []
    for index, row_record in enumerate(row_evidence):
        page = None
        for field in LINE_FIELDS:
            page = row_record[field].get("page")
            if page is not None:
                break
        if page is None:  # pragma: no cover - class-A rows are observed
            raise InjectError(f"{case.case_id}: row {index} has no page")
        row_references.append(
            {
                "item_index": index,
                "row_id": f"row-{index + 1}",
                "page_id": f"{case.candidate_id}:p{page}",
                "source_row_id": None,
                "field_evidence_keys": [
                    f"items[{index}].{field}"
                    for field in LINE_FIELDS
                    if f"items[{index}].{field}" in evidence
                ],
            }
        )

    candidate: Dict[str, Any] = {
        "schema_version": "1.1",
        "document_id": case.candidate_id,
        "invoice": candidate_invoice,
        "raw_fields": raw_fields,
        "scope_status": gold["scope"]["status"],
        "scope_signals": scope_signals,
        "out_of_scope_reasons": list(gold["scope"]["reasons"]),
        "evidence": evidence,
        "row_references": row_references,
        "uncertainties": [],
        "extras": {
            "po_number": invoice.get("purchase_order_number"),
            "due_date": invoice.get("due_date"),
        },
    }
    return candidate, actual_row


def _build_envelope(
    gold: Mapping[str, Any],
    gold_path: Path,
    case: InjectionCase,
    candidate: Mapping[str, Any],
    actual_row: Optional[int],
) -> Dict[str, Any]:
    """The evaluation envelope, including the neutral-id mapping."""
    injected = candidate["invoice"]
    if case.fault_type == "unit_decimal_shift":
        if actual_row is None:  # pragma: no cover - the plan always names a row
            raise InjectError(f"{case.case_id}: the fault has no row")
        original = gold["invoice"]["line_items"][actual_row]["unit_price"]
        injected_value = injected["items"][actual_row]["unit_price"]
        injected_path = f"items[{actual_row}].unit_price"
    elif case.fault_type == "line_digit_transpose":
        if actual_row is None:  # pragma: no cover - the plan always names a row
            raise InjectError(f"{case.case_id}: the fault has no row")
        original = gold["invoice"]["line_items"][actual_row]["amount"]
        injected_value = injected["items"][actual_row]["line_net_amount"]
        injected_path = f"items[{actual_row}].line_net_amount"
    elif case.fault_type == "missing_row":
        if actual_row is None:  # pragma: no cover - the plan always names a row
            raise InjectError(f"{case.case_id}: the fault has no row")
        original = gold["invoice"]["line_items"][actual_row]["amount"]
        injected_value = None
        injected_path = f"items[{actual_row}]"
    elif case.fault_type == "duplicate_row":
        if actual_row is None:  # pragma: no cover - the plan always names a row
            raise InjectError(f"{case.case_id}: the fault has no row")
        original = gold["invoice"]["line_items"][actual_row]["amount"]
        injected_value = original
        injected_path = f"items[{actual_row}]"
    else:  # tax_null
        original = gold["invoice"]["tax_amount"]
        injected_value = None
        injected_path = "tax_amount"

    sealed = case.split == "test"
    return {
        "schema_version": SCHEMA_VERSION,
        "suite": "fault_injection",
        "synthetic_fault": True,
        "case_id": case.case_id,
        "base_doc_id": case.base_doc_id,
        "group_id": case.group_id,
        "split": case.split,
        "document_sha256": gold["file"]["sha256"],
        "gold_sha256": _sha256(gold_path.read_bytes()),
        "injected": {
            "fault_type": case.fault_type,
            "operation": case.operation,
            "path": injected_path,
            "original": original,
            "injected": injected_value,
        },
        "initial_candidate_file": f"{CANDIDATE_DIR_NAME}/{case.candidate_id}.json",
        "candidate_id": case.candidate_id,
        "runtime_input_policy": "candidate_and_original_document_only",
        "expected_behavior": dict(EXPECTED_BEHAVIOUR[case.fault_type]),
        "sealed_for_final_evaluation": sealed,
        "sealed_note": (
            "Sealed: use this case only for the final evaluation; do not use it "
            "for development or tuning."
            if sealed
            else "Development case: it may be used to check repair behaviour; it "
                 "is not part of the sealed test split."
        ),
        "notes": case.notes,
    }


# ---------------------------------------------------------------------------
# Public generation and check API
# ---------------------------------------------------------------------------


def inject_faults(
    gold_dir: Path,
    out_dir: Path,
    *,
    seed: int = INJECTION_SEED,
    cases: Sequence[InjectionCase] | None = None,
) -> Dict[str, Any]:
    """Write the complete suite to ``out_dir`` and return a summary.

    ``gold_dir`` is read only.  ``out_dir`` must not exist, or must be empty;
    the caller (the dataset generator or a test) owns the enclosing temporary
    directory.
    """
    if seed != INJECTION_SEED:
        raise InjectError(f"the injection seed is fixed at {INJECTION_SEED}, got {seed}")
    gold_root = Path(gold_dir)
    if not gold_root.is_dir():
        raise InjectError(f"the gold directory does not exist: {gold_root}")
    out = Path(out_dir)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise InjectError(f"the injection directory {out} exists and is not empty")

    selected = list(INJECTION_PLAN if cases is None else cases)
    if [case.case_id for case in selected] != [case.case_id for case in INJECTION_PLAN]:
        if len(selected) != len(INJECTION_PLAN) or {
            case.case_id for case in selected
        } != {case.case_id for case in INJECTION_PLAN}:
            raise InjectError("the case selection must hold every registered case once")

    out.mkdir(parents=True, exist_ok=True)
    (out / CANDIDATE_DIR_NAME).mkdir(parents=True, exist_ok=True)

    gold_cache: Dict[str, Dict[str, Any]] = {}
    files: List[str] = []
    for case in selected:
        gold = gold_cache.get(case.base_doc_id)
        if gold is None:
            gold_path = gold_root / f"{case.base_doc_id}.json"
            if not gold_path.is_file():
                raise InjectError(f"the gold record does not exist: {gold_path}")
            gold = _read_json(gold_path)
            if gold.get("doc_id") != case.base_doc_id:
                raise InjectError(f"{gold_path}: the gold doc_id does not match")
            gold_cache[case.base_doc_id] = gold
        else:
            gold_path = gold_root / f"{case.base_doc_id}.json"

        candidate, actual_row = _build_candidate(gold, case)
        envelope = _build_envelope(gold, gold_path, case, candidate, actual_row)

        candidate_rel = f"{CANDIDATE_DIR_NAME}/{case.candidate_id}.json"
        _write_json(out / candidate_rel, candidate)
        _write_json(out / f"{case.case_id}.json", envelope)
        files.append(f"{case.case_id}.json")
        files.append(candidate_rel)
        files.sort()

    summary = plan_counts()
    summary["seed"] = seed
    summary["files"] = files
    return summary


def check_injections(
    candidate_dir: Path,
    reference_dir: Path,
    reference_manifest: Mapping[str, Any],
) -> Dict[str, Any]:
    """Compare the injection subtree of two dataset trees.

    The reference manifest is the only baseline.  Every declared injection
    file must exist in both trees with the declared SHA-256, and every
    envelope must point at one existing candidate, once.
    """
    candidate = Path(candidate_dir)
    reference = Path(reference_dir)
    entries = [
        entry for entry in reference_manifest.get("files", [])
        if str(entry.get("path", "")).startswith(DIR_NAME + "/")
    ]
    failures: List[str] = []
    compared = 0
    for entry in sorted(entries, key=lambda item: item["path"]):
        relative = entry["path"]
        reference_file = reference / relative
        candidate_file = candidate / relative
        if not reference_file.is_file():
            failures.append(f"{relative}: the reference file is missing")
        elif _sha256(reference_file.read_bytes()) != entry.get("sha256"):
            failures.append(f"{relative}: the reference manifest does not match the reference file")
        if not candidate_file.is_file():
            failures.append(f"{relative}: the candidate file is missing")
            continue
        compared += 1
        if _sha256(candidate_file.read_bytes()) != entry.get("sha256"):
            failures.append(f"{relative}: the bytes differ from the reference")

    envelopes = [entry for entry in entries
                 if entry["path"].count("/") == 1 and entry["path"].endswith(".json")]
    candidates = [entry for entry in entries
                  if entry["path"].startswith(DIR_NAME + "/" + CANDIDATE_DIR_NAME + "/")]
    expected = plan_counts()
    if len(envelopes) != expected["envelopes"]:
        failures.append(
            f"the manifest declares {len(envelopes)} envelopes, expected {expected['envelopes']}"
        )
    if len(candidates) != expected["candidates"]:
        failures.append(
            f"the manifest declares {len(candidates)} candidates, expected {expected['candidates']}"
        )

    claimed: Dict[str, str] = {}
    for entry in envelopes:
        path = candidate / entry["path"]
        if not path.is_file():
            continue
        try:
            envelope = _read_json(path)
        except json.JSONDecodeError:
            failures.append(f"{entry['path']}: the envelope is not readable JSON")
            continue
        candidate_id = envelope.get("candidate_id")
        relative = envelope.get("initial_candidate_file")
        expected_file = f"{CANDIDATE_DIR_NAME}/{candidate_id}.json"
        if not isinstance(candidate_id, str) or relative != expected_file:
            failures.append(f"{entry['path']}: the candidate mapping is invalid")
            continue
        if not (candidate / DIR_NAME / expected_file).is_file():
            failures.append(f"{entry['path']}: the candidate file {expected_file} is missing")
            continue
        if relative in claimed:
            failures.append(
                f"{entry['path']}: the candidate {relative} is also used by {claimed[relative]}"
            )
        claimed[relative] = entry["path"]
    declared_candidates = {
        entry["path"][len(DIR_NAME) + 1:] for entry in candidates
    }
    if set(claimed) != declared_candidates:
        missing = sorted(declared_candidates - set(claimed))
        unused = sorted(set(claimed) - declared_candidates)
        if missing:
            failures.append("candidates without an envelope: " + ", ".join(missing[:5]))
        if unused:
            failures.append("envelopes that point outside the manifest: " + ", ".join(unused[:5]))

    return {
        "ok": not failures,
        "failures": failures,
        "total": len(entries),
        "compared": compared,
        "envelopes": len(envelopes),
        "candidates": len(candidates),
    }


__all__ = [
    "A_DOC_IDS",
    "ALTERNATE_ROWS",
    "CANDIDATE_DIR_NAME",
    "CASE_BY_ID",
    "DIR_NAME",
    "EXPECTED_BEHAVIOUR",
    "ENVELOPE_KEYS",
    "FAULT_TYPES",
    "INJECTION_PLAN",
    "INJECTION_SEED",
    "InjectError",
    "InjectionCase",
    "OPERATIONS",
    "SCHEMA_VERSION",
    "STRUCTURAL_FAULTS",
    "TARGET_ROWS",
    "canonical_bytes",
    "check_injections",
    "inject_faults",
    "plan_counts",
    "shift_unit_price",
    "transpose_adjacent_digits",
]
