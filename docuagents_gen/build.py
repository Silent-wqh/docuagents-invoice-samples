"""Matrix rows -> upstream ``DocSpec`` objects (P2).

This is the only module that knows how a matrix row becomes a document the
upstream generator can draw.  It keeps every upstream import in one place,
so the matrix and the gold builder stay independent of upstream internals.

Construction rules (from the sample plan and the T07 instructions):

* A-D documents are US invoices: ``Currency: USD`` header, an unambiguous
  long date (F1), and a printed "Prices exclude tax." note.
* Every amount is built with ``Decimal`` and two-decimal rounding
  (``ROUND_HALF_UP``); printed line amounts are the values the matrix wrote,
  never a fresh ``float`` product.
* An explicit zero tax is a ``Tax`` object (F5): ``tax_rate=0`` through the
  factory would be replaced by a random rate.
* C-class documents print a deliberate conflict.  Upstream ``validate()`` is
  not the gate for them; this package proves "exactly one rule fails".
* B-class documents are single-page image renders of a clean base document
  (F9), with a fixed degradation kind, severity and order.

Sub-seeds use SHA-256 of ``(seed, doc_id, operation)``.  Python's per-process
``hash()`` is never used, so a run is reproducible across processes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, Iterable, List, Sequence, Tuple

from generator.layout import build_pages, page_text
from generator.spec import (
    US_NUM,
    Charge,
    Degradation,
    DocSpec,
    Layout,
    Line,
    MoneyStyle,
    Party,
    Render,
    Tax,
)

from . import matrix

SEED = matrix.SEED


class BuildError(RuntimeError):
    """Raised when a matrix row cannot be turned into a valid document."""


# ---------------------------------------------------------------------------
# Deterministic sub-seeds
# ---------------------------------------------------------------------------


def subseed(doc_id: str, operation: str, seed: int = SEED) -> int:
    """A stable 64-bit seed for one (document, operation) pair.

    The value comes from SHA-256 of ``"<seed>:<doc_id>:<operation>"``.  It does
    not depend on the process, the platform or the order of the calls.
    """
    if not doc_id or not operation:
        raise BuildError("subseed needs both a doc_id and an operation name")
    text = f"{seed}:{doc_id}:{operation}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(text).digest()[:8], "big")


# ---------------------------------------------------------------------------
# Parties and money
# ---------------------------------------------------------------------------


def _party(vendor: matrix.Vendor) -> Party:
    return Party(vendor.name, tuple(vendor.address), "US", vendor.phone, vendor.email,
                 vendor.tax_id, vendor.tax_label)


def _customer(customer: matrix.Customer) -> Party:
    return Party(customer.name, tuple(customer.address), "US")


def _money_style(row: matrix.Row) -> MoneyStyle:
    # USD and CAD both print "$"; the ISO code comes from the printed
    # "Currency:" header, never from the symbol (F13).
    return MoneyStyle(row.money_symbol, "prefix", False, "minus", US_NUM)


def _f(value: Decimal) -> float:
    """Decimal -> float without a detour through binary noise."""
    return float(value)


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------


def _layout(row: matrix.LayoutSpec) -> Layout:
    return Layout(
        family=row.family,
        header=row.header,
        table=row.table,
        paper=row.paper,
        rows_per_page=row.rows_per_page,
        logo=row.logo,
        show_currency_header=True,  # "Currency: USD" on every A-D original
    )


def _lines(row: matrix.Row) -> Tuple[Line, ...]:
    out = []
    for item in row.items:
        out.append(
            Line(
                item.description,
                _f(item.amount),
                qty=_f(item.quantity),
                unit_price=_f(item.unit_price),
            )
        )
    return tuple(out)


def tax_base(lines: Sequence[Line], charges: Sequence[Charge]) -> Decimal:
    """The base an invoice applies its rate to: lines - discounts + charges."""
    base = sum((Decimal(str(ln.amount)) for ln in lines), Decimal("0.00"))
    for charge in charges:
        amount = Decimal(str(charge.amount))
        base += -amount if charge.kind == "discount" else amount
    return matrix.round_money(base)


def tax_amount(row: matrix.Row, lines: Sequence[Line], charges: Sequence[Charge]) -> Decimal | None:
    if row.tax_rate is None:
        return None
    return matrix.round_money(tax_base(lines, charges) * row.tax_rate)


def _charges(row: matrix.Row) -> Tuple[Charge, ...]:
    return tuple(Charge(c.label, _f(c.amount), c.kind) for c in row.charges)


def _taxes(row: matrix.Row, lines: Sequence[Line], charges: Sequence[Charge]) -> Tuple[Tax, ...]:
    amount = tax_amount(row, lines, charges)
    if amount is None:
        return ()
    assert row.tax_rate is not None
    return (Tax(row.tax_label, _f(amount), _f(row.tax_rate)),)


def _notes(row: matrix.Row) -> Tuple[str, ...]:
    """Printed notes.  A-D originals state that prices exclude tax."""
    if row.doc_kind == "credit_note":
        return ("This credit note reverses the invoice named above.",)
    return ("Prices exclude tax.",)


def _render(row: matrix.Row) -> Render:
    if row.render_mode == "pdf":
        return Render(mode="pdf")
    return Render(
        mode="image",
        fmt=row.render_fmt,
        degradations=tuple(Degradation(kind, severity) for kind, severity in row.degradations),
    )


def _child_spec(row: matrix.Row, index: int, child: matrix.ChildSpec) -> DocSpec:
    vendor = matrix.VENDORS[child.vendor]
    items = child.items
    lines = tuple(
        Line(item.description, _f(item.amount), qty=_f(item.quantity), unit_price=_f(item.unit_price))
        for item in items
    )
    base = matrix.round_money(sum((item.amount for item in items), Decimal("0.00")))
    tax = matrix.round_money(base * (row.tax_rate or Decimal("0")))
    return DocSpec(
        doc_id=f"{row.doc_id}-child-{index + 1}",
        category="sample",
        difficulty="easy",
        description=f"{row.doc_id}: invoice {child.number} inside a multi-invoice file.",
        vendor=_party(vendor),
        customer=_customer(matrix.CUSTOMERS[row.customer]),
        issue_date=row.issue_date,
        currency=row.currency,
        money_style=_money_style(row),
        date_style="USLONG",
        title=row.title,
        number=child.number,
        number_label=row.number_label,
        due_date=row.issue_date.replace(day=min(28, row.issue_date.day)) + _days(row.due_days),
        terms=f"{row.due_days} days" if row.due_days else None,
        lines=lines,
        taxes=(Tax(row.tax_label, _f(tax), _f(row.tax_rate)),) if row.tax_rate is not None else (),
        notes_text=_notes(row),
        layout=_layout(row.layout),
        render=Render(mode="pdf"),
    )


def _days(days: int | None) -> timedelta:
    return timedelta(days=days or 0)


def build_spec(row: matrix.Row) -> DocSpec:
    """Turn one matrix row into an upstream ``DocSpec``."""
    vendor = matrix.VENDORS[row.vendor]
    customer = matrix.CUSTOMERS[row.customer]
    lines = _lines(row)
    charges = _charges(row)
    taxes = _taxes(row, lines, charges)

    total_printed = None if row.total_printed is None else _f(row.total_printed)
    subtotal_printed = None if row.subtotal_printed is None else _f(row.subtotal_printed)

    fields: Dict[str, object] = dict(
        doc_id=row.doc_id,
        category="sample",
        difficulty=_difficulty(row),
        description=row.target_question,
        vendor=_party(vendor),
        customer=_customer(customer),
        issue_date=row.issue_date,
        currency=row.currency,
        money_style=_money_style(row),
        date_style="USLONG",
        kind=row.doc_kind,
        title=row.title,
        number=row.number,
        number_label=row.number_label,
        due_date=None if row.due_days is None else row.issue_date + _days(row.due_days),
        terms=f"{row.due_days} days" if row.due_days else None,
        po_number=row.po_number,
        po_label=row.po_label,
        lines=lines,
        columns=row.columns,
        charges=charges,
        taxes=taxes,
        subtotal_printed=subtotal_printed,
        total_printed=total_printed,
        notes_text=_notes(row),
        layout=_layout(row.layout),
        render=_render(row),
        tags=(row.klass, row.subclass, row.source_family),
    )

    if row.doc_kind == "multi_invoice":
        if not row.children:
            raise BuildError(f"{row.doc_id}: multi_invoice without children")
        fields["children"] = tuple(_child_spec(row, i, child) for i, child in enumerate(row.children))
        fields["lines"] = ()

    spec = DocSpec(**fields)  # type: ignore[arg-type]
    if row.doc_kind != "multi_invoice" and not spec.lines:
        raise BuildError(f"{row.doc_id}: a document must print at least one line")
    return spec


def _difficulty(row: matrix.Row) -> str:
    if row.klass == "C" or row.doc_kind in ("multi_invoice", "credit_note"):
        return "hard"
    if row.klass in ("D", "E"):
        return "hard"
    if row.klass == "B":
        return {"mild": "easy", "moderate": "medium", "severe": "hard"}[row.degradations[0][1]]
    if row.klass == "A" and row.subclass == "A3":
        return "medium"
    return "easy"


def build_all(rows: Iterable[matrix.Row] | None = None) -> List[Tuple[matrix.Row, DocSpec]]:
    """Every row and its spec, in matrix order."""
    return [(row, build_spec(row)) for row in (matrix.ROWS if rows is None else rows)]


# ---------------------------------------------------------------------------
# A3: bounded candidate enumeration
# ---------------------------------------------------------------------------

#: The A3 definition, unchanged: two printed pages, every line item on the
#: first page, no line-item cell on the second page, and the complete
#: subtotal / tax / total block on the second page.
A3_ROWS_PER_PAGE_RANGE = range(18, 31)
A3_LINE_COUNT_RANGE = range(24, 39)


def _a3_template(rows_per_page: int, line_count: int) -> matrix.Row:
    """The A3 construction with the two parameters under test."""
    base = matrix.BY_ID["da-a3-01"]
    return replace(
        base,
        items=matrix.insert_lines(line_count),
        layout=replace(base.layout, rows_per_page=rows_per_page),
    )


def a3_structure(row: matrix.Row) -> Dict[str, object]:
    """Measure a candidate against the A3 definition, from the layout result."""
    spec = build_spec(row)
    pages = build_pages(spec)
    table_rows = [sum(len(table.rows) for table in page.tables) for page in pages]
    texts = [page_text(page) for page in pages]
    totals_page = None
    for index, text in enumerate(texts, 1):
        if "Subtotal" in text and "TOTAL" in text:
            totals_page = index
            break
    return {
        "rows_per_page": row.layout.rows_per_page,
        "line_count": row.line_count,
        "pages": len(pages),
        "rows_per_page_measured": table_rows,
        "totals_page": totals_page,
        "passed": (
            len(pages) == 2
            and table_rows[0] == row.line_count
            and table_rows[1] == 0
            and totals_page == 2
        ),
    }


def a3_candidates() -> List[Dict[str, object]]:
    """Enumerate A3 parameters in a fixed order: at most 200 candidates.

    Order: ``rows_per_page`` ascending, then ``line_count`` ascending.  The
    list is the evidence for the frozen A3 parameters in ``matrix.py``.
    """
    out: List[Dict[str, object]] = []
    for rows_per_page in A3_ROWS_PER_PAGE_RANGE:
        for line_count in A3_LINE_COUNT_RANGE:
            out.append(a3_structure(_a3_template(rows_per_page, line_count)))
    if len(out) > 200:
        raise BuildError(f"the A3 enumeration must stay within 200 candidates, got {len(out)}")
    return out


# ---------------------------------------------------------------------------
# A stable fingerprint of one spec
# ---------------------------------------------------------------------------


def _jsonable(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in sorted(value.items())}
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    return value


def spec_fingerprint(spec: DocSpec) -> str:
    """SHA-256 of the spec's printed facts, stable across processes.

    Two specs with the same fingerprint print the same document.  The value
    is used to prove that changing one row does not move another row.
    """
    facts = {
        "doc_id": spec.doc_id,
        "kind": spec.kind,
        "title": spec.title,
        "number": spec.number,
        "number_label": spec.number_label,
        "issue_date": spec.issue_date,
        "due_date": spec.due_date,
        "terms": spec.terms,
        "po_number": spec.po_number,
        "po_label": spec.po_label,
        "currency": spec.currency,
        "money_style": spec.money_style,
        "date_style": spec.date_style,
        "vendor": spec.vendor,
        "customer": spec.customer,
        "lines": spec.lines,
        "columns": spec.columns,
        "charges": spec.charges,
        "taxes": spec.taxes,
        "subtotal_printed": spec.subtotal_printed,
        "total_printed": spec.total_printed,
        "notes_text": spec.notes_text,
        "layout": spec.layout,
        "render": spec.render,
        "children": spec.children,
    }
    payload = json.dumps(_jsonable(facts), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def fingerprints() -> Dict[str, str]:
    """Fingerprint of every matrix document, keyed by doc id."""
    return {spec.doc_id: spec_fingerprint(spec) for _, spec in build_all()}


__all__ = [
    "A3_LINE_COUNT_RANGE",
    "A3_ROWS_PER_PAGE_RANGE",
    "BuildError",
    "a3_candidates",
    "a3_structure",
    "build_all",
    "build_spec",
    "fingerprints",
    "spec_fingerprint",
    "subseed",
    "tax_amount",
    "tax_base",
]
