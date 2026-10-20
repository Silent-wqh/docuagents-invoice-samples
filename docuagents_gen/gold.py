"""Gold annotation built from what the page prints (P3).

``build_gold(spec, rendered_pages, file_meta)`` reads the *printed* page --
the layout's text operations, its table cells and its totals rows -- and
returns the gold record.  It never calls ``DocSpec.expected()``: F8 showed
that the upstream key has no quantity or unit price, and reports an unprinted
tax as ``0.0``, so it cannot be the gold.

What the gold records:

* **Printed facts.**  Every amount is the string the page shows, parsed with
  ``Decimal`` and normalized to two decimals.  A deliberate C-class conflict
  keeps its printed value; nothing is "fixed".
* **Missing is not zero.**  A field that is not printed is ``null``, and its
  evidence says ``not_found`` with the pages that were searched.  A printed
  ``0.00`` is ``"0.00"``.
* **Independent arithmetic.**  Rules A (line product), B (subtotal), C
  (total) and D (required fields) are recomputed with ``Decimal`` and
  compared with the matrix declaration.  A disagreement raises
  ``GoldError``; the generator never quietly accepts it.
* **Scope and readability gates.**  The scope reasons are recomputed from
  scope signals read on the page and compared with ``matrix.py``.
* **Provenance.**  ``evidence[field].verification`` and
  ``annotation.source`` use the contract's gold-only namespace
  (``generator_recorded``).  Runtime verification grades are never mixed in.

The file name and SHA-256 are injected through ``file_meta``.  This task does
not render the final files; T14 renders them and passes the real values.  The
tests pass a deterministic placeholder hash.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from generator.layout import Page, Text, page_text, text_width
from generator.spec import DocSpec

from . import codes, matrix

#: Left/right page margin used by the upstream layout engine (points).
MARGIN = 38.0

#: Amount precision this project accepts: two decimals, half up.
CENT = Decimal("0.01")

#: Rule A tolerance.  F2 showed that a printed unit price can be one cent
#: short of the printed amount (the upstream factory rounds the price, then
#: multiplies).  The gold keeps the printed values and accepts a one-cent
#: difference; a larger difference is a real conflict.
LINE_TOLERANCE = CENT

#: Two branches for rule A (T07 defect DEF-01).  When the printed unit price
#: carries more than two decimals, an unrounded source price exists, so a
#: one-cent display difference is explained by rounding.  When it carries two
#: decimals, the price is already the source value and the comparison is
#: strict: any difference is a real conflict.  None of the 30 main samples
#: prints more than two decimals, so every one of them takes the strict branch.
ROUNDING_PRICE_DECIMALS = 2

#: Media types the sample set may produce.
MEDIA_TYPES = ("application/pdf", "image/jpeg", "image/png")

#: Required fields of a supported invoice.  A missing one is MISSING_REQUIRED_FIELD.
REQUIRED_FIELDS = (
    "vendor_name",
    "invoice_number",
    "invoice_date",
    "currency",
    "total_amount",
)


class GoldError(RuntimeError):
    """Raised when the printed pages and the matrix declaration disagree."""


# ---------------------------------------------------------------------------
# Reading a page
# ---------------------------------------------------------------------------


def _rows(page: Page) -> List[List[Text]]:
    """Text operations grouped by baseline, left to right."""
    texts = [op for op in page.ops if isinstance(op, Text) and op.text.strip()]
    texts.sort(key=lambda t: (t.y, t.left))
    rows: List[List[Text]] = []
    for text in texts:
        if rows and abs(rows[-1][0].y - text.y) <= 0.6:
            rows[-1].append(text)
        else:
            rows.append([text])
    return [sorted(row, key=lambda t: t.left) for row in rows]


def _right(text: Text) -> float:
    return text.left + _text_width(text)


def _text_width(text: Text) -> float:
    return text_width(text.text, text.font, text.size)


def _bbox(text: Text) -> List[float]:
    """An approximate box in PDF points, top-left origin, as the layout draws it."""
    size = text.size
    return [
        round(text.left, 2),
        round(text.y - 0.80 * size, 2),
        round(text.left + _text_width(text), 2),
        round(text.y + 0.22 * size, 2),
    ]


#: A meta label ends with a colon and starts with a word: "Invoice #:",
#: "Due Date:".  A sentence ending in a colon is not a meta row.
_META_LABEL = re.compile(r"^[A-Z][A-Za-z #/&-]{0,30}:$")

MONEY_RE = re.compile(
    r"^(?P<sign>[-(]?)\s*(?P<symbol>\$|US\$|€|£|R|N\$|K)?\s*"
    r"(?P<number>\d[\d,]*(?:\.\d{1,3})?)\s*(?P<cr>CR)?\s*\)?$"
)


def parse_printed_money(text: str) -> Optional[Decimal]:
    """Parse a printed amount; ``None`` when the text is not an amount."""
    match = MONEY_RE.match(text.strip().replace(" ", " "))
    if not match:
        return None
    raw = match.group("number").replace(",", "")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if match.group("sign") in ("-", "(") or match.group("cr"):
        value = -value
    return value


def _money_text(value: Decimal) -> str:
    """A gold amount: two decimals, printed sign kept, no symbol."""
    return f"{matrix.round_money(value):.2f}"


def parse_meta(pages: Sequence[Page]) -> Dict[str, Tuple[str, int, Text]]:
    """Label -> (printed value, page number, value text operation).

    A meta row can hold two label/value pairs (``Invoice #: ...  Terms: ...``).
    Every label is read, and its value is the nearest text to its right that is
    not itself a label.  A vendor line such as ``EIN: 31-4820195`` is a single
    text operation, so it is not mistaken for a label.
    """
    out: Dict[str, Tuple[str, int, Text]] = {}
    for pno, page in enumerate(pages, 1):
        for row in _rows(page):
            labels = [
                t for t in row
                if t.text.strip().endswith(":") and len(t.text.strip()) > 1
                and _META_LABEL.match(t.text.strip())
            ]
            for label in labels:
                candidates = [
                    t for t in row
                    if t.left > label.left + 1.0 and t not in labels
                ]
                if not candidates:
                    continue
                value = min(candidates, key=lambda t: t.left)
                name = label.text.strip().rstrip(":").strip()
                out.setdefault(name, (value.text.strip(), pno, value))
    return out


def parse_summary_rows(pages: Sequence[Page]) -> List[Tuple[str, Decimal, int, Text, Text]]:
    """The totals block: (label, amount, page, label op, amount op).

    A totals row is a right-aligned label and a right-aligned amount whose
    right edge is the page's right margin.  A table cell is inset by its
    column width, so it never matches.
    """
    rows: List[Tuple[str, Decimal, int, Text, Text]] = []
    for pno, page in enumerate(pages, 1):
        edge = page.width - MARGIN
        for row in _rows(page):
            amounts = [t for t in row if abs(_right(t) - edge) <= 1.5]
            if len(amounts) != 1:
                continue
            amount_op = amounts[0]
            value = parse_printed_money(amount_op.text)
            if value is None:
                continue
            labels = [t for t in row if t.left < amount_op.left - 1.0]
            if not labels:
                continue
            label_op = max(labels, key=lambda t: t.left)
            rows.append((label_op.text.strip(), value, pno, label_op, amount_op))
    return rows


def _is_tax_label(label: str) -> bool:
    lowered = label.casefold()
    return "tax" in lowered or "vat" in lowered or "gst" in lowered


def _is_total_label(label: str) -> bool:
    lowered = label.casefold().strip()
    return lowered.startswith("total")


def _is_subtotal_label(label: str) -> bool:
    return label.casefold().strip() in ("subtotal", "sub total")


def _is_discount_label(label: str) -> bool:
    return "discount" in label.casefold()


def parse_line_items(pages: Sequence[Page]) -> List[Dict[str, Any]]:
    """Printed line items, in reading order, with the page each row landed on.

    ``quantity`` and ``unit_price`` keep the printed cell string.  The page
    prints ``2.40``; the gold must not shorten it to ``2.4``.  Arithmetic uses
    ``Decimal`` on the same strings.
    """
    items: List[Dict[str, Any]] = []
    for pno, page in enumerate(pages, 1):
        for table in page.tables:
            headers = [h.strip() for h in table.headers]
            lower = [h.casefold() for h in headers]
            if "description" not in lower or "amount" not in lower:
                continue
            desc_i = lower.index("description")
            amount_i = lower.index("amount")
            qty_i = lower.index("qty") if "qty" in lower else None
            unit_i = lower.index("unit price") if "unit price" in lower else None
            for cells in table.rows:
                if len(cells) != len(headers):
                    raise GoldError(f"table row has {len(cells)} cells, expected {len(headers)}")
                amount = parse_printed_money(cells[amount_i])
                if amount is None:
                    raise GoldError(f"line amount is not a printed amount: {cells[amount_i]!r}")
                quantity = cells[qty_i].strip() if qty_i is not None else ""
                unit = cells[unit_i].strip() if unit_i is not None else ""
                if quantity and parse_printed_money(quantity) is None:
                    raise GoldError(f"quantity is not a printed number: {quantity!r}")
                if unit and parse_printed_money(unit) is None:
                    raise GoldError(f"unit price is not a printed amount: {unit!r}")
                items.append(
                    {
                        "index": len(items),
                        "description": cells[desc_i],
                        "quantity": quantity or None,
                        "unit_price": unit or None,
                        "amount": _money_text(amount),
                        "page": pno,
                    }
                )
    return items


# ---------------------------------------------------------------------------
# Scope signals, read from the page
# ---------------------------------------------------------------------------


def _signals(pages: Sequence[Page], meta: Mapping[str, Tuple[str, int, Text]],
             items: Sequence[Mapping[str, Any]],
             summary: Sequence[Tuple[str, Decimal, int, Text, Text]],
             multi_invoice: bool) -> Dict[str, Any]:
    """Scope signals read from the printed page (design §6.2)."""
    texts = [page_text(page) for page in pages]
    currency = meta.get("Currency", (None, 0, None))[0]
    invoice_labels = sum(
        text.count("Invoice #:") + text.count("Invoice No:") + text.count("Invoice Number:")
        for text in texts
    )
    credits = any("CREDIT NOTE" in text or "CREDIT MEMO" in text for text in texts)
    table_pages = sorted({item["page"] for item in items})
    # A multi-invoice file holds one complete table per invoice, one invoice
    # per page: no single invoice's table spans a page break.
    spans = len(table_pages) > 1 and not multi_invoice
    discounts = [row for row in summary if _is_discount_label(row[0])]
    charges = [
        row for row in summary
        if row[1] > 0 and not _is_tax_label(row[0]) and not _is_discount_label(row[0])
        and not _is_subtotal_label(row[0]) and not _is_total_label(row[0])
    ]
    tax_rows = [row for row in summary if _is_tax_label(row[0])]
    tax_note = any("Prices exclude tax." in text for text in texts)
    return {
        "document_type": "credit_note" if credits else "invoice",
        "invoice_count": invoice_labels if invoice_labels else 1,
        "language": "en",
        "currency_evidence": "explicit" if currency else "missing",
        "unit_price_basis": "pre_tax" if (tax_note or tax_rows) else "unknown",
        "discount": "present" if discounts else "absent",
        "additional_charges": "present" if charges else "absent",
        "prepayment_or_prior_balance": "absent",
        "tax_presentation": "single" if tax_rows else "missing",
        "table_layout": "spans_pages" if spans else "single_page",
        "observed_row_count": len(items),
        "table_pages": table_pages,
    }


_SCOPE_ORDER = {reason: index for index, reason in enumerate(codes.SCOPE_REASON)}


def _scope(signals: Mapping[str, Any], pages: Sequence[Page]) -> Tuple[str, List[str]]:
    reasons: List[str] = []
    if signals["document_type"] == "credit_note":
        reasons.append("CREDIT_NOTE")
    if signals["invoice_count"] > 1:
        reasons.append("MULTIPLE_INVOICES")
    if signals["currency_evidence"] == "explicit" and signals.get("currency") not in (None, "USD"):
        reasons.append("NON_USD_CURRENCY")
    if signals["discount"] == "present":
        reasons.append("HAS_DISCOUNT")
    if signals["additional_charges"] == "present":
        reasons.append("HAS_EXTRA_CHARGES")
    if signals["table_layout"] == "spans_pages":
        reasons.append("TABLE_SPANS_PAGES")
    if len(pages) > matrix.LIMITS["max_pages"]:
        reasons.append("TOO_MANY_PAGES")
    if signals["observed_row_count"] > matrix.LIMITS["max_lines"]:
        reasons.append("TOO_MANY_LINES")
    reasons.sort(key=lambda name: _SCOPE_ORDER[name])
    return ("UNSUPPORTED" if reasons else "SUPPORTED"), reasons


# ---------------------------------------------------------------------------
# Rule A: the two-branch price policy (T07 defect DEF-01)
# ---------------------------------------------------------------------------


def printed_price_precision(text: Optional[str]) -> int:
    """The number of printed decimals of a unit price cell.

    ``"2.40"`` -> 2, ``"0.334"`` -> 3, ``"5"`` -> 0.  A value that cannot be
    read as a decimal keeps the two-decimal rule, so a broken cell is never
    silently promoted to the lenient branch.
    """
    if text is None:
        raise GoldError("a unit price cell is missing")
    cleaned = str(text).strip().replace(",", "").replace("$", "")
    if "." not in cleaned:
        return 0
    return len(cleaned.split(".", 1)[1])


def evaluate_line_amount(
    quantity: Optional[str],
    unit_price: Optional[str],
    printed_amount: Decimal,
    *,
    credit: bool = False,
) -> Dict[str, Any]:
    """Decide rule A for one printed line, with the price-precision branch.

    Args:
        quantity: the printed quantity cell, or ``None``.
        unit_price: the printed unit price cell, or ``None``.
        printed_amount: the positive printed line amount.
        credit: the document is a credit note; magnitudes are compared.

    Returns:
        ``{"branch", "status", "delta", "unit_price_precision", "reason"}``.

    The rounding branch accepts a difference of at most one cent and states
    the printed unit price it relied on.  The strict branch accepts only an
    exact match, because a two-decimal price is the source value.
    """
    if quantity is None or unit_price is None:
        return {
            "branch": None,
            "status": "NOT_APPLICABLE",
            "delta": None,
            "unit_price_precision": None,
            "reason": None,
        }
    qty = Decimal(str(quantity).strip().replace(",", ""))
    unit = Decimal(str(unit_price).strip().replace("$", "").replace(",", ""))
    precision = printed_price_precision(str(unit_price))
    recomputed = matrix.round_money(qty * unit)
    # A credit note prints its amounts negative; rule A compares magnitudes,
    # so the recomputed amount takes the document's sign first.
    signed = -recomputed if credit else recomputed
    delta = matrix.round_money(signed - printed_amount)
    if precision > ROUNDING_PRICE_DECIMALS:
        ok = abs(delta) <= LINE_TOLERANCE
        reason = (
            f"the printed unit price {unit_price} carries {precision} decimals, so an "
            f"unrounded source price can explain the {delta} display difference "
            f"(rounding branch)"
            if ok and delta != 0
            else None
        )
        return {
            "branch": "rounding",
            "status": "PASS" if ok else "FAIL",
            "delta": _money_text(delta),
            "unit_price_precision": precision,
            "reason": reason,
        }
    return {
        "branch": "strict",
        "status": "PASS" if delta == 0 else "FAIL",
        "delta": _money_text(delta),
        "unit_price_precision": precision,
        "reason": None,
    }


# ---------------------------------------------------------------------------
# Arithmetic
# ---------------------------------------------------------------------------


def _evaluate(
    lines: Sequence[Mapping[str, Any]],
    summary: Sequence[Tuple[str, Decimal, int, Text, Text]],
    invoice_number: Optional[str],
    invoice_date: Optional[str],
    vendor_name: Optional[str],
    currency: Optional[str],
    doc_kind: str,
) -> Tuple[Dict[str, Any], Dict[str, str], List[str]]:
    """Recompute rules A-D and return (arithmetic, checks, issue codes)."""
    credit = doc_kind == "credit_note"
    subtotal = next((row for row in summary if _is_subtotal_label(row[0])), None)
    total = next((row for row in summary if _is_total_label(row[0])), None)
    tax_rows = [row for row in summary if _is_tax_label(row[0])]
    charges = [
        row for row in summary
        if not _is_subtotal_label(row[0]) and not _is_total_label(row[0]) and not _is_tax_label(row[0])
    ]

    line_checks: List[Dict[str, Any]] = []
    line_failures = 0
    rounding_branch = 0
    for item in lines:
        qty = None if item["quantity"] is None else Decimal(item["quantity"])
        unit = None if item["unit_price"] is None else Decimal(item["unit_price"])
        printed = Decimal(item["amount"])
        if qty is None or unit is None:
            recomputed = None
            delta = None
            status = "NOT_APPLICABLE"
            branch = None
        else:
            recomputed = matrix.round_money(qty * unit)
            signed = -recomputed if credit else recomputed
            delta = matrix.round_money(signed - printed)
            decision = evaluate_line_amount(item["quantity"], item["unit_price"], printed, credit=credit)
            status = decision["status"]
            branch = decision["branch"]
            if branch == "rounding":
                rounding_branch += 1
        if status == "FAIL":
            line_failures += 1
        line_checks.append(
            {
                "index": item["index"],
                "recomputed_amount": None if recomputed is None else _money_text(recomputed),
                "printed_amount": _money_text(printed),
                "delta": None if delta is None else _money_text(delta),
                "status": status,
            }
        )

    line_sum = matrix.round_money(sum((Decimal(item["amount"]) for item in lines), Decimal("0.00")))
    printed_subtotal = None if subtotal is None else subtotal[1]
    printed_total = None if total is None else total[1]
    tax_printed = matrix.round_money(sum((row[1] for row in tax_rows), Decimal("0.00"))) if tax_rows else None
    charge_sum = matrix.round_money(sum((row[1] for row in charges), Decimal("0.00")))
    recomputed_total = None
    if printed_subtotal is not None:
        recomputed_total = matrix.round_money(printed_subtotal + (tax_printed or Decimal("0.00")) + charge_sum)

    checks: Dict[str, str] = {}
    issues: List[str] = []
    checks["A_line_amount"] = "FAIL" if line_failures else "PASS"
    if line_failures:
        issues.append("LINE_AMOUNT_MISMATCH")

    if printed_subtotal is None:
        checks["B_subtotal"] = "NOT_APPLICABLE"
    else:
        subtotal_delta = matrix.round_money(abs(line_sum) - abs(printed_subtotal)) if credit else matrix.round_money(line_sum - printed_subtotal)
        checks["B_subtotal"] = "PASS" if subtotal_delta == 0 else "FAIL"
        if subtotal_delta != 0:
            issues.append("SUBTOTAL_MISMATCH")

    if printed_total is None or recomputed_total is None:
        checks["C_total"] = "NOT_APPLICABLE"
    else:
        total_delta = matrix.round_money(recomputed_total - printed_total)
        checks["C_total"] = "PASS" if total_delta == 0 else "FAIL"
        if total_delta != 0:
            issues.append("TOTAL_MISMATCH")

    missing = []
    for name in REQUIRED_FIELDS:
        value = {
            "vendor_name": vendor_name,
            "invoice_number": invoice_number,
            "invoice_date": invoice_date,
            "currency": currency,
            "total_amount": None if printed_total is None else "printed",
        }[name]
        if not value:
            missing.append(name)
    checks["D_required_fields"] = "FAIL" if missing else "PASS"
    if missing:
        issues.append("MISSING_REQUIRED_FIELD")

    order = {code: index for index, code in enumerate(codes.ISSUE)}
    issues.sort(key=lambda name: order[name])

    arithmetic = {
        "lines": line_checks,
        "line_sum": _money_text(line_sum),
        "printed_subtotal": None if printed_subtotal is None else _money_text(printed_subtotal),
        "recomputed_total": None if recomputed_total is None else _money_text(recomputed_total),
        "printed_total": None if printed_total is None else _money_text(printed_total),
        "tax_printed": None if tax_printed is None else _money_text(tax_printed),
        "charges_printed": _money_text(charge_sum),
        "delta_subtotal": None if printed_subtotal is None else _money_text(line_sum - printed_subtotal),
        "delta_total": None if (printed_total is None or recomputed_total is None)
        else _money_text(recomputed_total - printed_total),
    }
    return arithmetic, checks, issues, rounding_branch


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


def _evidence(
    status: str,
    page: Optional[int],
    text: Optional[str],
    searched: Sequence[int],
    op: Optional[Text],
    rendered_as_image: bool,
) -> Dict[str, Any]:
    if status not in codes.EVIDENCE_STATUS:
        raise GoldError(f"{status!r} is not a contract evidence_status")
    entry: Dict[str, Any] = {
        "status": status,
        "page": page,
        "text": text,
        "searched_pages": sorted(set(searched)),
        "verification": "generator_recorded",
    }
    if status == "not_found":
        if page is not None or text is not None:
            raise GoldError("a not_found evidence entry must not carry a page or text")
        if not entry["searched_pages"]:
            raise GoldError("a not_found evidence entry must record the pages it searched")
    if status == "observed":
        if page is None:
            raise GoldError("an observed evidence entry must name its page")
        if op is not None:
            if rendered_as_image:
                entry["bbox_precision"] = "unavailable"
            else:
                entry["bbox_pt"] = _bbox(op)
                entry["bbox_precision"] = "approximate"
    return entry


# ---------------------------------------------------------------------------
# Gold
# ---------------------------------------------------------------------------


def _check_file_meta(file_meta: Mapping[str, Any], pages: Sequence[Page]) -> Dict[str, Any]:
    required = ("file_name", "sha256", "media_type")
    for key in required:
        if key not in file_meta or not file_meta[key]:
            raise GoldError(f"file_meta is missing {key!r}")
    sha = str(file_meta["sha256"])
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise GoldError(f"file_meta sha256 is not a lowercase hex digest: {sha!r}")
    media = str(file_meta["media_type"])
    if media not in MEDIA_TYPES:
        raise GoldError(f"file_meta media_type {media!r} is not one of {MEDIA_TYPES}")
    page_count = file_meta.get("page_count", len(pages))
    if int(page_count) != len(pages):
        raise GoldError(
            f"file_meta page_count {page_count} does not match the rendered pages {len(pages)}"
        )
    return {
        "file_name": str(file_meta["file_name"]),
        "sha256": sha,
        "media_type": media,
        "page_count": int(page_count),
    }


_LONG_DATE = re.compile(r"^(?P<month>[A-Z][a-z]+)\s+(?P<day>\d{1,2}),\s*(?P<year>\d{4})$")


def _parse_date(text: Optional[str]) -> Optional[str]:
    """An unambiguous long date ("June 10, 2026") as an ISO date.

    The matrix forces ``date_style="USLONG"`` (F1).  A numeric or short date
    would be ambiguous, so it raises instead of being guessed.
    """
    if not text:
        return None
    match = _LONG_DATE.match(text.strip())
    if not match:
        raise GoldError(f"the printed date {text!r} is not an unambiguous long date")
    normalized = f"{match.group('month')} {int(match.group('day')):02d}, {match.group('year')}"
    try:
        return datetime.strptime(normalized, "%B %d, %Y").date().isoformat()
    except ValueError as exc:
        raise GoldError(f"the printed date {text!r} is not a real calendar date") from exc


def _declaration(spec: DocSpec) -> Optional[matrix.Row]:
    return matrix.BY_ID.get(spec.doc_id)


def build_gold(spec: DocSpec, rendered_pages: Sequence[Page], file_meta: Mapping[str, Any]) -> Dict[str, Any]:
    """Build the gold record for one document.

    Args:
        spec: the upstream ``DocSpec`` the pages were built from.
        rendered_pages: the result of ``build_pages(spec)`` -- the printed page.
        file_meta: ``file_name``, ``sha256``, ``media_type`` and optionally
            ``page_count``.  T07 does not render files; T14 supplies the real
            values.

    Raises:
        GoldError: when the printed page and the matrix declaration disagree,
            or when ``file_meta`` is not usable.
    """
    if not rendered_pages:
        raise GoldError("build_gold needs at least one rendered page")
    file_info = _check_file_meta(file_meta, rendered_pages)

    meta = parse_meta(rendered_pages)
    summary = parse_summary_rows(rendered_pages)
    items = parse_line_items(rendered_pages)

    is_image = spec.render.mode == "image"
    search_pages = list(range(1, len(rendered_pages) + 1))

    def meta_value(*names: str) -> Tuple[Optional[str], Optional[int], Optional[Text]]:
        for name in names:
            found = meta.get(name)
            if found is not None:
                return found[0], found[1], found[2]
        return None, None, None

    vendor_name = spec.vendor.name  # printed in the header by the layout engine
    customer_name = spec.customer.name
    currency, currency_page, currency_op = meta_value("Currency")
    number, number_page, number_op = meta_value("Invoice #", "Invoice No", "Invoice Number",
                                                "Document No", "Credit Note No", "Credit Memo #")
    issue_text, date_page, date_op = meta_value("Date", "Credit Date", "Invoice Date")
    due_text, due_page, due_op = meta_value("Due Date")
    po_text, po_page, po_op = meta_value("Order No", "PO Number", "PO #", "Your Ref",
                                         "Customer Order No")

    signals = _signals(rendered_pages, meta, items, summary, spec.kind == "multi_invoice")
    signals["currency"] = currency
    scope_status, scope_reasons = _scope(signals, rendered_pages)

    document_type = "credit_note" if spec.kind == "credit_note" else (
        "other" if spec.kind == "multi_invoice" else "invoice"
    )

    invoice: Optional[Dict[str, Any]]
    rounding_branch = 0
    if spec.kind == "multi_invoice":
        invoice = None
        arithmetic = {
            "lines": [],
            "line_sum": None,
            "printed_subtotal": None,
            "recomputed_total": None,
            "printed_total": None,
            "tax_printed": None,
            "charges_printed": None,
            "delta_subtotal": None,
            "delta_total": None,
        }
        checks = {
            "A_line_amount": "NOT_APPLICABLE",
            "B_subtotal": "NOT_APPLICABLE",
            "C_total": "NOT_APPLICABLE",
            "D_required_fields": "NOT_APPLICABLE",
        }
        issues: List[str] = []
    else:
        subtotal_row = next((row for row in summary if _is_subtotal_label(row[0])), None)
        total_row = next((row for row in summary if _is_total_label(row[0])), None)
        tax_rows = [row for row in summary if _is_tax_label(row[0])]
        charge_rows = [
            row for row in summary
            if not _is_subtotal_label(row[0]) and not _is_total_label(row[0]) and not _is_tax_label(row[0])
        ]
        arithmetic, checks, issues, rounding_branch = _evaluate(
            items, summary, number, _parse_date(issue_text), vendor_name, currency, spec.kind
        )
        tax_amount = (
            None if not tax_rows
            else _money_text(matrix.round_money(sum((row[1] for row in tax_rows), Decimal("0.00"))))
        )
        invoice = {
            "invoice_number": number,
            "invoice_date": _parse_date(issue_text),
            "due_date": _parse_date(due_text),
            "vendor_name": vendor_name,
            "customer_name": customer_name,
            "currency": currency,
            "document_type": document_type,
            "subtotal_amount": None if subtotal_row is None else _money_text(subtotal_row[1]),
            "tax_amount": tax_amount,
            "total_amount": None if total_row is None else _money_text(total_row[1]),
            "purchase_order_number": po_text,
            "line_items": [
                {
                    "index": item["index"],
                    "description": item["description"],
                    "quantity": item["quantity"],
                    "unit_price": item["unit_price"],
                    "amount": item["amount"],
                }
                for item in items
            ],
            "charges": [
                {
                    "label": row[0],
                    "amount": _money_text(row[1]),
                    "kind": "discount" if row[1] < 0 else "charge",
                }
                for row in charge_rows
            ],
        }

    # -- evidence -----------------------------------------------------------

    evidence: Dict[str, Dict[str, Any]] = {}

    def find_text(needle: str) -> Tuple[Optional[int], Optional[Text]]:
        for pno, page in enumerate(rendered_pages, 1):
            for row in _rows(page):
                for text in row:
                    if text.text.strip() == needle:
                        return pno, text
        return None, None

    vendor_page, vendor_op = find_text(vendor_name)
    if vendor_op is None:
        # Every document in the matrix prints its vendor in the header.  A
        # missing vendor is a construction error, not an acceptable absence.
        raise GoldError(f"{spec.doc_id}: the vendor name {vendor_name!r} is not printed")
    evidence["vendor_name"] = _evidence("observed", vendor_page, vendor_name,
                                        search_pages, vendor_op, is_image)

    customer_page, customer_op = find_text(customer_name)
    if customer_op is None:
        evidence["customer_name"] = _evidence("not_found", None, None, search_pages, None, is_image)
    else:
        evidence["customer_name"] = _evidence("observed", customer_page, customer_name,
                                              search_pages, customer_op, is_image)
    if currency:
        evidence["currency"] = _evidence("observed", currency_page, currency, search_pages, currency_op, is_image)
    else:
        evidence["currency"] = _evidence("not_found", None, None, search_pages, None, is_image)

    if invoice is None:
        # A multi-invoice file has no single invoice-level value.  The labels
        # on the page belong to the child invoices, so none of them is the
        # value of *this* document.  Record absence, not a guessed value.
        for field in ("invoice_number", "invoice_date", "due_date",
                      "purchase_order_number", "subtotal_amount", "tax_amount",
                      "total_amount"):
            evidence[field] = _evidence("not_found", None, None, search_pages, None, is_image)
    else:
        if number is not None:
            evidence["invoice_number"] = _evidence("observed", number_page, number,
                                                   search_pages, number_op, is_image)
        else:
            evidence["invoice_number"] = _evidence("not_found", None, None,
                                                   search_pages, None, is_image)
        if invoice["invoice_date"] is not None:
            evidence["invoice_date"] = _evidence("observed", date_page, issue_text,
                                                 search_pages, date_op, is_image)
        else:
            evidence["invoice_date"] = _evidence("not_found", None, None,
                                                 search_pages, None, is_image)
        if due_text and invoice["due_date"] is not None:
            evidence["due_date"] = _evidence("observed", due_page, due_text,
                                             search_pages, due_op, is_image)
        else:
            evidence["due_date"] = _evidence("not_found", None, None,
                                             search_pages, None, is_image)
        if po_text:
            evidence["purchase_order_number"] = _evidence("observed", po_page, po_text,
                                                          search_pages, po_op, is_image)
        else:
            evidence["purchase_order_number"] = _evidence("not_found", None, None,
                                                          search_pages, None, is_image)

        for field in ("subtotal_amount", "total_amount"):
            row = next(
                (row for row in summary
                 if (_is_subtotal_label(row[0]) if field == "subtotal_amount" else _is_total_label(row[0]))),
                None,
            )
            if row is None or invoice[field] is None:
                evidence[field] = _evidence("not_found", None, None, search_pages, None, is_image)
            else:
                evidence[field] = _evidence("observed", row[2], row[4].text.strip(),
                                            [row[2]], row[4], is_image)
        if invoice["tax_amount"] is None:
            evidence["tax_amount"] = _evidence("not_found", None, None, search_pages, None, is_image)
        else:
            tax_row = next(row for row in summary if _is_tax_label(row[0]))
            evidence["tax_amount"] = _evidence("observed", tax_row[2], tax_row[4].text.strip(),
                                               [tax_row[2]], tax_row[4], is_image)
        for item in items:
            evidence[f"line_items[{item['index']}].amount"] = _evidence(
                "observed", item["page"], item["amount"], [item["page"]], None, is_image
            )
            for part in ("quantity", "unit_price"):
                if item[part] is not None:
                    evidence[f"line_items[{item['index']}].{part}"] = _evidence(
                        "observed", item["page"], item[part], [item["page"]], None, is_image
                    )

    signal_evidence = _signal_evidence(signals, rendered_pages, meta, summary, is_image)
    evidence.update(signal_evidence)

    # -- gates --------------------------------------------------------------

    row = _declaration(spec)
    if row is not None:
        if scope_status != row.scope_status:
            raise GoldError(
                f"{spec.doc_id}: scope_status {scope_status} on the page, {row.scope_status} in the matrix"
            )
        if tuple(scope_reasons) != tuple(row.scope_reasons):
            raise GoldError(
                f"{spec.doc_id}: scope reasons {scope_reasons} on the page, {list(row.scope_reasons)} in the matrix"
            )
        if tuple(issues) != tuple(row.expected_issues):
            raise GoldError(
                f"{spec.doc_id}: issue codes {issues} from arithmetic, {list(row.expected_issues)} in the matrix"
            )

    upstream_validate = _upstream_validate_policy(spec.kind, row)
    acceptance_policy = {
        "rules": checks,
        "tax_rule": "NOT_APPLICABLE" if (invoice is None or invoice["tax_amount"] is None) else "APPLICABLE",
        "upstream_validate": upstream_validate,
        "decisions": issues,
        "notes": _policy_note(spec.kind, checks, invoice),
    }
    if rounding_branch:
        acceptance_policy["notes"] = (
            f"{rounding_branch} line(s) use the rounding branch: the printed unit price "
            f"carries more than {ROUNDING_PRICE_DECIMALS} decimals. "
            + acceptance_policy["notes"]
        )

    labels = row.label() if row is not None else {
        "split": "unassigned", "class": "?", "subclass": "?",
        "target_question": spec.description, "source_family": "unassigned", "legibility": "clean_text",
    }
    readability_gate = row.legibility if row is not None else (
        "clean_text" if not is_image else "image_readable"
    )

    tax_note = next((text for text in page_text(rendered_pages[-1]).splitlines()
                     if text.strip() == "Prices exclude tax."), None)

    gold: Dict[str, Any] = {
        "schema_version": 2,
        "contract_version": codes.CONTRACT_VERSION,
        "doc_id": spec.doc_id,
        "split": labels["split"],
        "class": labels["class"],
        "subclass": labels["subclass"],
        "target_question": labels["target_question"],
        "source_family": labels["source_family"],
        "file": file_info,
        "printing": {
            "currency_header": None if not currency else f"Currency: {currency}",
            "date_form": spec.date_style,
            "tax_note": tax_note,
            "currency_symbol": spec.money_style.symbol,
        },
        "invoice": invoice,
        "scope": {
            "status": scope_status,
            "reasons": scope_reasons,
            "signals": {key: value for key, value in signals.items() if key != "currency"},
        },
        "readability": {
            "gate": readability_gate,
            "verified": False,
            "note": "Final legibility freeze is a T17 annotation check; this task prefills the gate.",
        },
        "expected_issues": issues,
        "acceptance_policy": acceptance_policy,
        "arithmetic": arithmetic,
        "evidence": evidence,
        "annotation": {
            "source": "generator_recorded",
            "verification": "generator_recorded",
            "generated_by": "docuagents_gen/gold.py (T07)",
            "seed": matrix.SEED,
        },
    }
    return gold


def _signal_evidence(
    signals: Mapping[str, Any],
    pages: Sequence[Page],
    meta: Mapping[str, Tuple[str, int, Text]],
    summary: Sequence[Tuple[str, Decimal, int, Text, Text]],
    is_image: bool,
) -> Dict[str, Dict[str, Any]]:
    """Evidence for ``scope_signals.<name>``, with the page that shows it.

    A signal that is absent from the page is ``not_found`` with the pages that
    were searched.  Absence is never proved from the country, the base id or
    a generation parameter -- only from the printed page.
    """
    searched_all = list(range(1, len(pages) + 1))
    out: Dict[str, Dict[str, Any]] = {}

    def add(name: str, status: str, page: Optional[int], text: Optional[str],
            searched: Sequence[int], op: Optional[Text] = None) -> None:
        out[f"scope_signals.{name}"] = _evidence(status, page, text, searched, op, is_image)

    rows_all = [(pno, row) for pno, page in enumerate(pages, 1) for row in _rows(page)]

    def find(predicate) -> Tuple[Optional[int], Optional[Text]]:
        for pno, row in rows_all:
            for text in row:
                if predicate(text):
                    return pno, text
        return None, None

    title_page, title = find(lambda t: t.text.strip() in ("INVOICE", "CREDIT NOTE", "INVOICES"))
    if title is None:
        raise GoldError("the page does not print a document title")
    add("document_type", "observed", title_page, title.text.strip(), [title_page], title)

    count = signals["invoice_count"]
    add("invoice_count", "observed", title_page, f"Invoice labels: {count}", [title_page])

    add("language", "observed", title_page, title.text.strip(), [title_page], title)

    currency = meta.get("Currency")
    if currency is not None:
        add("currency_evidence", "observed", currency[1], f"Currency: {currency[0]}",
            [currency[1]], currency[2])
    else:
        add("currency_evidence", "not_found", None, None, searched_all)

    note_page, note_op = find(lambda t: t.text.strip() == "Prices exclude tax.")
    tax_row = next((row for row in summary if _is_tax_label(row[0])), None)
    if note_op is not None:
        add("unit_price_basis", "observed", note_page, note_op.text.strip(), [note_page], note_op)
    elif tax_row is not None:
        add("unit_price_basis", "observed", tax_row[2], tax_row[0], [tax_row[2]], tax_row[3])
    else:
        add("unit_price_basis", "not_found", None, None, searched_all)

    discount = next((row for row in summary if _is_discount_label(row[0])), None)
    if discount is not None:
        add("discount", "observed", discount[2], discount[0], [discount[2]], discount[3])
    else:
        add("discount", "not_found", None, None, searched_all)

    charge = next(
        (row for row in summary
         if row[1] > 0 and not _is_tax_label(row[0]) and not _is_discount_label(row[0])
         and not _is_subtotal_label(row[0]) and not _is_total_label(row[0])),
        None,
    )
    if charge is not None:
        add("additional_charges", "observed", charge[2], charge[0], [charge[2]], charge[3])
    else:
        add("additional_charges", "not_found", None, None, searched_all)

    # No document in the matrix prints a prepayment or a prior-balance row.
    add("prepayment_or_prior_balance", "not_found", None, None, searched_all)

    if tax_row is not None:
        add("tax_presentation", "observed", tax_row[2], tax_row[0], [tax_row[2]], tax_row[3])
    else:
        add("tax_presentation", "not_found", None, None, searched_all)

    table_pages = signals["table_pages"]
    if table_pages:
        add("table_layout", "observed", table_pages[0], f"Rows on pages {table_pages}", table_pages)
        add("observed_row_count", "observed", table_pages[0],
            f"Rows observed: {signals['observed_row_count']}", table_pages)
        add("table_pages", "observed", table_pages[0],
            f"Pages with table rows: {table_pages}", table_pages)
    else:
        add("table_layout", "not_found", None, None, searched_all)
        add("observed_row_count", "observed", 1, "Rows observed: 0", [1])
        add("table_pages", "observed", 1, "Pages with table rows: []", [1])
    return out


def _upstream_validate_policy(kind: str, row: Optional[matrix.Row]) -> str:
    if kind == "multi_invoice":
        return "NOT_APPLICABLE"
    if row is not None and row.klass == "C":
        return "SKIPPED_DELIBERATE_CONFLICT"
    if row is not None and row.klass == "E":
        return "OPTIONAL"
    return "APPLICABLE"


def _policy_note(kind: str, checks: Mapping[str, str], invoice: Optional[Mapping[str, Any]]) -> str:
    if kind == "multi_invoice":
        return "Several invoices share one file; per-invoice arithmetic is not scored."
    if kind == "credit_note":
        return "Amounts keep their printed negative sign; rule A compares magnitudes."
    if invoice is not None and invoice["tax_amount"] is None:
        return "No tax row is printed: the tax rule is not applicable, and tax_amount is null, not zero."
    failing = [name for name, value in checks.items() if value == "FAIL"]
    if failing:
        return "Deliberate conflict for: " + ", ".join(sorted(failing)) + "."
    return "All applicable rules pass on the printed values."


# ---------------------------------------------------------------------------
# Convenience: build the gold for one matrix row
# ---------------------------------------------------------------------------


def placeholder_file_meta(spec: DocSpec, page_count: int) -> Dict[str, Any]:
    """A deterministic placeholder for the T14 render step.

    The SHA-256 is the digest of ``"placeholder:<doc_id>"``.  It is not the
    digest of a real file, and T14 must replace it with the rendered bytes.
    """
    media = "application/pdf" if spec.render.mode == "pdf" else (
        "image/png" if spec.render.fmt == "png" else "image/jpeg"
    )
    ext = "pdf" if spec.render.mode == "pdf" else spec.render.fmt
    return {
        "file_name": f"documents/{spec.doc_id}.{ext}",
        "sha256": hashlib.sha256(f"placeholder:{spec.doc_id}".encode("utf-8")).hexdigest(),
        "media_type": media,
        "page_count": page_count,
    }


def build_gold_for_row(row: matrix.Row) -> Dict[str, Any]:
    """Build and return the gold for one matrix row, with placeholder file_meta."""
    from generator.layout import build_pages

    from .build import build_spec

    spec = build_spec(row)
    pages = build_pages(spec)
    return build_gold(spec, pages, placeholder_file_meta(spec, len(pages)))


__all__ = [
    "CENT",
    "GoldError",
    "LINE_TOLERANCE",
    "MEDIA_TYPES",
    "REQUIRED_FIELDS",
    "ROUNDING_PRICE_DECIMALS",
    "build_gold",
    "build_gold_for_row",
    "evaluate_line_amount",
    "parse_line_items",
    "parse_meta",
    "parse_printed_money",
    "parse_summary_rows",
    "placeholder_file_meta",
    "printed_price_precision",
]
