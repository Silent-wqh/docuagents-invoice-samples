"""Boundary and technical fixtures for the sample set (T15, sample plan §6.4).

The fixtures are **separate from the 30 main samples**.  They live in
``datasets/docuagents_v1/boundary_fixtures/`` with their own manifest, their
own seed (2027) and their own party catalogue.  They are counted separately
and are never folded into the main counts.

What this module owns:

* :data:`FIXTURES` -- one row per §6.4 item, with the coverage level, the
  expected routing, the expected issue codes and the expected technical error.
  This table is the single source of truth for the fixture set, the way
  ``matrix.py`` is for the main set.
* :func:`generate_boundary_fixtures` -- writes the whole fixture tree.
* :func:`check_fixtures` -- compares a generated fixture tree with a reference
  manifest.  :func:`docuagents_gen.generate.check_dataset` calls it for the
  ``boundary_fixtures/`` subtree.

Rules this module keeps:

* **Isolation.**  The fixture vendors, customers and source families are new
  entities.  None of them appears in the main set, in either split (sample
  plan §6.3, §9).
* **Honest gold.**  Every fixture gold is built from the printed page with the
  shared helpers in ``gold.py``.  A field the page does not print is ``null``
  with ``not_found`` evidence.  A field the page deliberately makes
  unreadable is ``null`` with ``unreadable`` evidence, and the constructed
  true value never reaches the gold, a preview or a report.
* **No relaxation.**  The scope status, the scope reasons and the issue list
  are derived from the printed page, then checked against the §6.4 table.  A
  disagreement stops the run; the module never lowers a definition to make a
  fixture exist.
* **Determinism.**  No randomness.  The seed is fixed at 2027, the encrypted
  PDF uses a fixed password, and the file order never changes a byte.

Limits.  The main set measures a document against ``matrix.LIMITS`` (2 pages,
80 lines), because the main matrix keeps the 60-line construction in scope.
The fixtures must show the *preparation* limits of design §12.5 instead: a
document with more than 2 pages, or more than 20 line items, is out of scope.
:data:`FIXTURE_LIMITS` holds those two numbers.  A fixture is never out of
scope for a reason the §6.4 table does not name.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from generator.layout import PDF_FONTS, Box, Page, Rule, Text, build_pages, page_text
from generator.render import render_images, render_pdf
from generator.spec import US_NUM, Charge, DocSpec, Layout, Line, MoneyStyle, Party, Render, Tax

from . import codes, gold, matrix

#: The fixture seed.  It is deliberately different from ``matrix.SEED``.
FIXTURE_SEED = 2027

#: The fixed password of the encrypted technical fixture.  A fixed password
#: keeps the bytes reproducible; it is not a secret.
FIXTURE_PASSWORD = "docuagents-t15-fixture"

#: Design §12.5 preparation limits, applied to the fixtures.
FIXTURE_LIMITS: Mapping[str, int] = {"max_pages": 2, "max_lines": 20}

#: File-level fixtures: 18 content + 4 technical (sample plan §6.4).
CONTENT_COUNT = 18
TECHNICAL_COUNT = 4
FILE_COUNT = CONTENT_COUNT + TECHNICAL_COUNT

#: Fields the unreadable fixture deliberately hides.  The gold sets them to
#: null and records ``unreadable`` evidence; the constructed value stays out.
UNREADABLE_FIELDS: Tuple[str, ...] = ("subtotal_amount", "tax_amount", "total_amount")

_ROOT = Path(__file__).resolve().parents[1]


class FixtureError(RuntimeError):
    """Raised when a fixture cannot be built as §6.4 specifies."""


# ---------------------------------------------------------------------------
# Fixture parties.  New entities only: no key and no name is shared with the
# main catalogue in ``matrix.py`` (D4, sample plan §6.3).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FixtureVendor:
    key: str
    name: str
    address: Tuple[str, ...]
    phone: str
    email: str
    tax_id: str
    tax_label: str = "EIN"


FIXTURE_VENDORS: Tuple[FixtureVendor, ...] = (
    FixtureVendor("quayside", "Quayside Fastener Works LLC",
                  ("1170 Pier Road", "Portland, ME 04101"),
                  "(207) 555-0131", "accounts@quaysidefastener.example", "12-7740221"),
    FixtureVendor("granite", "Granite Harbor Supply Co",
                  ("48 Breakwater Avenue", "Portsmouth, NH 03801"),
                  "(603) 555-0184", "billing@graniteharbor.example", "02-5591834"),
    FixtureVendor("lumen", "Lumen Carton Systems Inc",
                  ("725 Beacon Street", "Providence, RI 02903"),
                  "(401) 555-0119", "invoices@lumencarton.example", "05-3384117"),
    FixtureVendor("orchard", "Orchard Ridge Paper Ltd",
                  ("90 Mill Lane", "Burlington, VT 05401"),
                  "(802) 555-0166", "ar@orchardridgepaper.example", "03-6612905"),
)


@dataclass(frozen=True)
class FixtureCustomer:
    key: str
    name: str
    address: Tuple[str, ...]


FIXTURE_CUSTOMERS: Tuple[FixtureCustomer, ...] = (
    FixtureCustomer("beacon", "Beacon Point Assembly Inc",
                    ("310 Foundry Road", "Worcester, MA 01608")),
    FixtureCustomer("fieldstone", "Fieldstone Marine LLC",
                    ("27 Wharf Street", "New Bedford, MA 02740")),
)


#: Source-family labels of the fixture set.  None of them is a main-set label.
FIXTURE_SOURCE_FAMILIES: Tuple[str, ...] = (
    "fixture_scope",
    "fixture_language",
    "fixture_tax_basis",
    "fixture_limits",
    "fixture_medium",
    "fixture_quality",
    "fixture_technical",
)


# ---------------------------------------------------------------------------
# The §6.4 fixture table
# ---------------------------------------------------------------------------


def _rows() -> Tuple[Dict[str, Any], ...]:
    """One dict per fixture, in §6.4 order: 18 content, then 4 technical."""
    return (
        {
            "doc_id": "da-fx-po",
            "slug": "po",
            "plan_item": "§6.4 1 真正采购订单",
            "coverage": "file",
            "fixture_kind": "content",
            "source_family": "fixture_scope",
            "vendor": "quayside",
            "customer": "beacon",
            "expected_status": "needs_review",
            "must_not_auto_accept": True,
            "expected_scope_status": "UNSUPPORTED",
            "expected_scope_reasons": ("PURCHASE_ORDER",),
            "expected_issues": (),
            "notes": "Title PURCHASE ORDER with a PO number, line items and amounts; no invoice wording.",
        },
        {
            "doc_id": "da-fx-poref",
            "slug": "poref",
            "plan_item": "§6.4 2 引用 PO 号的普通发票",
            "coverage": "file",
            "fixture_kind": "content",
            "source_family": "fixture_scope",
            "vendor": "quayside",
            "customer": "beacon",
            "expected_status": "auto_accepted",
            "must_not_auto_accept": False,
            "expected_scope_status": "SUPPORTED",
            "expected_scope_reasons": (),
            "expected_issues": (),
            "notes": "Ordinary USD invoice that also prints PO-2026-7701; the PO number is never the invoice number.",
        },
        {
            "doc_id": "da-fx-cur",
            "slug": "cur",
            "plan_item": "§6.4 3 仅有 $",
            "coverage": "file",
            "fixture_kind": "content",
            "source_family": "fixture_scope",
            "vendor": "granite",
            "customer": "fieldstone",
            "expected_status": "needs_review",
            "must_not_auto_accept": True,
            "expected_scope_status": "UNKNOWN",
            "expected_scope_reasons": (),
            "expected_issues": ("CURRENCY_UNCONFIRMED", "SCOPE_UNKNOWN"),
            "show_currency_header": False,
            "notes": "No Currency header: the amounts carry only $, so the currency is not confirmed.",
        },
        {
            "doc_id": "da-fx-date",
            "slug": "date",
            "plan_item": "§6.4 4 歧义日期",
            "coverage": "file",
            "fixture_kind": "content",
            "source_family": "fixture_scope",
            "vendor": "granite",
            "customer": "fieldstone",
            "expected_status": "needs_review",
            "must_not_auto_accept": True,
            "expected_scope_status": "SUPPORTED",
            "expected_scope_reasons": (),
            "expected_issues": ("AMBIGUOUS_DATE",),
            "date_style": "US",
            "issue_date": date(2026, 3, 4),
            "notes": "Prints 03/04/2026 with no format evidence: invoice_date is null and the raw text stays in evidence.",
        },
        {
            "doc_id": "da-fx-lang",
            "slug": "lang",
            "plan_item": "§6.4 5 非英文主单据",
            "coverage": "file",
            "fixture_kind": "content",
            "source_family": "fixture_language",
            "vendor": "lumen",
            "customer": "beacon",
            "expected_status": "needs_review",
            "must_not_auto_accept": True,
            "expected_scope_status": "UNSUPPORTED",
            "expected_scope_reasons": ("NON_ENGLISH",),
            "expected_issues": (),
            "date_style": "ISO",
            "language": "other",
            "notes": "German body text; the language is read from the page, not from the vendor name. An ISO date avoids a second ambiguity.",
        },
        {
            "doc_id": "da-fx-taxunk",
            "slug": "taxunk",
            "plan_item": "§6.4 6 税前/含税语义不明",
            "coverage": "file",
            "fixture_kind": "content",
            "source_family": "fixture_tax_basis",
            "vendor": "lumen",
            "customer": "beacon",
            "expected_status": "needs_review",
            "must_not_auto_accept": True,
            "expected_scope_status": "UNKNOWN",
            "expected_scope_reasons": (),
            "expected_issues": ("SCOPE_UNKNOWN",),
            "tax_note": None,
            "tax_rate": None,
            "notes": "No 'Prices exclude tax.' note, no tax row and no inclusive wording: unit_price_basis is unknown.",
        },
        {
            "doc_id": "da-fx-incl",
            "slug": "incl",
            "plan_item": "§6.4 7 明确含税",
            "coverage": "file",
            "fixture_kind": "content",
            "source_family": "fixture_tax_basis",
            "vendor": "orchard",
            "customer": "fieldstone",
            "expected_status": "needs_review",
            "must_not_auto_accept": True,
            "expected_scope_status": "UNSUPPORTED",
            "expected_scope_reasons": ("TAX_INCLUSIVE",),
            "expected_issues": (),
            "tax_note": "Prices include tax.",
            "tax_inclusive": True,
            "notes": "The page states that the unit prices include tax: unit_price_basis is tax_inclusive.",
        },
        {
            "doc_id": "da-fx-prepay",
            "slug": "prepay",
            "plan_item": "§6.4 8 预付款/上期余额",
            "coverage": "file",
            "fixture_kind": "content",
            "source_family": "fixture_tax_basis",
            "vendor": "orchard",
            "customer": "fieldstone",
            "expected_status": "needs_review",
            "must_not_auto_accept": True,
            "expected_scope_status": "UNSUPPORTED",
            "expected_scope_reasons": ("PREPAYMENT_OR_PRIOR_BALANCE",),
            "expected_issues": (),
            "prepayment": True,
            "notes": "The totals block prints a prepayment already received.",
        },
        {
            "doc_id": "da-fx-21ln",
            "slug": "21ln",
            "plan_item": "§6.4 9 恰好 21 行",
            "coverage": "file",
            "fixture_kind": "content",
            "source_family": "fixture_limits",
            "vendor": "quayside",
            "customer": "beacon",
            "expected_status": "needs_review",
            "must_not_auto_accept": True,
            "expected_scope_status": "UNSUPPORTED",
            "expected_scope_reasons": ("TOO_MANY_LINES",),
            "expected_issues": (),
            "line_count": 21,
            "rows_per_page": 24,
            "notes": "Exactly 21 lines on one page. The preparation limit is 20, so the row count alone is out of scope.",
        },
        {
            "doc_id": "da-fx-empty",
            "slug": "empty",
            "plan_item": "§6.4 10 空表",
            "coverage": "file",
            "fixture_kind": "content",
            "source_family": "fixture_limits",
            "vendor": "granite",
            "customer": "fieldstone",
            "expected_status": "needs_review",
            "must_not_auto_accept": True,
            "expected_scope_status": "SUPPORTED",
            "expected_scope_reasons": (),
            "expected_issues": ("MISSING_REQUIRED_FIELD",),
            "line_count": 0,
            "notes": "The line-item table prints no rows. Zero rows is a missing amount, not a row-limit problem.",
        },
        {
            "doc_id": "da-fx-span",
            "slug": "span",
            "plan_item": "§6.4 11 两页且明细跨页",
            "coverage": "file",
            "fixture_kind": "content",
            "source_family": "fixture_limits",
            "vendor": "lumen",
            "customer": "beacon",
            "expected_status": "needs_review",
            "must_not_auto_accept": True,
            "expected_scope_status": "UNSUPPORTED",
            "expected_scope_reasons": ("TABLE_SPANS_PAGES",),
            "expected_issues": (),
            "line_count": 20,
            "rows_per_page": 18,
            "notes": "Two pages, and the line-item table continues onto page 2. The row count stays at the limit.",
        },
        {
            "doc_id": "da-fx-3pg",
            "slug": "3pg",
            "plan_item": "§6.4 12 三页且明细不跨页",
            "coverage": "file",
            "fixture_kind": "content",
            "source_family": "fixture_limits",
            "vendor": "orchard",
            "customer": "fieldstone",
            "expected_status": "needs_review",
            "must_not_auto_accept": True,
            "expected_scope_status": "UNSUPPORTED",
            "expected_scope_reasons": ("TOO_MANY_PAGES",),
            "expected_issues": (),
            "line_count": 16,
            "rows_per_page": 18,
            "wrapped_descriptions": True,
            "terms_page": True,
            "notes": "Three pages: every line item on page 1, the totals block on page 2, a terms page as page 3.",
        },
        {
            "doc_id": "da-fx-unread",
            "slug": "unread",
            "plan_item": "§6.4 13 关键字段确实不可辨认的图像",
            "coverage": "file",
            "fixture_kind": "content",
            "source_family": "fixture_quality",
            "vendor": "granite",
            "customer": "beacon",
            "expected_status": "needs_review",
            "must_not_auto_accept": True,
            "expected_scope_status": "SUPPORTED",
            "expected_scope_reasons": (),
            "expected_issues": ("UNREADABLE_FIELD",),
            "unreadable_fields": UNREADABLE_FIELDS,
            "readability_gate": "image_degraded_unverified",
            "notes": "The totals block is covered by an opaque patch in the rendered image. The covered values are null with unreadable evidence; the constructed truth never enters the gold.",
        },
        {
            "doc_id": "da-fx-scan",
            "slug": "scan",
            "plan_item": "§6.4 14 扫描 PDF",
            "coverage": "file",
            "fixture_kind": "content",
            "source_family": "fixture_medium",
            "vendor": "quayside",
            "customer": "fieldstone",
            "expected_status": "auto_accepted",
            "must_not_auto_accept": False,
            "expected_scope_status": "SUPPORTED",
            "expected_scope_reasons": (),
            "expected_issues": (),
            "render_mode": "image",
            "readability_gate": "image_readable",
            "notes": "Every page is a raster image with no text layer. No text layer is not an empty file.",
        },
        {
            "doc_id": "da-fx-mixed",
            "slug": "mixed",
            "plan_item": "§6.4 15 混合文本与图像页 PDF",
            "coverage": "file",
            "fixture_kind": "content",
            "source_family": "fixture_medium",
            "vendor": "lumen",
            "customer": "fieldstone",
            "expected_status": "auto_accepted",
            "must_not_auto_accept": False,
            "expected_scope_status": "SUPPORTED",
            "expected_scope_reasons": (),
            "expected_issues": (),
            "render_mode": "image",
            "readability_gate": "image_readable",
            "notes": "Page 1 carries the invoice as an image; page 2 keeps a text layer.",
        },
        {
            "doc_id": "da-fx-blank",
            "slug": "blank",
            "plan_item": "§6.4 16 有效发票加空白页 PDF",
            "coverage": "file",
            "fixture_kind": "content",
            "source_family": "fixture_medium",
            "vendor": "orchard",
            "customer": "beacon",
            "expected_status": "auto_accepted",
            "must_not_auto_accept": False,
            "expected_scope_status": "SUPPORTED",
            "expected_scope_reasons": (),
            "expected_issues": (),
            "trailing_blank_page": True,
            "notes": "Page 1 holds a valid invoice; page 2 is blank.",
        },
        {
            "doc_id": "da-fx-dup",
            "slug": "dup",
            "plan_item": "§6.4 17 合法相同行",
            "coverage": "file",
            "fixture_kind": "content",
            "source_family": "fixture_quality",
            "vendor": "quayside",
            "customer": "fieldstone",
            "expected_status": "auto_accepted",
            "must_not_auto_accept": False,
            "expected_scope_status": "SUPPORTED",
            "expected_scope_reasons": (),
            "expected_issues": (),
            "duplicate_lines": True,
            "notes": "Two lines with the same description and the same amount. Both are legal; the gold keeps both and never de-duplicates.",
        },
        {
            "doc_id": "da-fx-prec",
            "slug": "prec",
            "plan_item": "§6.4 18 小数数量与三位以上单价",
            "coverage": "file",
            "fixture_kind": "content",
            "source_family": "fixture_quality",
            "vendor": "orchard",
            "customer": "beacon",
            "expected_status": "auto_accepted",
            "must_not_auto_accept": False,
            "expected_scope_status": "SUPPORTED",
            "expected_scope_reasons": (),
            "expected_issues": (),
            "fractional_quantity": True,
            "notes": "quantity 1.5 is printed as such; the upstream layout prints the unit price with two decimals, so the third decimal is covered by the object-level cases instead.",
        },
        {
            "doc_id": "da-fx-crc",
            "slug": "crc",
            "plan_item": "§6.4 20 技术夹具：损坏",
            "coverage": "file",
            "fixture_kind": "technical",
            "source_family": "fixture_technical",
            "expected_status": "failed",
            "must_not_auto_accept": True,
            "expected_scope_status": "UNKNOWN",
            "expected_scope_reasons": (),
            "expected_issues": (),
            "expected_technical_error": "UNREADABLE_FILE",
            "notes": "A deterministically truncated PDF: the file is real, but its pages cannot be read.",
        },
        {
            "doc_id": "da-fx-zero",
            "slug": "zero",
            "plan_item": "§6.4 20 技术夹具：零字节",
            "coverage": "file",
            "fixture_kind": "technical",
            "source_family": "fixture_technical",
            "expected_status": "failed",
            "must_not_auto_accept": True,
            "expected_scope_status": "UNKNOWN",
            "expected_scope_reasons": (),
            "expected_issues": (),
            "expected_technical_error": "EMPTY_FILE",
            "notes": "A zero-byte file.",
        },
        {
            "doc_id": "da-fx-enc",
            "slug": "enc",
            "plan_item": "§6.4 20 技术夹具：加密不可读",
            "coverage": "file",
            "fixture_kind": "technical",
            "source_family": "fixture_technical",
            "expected_status": "failed",
            "must_not_auto_accept": True,
            "expected_scope_status": "UNKNOWN",
            "expected_scope_reasons": (),
            "expected_issues": (),
            "expected_technical_error": "ENCRYPTED_FILE",
            "notes": "A password-protected PDF with a fixed password, generated deterministically.",
        },
        {
            "doc_id": "da-fx-fake",
            "slug": "fake",
            "plan_item": "§6.4 20 技术夹具：伪扩展名",
            "coverage": "file",
            "fixture_kind": "technical",
            "source_family": "fixture_technical",
            "expected_status": "failed",
            "must_not_auto_accept": True,
            "expected_scope_status": "UNKNOWN",
            "expected_scope_reasons": (),
            "expected_issues": (),
            "expected_technical_error": "INVALID_FILE_TYPE",
            "notes": "Plain text stored under a .pdf name: the extension is not proof of the format.",
        },
    )


#: The fixture table: 18 content fixtures, then 4 technical fixtures.
FIXTURES: Tuple[Mapping[str, Any], ...] = _rows()
BY_ID: Dict[str, Mapping[str, Any]] = {row["doc_id"]: row for row in FIXTURES}


def fixture_rows() -> Tuple[Mapping[str, Any], ...]:
    """The §6.4 fixture table (a read-only view of :data:`FIXTURES`)."""
    return FIXTURES


# ---------------------------------------------------------------------------
# Object-level cases.  They live in tests/fixtures/numeric/ and are covered by
# pytest only: they are never part of the file-level fixture counts (D8, and
# the coordinator ruling that keeps them out of the fixture manifest).
# ---------------------------------------------------------------------------


def object_level_cases(root: Optional[Path] = None) -> List[Dict[str, Any]]:
    """The object-level rounding cases, read from ``tests/fixtures/numeric``."""
    directory = (Path(root) if root is not None else _ROOT) / "tests" / "fixtures" / "numeric"
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.json"))]


def _check_object_level_cases() -> None:
    """Every object-level case must round half up to its hand-computed value."""
    cases = object_level_cases()
    if len(cases) < 3:
        raise FixtureError(f"at least three object-level cases are required, got {len(cases)}")
    for case in cases:
        product = Decimal(case["quantity"]) * Decimal(case["unit_price"])
        if matrix.round_money(product) != Decimal(case["expected_line_amount"]):
            raise FixtureError(
                f"{case['case_id']}: {case['quantity']} x {case['unit_price']} does not round to "
                f"{case['expected_line_amount']}"
            )


# ---------------------------------------------------------------------------
# Fixture specs
# ---------------------------------------------------------------------------


def _vendor(row: Mapping[str, Any]) -> Party:
    key = row["vendor"]
    match = next((v for v in FIXTURE_VENDORS if v.key == key), None)
    if match is None:
        raise FixtureError(f"{row['doc_id']}: unknown fixture vendor {key!r}")
    return Party(match.name, tuple(match.address), "US", match.phone, match.email,
                 match.tax_id, match.tax_label)


def _customer(row: Mapping[str, Any]) -> Party:
    key = row["customer"]
    match = next((c for c in FIXTURE_CUSTOMERS if c.key == key), None)
    if match is None:
        raise FixtureError(f"{row['doc_id']}: unknown fixture customer {key!r}")
    return Party(match.name, tuple(match.address), "US")


_MONEY = MoneyStyle("$", "prefix", False, "minus", US_NUM)

#: The default tax note of a fixture.  It is the note the shared gold parser
#: reads as ``pre_tax``; a fixture that must read ``unknown`` sets
#: ``tax_note`` to ``None`` in the table.
_DEFAULT_TAX_NOTE = "Prices exclude tax."


def _line(item: matrix.Item) -> Line:
    return Line(item.description, float(item.amount), qty=float(item.quantity),
                unit_price=float(item.unit_price))


#: Two fixture-only products.  Their prices are exact at the cent.
_INSERT = matrix.Item("Corrugated insert, double wall", Decimal("12"), Decimal("7.60"))
_DUPLICATE = matrix.Item("Corrugated insert, size 04", Decimal("4"), Decimal("3.40"))


def _fixture_lines(row: Mapping[str, Any]) -> Tuple[Line, ...]:
    """The line items of one content fixture."""
    doc_id = row["doc_id"]
    if doc_id == "da-fx-dup":
        item = _line(_DUPLICATE)
        return (item, item)
    if doc_id == "da-fx-prec":
        # 1.5 x 12.40 = 18.60 exactly.  The quantity keeps its fractional
        # form; the third unit-price decimal is an object-level case, because
        # the upstream layout prints a unit price with two decimals.
        return (Line("Corrugated insert, size 06", 18.60, qty=1.5, unit_price=12.40),)
    if doc_id == "da-fx-unread":
        # 25 x 2.40 = 60.00; 12 x 7.60 = 91.20; subtotal 151.20, tax 9.07,
        # total 160.27.  The last three are the hidden truth.
        return (_line(matrix.CARTONS), _line(_INSERT))
    count = row.get("line_count")
    if count == 0:
        return ()
    if count is not None:
        items = (matrix.a3_items(count) if row.get("wrapped_descriptions")
                 else matrix.insert_lines(count))
        return tuple(_line(item) for item in items)
    return (_line(matrix.PACKING_TAPE), _line(matrix.CARTONS), _line(matrix.STRETCH_FILM))


def _issue_date(row: Mapping[str, Any]) -> date:
    if "issue_date" in row:
        return row["issue_date"]
    index = [r["doc_id"] for r in FIXTURES].index(row["doc_id"])
    return date(2026, 11, 1 + index)


def _number(row: Mapping[str, Any]) -> Tuple[str, str]:
    doc_id = row["doc_id"]
    if doc_id == "da-fx-po":
        return "PO-2027-3301", "Purchase Order No"
    if doc_id == "da-fx-poref":
        return "INV-2027-2101", "Invoice #"
    index = [r["doc_id"] for r in FIXTURES].index(doc_id)
    return f"INV-2027-{2201 + index}", "Invoice #"


def _notes(row: Mapping[str, Any]) -> Tuple[str, ...]:
    if row["doc_id"] == "da-fx-lang":
        return ("Preise verstehen sich zuzüglich Mehrwertsteuer.",
                "Zahlbar innerhalb von 30 Tagen.")
    if row["doc_id"] == "da-fx-po":
        return ("Authorised by the purchasing department.", "Deliver to the address above.")
    note = row.get("tax_note", _DEFAULT_TAX_NOTE)
    if note is None:
        return ()
    return (note,)


def _taxes(row: Mapping[str, Any], lines: Sequence[Line]) -> Tuple[Tax, ...]:
    if row.get("tax_rate", "default") is None or not lines:
        return ()
    total = matrix.round_money(sum((Decimal(str(ln.amount)) for ln in lines), Decimal("0.00")))
    return (Tax("Sales Tax (6%)", float(matrix.round_money(total * Decimal("0.06"))),
                float(Decimal("0.06"))),)


def _title(row: Mapping[str, Any]) -> str:
    return "PURCHASE ORDER" if row["doc_id"] == "da-fx-po" else "INVOICE"


def fixture_spec(row: Mapping[str, Any]) -> DocSpec:
    """The upstream ``DocSpec`` of one content fixture."""
    if row["fixture_kind"] != "content":
        raise FixtureError(f"{row['doc_id']}: a technical fixture has no DocSpec")
    doc_id = row["doc_id"]
    number, number_label = _number(row)
    issue = _issue_date(row)
    lines = _fixture_lines(row)
    is_po = doc_id == "da-fx-po"

    charges: Tuple[Charge, ...] = ()
    if row.get("prepayment"):
        charges = (Charge("Prepayment received", 40.00, "discount"),)

    return DocSpec(
        doc_id=doc_id,
        category="fixture",
        difficulty="hard",
        description=row["notes"],
        vendor=_vendor(row),
        customer=_customer(row),
        issue_date=issue,
        currency="USD",
        money_style=_MONEY,
        date_style=row.get("date_style", "USLONG"),
        kind="invoice",
        title=_title(row),
        number=number,
        number_label=number_label,
        due_date=None if is_po else issue + timedelta(days=30),
        terms=None if is_po else "30 days",
        po_number="PO-2026-7701" if doc_id == "da-fx-poref" else None,
        lines=lines,
        charges=charges,
        taxes=_taxes(row, lines),
        tax_inclusive=bool(row.get("tax_inclusive")),
        notes_text=_notes(row),
        layout=Layout(family="sans", header="left", table="band", paper="LETTER",
                      rows_per_page=int(row.get("rows_per_page", 24)),
                      show_currency_header=bool(row.get("show_currency_header", True))),
        render=Render(mode="image" if row.get("render_mode") == "image" else "pdf"),
        tags=("fixture", row["coverage"], row["source_family"]),
    )


# ---------------------------------------------------------------------------
# Rendering: the standard PDF, an image-only PDF, a mixed PDF, a blank page,
# a terms page, and the occluded totals block.
# ---------------------------------------------------------------------------


def _canvas(pages: Sequence[Page], *, password: Optional[str] = None):
    from reportlab.pdfgen.canvas import Canvas

    buffer = io.BytesIO()
    canvas = Canvas(buffer, pagesize=(pages[0].width, pages[0].height), invariant=1,
                    encrypt=password)
    canvas.setCreator("IPP accuracy benchmark generator")
    return buffer, canvas


def _draw_page(canvas, page: Page) -> None:
    """Draw one layout page on a reportlab canvas.

    This mirrors ``generator.render.render_pdf`` for a single page.  The mixed
    fixture must put a raster page and a text page in one file, which
    ``render_pdf`` cannot do for a whole document; the drawing itself stays
    the upstream drawing, unchanged.
    """
    height = page.height
    for op in page.ops:
        if isinstance(op, Box):
            if op.fill is not None:
                canvas.setFillColorRGB(*(v / 255 for v in op.fill))
            if op.stroke is not None:
                canvas.setStrokeColorRGB(*(v / 255 for v in op.stroke))
                canvas.setLineWidth(op.width)
            canvas.rect(op.x, height - op.y - op.h, op.w, op.h,
                        fill=1 if op.fill else 0, stroke=1 if op.stroke else 0)
        elif isinstance(op, Rule):
            canvas.setStrokeColorRGB(*(v / 255 for v in op.color))
            canvas.setLineWidth(op.width)
            canvas.line(op.x1, height - op.y1, op.x2, height - op.y2)
        elif isinstance(op, Text):
            canvas.setFillColorRGB(*(v / 255 for v in op.color))
            canvas.setFont(PDF_FONTS.get(op.font, "Helvetica"), op.size)
            canvas.drawString(op.left, height - op.y, op.text)


def _png(image) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "PNG", optimize=False)
    return buffer.getvalue()


def _image_pdf(pages: Sequence[Page], seed: str) -> bytes:
    """A PDF whose pages are raster images only: no text layer anywhere."""
    from reportlab.lib.utils import ImageReader

    pictures = render_images(pages, dpi=144, seed=seed)
    if len(pictures) != len(pages):
        raise FixtureError(f"{len(pictures)} rasters for {len(pages)} pages")
    buffer, canvas = _canvas(pages)
    for page, picture in zip(pages, pictures):
        canvas.drawImage(ImageReader(io.BytesIO(_png(picture))), 0, 0,
                         width=page.width, height=page.height)
        canvas.showPage()
    canvas.save()
    return buffer.getvalue()


def _mixed_pdf(pages: Sequence[Page], seed: str) -> bytes:
    """Page 1 is an image; the other pages keep their text layer."""
    from reportlab.lib.utils import ImageReader

    picture = render_images(pages[:1], dpi=144, seed=seed)[0]
    buffer, canvas = _canvas(pages)
    canvas.drawImage(ImageReader(io.BytesIO(_png(picture))), 0, 0,
                     width=pages[0].width, height=pages[0].height)
    canvas.showPage()
    for page in pages[1:]:
        _draw_page(canvas, page)
        canvas.showPage()
    canvas.save()
    return buffer.getvalue()


def _occluded_pdf(pages: Sequence[Page], seed: str, doc_id: str) -> bytes:
    """An image-only PDF with an opaque patch over the totals block.

    The patch hides the subtotal, the tax and the total in the rendered image.
    The file has no text layer, so no reader can recover them from the page
    text either.  The gold is built with :data:`UNREADABLE_FIELDS` forced, so
    the constructed values never reach it.
    """
    from PIL import ImageDraw
    from reportlab.lib.utils import ImageReader

    rows = gold.parse_summary_rows(pages)
    if not rows:
        raise FixtureError(f"{doc_id}: the totals block was not found")
    page = pages[0]
    tops = [row[3].y for row in rows] + [row[4].y for row in rows]
    lefts = [row[3].left for row in rows]
    top = min(tops) - 14.0
    bottom = max(tops) + 6.0
    left = min(lefts) - 8.0
    right = page.width - gold.MARGIN + 8.0

    sprite = render_images([page], dpi=144, seed=seed)[0]
    k = 144.0 / 72.0
    ImageDraw.Draw(sprite).rectangle((left * k, top * k, right * k, bottom * k),
                                     fill=(255, 255, 255))

    buffer, canvas = _canvas(pages)
    canvas.drawImage(ImageReader(io.BytesIO(_png(sprite))), 0, 0,
                     width=page.width, height=page.height)
    canvas.showPage()
    canvas.save()
    return buffer.getvalue()


def _terms_page(width: float, height: float) -> Page:
    """A third page that prints terms and no line-item table."""
    page = Page(width, height)
    page.ops.append(Text(38.0, 60.0, "TERMS AND CONDITIONS", "sans-bold", 12))
    body = (
        "1. Payment is due within 30 days of the invoice date.",
        "2. Goods remain the property of the seller until paid in full.",
        "3. Claims must be raised within 7 days of delivery.",
        "4. This document is not a tax invoice for resale.",
    )
    y = 82.0
    for line in body:
        page.ops.append(Text(38.0, y, line, "sans", 9))
        y += 14
    return page


def _delivery_page(width: float, height: float) -> Page:
    """A short second page for the mixed fixture."""
    page = Page(width, height)
    page.ops.append(Text(38.0, 60.0, "DELIVERY NOTE", "sans-bold", 12))
    page.ops.append(Text(38.0, 82.0,
                         "The goods listed above left the warehouse in one shipment.", "sans", 9))
    return page


def render_fixture(row: Mapping[str, Any]) -> Tuple[bytes, List[Page]]:
    """The bytes and the printed pages of one content fixture."""
    doc = fixture_spec(row)
    pages: List[Page] = list(build_pages(doc))
    doc_id = doc.doc_id
    title = f"{doc.title} {doc.number or ''}".strip()

    if doc_id == "da-fx-3pg":
        pages = [*pages, _terms_page(pages[0].width, pages[0].height)]
        return render_pdf(pages, title=title, author=doc.vendor.name), pages
    if doc_id == "da-fx-blank":
        pages = [*pages, Page(pages[0].width, pages[0].height)]
        return render_pdf(pages, title=title, author=doc.vendor.name), pages
    if doc_id == "da-fx-scan":
        return _image_pdf(pages, f"{FIXTURE_SEED}:{doc_id}"), pages
    if doc_id == "da-fx-mixed":
        pages = [*pages, _delivery_page(pages[0].width, pages[0].height)]
        return _mixed_pdf(pages, f"{FIXTURE_SEED}:{doc_id}"), pages
    if doc_id == "da-fx-unread":
        return _occluded_pdf(pages, f"{FIXTURE_SEED}:{doc_id}", doc_id), pages
    return render_pdf(pages, title=title, author=doc.vendor.name), pages


# ---------------------------------------------------------------------------
# Technical fixtures
# ---------------------------------------------------------------------------


def _technical_bytes(row: Mapping[str, Any]) -> bytes:
    """The bytes of one technical fixture; every one is named ``.pdf``."""
    doc_id = row["doc_id"]
    if doc_id == "da-fx-zero":
        return b""
    if doc_id == "da-fx-fake":
        return (
            b"This is not a PDF. The name says .pdf; the content is text.\n"
            b"The extension is not proof of the format.\n"
        )
    if doc_id == "da-fx-enc":
        from reportlab.pdfgen.canvas import Canvas

        buffer = io.BytesIO()
        canvas = Canvas(buffer, pagesize=(612.0, 792.0), invariant=1, encrypt=FIXTURE_PASSWORD)
        canvas.setCreator("IPP accuracy benchmark generator")
        canvas.setFont("Helvetica", 11)
        canvas.drawString(60, 720, "INVOICE")
        canvas.drawString(60, 700, "This file is password protected.")
        canvas.showPage()
        canvas.save()
        return buffer.getvalue()
    if doc_id == "da-fx-crc":
        # A real PDF, deterministically truncated inside the page content, so
        # no reader can recover the pages.
        source = replace(fixture_spec(BY_ID["da-fx-poref"]), doc_id="da-fx-crc-source")
        data = render_pdf(build_pages(source), title="INVOICE", author=source.vendor.name)
        if len(data) < 400:
            raise FixtureError("the truncation base is too short to corrupt")
        return data[: len(data) // 2]
    raise FixtureError(f"{doc_id}: unknown technical fixture")


# ---------------------------------------------------------------------------
# Gold
# ---------------------------------------------------------------------------


def hidden_truth(doc_id: str) -> Dict[str, Any]:
    """The constructed true values of the unreadable fixture.

    It is returned only to the tests, so they can prove the values are absent
    from the gold, the previews and the reports.  It is never written to an
    artifact.
    """
    if doc_id != "da-fx-unread":
        return {}
    spec = fixture_spec(BY_ID[doc_id])
    lines_total = matrix.round_money(
        sum((Decimal(str(ln.amount)) for ln in spec.lines), Decimal("0.00"))
    )
    tax = matrix.round_money(lines_total * Decimal("0.06"))
    return {
        "subtotal_amount": f"{lines_total:.2f}",
        "tax_amount": f"{tax:.2f}",
        "total_amount": f"{matrix.round_money(lines_total + tax):.2f}",
    }


_US_DATE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _fixture_date(text: Optional[str], style: str) -> Tuple[Optional[str], bool]:
    """``(iso value, ambiguous)`` for one printed date of a fixture.

    A numeric US date has no format evidence, so it stays ambiguous: the value
    is ``None`` and the raw text is the evidence.  A long date and an ISO date
    carry their own format.
    """
    if not text:
        return None, False
    stripped = text.strip()
    if style == "US":
        return (None, True) if _US_DATE.match(stripped) else (None, False)
    if style == "ISO" and _ISO_DATE.match(stripped):
        return date.fromisoformat(stripped).isoformat(), False
    try:
        return gold._parse_date(stripped), False
    except gold.GoldError:
        return None, False


def _meta(meta: Mapping[str, Tuple[str, int, Text]], *names: str):
    for name in names:
        found = meta.get(name)
        if found is not None:
            return found[0], found[1], found[2]
    return None, None, None


#: Words that mark the printed text as not English.  The language is read from
#: the page; the fixture table only declares what the fixture must show.
_NON_ENGLISH_MARKERS: Tuple[str, ...] = ("Preise", "Mehrwertsteuer", "Zahlbar")


def page_language(pages: Sequence[Page]) -> str:
    """``"other"`` when the printed text carries non-English markers."""
    text = " ".join(page_text(page) for page in pages)
    return "other" if any(marker in text for marker in _NON_ENGLISH_MARKERS) else "en"


def _printed_note(pages: Sequence[Page], needle: str) -> Optional[str]:
    """The exact printed line that carries ``needle``, or ``None``.

    The whole page text is never returned: a note used as evidence must not
    carry an amount that the same fixture keeps hidden.
    """
    for page in pages:
        for op in page.ops:
            if isinstance(op, Text) and op.text.strip() == needle:
                return op.text.strip()
    return None


def _evidence(status: str, page: Optional[int], text: Optional[str],
              searched: Sequence[int], op: Optional[Text] = None,
              as_image: bool = False) -> Dict[str, Any]:
    return gold._evidence(status, page, text, searched, op, as_image)


def build_fixture_gold(doc: DocSpec, pages: Sequence[Page],
                       file_meta: Mapping[str, Any], row: Mapping[str, Any]) -> Dict[str, Any]:
    """Build one fixture gold record from the printed page.

    The shared parsers in ``gold.py`` read the page.  The fixture-only rules
    live here: a PO title is a ``purchase_order`` signal, the language is read
    from the page, a numeric date stays ambiguous, an unreadable field stays
    null with ``unreadable`` evidence, and :data:`FIXTURE_LIMITS` decides the
    page and row limits.

    The derived scope status, reasons and issues are checked against the §6.4
    row.  A disagreement raises :class:`FixtureError`.
    """
    if not pages:
        raise FixtureError(f"{doc.doc_id}: no printed page")
    unreadable = set(row.get("unreadable_fields", ()))
    as_image = row.get("render_mode") == "image" or bool(unreadable)
    file_info = gold._check_file_meta(file_meta, pages)
    meta = gold.parse_meta(pages)
    summary = gold.parse_summary_rows(pages)
    items = gold.parse_line_items(pages)
    searched = list(range(1, len(pages) + 1))
    texts = [page_text(page) for page in pages]
    joined = " ".join(texts)

    is_po = "PURCHASE ORDER" in joined
    language = page_language(pages)
    if row.get("language") and language != row["language"]:
        raise FixtureError(f"{doc.doc_id}: the page reads {language!r}, the table expects "
                           f"{row['language']!r}")

    currency, currency_page, currency_op = _meta(meta, "Currency")
    date_text, date_page, date_op = _meta(meta, "Date", "Invoice Date")
    due_text, due_page, due_op = _meta(meta, "Due Date")
    po_text, po_page, po_op = _meta(meta, "Purchase Order No", "PO Number", "PO #", "Order No")
    number_text, number_page, number_op = _meta(meta, "Invoice #", "Invoice No", "Invoice Number")

    invoice_number = None if is_po else number_text
    po_number = po_text or (number_text if is_po else None)

    style = doc.date_style
    invoice_date, ambiguous_date = _fixture_date(date_text, style)
    due_date, ambiguous_due = _fixture_date(due_text, style)

    tax_rows = [r for r in summary if gold._is_tax_label(r[0])]
    total_row = next((r for r in summary if gold._is_total_label(r[0])), None)
    subtotal_row = next((r for r in summary if gold._is_subtotal_label(r[0])), None)
    discount_rows = [r for r in summary if gold._is_discount_label(r[0])]
    prepay_rows = [r for r in summary
                   if "prepayment" in r[0].casefold() or "prior balance" in r[0].casefold()]
    charge_rows = [
        r for r in summary
        if not gold._is_tax_label(r[0]) and not gold._is_discount_label(r[0])
        and not gold._is_subtotal_label(r[0]) and not gold._is_total_label(r[0])
        and "prepayment" not in r[0].casefold() and "prior balance" not in r[0].casefold()
    ]
    inclusive_note = _printed_note(pages, "Prices include tax.")
    exclude_note = _printed_note(pages, "Prices exclude tax.")

    if inclusive_note:
        basis = "tax_inclusive"
    elif exclude_note or tax_rows:
        basis = "pre_tax"
    else:
        basis = "unknown"

    table_pages = sorted({item["page"] for item in items})
    spans = len(table_pages) > 1

    signals: Dict[str, Any] = {
        "document_type": "purchase_order" if is_po else "invoice",
        "invoice_count": 1,
        "language": language,
        "currency_evidence": "explicit" if currency else "missing",
        "unit_price_basis": basis,
        "discount": "present" if discount_rows else "absent",
        "additional_charges": "present" if charge_rows else "absent",
        "prepayment_or_prior_balance": "present" if prepay_rows else "absent",
        "tax_presentation": ("inclusive" if basis == "tax_inclusive" and tax_rows
                             else ("single" if tax_rows else "missing")),
        "table_layout": "spans_pages" if spans else "single_page",
        "observed_row_count": len(items),
        "table_pages": table_pages,
    }

    # -- scope, derived from the page --------------------------------------
    reasons: List[str] = []
    if is_po:
        reasons.append("PURCHASE_ORDER")
    if language != "en":
        reasons.append("NON_ENGLISH")
    if basis == "tax_inclusive":
        reasons.append("TAX_INCLUSIVE")
    if prepay_rows:
        reasons.append("PREPAYMENT_OR_PRIOR_BALANCE")
    if charge_rows:
        reasons.append("HAS_EXTRA_CHARGES")
    if discount_rows:
        reasons.append("HAS_DISCOUNT")
    if spans:
        reasons.append("TABLE_SPANS_PAGES")
    if len(pages) > FIXTURE_LIMITS["max_pages"]:
        reasons.append("TOO_MANY_PAGES")
    if len(items) > FIXTURE_LIMITS["max_lines"]:
        reasons.append("TOO_MANY_LINES")
    order = {name: index for index, name in enumerate(codes.SCOPE_REASON)}
    reasons.sort(key=lambda name: order[name])

    unknown: List[str] = []
    if not currency:
        unknown.append("currency")
    if basis == "unknown":
        unknown.append("unit_price_basis")
    if reasons:
        scope_status = "UNSUPPORTED"
    elif unknown:
        scope_status = "UNKNOWN"
    else:
        scope_status = "SUPPORTED"

    # -- issues, derived from the page -------------------------------------
    issues: List[str] = []
    if ambiguous_date:
        issues.append("AMBIGUOUS_DATE")
    if not currency:
        issues.append("CURRENCY_UNCONFIRMED")
    if unreadable:
        issues.append("UNREADABLE_FIELD")
    if scope_status == "UNKNOWN":
        issues.append("SCOPE_UNKNOWN")
    if not items:
        issues.append("MISSING_REQUIRED_FIELD")
    issue_order = {name: index for index, name in enumerate(codes.ISSUE)}
    issues.sort(key=lambda name: issue_order[name])

    # -- the table must agree with the page --------------------------------
    expected_status = "auto_accepted" if scope_status == "SUPPORTED" and not issues else "needs_review"
    if scope_status != row["expected_scope_status"]:
        raise FixtureError(f"{doc.doc_id}: the page routes to {scope_status}, "
                           f"the §6.4 table expects {row['expected_scope_status']}")
    if reasons != list(row["expected_scope_reasons"]):
        raise FixtureError(f"{doc.doc_id}: the page gives scope reasons {reasons}, "
                           f"the §6.4 table expects {list(row['expected_scope_reasons'])}")
    if issues != list(row["expected_issues"]):
        raise FixtureError(f"{doc.doc_id}: the page gives issues {issues}, "
                           f"the §6.4 table expects {list(row['expected_issues'])}")
    if expected_status != row["expected_status"]:
        raise FixtureError(f"{doc.doc_id}: the page routes to {expected_status}, "
                           f"the §6.4 table expects {row['expected_status']}")

    # -- amounts ------------------------------------------------------------
    def printed(field: str) -> Optional[str]:
        if field in unreadable:
            return None
        if field == "subtotal_amount":
            return None if subtotal_row is None else f"{matrix.round_money(subtotal_row[1]):.2f}"
        if field == "tax_amount":
            if not tax_rows:
                return None
            return f"{matrix.round_money(sum((r[1] for r in tax_rows), Decimal('0.00'))):.2f}"
        return None if total_row is None else f"{matrix.round_money(total_row[1]):.2f}"

    charge_entries = [
        {
            "label": r[0],
            "amount": f"{matrix.round_money(r[1]):.2f}",
            "kind": "discount" if gold._is_discount_label(r[0]) else "charge",
        }
        for r in summary
        if not gold._is_tax_label(r[0]) and not gold._is_subtotal_label(r[0])
        and not gold._is_total_label(r[0])
    ]

    invoice: Dict[str, Any] = {
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "due_date": due_date,
        "vendor_name": doc.vendor.name,
        "customer_name": doc.customer.name,
        "currency": currency,
        "document_type": "other" if is_po else "invoice",
        "subtotal_amount": printed("subtotal_amount"),
        "tax_amount": printed("tax_amount"),
        "total_amount": printed("total_amount"),
        "purchase_order_number": po_number,
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
        "charges": charge_entries,
    }

    # -- arithmetic ---------------------------------------------------------
    line_checks = []
    for item in items:
        qty = Decimal(item["quantity"]) if item["quantity"] else None
        unit = Decimal(item["unit_price"]) if item["unit_price"] else None
        recomputed = None if (qty is None or unit is None) else matrix.round_money(qty * unit)
        printed_amount = Decimal(item["amount"])
        delta = None if recomputed is None else matrix.round_money(recomputed - printed_amount)
        line_checks.append({
            "index": item["index"],
            "recomputed_amount": None if recomputed is None else f"{recomputed:.2f}",
            "printed_amount": f"{printed_amount:.2f}",
            "delta": None if delta is None else f"{delta:.2f}",
            "status": "NOT_APPLICABLE" if recomputed is None else ("PASS" if delta == 0 else "FAIL"),
        })
    line_sum: Optional[str] = f"{matrix.round_money(sum((Decimal(i['amount']) for i in items), Decimal('0.00'))):.2f}"
    charges_sum = matrix.round_money(sum((Decimal(c["amount"]) for c in charge_entries),
                                         Decimal("0.00")))
    recomputed_total = None
    if invoice["subtotal_amount"] is not None and invoice["total_amount"] is not None:
        tax_value = Decimal(invoice["tax_amount"] or "0.00")
        recomputed_total = matrix.round_money(
            Decimal(line_sum) + charges_sum + tax_value
        )
    if unreadable:
        # The totals block cannot be read, so no reconciliation that starts
        # from the printed subtotal is possible.  The line sum is withheld
        # too: in this construction it is the hidden subtotal.
        line_sum = None
    arithmetic = {
        "lines": line_checks,
        "line_sum": line_sum,
        "printed_subtotal": invoice["subtotal_amount"],
        "recomputed_total": None if recomputed_total is None else f"{recomputed_total:.2f}",
        "printed_total": invoice["total_amount"],
        "tax_printed": invoice["tax_amount"],
        "charges_printed": f"{charges_sum:.2f}",
        "delta_subtotal": (None if (invoice["subtotal_amount"] is None or line_sum is None)
                           else f"{matrix.round_money(Decimal(line_sum) - Decimal(invoice['subtotal_amount'])):.2f}"),
        "delta_total": (None if recomputed_total is None
                        else f"{matrix.round_money(recomputed_total - Decimal(invoice['total_amount'])):.2f}"),
    }

    rules = {
        "A_line_amount": ("FAIL" if any(c["status"] == "FAIL" for c in line_checks)
                          else ("NOT_APPLICABLE" if not line_checks else "PASS")),
        "B_subtotal": ("NOT_APPLICABLE" if arithmetic["delta_subtotal"] is None
                       else ("PASS" if arithmetic["delta_subtotal"] == "0.00" else "FAIL")),
        "C_total": ("NOT_APPLICABLE" if recomputed_total is None
                    else ("PASS" if arithmetic["delta_total"] == "0.00" else "FAIL")),
        "D_required_fields": "FAIL" if not items else "PASS",
    }

    # -- evidence -----------------------------------------------------------
    evidence: Dict[str, Dict[str, Any]] = {}

    def observed(name: str, page: Optional[int], text: Optional[str],
                 search: Sequence[int], op: Optional[Text]) -> None:
        evidence[name] = _evidence("observed", page, text, search, op, as_image)

    observed("vendor_name", 1, doc.vendor.name, [1], None)
    observed("customer_name", 1, doc.customer.name, [1], None)

    if currency:
        observed("currency", currency_page, currency, [currency_page], currency_op)
    else:
        evidence["currency"] = _evidence("not_found", None, None, searched, None, as_image)

    if invoice_number:
        observed("invoice_number", number_page, invoice_number, [number_page], number_op)
    else:
        evidence["invoice_number"] = _evidence("not_found", None, None, searched, None, as_image)

    if ambiguous_date:
        evidence["invoice_date"] = _evidence("ambiguous", date_page, date_text, [date_page],
                                             date_op, as_image)
    elif invoice_date:
        observed("invoice_date", date_page, date_text, [date_page], date_op)
    else:
        evidence["invoice_date"] = _evidence("not_found", None, None, searched, None, as_image)

    if ambiguous_due:
        evidence["due_date"] = _evidence("ambiguous", due_page, due_text, [due_page],
                                         due_op, as_image)
    elif due_date:
        observed("due_date", due_page, due_text, [due_page], due_op)
    else:
        evidence["due_date"] = _evidence("not_found", None, None, searched, None, as_image)

    if po_number:
        observed("purchase_order_number", po_page, po_number, [po_page], po_op)
    else:
        evidence["purchase_order_number"] = _evidence("not_found", None, None, searched,
                                                      None, as_image)

    for field, source in (("subtotal_amount", subtotal_row),
                          ("tax_amount", tax_rows[0] if tax_rows else None),
                          ("total_amount", total_row)):
        if field in unreadable:
            evidence[field] = _evidence("unreadable", None, None, searched, None, True)
        elif source is None:
            evidence[field] = _evidence("not_found", None, None, searched, None, as_image)
        else:
            observed(field, source[2], source[4].text.strip(), [source[2]], source[4])

    for item in items:
        line_page = item["page"]
        evidence[f"line_items[{item['index']}].amount"] = _evidence(
            "observed", line_page, item["amount"], [line_page], None, as_image)
        for part in ("quantity", "unit_price"):
            if item[part] is not None:
                evidence[f"line_items[{item['index']}].{part}"] = _evidence(
                    "observed", line_page, item[part], [line_page], None, as_image)

    # -- signal evidence ----------------------------------------------------
    def signal(name: str, status: str, page: Optional[int], text: Optional[str],
               search: Sequence[int]) -> None:
        evidence[f"scope_signals.{name}"] = _evidence(status, page, text, search, None, as_image)

    signal("document_type", "observed", 1, "PURCHASE ORDER" if is_po else "INVOICE", [1])
    signal("invoice_count", "observed", 1,
           "Invoice labels: 0 (a purchase order)" if is_po else "Invoice labels: 1", [1])
    signal("language", "observed", 1, language, [1])
    if currency:
        signal("currency_evidence", "observed", currency_page, f"Currency: {currency}",
               [currency_page])
    else:
        signal("currency_evidence", "not_found", None, None, searched)
    if inclusive_note:
        signal("unit_price_basis", "observed", 1, inclusive_note, [1])
    elif exclude_note:
        signal("unit_price_basis", "observed", 1, exclude_note, [1])
    elif tax_rows:
        signal("unit_price_basis", "observed", tax_rows[0][2], tax_rows[0][0], [tax_rows[0][2]])
    else:
        signal("unit_price_basis", "not_found", None, None, searched)
    for name, found in (
        ("discount", discount_rows[0] if discount_rows else None),
        ("additional_charges", charge_rows[0] if charge_rows else None),
        ("prepayment_or_prior_balance", prepay_rows[0] if prepay_rows else None),
        ("tax_presentation", tax_rows[0] if tax_rows else None),
    ):
        if found is None:
            signal(name, "not_found", None, None, searched)
        else:
            signal(name, "observed", found[2], found[0], [found[2]])
    if table_pages:
        signal("table_layout", "observed", table_pages[0],
               f"Rows on pages {table_pages}", table_pages)
        signal("observed_row_count", "observed", table_pages[0],
               f"Rows observed: {len(items)}", table_pages)
        signal("table_pages", "observed", table_pages[0],
               f"Pages with table rows: {table_pages}", table_pages)
    else:
        signal("table_layout", "observed", 1, "No table rows", [1])
        signal("observed_row_count", "observed", 1, "Rows observed: 0", [1])
        signal("table_pages", "observed", 1, "Pages with table rows: []", [1])

    return {
        "schema_version": 2,
        "contract_version": codes.CONTRACT_VERSION,
        "doc_id": doc.doc_id,
        "split": "unassigned",
        "class": "?",
        "subclass": "fixture",
        "target_question": row["notes"],
        "source_family": row["source_family"],
        "file": file_info,
        "printing": {
            "currency_header": f"Currency: {currency}" if currency else None,
            "date_form": style,
            "tax_note": inclusive_note or exclude_note,
            "currency_symbol": doc.money_style.symbol,
        },
        "invoice": invoice,
        "scope": {"status": scope_status, "reasons": reasons, "signals": signals},
        "readability": {
            "gate": row.get("readability_gate", "clean_text"),
            "verified": False,
            "note": ("Fixture readability is prefilled from the construction; the human "
                     "confirmation is recorded in reports/legibility_review.json "
                     "(Gate 2 / T17)."),
        },
        "expected_issues": issues,
        "acceptance_policy": {
            "rules": rules,
            "tax_rule": "APPLICABLE" if tax_rows else "NOT_APPLICABLE",
            "upstream_validate": "OPTIONAL",
            "decisions": issues,
            "notes": row["notes"],
        },
        "arithmetic": arithmetic,
        "evidence": evidence,
        "annotation": {
            "source": "generator_recorded",
            "verification": "generator_recorded",
            "generated_by": "docuagents_gen/fixtures.py (T15)",
            "seed": FIXTURE_SEED,
        },
        "expected_status": row["expected_status"],
        "must_not_auto_accept": row["must_not_auto_accept"],
    }


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


def _main_vendor_ids() -> set:
    ids = {f"vendor:{matrix.VENDORS[row.vendor].key}" for row in matrix.ROWS}
    ids |= {f"vendor:{matrix.VENDORS[child.vendor].key}"
            for row in matrix.ROWS for child in row.children}
    return ids


def check_isolation() -> Dict[str, Any]:
    """Prove that the fixture set shares no entity with the main set."""
    fixture_vendors = {f"vendor:{v.key}" for v in FIXTURE_VENDORS}
    main_vendors = _main_vendor_ids()
    fixture_families = {row["source_family"] for row in FIXTURES}
    main_families = {row.source_family for row in matrix.ROWS}
    report = {
        "vendor_overlap": sorted(fixture_vendors & main_vendors),
        "source_family_overlap": sorted(fixture_families & main_families),
        "doc_id_overlap": sorted({row["doc_id"] for row in FIXTURES} & set(matrix.BY_ID)),
        "fixture_vendors": sorted(fixture_vendors),
        "fixture_source_families": sorted(fixture_families),
        "main_vendors": sorted(main_vendors),
    }
    if report["vendor_overlap"] or report["source_family_overlap"] or report["doc_id_overlap"]:
        raise FixtureError(
            "the fixture isolation does not hold: "
            + ", ".join(f"{key}={report[key]}" for key in
                        ("vendor_overlap", "source_family_overlap", "doc_id_overlap")
                        if report[key])
        )
    return report


# ---------------------------------------------------------------------------
# The fixture manifest
# ---------------------------------------------------------------------------


def _plain(value: Any) -> Any:
    """A JSON-shaped copy of one fixture-table value."""
    if isinstance(value, (date,)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    return value


def fixtures_digest() -> str:
    """The canonical digest of the fixture table itself."""
    from . import generate

    return generate.canonical_sha256([_plain(row) for row in FIXTURES])


def _write_json(path: Path, obj: Any) -> None:
    from . import generate

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(generate.canonical_bytes(obj))


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _size(root: Path, entry: Mapping[str, Any]) -> Dict[str, Any]:
    data = (root / entry["path"]).read_bytes()
    out = dict(entry)
    out["bytes"] = len(data)
    out["sha256"] = hashlib.sha256(data).hexdigest()
    return out


def _ordered(order: Optional[Sequence[str]]) -> List[Mapping[str, Any]]:
    ids = [row["doc_id"] for row in FIXTURES]
    if order is None:
        return list(FIXTURES)
    if len(order) != len(ids) or len(set(order)) != len(ids) or sorted(order) != sorted(ids):
        raise FixtureError("the fixture order must name every fixture exactly once")
    return [BY_ID[doc_id] for doc_id in order]


def _legibility_review(records: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    """The human-confirmation record for the fixture set (D6).

    The status is ``pending_human_confirmation``: an agent cannot confirm a
    legibility reading.  The constructed true values of the unreadable
    fixture are not written here.
    """
    documents = []
    fields = ("vendor_name", "invoice_number", "invoice_date", "currency",
              "subtotal_amount", "tax_amount", "total_amount")
    # Sorted by doc_id: the processing order must never change a byte.
    for doc_id in sorted(records):
        record = records[doc_id]
        entries = []
        for name in fields:
            found = record["evidence"].get(name)
            if found is None:
                continue
            entries.append({
                "field": name,
                "status": found["status"],
                "page": found["page"],
                "text": found["text"],
            })
        documents.append({
            "doc_id": doc_id,
            "file_name": record["file"]["file_name"],
            "file_sha256": record["file"]["sha256"],
            "page_count": record["file"]["page_count"],
            "gate": record["readability"]["gate"],
            "fields": entries,
            "previews": [f"previews/pages/{doc_id}.p{page}.png"
                         for page in range(1, record["file"]["page_count"] + 1)],
        })
    return {
        "schema_version": 1,
        "contract_version": codes.CONTRACT_VERSION,
        "status": "pending_human_confirmation",
        "reviewer": None,
        "reviewed_at": None,
        "confirmation_path": (
            "The Project Owner confirms the legibility of the fixtures in the previews, "
            "and the full annotation freeze at Gate 2 (T17) records the result. Until "
            "then this record stays pending_human_confirmation: an agent cannot confirm it."
        ),
        "documents": documents,
    }


def generate_boundary_fixtures(
    out_dir: Path,
    *,
    seed: int = FIXTURE_SEED,
    order: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Write the whole fixture tree to ``out_dir`` and return a summary."""
    if seed != FIXTURE_SEED:
        raise FixtureError(f"the fixture seed is fixed at {FIXTURE_SEED}, got {seed}")
    rows = _ordered(order)
    check_isolation()
    _check_object_level_cases()

    from . import generate

    out = Path(out_dir)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise FixtureError(f"the output directory {out} exists and is not empty")
    out.mkdir(parents=True, exist_ok=True)

    entries: List[Dict[str, Any]] = []
    records: Dict[str, Dict[str, Any]] = {}

    for row in rows:
        doc_id = row["doc_id"]
        if row["fixture_kind"] == "technical":
            data = _technical_bytes(row)
            rel = f"technical/{doc_id}.pdf"
            _write_bytes(out / rel, data)
            entries.append({
                "path": rel,
                "media_type": "application/pdf",
                "artifact_role": "generated",
                "fixture_id": doc_id,
                "coverage": row["coverage"],
                "expected": {
                    "technical_error": row["expected_technical_error"],
                    "business_status": "failed",
                },
            })
            continue

        doc = fixture_spec(row)
        data, pages = render_fixture(row)
        rel = f"documents/{doc_id}.pdf"
        _write_bytes(out / rel, data)

        file_meta = {
            "file_name": rel,
            "sha256": hashlib.sha256(data).hexdigest(),
            "media_type": "application/pdf",
            "page_count": len(pages),
        }
        record = build_fixture_gold(doc, pages, file_meta, row)
        _write_json(out / "gold" / f"{doc_id}.json", record)
        records[doc_id] = record

        entries.append({
            "path": rel,
            "media_type": "application/pdf",
            "artifact_role": "generated",
            "fixture_id": doc_id,
            "coverage": row["coverage"],
            "page_count": len(pages),
            "expected": {
                "business_status": row["expected_status"],
                "must_not_auto_accept": row["must_not_auto_accept"],
                "scope_status": row["expected_scope_status"],
                "scope_reasons": list(row["expected_scope_reasons"]),
                "issues": list(row["expected_issues"]),
            },
        })
        entries.append({
            "path": f"gold/{doc_id}.json",
            "media_type": "application/json",
            "artifact_role": "generated",
            "fixture_id": doc_id,
            "coverage": row["coverage"],
        })

        images = generate.raster_pages(data)
        if len(images) != len(pages):
            raise FixtureError(f"{doc_id}: {len(images)} previews for {len(pages)} pages")
        for index, image in enumerate(images, 1):
            preview_rel = f"previews/pages/{doc_id}.p{index}.png"
            preview = _png(image)
            _write_bytes(out / preview_rel, preview)
            entries.append({
                "path": preview_rel,
                "media_type": "image/png",
                "artifact_role": "generated",
                "fixture_id": doc_id,
                "coverage": row["coverage"],
                "page": index,
                "digests": generate.image_digests(preview),
            })

    review = _legibility_review(records)
    _write_json(out / "reports" / "legibility_review.json", review)
    entries.append({
        "path": "reports/legibility_review.json",
        "media_type": "application/json",
        "artifact_role": "frozen_review",
    })

    files = sorted((_size(out, entry) for entry in entries), key=lambda e: e["path"])
    isolation = check_isolation()
    manifest_doc: Dict[str, Any] = {
        "schema_version": generate.SCHEMA_VERSION,
        "contract_version": codes.CONTRACT_VERSION,
        "seed": seed,
        "upstream_commit": generate.UPSTREAM_COMMIT,
        "generator_commit": generate.generator_commit(),
        "generator_source_digest": generate.generator_source_digest(),
        "fixtures_digest": fixtures_digest(),
        "environment_digest": generate.environment_digest(),
        "digest_method_version": generate.DIGEST_METHOD_VERSION,
        "counts": {
            "content": sum(1 for row in FIXTURES if row["fixture_kind"] == "content"),
            "technical": sum(1 for row in FIXTURES if row["fixture_kind"] == "technical"),
            "file": len(FIXTURES),
        },
        "isolation": {key: isolation[key] for key in
                      ("vendor_overlap", "source_family_overlap", "doc_id_overlap")},
        "files": files,
    }
    _write_json(out / "manifest.json", manifest_doc)

    return {
        "out": str(out),
        "seed": seed,
        "content": manifest_doc["counts"]["content"],
        "technical": manifest_doc["counts"]["technical"],
        "files": len(files) + 1,
        "bytes": sum(entry["bytes"] for entry in files),
        "fixtures_digest": manifest_doc["fixtures_digest"],
    }


# ---------------------------------------------------------------------------
# Checking a fixture tree
# ---------------------------------------------------------------------------


def _digest_failure(rel: str, reference_file: Path, candidate_file: Path, media: str,
                    failures: List[str]) -> bool:
    """Compare the semantic and pixel summaries; report a parser failure.

    A changed file can stop being readable at all.  That is a failure like any
    other, not an error that stops the check.
    """
    from . import generate

    try:
        failures.extend(generate._compare_digests(rel, reference_file, candidate_file, media))
    except Exception as exc:  # a corrupt candidate must not abort the check
        failures.append(f"{rel}: the summary cannot be read ({type(exc).__name__}: {exc})")
        return True
    return False


def check_fixtures(out_dir: Path, reference_manifest: Path) -> Dict[str, Any]:
    """Compare a fixture tree with a reference manifest; never writes.

    Returns ``{"ok", "failures", "checks", "counts"}``.  The reference
    manifest and the reference files are the only baseline.

    ``reports/legibility_review.json`` is a frozen record, not a generated
    artifact.  The reference manifest must match the reference file, and the
    record must be internally consistent: a pending record leaves the
    reviewer and the time empty, and a confirmed record names both.  A newly
    generated candidate writes the record as pending, so the candidate copy
    is never compared byte for byte with a signed reference.
    """
    from . import generate

    candidate = Path(out_dir)
    reference_manifest = Path(reference_manifest)
    reference_dir = reference_manifest.parent
    failures: List[str] = []
    counts = {"missing": 0, "extra": 0, "generated_total": 0, "generated_compared": 0,
              "frozen_total": 0, "frozen_verified": 0,
              "semantic_compared": 0, "pdf_pixel_compared": 0, "image_pixel_compared": 0}
    checks: Dict[str, Any] = {}

    if not reference_manifest.is_file():
        return {"ok": False,
                "failures": [f"the reference fixture manifest does not exist: {reference_manifest}"],
                "checks": checks, "counts": counts}
    candidate_manifest_path = candidate / "manifest.json"
    if not candidate_manifest_path.is_file():
        return {"ok": False,
                "failures": [f"no manifest in the candidate fixture tree: {candidate_manifest_path}"],
                "checks": checks, "counts": counts}

    reference = json.loads(reference_manifest.read_text(encoding="utf-8"))
    produced = json.loads(candidate_manifest_path.read_text(encoding="utf-8"))

    # 1. The file set.
    expected = {entry["path"] for entry in reference["files"]} | {"manifest.json"}
    actual = {path.relative_to(candidate).as_posix()
              for path in candidate.rglob("*") if path.is_file()}
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    counts["missing"], counts["extra"] = len(missing), len(extra)
    if missing:
        failures.append("missing fixture files: " + ", ".join(missing[:10]))
    if extra:
        failures.append("undeclared fixture files: " + ", ".join(extra[:10]))
    checks["file_set"] = {"missing": missing, "extra": extra}

    # 2. Byte equality of every declared artifact.
    for entry in sorted(reference["files"], key=lambda e: e["path"]):
        rel = entry["path"]
        reference_file = reference_dir / rel
        candidate_file = candidate / rel
        if not reference_file.is_file():
            failures.append(f"{rel}: the reference file is missing")
            continue
        if entry["artifact_role"] == "runtime_report":
            if not candidate_file.is_file():
                failures.append(f"{rel}: the fixture run report is missing")
            continue
        if entry["artifact_role"] == "frozen_review":
            counts["frozen_total"] += 1
            data = reference_file.read_bytes()
            if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                failures.append(f"{rel}: the reference manifest does not match the frozen record")
                continue
            if len(data) != entry.get("bytes"):
                failures.append(f"{rel}: the frozen record byte count is stale")
                continue
            try:
                record = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                failures.append(f"{rel}: the frozen record is not readable JSON")
                continue
            status = record.get("status")
            if status == "pending_human_confirmation":
                if record.get("reviewer") is not None or record.get("reviewed_at") is not None:
                    failures.append(
                        f"{rel}: a pending record must leave reviewer and reviewed_at empty"
                    )
                    continue
            elif status == "confirmed":
                if not record.get("reviewer") or not record.get("reviewed_at"):
                    failures.append(f"{rel}: a confirmed record must name the reviewer and the time")
                    continue
            else:
                failures.append(f"{rel}: the status {status!r} is not supported")
                continue
            counts["frozen_verified"] += 1
            continue
        counts["generated_total"] += 1
        if hashlib.sha256(reference_file.read_bytes()).hexdigest() != entry["sha256"]:
            failures.append(f"{rel}: the reference manifest does not match the reference file")
        if not candidate_file.is_file():
            continue
        counts["generated_compared"] += 1
        if hashlib.sha256(candidate_file.read_bytes()).hexdigest() != hashlib.sha256(
                reference_file.read_bytes()).hexdigest():
            failures.append(f"{rel}: the bytes differ from the reference")
            continue
        media = entry.get("media_type")
        # Byte equality is the whole check for a technical fixture: those
        # files are deliberately unreadable, so a semantic or pixel comparison
        # would report a parser error instead of a difference.
        if rel.startswith("technical/"):
            continue
        if media == "application/pdf":
            counts["semantic_compared"] += 1
            counts["pdf_pixel_compared"] += 1
            if _digest_failure(rel, reference_file, candidate_file, media, failures):
                continue
        elif media == "image/png" and rel.startswith("previews/"):
            counts["image_pixel_compared"] += 1
            if _digest_failure(rel, reference_file, candidate_file, media, failures):
                continue

    # 3. The fixture header fields and the fixture table itself.
    version_failures: List[str] = []
    for field in ("schema_version", "contract_version", "digest_method_version",
                  "upstream_commit", "fixtures_digest", "seed", "counts", "isolation"):
        if reference.get(field) != produced.get(field):
            version_failures.append(f"{field} differs")
    failures.extend(version_failures)
    checks["versions"] = {"failures": version_failures}

    table_failures: List[str] = []
    if produced.get("fixtures_digest") != fixtures_digest():
        table_failures.append("fixtures_digest does not match the current fixture table")
    try:
        check_isolation()
    except FixtureError as exc:
        table_failures.append(str(exc))
    failures.extend(table_failures)
    checks["fixture_table"] = {"failures": table_failures}

    return {"ok": not failures, "failures": failures, "checks": checks, "counts": counts}


__all__ = [
    "BY_ID",
    "CONTENT_COUNT",
    "FILE_COUNT",
    "FIXTURE_CUSTOMERS",
    "FIXTURE_LIMITS",
    "FIXTURE_PASSWORD",
    "FIXTURE_SEED",
    "FIXTURE_SOURCE_FAMILIES",
    "FIXTURE_VENDORS",
    "FIXTURES",
    "FixtureCustomer",
    "FixtureError",
    "FixtureVendor",
    "TECHNICAL_COUNT",
    "UNREADABLE_FIELDS",
    "build_fixture_gold",
    "check_fixtures",
    "check_isolation",
    "fixture_rows",
    "fixture_spec",
    "fixtures_digest",
    "generate_boundary_fixtures",
    "hidden_truth",
    "object_level_cases",
    "page_language",
    "render_fixture",
]
