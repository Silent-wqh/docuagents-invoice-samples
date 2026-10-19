"""Upstream-compatibility probe for the invoice-extraction benchmark.

This module answers one question, on the pinned upstream commit
``3f8864d0d96103a279b5320000c69a71068edd14``: which facts (F1-F17 of the
DocuAgentS sample-set execute plan) hold, which do not, and which are blocked.

Rules that shape the code:

* The probe builds its own ``DocSpec`` inputs.  It does not edit
  ``generator/``, ``scorer/``, ``corpus/`` or ``examples/``.
* Every conclusion comes from the *printed output*: the renderer's bytes,
  the layout's ``Page.ops`` / ``Page.tables`` / ``page_text()``, and the PDF
  text layer read back with pdfplumber.  Asserting on ``spec`` fields alone
  is not evidence.
* Money is re-checked from the printed string with ``Decimal``; an internal
  ``float`` is never treated as the amount a reader sees.
* A failed fact is recorded as ``fail`` with the observed value.  Nothing is
  rewritten to make a check pass.

Run ``python -m docuagents_gen.probe`` to write
``reports/upstream_compatibility.json``.  No network access is needed.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import re
import sys
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pdfplumber  # noqa: E402
import pypdfium2 as pdfium  # noqa: E402

from generator.corpus_v1 import Factory, build_corpus  # noqa: E402
from generator.degrade import PHOTO_KINDS, SCAN_KINDS  # noqa: E402
from generator.generate import render_document  # noqa: E402
from generator.handwriting import hand_font_fingerprint, hand_font_path  # noqa: E402
from generator.layout import Page, Text, build_pages, page_text  # noqa: E402
from generator.spec import (  # noqa: E402
    DATE_STYLES,
    DocSpec,
    Layout,
    Line,
    MoneyStyle,
    Party,
    Render,
    Tax,
    US_NUM,
    fmt_date,
    money,
)

PINNED_COMMIT = "3f8864d0d96103a279b5320000c69a71068edd14"
BASELINE_DATE = date(2026, 3, 4)
REPORT_PATH = ROOT / "reports" / "upstream_compatibility.json"

UNKNOWN = "UNKNOWN"
SUPPORTED = "SUPPORTED"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _party(name: str) -> Party:
    return Party(name, ("1 Probe Way", "Columbus, OH 43201"), "US")


VENDOR = _party("Probe Supply LLC")
CUSTOMER = _party("Probe Buyer Inc")


def usd_style(**kw: Any) -> MoneyStyle:
    return replace(MoneyStyle("$", "prefix", False, "minus", US_NUM), **kw)


def base_spec(**kw: Any) -> DocSpec:
    """A normal US invoice: explicit unambiguous date, currency header, US tax."""
    fields: Dict[str, Any] = dict(
        doc_id="probe-normal",
        category="probe",
        difficulty="easy",
        description="Normal US invoice with an explicit date and a currency header.",
        vendor=VENDOR,
        customer=CUSTOMER,
        issue_date=BASELINE_DATE,
        currency="USD",
        money_style=usd_style(),
        date_style="USLONG",
        title="INVOICE",
        number="PROBE-0001",
        number_label="Invoice #",
        due_date=date(2026, 4, 3),
        terms="30 days",
        lines=(
            Line("Widget A", 100.00, qty=4, unit_price=25.00),
            Line("Widget B", 62.50, qty=2, unit_price=31.25),
        ),
        taxes=(Tax("Sales Tax (6%)", 9.75, 0.06),),
        layout=Layout(show_currency_header=True, table="band", header="left"),
    )
    fields.update(kw)
    return DocSpec(**fields)


def build(spec: DocSpec) -> List[Page]:
    return build_pages(spec)


def rendered(spec: DocSpec, seed: int = 42) -> Tuple[bytes, str, int, Dict]:
    return render_document(spec, seed)


def pdf_text(data: bytes, pages: Optional[Sequence[int]] = None) -> List[str]:
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        idx = range(len(pdf.pages)) if pages is None else pages
        return [pdf.pages[i].extract_text() or "" for i in idx]


def pdf_lines(data: bytes) -> List[str]:
    out: List[str] = []
    for text in pdf_text(data):
        out.extend(text.splitlines())
    return out


def printed_rows(pages: Sequence[Page]) -> List[str]:
    """Rows of the printed page as the layout says a reader would see them."""
    out: List[str] = []
    for page in pages:
        out.extend(page_text(page).splitlines())
    return out


def table_rows(pages: Sequence[Page]) -> List[List[str]]:
    rows: List[List[str]] = []
    for page in pages:
        for table in page.tables:
            rows.extend(table.rows)
    return rows


def table_headers(pages: Sequence[Page]) -> List[str]:
    for page in pages:
        for table in page.tables:
            return list(table.headers)
    return []


def find_row(rows: Sequence[str], needle: str, *, exact_label: bool = False) -> Optional[str]:
    """The first printed row that carries ``needle``.

    ``exact_label`` matches the row's label (everything before the amount)
    exactly, so "TOTAL" does not match "Subtotal".
    """
    wanted = needle.casefold()
    for row in rows:
        if exact_label:
            label = re.sub(r"[-($]?\s?[\d][\d,.\s]*$", "", row).strip().rstrip(":").casefold()
            if label == wanted:
                return row
            continue
        if wanted in row.casefold():
            return row
    return None


def amount_string(row: str) -> str:
    """The last money-looking token of a printed row ("Subtotal $162.50")."""
    matches = re.findall(r"-?\$?\s?-?[\d][\d,.\s]*\d|\d", row)
    if not matches:
        raise AssertionError(f"no amount in printed row {row!r}")
    return matches[-1].strip()


def parse_amount(text: str, *, thousands: str = ",", decimal: str = ".") -> Decimal:
    """Parse a printed amount with the document's own number style."""
    t = text.replace("$", "").replace("\u00a0", " ").strip()
    t = t.replace(thousands, "")
    if decimal != ".":
        t = t.replace(decimal, ".")
    t = t.replace(" ", "")
    return Decimal(t)


def money_text(style: MoneyStyle, x: float) -> str:
    """What the layout would print for ``x`` (symbol included)."""
    return style.fmt(x)


def cents(x: float) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# Probe corpus
# ---------------------------------------------------------------------------


def probe_specs() -> Dict[str, DocSpec]:
    """The eight probe inputs the task requires, plus a few companions.

    Every spec is explicit: dates and currency identifiers are unambiguous,
    and amounts use the document's own number style.
    """
    normal = base_spec()

    zero_tax = base_spec(
        doc_id="probe-zero-tax",
        description="Explicit zero-rated tax: the tax row must print 0.00.",
        lines=(Line("Consulting hour", 500.00, qty=5, unit_price=100.00),),
        taxes=(Tax("Sales Tax (0%)", 0.0, 0.0),),
    )

    no_tax = base_spec(
        doc_id="probe-no-tax",
        description="No tax at all: taxes=() must print no tax row.",
        lines=(Line("Consulting hour", 500.00, qty=5, unit_price=100.00),),
        taxes=(),
    )

    no_number = base_spec(
        doc_id="probe-no-number",
        description="No invoice number printed.",
        number=None,
        due_date=None,
        terms=None,
        po_number="PO-12345",
    )

    line_conflict = base_spec(
        doc_id="probe-line-conflict",
        description="Printed quantity times unit price differs from the printed amount.",
        lines=(Line("Mismatched line", 25.00, qty=2, unit_price=10.00),),
        taxes=(Tax("Sales Tax (6%)", 1.50, 0.06),),
    )

    override = base_spec(
        doc_id="probe-overrides",
        description="Printed subtotal and total do not equal the computed values.",
        subtotal_printed=100.00,
        total_printed=120.00,
        should_flag=True,
    )

    two_pages = base_spec(
        doc_id="probe-two-pages",
        description="Two pages of line items; totals on the last page.",
        lines=tuple(Line(f"Line item {i:02d}", 10.00, qty=1, unit_price=10.00) for i in range(6)),
        taxes=(Tax("Sales Tax (6%)", 3.60, 0.06),),
        layout=Layout(rows_per_page=2, carried_forward=True, show_currency_header=True),
    )

    image_one_page = base_spec(
        doc_id="probe-image",
        description="A single-page raster document (a JPEG 'photo').",
        lines=(Line("Photo line", 42.00, qty=1, unit_price=42.00),),
        taxes=(Tax("Sales Tax (6%)", 2.52, 0.06),),
        render=Render(mode="image", fmt="jpg"),
    )

    return {
        "normal": normal,
        "zero_tax": zero_tax,
        "no_tax": no_tax,
        "no_number": no_number,
        "line_conflict": line_conflict,
        "overrides": override,
        "two_pages": two_pages,
        "image": image_one_page,
    }


# ---------------------------------------------------------------------------
# Result plumbing
# ---------------------------------------------------------------------------


@dataclass
class Check:
    fact: str
    status: str = "pass"          # pass | fail | blocked
    location: str = ""
    inputs: Dict[str, Any] = field(default_factory=dict)
    observed: Dict[str, Any] = field(default_factory=dict)
    assertions: List[str] = field(default_factory=list)
    failure: Optional[str] = None
    affects: List[str] = field(default_factory=list)
    adaptation: str = ""
    differences: List[Dict[str, str]] = field(default_factory=list)

    def ok(self, text: str) -> None:
        self.assertions.append(f"PASS: {text}")

    def bad(self, text: str) -> None:
        self.assertions.append(f"FAIL: {text}")
        if self.status == "pass":
            self.status = "fail"
            self.failure = text

    def block(self, text: str) -> None:
        self.assertions.append(f"BLOCKED: {text}")
        self.status = "blocked"
        self.failure = text

    def difference(self, title: str, severity: str, detail: str, affects: Sequence[str],
                   adaptation: str) -> None:
        """A confirmed upstream behaviour that constrains this project's construction.

        A difference is not a failure: the fact above was verified.  It is the
        consequence that the sample builder must handle.
        """
        self.differences.append({
            "title": title,
            "severity": severity,        # high | medium | low
            "detail": detail,
            "affected_sample_constructions": list(affects),
            "adaptation": adaptation,
        })
        for a in affects:
            if a not in self.affects:
                self.affects.append(a)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "fact": self.fact,
            "commit": PINNED_COMMIT,
            "status": self.status,
            "location": self.location,
            "inputs": self.inputs,
            "observed": self.observed,
            "assertions": self.assertions,
            "failure": self.failure,
            "differences": self.differences,
            "affected_sample_constructions": self.affects,
            "adaptation": self.adaptation,
        }


class Prober:
    def __init__(self, check_all: bool = False) -> None:
        self.check_all = check_all
        self.specs = probe_specs()
        self.cache: Dict[str, Tuple[bytes, str, int, Dict]] = {}
        self.checks: Dict[str, Check] = {}

    def render(self, key: str) -> Tuple[bytes, str, int, Dict]:
        if key not in self.cache:
            self.cache[key] = rendered(self.specs[key])
        return self.cache[key]

    def pages(self, key: str) -> List[Page]:
        return build(self.specs[key])

    def check(self, fact: str, location: str) -> Check:
        c = Check(fact=fact, location=location)
        self.checks[fact] = c
        return c


# ---------------------------------------------------------------------------
# F1 .. F17
# ---------------------------------------------------------------------------


def f01_defaults(p: Prober) -> Check:
    c = p.check("F1", "generator/corpus_v1.py: Factory.invoice, Factory.vat, DATE_STYLES")
    f = Factory(11)
    us = f.invoice("f1-us", "probe", "easy", "US default", country="US")
    c.inputs = {"factory_seed": 11, "call": 'Factory.invoice(country="US")',
                "date_used": "spec.issue_date", "amount_sentinel": "kw lines/taxes override"}
    from generator.corpus_v1 import DATE_STYLES as CORPUS_DATE_STYLES

    c.observed["currency"] = us.currency
    c.observed["date_style"] = us.date_style
    c.observed["date_style_choices"] = list(CORPUS_DATE_STYLES["US"])
    c.observed["tax_labels"] = [t.label for t in us.taxes]
    c.observed["money_style"] = repr(us.money_style)

    if us.currency == "USD":
        c.ok("country='US' gives currency USD")
    else:
        c.bad(f"country='US' gives currency {us.currency!r}, not USD")
    if us.date_style in DATE_STYLES and DATE_STYLES[us.date_style] in ("%m/%d/%Y", "%B %d, %Y"):
        c.ok(f"US date style is {us.date_style!r} ({DATE_STYLES[us.date_style]})")
    else:
        c.bad(f"US default date style {us.date_style!r} is not a US format")
    if len(us.taxes) == 1 and us.taxes[0].label.startswith("Sales Tax"):
        c.ok(f"US default tax is {us.taxes[0].label!r} with rate {us.taxes[0].rate}")
    else:
        c.bad(f"US default taxes are {[t.label for t in us.taxes]!r}")
    if us.date_style in CORPUS_DATE_STYLES["US"]:
        c.ok("the US default date style is drawn from DATE_STYLES['US']")
        c.difference(
            "The US default date style is random between US and USLONG",
            "medium",
            "DATE_STYLES['US'] is ['US', 'USLONG'], and Factory.invoice() picks one at random; "
            "the same 2026-03-04 prints as 03/04/2026 or March 4, 2026. A US date can also be "
            "ambiguous when Factory.ambiguous_issue() is used.",
            ["A-D 原件（正常样本日期）"],
            "pass date_style='USLONG' explicitly and print an unambiguous month name; never rely "
            "on Factory's default US date style",
        )
    else:
        c.bad(f"{us.date_style!r} is not in DATE_STYLES['US']")

    # kw overrides: the normal probe passes explicit lines, tax and number.
    normal = p.specs["normal"]
    rendered_bytes, ext, npages, _ = p.render("normal")
    lines = printed_rows(p.pages("normal"))
    c.observed["normal_rendered"] = {"ext": ext, "pages": npages,
                                     "sha256_16": hashlib.sha256(rendered_bytes).hexdigest()[:16]}
    c.observed["normal_date_line"] = find_row(lines, "Date:")
    c.observed["normal_currency_line"] = find_row(lines, "Currency:")
    rows = table_rows(p.pages("normal"))
    c.observed["normal_table_rows"] = rows
    if normal.number == "PROBE-0001" and find_row(lines, "PROBE-0001"):
        c.ok("kw number override is printed")
    else:
        c.bad("kw number override is not printed")
    if find_row(lines, "March 4, 2026"):
        c.ok("explicit date 2026-03-04 prints as 'March 4, 2026' (USLONG)")
    else:
        c.bad("explicit date 2026-03-04 is not printed as 'March 4, 2026'")
    if rows == [["Widget A", "4", "25.00", "100.00"], ["Widget B", "2", "31.25", "62.50"]]:
        c.ok("kw lines override is printed unchanged in the table")
    else:
        c.bad(f"kw lines printed as {rows!r}")
    if find_row(lines, "Currency: USD"):
        c.ok("show_currency_header prints 'Currency: USD'")
    else:
        c.bad("show_currency_header did not print 'Currency: USD'")

    # The defaults, printed: a default-US invoice must still be unambiguous.
    bytes_default, ext_d, n_d, _ = rendered(us, 42)
    default_rows = printed_rows(build(us))
    c.observed["default_us_date_line"] = find_row(default_rows, "Date:")
    c.affects = ["A-D 原件（正常样本）", "所有 US 默认发票"]
    c.adaptation = "显式传 date_style='USLONG'、number、lines 与 taxes；不依赖 Factory 默认的随机日期格式。"
    return c


def f02_goods_lines(p: Prober) -> Check:
    c = p.check("F2", "generator/corpus_v1.py: Factory.goods_lines; generator/spec.py: money()")
    f = Factory(23)
    lines = f.goods_lines("supply", 3)
    c.inputs = {"factory_seed": 23, "call": "Factory.goods_lines('supply', 3)"}
    c.observed["lines"] = [
        {"description": ln.description, "qty": ln.qty, "unit_price": ln.unit_price,
         "amount": ln.amount,
         "decimal_qty_x_price": str(cents(Decimal(str(ln.qty)) * Decimal(str(ln.unit_price))))}
        for ln in lines
    ]
    for ln in lines:
        if ln.qty is None or ln.unit_price is None:
            c.bad(f"line {ln.description!r} has no qty/unit_price")
            continue
        expect = Decimal(str(ln.qty)) * Decimal(str(ln.unit_price))
        if cents(expect) == cents(ln.amount):
            c.ok(f"{ln.description!r}: qty x unit_price = amount ({ln.qty} x {ln.unit_price} = {ln.amount})")
        else:
            c.bad(f"{ln.description!r}: qty x unit_price {expect} != amount {ln.amount}")

    # Printed values: the printed strings are the amount of record.
    spec = base_spec(
        doc_id="probe-f2",
        lines=(Line("Scale line", money(3 * 0.125), qty=3, unit_price=0.125),),
        taxes=(Tax("Sales Tax (6%)", 0.02, 0.06),),
    )
    pages = build(spec)
    rows = table_rows(pages)
    printed = printed_rows(pages)
    c.observed["half_cent_printed_row"] = rows
    c.observed["half_cent_totals"] = [r for r in printed if "Subtotal" in r or "TOTAL" in r]
    if rows and rows[0][1:4] == ["3", "0.13", "0.38"]:
        c.ok("printed qty/unit/amount are 3 / 0.13 / 0.38")
    else:
        c.bad(f"half-cent line printed as {rows!r}")
    if Decimal("3") * Decimal("0.13") == Decimal("0.39"):
        c.observed["decimal_recompute"] = "3 x 0.13 = 0.39, printed amount 0.38"
        c.ok("the printed rows do not multiply exactly (0.13 x 3 = 0.39 vs printed 0.38)")
        c.difference(
            "Printed qty x unit price can miss the printed amount by one cent",
            "medium",
            "goods_lines() rounds the unit price to the cent, then multiplies the rounded price "
            "and rounds again; 0.125 x 3 prints 0.13 / 3 / 0.38.",
            ["A-D 原件（逐行 qty / unit price / amount）", "C 类行级矛盾（金额重算）"],
            "compare with a 1-cent tolerance; never rebuild the amount from the printed unit price alone",
        )
    c.affects = ["A-D 原件（逐行 qty / unit price / amount）", "C 类行级矛盾（金额重算）"]
    c.adaptation = ("gold 的 amount 取印刷值并用 Decimal 重算 qty x unit_price 时，允许 1 分差异；"
                    "内部 float 值不得当作金额真值。")
    return c


def f03_columns(p: Prober) -> Check:
    c = p.check("F3", "generator/layout.py: _Builder.columns, _Builder.table")
    pages = p.pages("normal")
    headers = table_headers(pages)
    rows = table_rows(pages)
    text = page_text(pages[0])
    c.inputs = {"spec": "probe-normal", "columns": "std"}
    c.observed["headers"] = headers
    c.observed["rows"] = rows
    c.observed["page_text_line"] = [ln for ln in text.splitlines() if ln.startswith("Description")]

    if headers == ["Description", "Qty", "Unit Price", "Amount"]:
        c.ok("std column set prints Description / Qty / Unit Price / Amount")
    else:
        c.bad(f"std headers are {headers!r}")
    if len(rows) == 2 and all(len(r) == 4 for r in rows):
        c.ok("every row has four cells")
    else:
        c.bad(f"rows/cells mismatch: {rows!r}")

    # No clipping: long descriptions wrap instead of being cut.
    long_desc = "Wide format plotter paper roll 914 mm x 50 m, 90 gsm, boxed"
    wrapped = base_spec(
        doc_id="probe-f3-wrap",
        lines=(Line(long_desc, 250.00, qty=2, unit_price=125.00),),
        taxes=(Tax("Sales Tax (6%)", 15.00, 0.06),),
    )
    wpages = build(wrapped)
    wraptext = page_text(wpages[0])
    desc_ops = [op for op in wpages[0].ops if isinstance(op, Text) and op.text and op.text.startswith("Wide")]
    c.observed["wrapped_description"] = [op.text for op in desc_ops]
    joined = " ".join(op.text for op in desc_ops)
    if joined == long_desc:
        c.ok("a long description is wrapped across lines and every word is printed")
    else:
        c.bad(f"long description printed as {joined!r}")

    # Column names drive bindings: check the amount column is last and right-aligned.
    amount_ops = [op for op in pages[0].ops if isinstance(op, Text) and
                  op.text in ("100.00", "62.50")]
    c.observed["amount_op_align"] = [(op.text, op.align) for op in amount_ops]
    if amount_ops and all(op.align == "right" for op in amount_ops):
        c.ok("amount cells are right-aligned")
    else:
        c.bad("amount cells are not right-aligned")
    c.affects = ["A-D 原件（列绑定规则）", "表格标注与 gold 行匹配"]
    c.adaptation = "列名以 'Description / Qty / Unit Price / Amount' 为契约键；列名变化时更新绑定规则。"
    return c


def f04_totals(p: Prober) -> Check:
    c = p.check("F4", "generator/layout.py: _Builder.totals_rows, totals, meta_rows")
    pages = p.pages("normal")
    rows = [r for r in printed_rows(pages) if "$" in r]
    c.inputs = {"specs": ["probe-normal", "probe-no-tax", "probe-zero-tax"]}
    c.observed["normal_total_rows"] = rows
    if any("Sales Tax" in r for r in rows):
        c.ok("the tax row is printed when taxes is non-empty")
    else:
        c.bad("the tax row is missing")

    no_tax_pages = p.pages("no_tax")
    no_tax_rows = printed_rows(no_tax_pages)
    c.observed["no_tax_rows"] = no_tax_rows
    tax_like = [r for r in no_tax_rows if re.search(r"tax|vat|sales", r, re.I)]
    if not tax_like:
        c.ok("taxes=() prints no tax row and no other tax wording")
    else:
        c.bad(f"taxes=() still prints tax wording: {tax_like!r}")

    zero_pages = p.pages("zero_tax")
    zero_rows = [r for r in printed_rows(zero_pages) if "$" in r]
    c.observed["zero_tax_rows"] = zero_rows
    zero = find_row(zero_rows, "Sales Tax")
    if zero and zero.rstrip().endswith("$0.00"):
        c.ok("an explicit zero Tax prints $0.00")
    else:
        c.bad(f"explicit zero tax printed as {zero!r}")

    # Rendered PDF agrees with the layout rows.
    data, _, _, _ = p.render("no_tax")
    pdf_rows = pdf_lines(data)
    pdf_tax = [r for r in pdf_rows if re.search(r"tax|vat|sales", r, re.I)]
    c.observed["no_tax_pdf_tax_rows"] = pdf_tax
    if not pdf_tax:
        c.ok("the rendered PDF of the no-tax invoice has no tax wording")
    else:
        c.bad(f"rendered PDF still has tax wording: {pdf_tax!r}")

    total_rows = [r for r in no_tax_rows if r.strip().startswith("TOTAL")]
    c.observed["no_tax_total_row"] = total_rows
    if total_rows and total_rows[0].endswith("$500.00"):
        c.ok("no-tax total is the line sum, $500.00")
    else:
        c.bad(f"no-tax total row is {total_rows!r}")
    c.affects = ["B 类（无税样本）", "A 类正常样本总额区"]
    c.adaptation = "无税样本必须同时检查票面无 tax/VAT/Sales 字样，不能只检查 totals_rows。"
    return c


def f05_zero_rate(p: Prober) -> Check:
    c = p.check("F5", "generator/corpus_v1.py: Factory.vat (rate or pick(...)), Factory.invoice(tax_rate=...)")
    f = Factory(31)
    zero_us = f.invoice("f5-zero", "probe", "easy", "zero rate", country="US", tax_rate=0)
    none_us = f.invoice("f5-none", "probe", "easy", "default rate", country="US")
    zero_za = f.invoice("f5-zero-za", "probe", "easy", "zero rate", country="ZA", tax_rate=0)
    c.inputs = {"calls": ["Factory.invoice(country='US', tax_rate=0)",
                          "Factory.invoice(country='US')",
                          "Factory.invoice(country='ZA', tax_rate=0)"]}
    c.observed["us_tax_rate_0"] = [(t.label, t.amount, t.rate) for t in zero_us.taxes]
    c.observed["us_default"] = [(t.label, t.amount, t.rate) for t in none_us.taxes]
    c.observed["za_tax_rate_0"] = [(t.label, t.amount, t.rate) for t in zero_za.taxes]

    if none_us.taxes and none_us.taxes[0].rate:
        c.ok(f"default US tax rate is drawn from the random list ({none_us.taxes[0].rate})")
    else:
        c.bad("default US tax rate is not drawn from the random list")
    # The fact under test is the defect, so a confirmed override is a PASS of F5.
    if zero_us.taxes and zero_us.taxes[0].rate:
        c.ok(f"tax_rate=0 for US is replaced by the random rate {zero_us.taxes[0].rate}, as F5 warns")
        c.difference(
            "US tax_rate=0 is silently replaced by a random rate",
            "medium",
            "Factory.vat() evaluates `rate or self.pick([...])`; 0.0 is falsy, so an explicit "
            "zero rate for country='US' becomes 6.00%, 6.25%, 7.00%, 8.25% or 8.875%.",
            ["B 类明确零税额样本"],
            "never express a zero rate through Factory.invoice(tax_rate=0); pass an explicit "
            "Tax('Sales Tax (0%)', 0.0, 0.0) and set taxes=(...)",
        )
    else:
        c.bad("tax_rate=0 for US is kept")
    if zero_za.taxes and zero_za.taxes[0].rate == 0 and zero_za.taxes[0].amount == 0:
        c.ok("country='ZA' keeps tax_rate=0 (VAT 0%)")
    else:
        c.bad(f"ZA tax_rate=0 is not kept: {[(t.label, t.amount, t.rate) for t in zero_za.taxes]!r}")

    # The explicit Tax override prints $0.00.
    zero_rows = [r for r in printed_rows(p.pages("zero_tax")) if "$" in r]
    c.observed["explicit_tax_printed_rows"] = zero_rows
    zero = find_row(zero_rows, "Sales Tax")
    if zero and zero.rstrip().endswith("$0.00"):
        c.ok("passing Tax('Sales Tax (0%)', 0.0, 0.0) prints $0.00")
    else:
        c.bad(f"explicit zero Tax printed as {zero!r}")

    data, _, _, _ = p.render("zero_tax")
    c.observed["zero_tax_pdf_line"] = find_row(pdf_lines(data), "Sales Tax")
    c.affects = ["B 类明确零税额样本"]
    c.adaptation = ("零税额一律直接传入显式 Tax(label, 0.0, 0.0) 并设置 taxes=(...)；"
                    "不要通过 Factory 的 tax_rate=0 表达。")
    return c


def f06_validate(p: Prober) -> Check:
    c = p.check("F6", "generator/spec.py: DocSpec.validate; generator/corpus_v1.py: _validate")
    c.inputs = {"specs": ["probe-line-conflict", "probe-overrides", "published hard-*"]}

    def probe_validate(name: str, spec: DocSpec) -> Optional[str]:
        try:
            spec.validate()
            return None
        except ValueError as exc:
            return str(exc)

    conflict = p.specs["line_conflict"]
    err = probe_validate("line_conflict", conflict)
    c.observed["line_conflict_validate_error"] = err
    if err and ("does not add up" in err or "qty x price != amount" in err):
        c.ok("validate() rejects a spec whose printed figures do not add up")
    else:
        c.bad(f"validate() accepted a contradictory spec (error: {err!r})")

    # Isolate the line-product rule: make the totals consistent, keep the line wrong.
    line_only = replace(
        conflict,
        doc_id="probe-line-product-only",
        subtotal_printed=25.00,
        total_printed=26.50,
    )
    err = probe_validate("line_product", line_only)
    c.observed["line_product_validate_error"] = err
    if err and "qty x price != amount" in err:
        c.ok("validate() rejects a line whose qty x price != amount even when the totals add up")
    else:
        c.bad(f"validate() did not reach the line-product rule (error: {err!r})")

    # The line-conflict doc cannot be rendered through the corpus path because
    # _validate() rejects it, but the layout itself still renders.
    conflict_pages = build(conflict)
    c.observed["line_conflict_printed_row"] = table_rows(conflict_pages)
    c.observed["line_conflict_is_rendered"] = True
    if table_rows(conflict_pages) == [["Mismatched line", "2", "10.00", "25.00"]]:
        c.ok("the contradictory line still renders (2 x 10.00 printed beside 25.00)")
    else:
        c.bad(f"contradictory line rendered as {table_rows(conflict_pages)!r}")

    overrides = p.specs["overrides"]
    err = probe_validate("overrides", overrides)
    c.observed["overrides_validate_error"] = err
    if err is None:
        c.ok("validate() accepts printed subtotal/total overrides when should_flag=True")
    else:
        c.bad(f"validate() rejects valid overrides: {err}")

    quiet = replace(overrides, should_flag=False)
    err = probe_validate("overrides_no_flag", quiet)
    c.observed["overrides_no_flag_validate_error"] = err
    if err and "does not add up" in err:
        c.ok("validate() rejects inconsistent arithmetic without should_flag")
    else:
        c.bad(f"validate() accepted inconsistent arithmetic without should_flag (error: {err!r})")

    # Published corpus: every hard-* document with should_flag survives _validate.
    corpus = {s.doc_id: s for s in build_corpus(42)}
    hard = {k: v for k, v in corpus.items() if v.category == "hard"}
    c.observed["published_hard_flagged"] = sorted(k for k, v in hard.items() if v.should_flag)
    ok_all = True
    for doc_id, spec in hard.items():
        if "wrong_vat" in spec.tags:
            continue
        try:
            spec.validate()
        except ValueError as exc:
            ok_all = False
            c.bad(f"published {doc_id} fails validate(): {exc}")
    if ok_all:
        c.ok("every published hard document except the wrong-VAT one passes validate()")
    c.observed["wrong_vat_exempt"] = [k for k, v in hard.items() if "wrong_vat" in v.tags]

    # C-class rule of this project: strict validation is done by this package.
    c.affects = ["C 类行级矛盾样本", "所有覆盖小计/总额的样本"]
    c.adaptation = ("C 类样本由本项目自己的严格校验器检查，不用上游 validate() 作为唯一闸门；"
                    "上游 validate() 只作为额外检查，并用 should_flag=True 放行已知不一致。")
    return c


def f07_overrides(p: Prober) -> Check:
    c = p.check("F7", "generator/spec.py: subtotal/total properties, expected(); generator/layout.py: totals_rows")
    spec = p.specs["overrides"]
    c.inputs = {"spec": "probe-overrides", "subtotal_printed": 100.0, "total_printed": 120.0}
    c.observed["computed_line_sum"] = spec.line_sum
    c.observed["subtotal"] = spec.subtotal
    c.observed["total"] = spec.total
    c.observed["computed_total"] = spec.computed_total
    c.observed["expected_subtotal"] = spec.expected().subtotal
    c.observed["expected_total"] = spec.expected().total

    pages = build(spec)
    rows = printed_rows(pages)
    c.observed["printed_totals"] = [r for r in rows if "Subtotal" in r or "TOTAL" in r]
    sub_row = find_row(rows, "Subtotal", exact_label=True)
    total_row = find_row(rows, "TOTAL", exact_label=True)
    if sub_row and sub_row.rstrip().endswith("$100.00"):
        c.ok("the printed subtotal is the override $100.00")
    else:
        c.bad(f"printed subtotal is {sub_row!r}")
    if total_row and total_row.rstrip().endswith("$120.00"):
        c.ok("the printed total is the override $120.00")
    else:
        c.bad(f"printed total is {total_row!r}")
    if spec.expected().subtotal == 100.0 and spec.expected().total == 120.0:
        c.ok("expected() returns the overrides, not the computed values")
    else:
        c.bad(f"expected() returns {spec.expected().subtotal}/{spec.expected().total}")

    data, _, _, _ = p.render("overrides")
    pdf = pdf_lines(data)
    c.observed["pdf_subtotal"] = find_row(pdf, "Subtotal", exact_label=True)
    c.observed["pdf_total"] = find_row(pdf, "TOTAL", exact_label=True)
    if find_row(pdf, "Subtotal", exact_label=True) and find_row(pdf, "Subtotal", exact_label=True).rstrip().endswith("$100.00"):
        c.ok("the rendered PDF prints the override subtotal")
    else:
        c.bad(f"rendered PDF subtotal is {find_row(pdf, 'Subtotal', exact_label=True)!r}")
    c.affects = ["C 类覆盖小计/总额样本", "gold 的 subtotal / total"]
    c.adaptation = "覆盖样本的 gold 必须取 subtotal_printed / total_printed，并与票面字符串逐字一致。"
    return c


def f08_expected(p: Prober) -> Check:
    c = p.check("F8", "generator/spec.py: Expected, ExpectedLine, DocSpec.expected")
    spec = base_spec(
        doc_id="probe-f8",
        taxes=(),
        lines=(Line("Service", 250.00, qty=5, unit_price=50.00),),
    )
    expected = spec.expected()
    d = expected.as_dict()
    c.inputs = {"spec": "a one-line, no-tax invoice", "call": "DocSpec.expected().as_dict()"}
    c.observed["expected_dict"] = d
    c.observed["line_keys"] = list(d["lines"][0].keys())
    if "qty" not in d["lines"][0] and "unit_price" not in d["lines"][0]:
        c.ok("ExpectedLine has no qty / unit_price")
    else:
        c.bad(f"ExpectedLine exposes qty/unit_price: {d['lines'][0]!r}")
    c.observed["expected_tax"] = expected.tax
    if expected.tax == 0.0:
        c.ok("an unprinted tax comes out as 0.0, not None")
    else:
        c.bad(f"an unprinted tax comes out as {expected.tax!r}")

    printed = printed_rows(build(spec))
    c.observed["printed_rows_with_tax"] = [r for r in printed if re.search(r"tax|vat", r, re.I)]
    if not [r for r in printed if re.search(r"tax|vat", r, re.I)]:
        c.ok("the same invoice prints no tax row, yet expected().tax is 0.0")
    else:
        c.bad("the no-tax invoice still prints a tax row")

    # Scorer rule: tax is right when missing and expected 0.
    from scorer.score import score_money

    res = score_money("tax", None, expected.tax, magnitude=False, none_is_zero=True)
    c.observed["scorer_tax_none_vs_0"] = res.as_dict()
    if res.correct:
        c.ok("the upstream scorer counts a missing tax as correct when expected tax is 0")
    else:
        c.bad("the upstream scorer rejects a missing tax")

    # Also: a no-number invoice gives invoice_number None; the scorer wants empty.
    no_num = p.specs["no_number"]
    c.observed["no_number_expected"] = no_num.expected().invoice_number
    c.observed["no_number_printed"] = [
        r for r in printed_rows(build(no_num)) if "Invoice" in r or "PO Number" in r]
    c.affects = ["A-D 全部 gold（完整字段与缺失状态）"]
    c.adaptation = ("本项目独立生成 gold：每条行都带 qty / unit_price，未印字段用 null 表示缺失，"
                    "不用 spec.expected() 充当 DocuAgentS gold。")
    return c


def f09_image_single_page(p: Prober) -> Check:
    c = p.check("F9", "generator/generate.py: render_document (image branch)")
    c.inputs = {"specs": ["probe-image (1 page)", "probe-two-pages as image (3 pages)"]}
    data, ext, npages, info = p.render("image")
    c.observed["single_page"] = {"ext": ext, "pages": npages, "info": info,
                                 "sha256_16": hashlib.sha256(data).hexdigest()[:16]}
    if ext == "jpg" and npages == 1:
        c.ok("an image document reports one page")
    else:
        c.bad(f"image document reports ext={ext!r} pages={npages}")

    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        c.observed["pil"] = {"format": img.format, "size": list(img.size), "dpi": list(img.info.get("dpi", ()))}
    if c.observed["pil"]["format"] == "JPEG":
        c.ok("the saved file is a JPEG")

    two = replace(p.specs["two_pages"], doc_id="probe-two-pages-image",
                  render=Render(mode="image", fmt="jpg"))
    err = None
    try:
        rendered(two)
    except ValueError as exc:
        err = str(exc)
    c.observed["two_page_image_error"] = err
    if err and "must be one page" in err:
        c.ok("a multi-page image input is rejected after rendering all pages")
    else:
        c.bad(f"a multi-page image input was not rejected (error: {err!r})")

    # A multi-page PDF is allowed; the check must not be applied to PDFs.
    _, pext, pn, _ = p.render("two_pages")
    c.observed["two_page_pdf"] = {"ext": pext, "pages": pn}
    if pext == "pdf" and pn == 3:
        c.ok("the same content as a PDF renders three pages")
    else:
        c.bad(f"two-page PDF reported {pext}/{pn}")

    # Multi-page image input needs its own PDF fixture.
    pdf_pages = build(replace(two, render=Render(mode="pdf")))
    c.observed["two_page_image_layout_pages"] = len(pdf_pages)
    c.affects = ["D 类降质图（jpg/png 单页）", "多页图像输入（需要独立 PDF 夹具）"]
    c.adaptation = ("所有降质图样本都必须是单页；多页输入单独用 PDF 夹具承载，"
                    "并对最终文件断言单页。")
    return c


def f10_pages_tables(p: Prober) -> Check:
    c = p.check("F10", "generator/layout.py: Page, TableModel, page_text, _Builder.table")
    pages = p.pages("two_pages")
    c.inputs = {"spec": "probe-two-pages (6 lines, rows_per_page=2)"}
    c.observed["page_count"] = len(pages)
    c.observed["page_sizes"] = [[pg.width, pg.height] for pg in pages]
    c.observed["ops_per_page"] = [len(pg.ops) for pg in pages]
    c.observed["tables_per_page"] = [[{"headers": t.headers, "rows": t.rows} for t in pg.tables] for pg in pages]
    c.observed["text_per_page"] = [page_text(pg) for pg in pages]

    if all(pg.ops for pg in pages):
        c.ok("every page exposes ops")
    else:
        c.bad("a page has no ops")
    if all(pg.tables for pg in pages):
        c.ok("every page with line items exposes a TableModel")
    else:
        c.bad("a page has no TableModel")

    # Row -> page mapping and continuation text.
    row_counts = [sum(len(t.rows) for t in pg.tables) for pg in pages]
    c.observed["row_counts_per_page"] = row_counts
    if sum(row_counts) == 6:
        c.ok("all six rows are bound to a page")
    else:
        c.bad(f"row binding lost rows: {row_counts!r}")
    cont = [i + 1 for i, t in enumerate(c.observed["text_per_page"]) if "Continued on next page" in t]
    c.observed["continuation_pages"] = cont
    if cont:
        c.ok("the table prints 'Continued on next page' before a page break")
    else:
        c.bad("no continuation marker before the page break")

    # Coordinates: top-left origin, y is a baseline, units are points.
    first = pages[0]
    texts = [op for op in first.ops if isinstance(op, Text)]
    c.observed["coordinate_sample"] = [
        {"text": op.text, "x": op.x, "y": op.y, "align": op.align} for op in texts[:4]
    ]
    within = all(0 <= op.x <= first.width and 0 <= op.y <= first.height for op in texts)
    if within:
        c.ok("all text coordinates fall inside the page box (points, top-left origin)")
    else:
        c.bad("text coordinates fall outside the page box")
    if all(op.y <= first.height for op in texts) and any(op.y < 60 for op in texts):
        c.ok("y grows downward from the top edge (header text has the smallest y)")
    else:
        c.bad("y does not look like a top-left origin")

    # Row index inside the table model, not the printed page.
    c.observed["table_row_order"] = [r[0] for pg in pages for t in pg.tables for r in t.rows]
    if c.observed["table_row_order"] == [f"Line item {i:02d}" for i in range(6)]:
        c.ok("table rows keep reading order across pages")
    else:
        c.bad(f"table row order is {c.observed['table_row_order']!r}")

    # Repeated text: continuation headers repeat the invoice number.
    repeated = {}
    for pg in pages:
        for op in pg.ops:
            if isinstance(op, Text) and op.text:
                repeated[op.text] = repeated.get(op.text, 0) + 1
    c.observed["repeated_text"] = {k: v for k, v in repeated.items() if v > 1}
    c.affects = ["标注辅助（页号、行号、单元格绑定）"]
    c.adaptation = ("page_text/tables 只作为标注辅助；行索引按表格模型、页号按 Page 顺序记录，"
                    "重复的续页页眉文本必须排除在唯一文本绑定之外。")
    return c


def f11_degraded(p: Prober) -> Check:
    c = p.check("F11", "generator/corpus_v1.py: _degraded, photos_and_scans; generator/degrade.py: PHOTO_KINDS, SCAN_KINDS, PARAMS")
    c.inputs = {"PHOTO_KINDS": list(PHOTO_KINDS), "SCAN_KINDS": list(SCAN_KINDS)}
    corpus = build_corpus(42)
    photos = [s for s in corpus if s.category == "photo"]
    scans = [s for s in corpus if s.category == "scan"]
    c.observed["photo_count"] = len(photos)
    c.observed["scan_count"] = len(scans)
    c.observed["photo_ids"] = [s.doc_id for s in photos]
    c.observed["degradation_params"] = {k: v for k, v in __import__("generator.degrade", fromlist=["PARAMS"]).PARAMS.items()}

    if len(PHOTO_KINDS) == 15 and len(photos) == 45:
        c.ok("15 photo kinds x 3 severities = 45 documents")
    else:
        c.bad(f"photo count mismatch: {len(PHOTO_KINDS)} kinds, {len(photos)} docs")
    if len(SCAN_KINDS) == 5 and len(scans) == 15:
        c.ok("5 scan kinds x 3 severities = 15 documents")
    else:
        c.bad(f"scan count mismatch: {len(SCAN_KINDS)} kinds, {len(scans)} docs")

    severities = {s.render.degradations[0].severity for s in photos + scans}
    c.observed["severities"] = sorted(severities)
    if severities == {"mild", "moderate", "severe"}:
        c.ok("all three severities are used")
    else:
        c.bad(f"severities are {sorted(severities)}")

    # One degraded document, actually rendered: a single JPEG with recorded parameters.
    sample = next(s for s in photos if s.doc_id == "photo-defocus-blur-mild")
    data, ext, npages, info = rendered(sample)
    c.observed["sample"] = {"doc_id": sample.doc_id, "ext": ext, "pages": npages,
                            "base_id": sample.base_id,
                            "degradations": [{"kind": d.kind, "severity": d.severity}
                                             for d in sample.render.degradations],
                            "params": __import__("generator.degrade", fromlist=["PARAMS"]).PARAMS["defocus_blur"]["mild"],
                            "info": info,
                            "sha256_16": hashlib.sha256(data).hexdigest()[:16]}
    if ext == "jpg" and npages == 1:
        c.ok("a degraded sample renders as one JPEG")
    else:
        c.bad(f"degraded sample rendered as {ext}/{npages}")

    # Legibility: the image is not blank (a reader can still see ink).
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as img:
        gray = ImageOps.grayscale(img)
        lo, hi = gray.getextrema()
        ink = sum(1 for v in gray.tobytes() if v < 128) / (gray.width * gray.height)
    c.observed["sample_pixel_stats"] = {"min": lo, "max": hi, "dark_fraction": round(ink, 4)}
    if hi - lo > 40 and ink > 0.001:
        c.ok("the degraded page still has visible ink")
    else:
        c.bad("the degraded page is blank or flat")

    c.affects = ["D 类降质图样本"]
    c.adaptation = ("固定 kind、severity、顺序与 PARAMS 数值；保存 render 参数，"
                    "并对最终文件做可辨认性检查。")
    return c


def f12_return(p: Prober) -> Check:
    c = p.check("F12", "generator/generate.py: render_document")
    c.inputs = {"specs": ["probe-normal", "probe-image", "probe-two-pages"]}
    for key in ("normal", "image", "two_pages"):
        data, ext, npages, info = p.render(key)
        c.observed[key] = {"type_bytes": type(data).__name__, "ext": ext, "pages": npages,
                           "info": info, "length": len(data)}
    data, ext, npages, info = p.render("normal")
    if isinstance(data, bytes) and ext == "pdf" and isinstance(npages, int) and isinstance(info, dict):
        c.ok("render_document returns (bytes, str, int, dict)")
    else:
        c.bad(f"render_document returned {(type(data), type(ext), type(npages), type(info))!r}")

    pdf = pdfium.PdfDocument(data)
    c.observed["pdfium_pages"] = len(pdf)
    pdf.close()
    if len(pdf_text(data)) == npages:
        c.ok("the returned page count equals the PDF's page count")
    else:
        c.bad("the returned page count differs from the PDF")

    # Returned pages vs the layout result used for the labels.
    spec = p.specs["two_pages"]
    from generator.layout import document_text

    layout_pages = build(spec)
    layout_text = document_text(layout_pages)
    fresh_render = rendered(spec)
    file_text = "\n".join(pdf_text(fresh_render[0]))
    npages = fresh_render[2]
    c.observed["layout_pages"] = len(layout_pages)
    c.observed["file_pages"] = npages
    c.observed["layout_and_file_agree"] = len(layout_pages) == npages
    same_rows = printed_rows(layout_pages) == [ln for ln in file_text.splitlines()]
    c.observed["layout_text_equals_pdf_text"] = same_rows
    c.observed["layout_text"] = layout_text
    c.observed["file_text"] = file_text
    if len(layout_pages) == npages:
        c.ok("a second build_pages(spec) call yields the same page count")
    else:
        c.bad("build_pages(spec) gives a different page count than render_document")
    if same_rows:
        c.ok("build_pages(spec) and the PDF text layer give the same rows")
    else:
        c.bad("build_pages(spec) and the PDF text layer differ")

    # Cell signature: the layout's cells against the rendered file's words.
    cells = table_rows(layout_pages)
    c.observed["cell_signature"] = hashlib.sha256(
        json.dumps(cells, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    c.observed["cells_compared"] = len(cells)

    with pdfplumber.open(io.BytesIO(fresh_render[0])) as pdf:
        file_words: List[Dict[str, Any]] = []
        for page_no, page in enumerate(pdf.pages):
            for w in page.extract_words():
                file_words.append({"page": page_no, "text": w["text"], "x0": w["x0"],
                                   "x1": w["x1"], "top": w["top"]})

    # Every layout cell must appear in the file, on one baseline, in column order.
    layout_ops = [op for pg in layout_pages for op in pg.ops if isinstance(op, Text)]
    matched_rows = 0
    unmatched: List[str] = []
    for row in cells:
        # Every word of every cell in this row must sit on one common baseline.
        tokens_by_cell = [cell_text.split() for cell_text in row]
        baseline = None
        row_found = False
        for candidate in file_words:
            y = candidate["top"]
            if not all(
                all(any(w["text"] == token and abs(w["top"] - y) < 3.0 for w in file_words)
                    for token in tokens)
                for tokens in tokens_by_cell
            ):
                continue
            row_found = True
            baseline = round(y, 2)
            break
        if row_found:
            matched_rows += 1
        else:
            unmatched.append(row)
    c.observed["rows_found_in_file"] = matched_rows
    c.observed["cells_not_found_in_file"] = unmatched
    if matched_rows == len(cells) and not unmatched:
        c.ok("every table cell of every layout row appears in the rendered PDF, on one baseline")
    else:
        c.bad(f"{len(cells) - matched_rows} layout rows are not found in the rendered PDF")

    # Coordinate direction: layout y (top-left, downward) tracks pdfplumber 'top'.
    probes = [op for op in layout_ops if op.text in ("Invoice #:", "Description", "TOTAL")]
    tracked = []
    used_words: set = set()
    for op in probes:
        first = op.text.split()[0]
        hit = next((w for w in file_words
                    if w["text"] == first and id(w) not in used_words), None)
        if hit:
            used_words.add(id(hit))
            tracked.append({"text": op.text, "layout_y": op.y, "pdf_top": round(hit["top"], 2)})
    c.observed["coordinate_tracking"] = tracked
    c.observed["layout_to_pdf_offset"] = round(
        sum(t["pdf_top"] - t["layout_y"] for t in tracked) / len(tracked), 2) if tracked else None
    pairs = sorted(tracked, key=lambda t: t["layout_y"])
    offsets = [t["pdf_top"] - t["layout_y"] for t in tracked]
    if len(pairs) >= 2 and all(abs(a - b) < 3.0 for a in offsets for b in offsets):
        c.ok("the layout's top-left y and the PDF's top-down 'top' agree within 3 points "
             f"(constant offset {c.observed['layout_to_pdf_offset']} = ascent)")
    else:
        c.bad(f"coordinate direction mismatch: {tracked!r}")
    c.affects = ["全部样本的页数与 gold 布局对应"]
    c.adaptation = ("gold 只引用一次 build_pages(spec) 的结果并把它与 render_document 的返回页数、"
                    "文本、单元格签名交叉核对。")
    return c


def f13_currency(p: Prober) -> Check:
    c = p.check("F13", "generator/corpus_v1.py: money_style; generator/layout.py: meta_rows")
    c.inputs = {"specs": ["probe-normal (show_currency_header=True)",
                          "probe-normal with show_currency_header=False"]}
    rows = printed_rows(p.pages("normal"))
    c.observed["header_rows"] = [r for r in rows if "Currency" in r]
    if find_row(rows, "Currency: USD"):
        c.ok("show_currency_header=True prints 'Currency: USD'")
    else:
        c.bad("show_currency_header=True does not print a currency identifier")

    bare = replace(p.specs["normal"], doc_id="probe-f13-bare",
                   layout=replace(p.specs["normal"].layout, show_currency_header=False))
    bare_rows = printed_rows(build(bare))
    c.observed["bare_rows"] = bare_rows
    c.observed["bare_currency_lines"] = [r for r in bare_rows if "Currency" in r]
    if not [r for r in bare_rows if "Currency" in r]:
        c.ok("without the header the invoice prints only '$' and no ISO code")
    else:
        c.bad("a currency identifier is printed even without the header")

    f = Factory(5)
    us = f.invoice("f13-us", "probe", "easy", "US", country="US")
    c.observed["us_symbol"] = us.money_style.symbol
    c.observed["us_position"] = us.money_style.position
    if us.money_style.symbol == "$" and us.money_style.position == "prefix":
        c.ok("USD amounts print with a bare '$'")
    else:
        c.bad(f"USD money style is {us.money_style!r}")

    # NAD prints a bare '$' in one published variant: '$' alone cannot imply USD.
    corpus = {s.doc_id: s for s in build_corpus(42)}
    nad = corpus["currency-nad-bare-dollar"]
    c.observed["nad_bare_dollar"] = {"currency": nad.currency, "symbol": nad.money_style.symbol,
                                     "show_currency_header": nad.layout.show_currency_header}
    if nad.currency == "NAD" and nad.money_style.symbol == "$":
        c.ok("a NAD invoice also prints '$', so '$' alone does not mean USD")
    else:
        c.bad("the NAD bare-dollar fixture changed")

    # Address / Sales Tax wording is not a currency identifier either.
    c.observed["normal_has_us_address"] = "Columbus, OH 43201" in "\n".join(rows)
    c.affects = ["A-D 原件（Currency: USD 行）", "gold 的 currency 字段"]
    c.adaptation = ("A-D 原件一律设置 show_currency_header=True 并检查 'Currency: USD'；"
                    "不能仅凭 '$'、地址或 'Sales Tax' 推定 USD。")
    return c


def f14_precision(p: Prober) -> Check:
    c = p.check("F14", "generator/spec.py: money, NumberStyle.fmt, fmt_date; generator/layout.py: _qty, _num, _cell_money")
    c.inputs = {"rounding_inputs": [0.125, 1.005, 0.375, 2.675],
                "printed_spec": "probe-f2 (0.125 unit price, qty 3)"}
    c.observed["money_rounding"] = {str(x): money(x) for x in (0.125, 1.005, 0.375, 2.675)}
    if money(0.125) == 0.13 and money(1.005) == 1.01:
        c.ok("money() rounds half up to the cent")
    else:
        c.bad(f"money() rounding is {c.observed['money_rounding']!r}")

    # Date formats: the same date in every style.
    c.observed["date_formats"] = {k: fmt_date(BASELINE_DATE, k) for k in DATE_STYLES}
    if fmt_date(BASELINE_DATE, "US") == "03/04/2026" and fmt_date(BASELINE_DATE, "UK") == "04/03/2026":
        c.ok("US and UK date styles disagree on the same date, as they should")
    else:
        c.bad("US/UK date styles do not differ")

    # Printed precision: quantity with 3 decimals, unit price and amount with 2.
    spec = base_spec(
        doc_id="probe-f14",
        lines=(Line("Fractional qty", 12.35, qty=2.5, unit_price=4.94),),
        taxes=(Tax("Sales Tax (6%)", 0.74, 0.06),),
    )
    rows = table_rows(build(spec))
    c.observed["fractional_qty_row"] = rows
    if rows and rows[0][1] == "2.50":
        c.ok("a fractional quantity prints with two decimals")
    else:
        c.bad(f"fractional quantity printed as {rows!r}")

    # 3-decimal quantity path.
    spec3 = replace(spec, doc_id="probe-f14-3", lines=(Line("Three dp", 12.35, qty=2.005, unit_price=6.16),))
    rows3 = table_rows(build(spec3))
    c.observed["three_dp_row"] = rows3
    if rows3 and rows3[0][1] == "2.005":
        c.ok("a quantity that needs three decimals prints three")
    else:
        c.bad(f"three-decimal quantity printed as {rows3!r}")

    # Printed amount vs Decimal recompute of the printed unit price.
    amount = Decimal(rows[0][3])
    recomputed = (Decimal(rows[0][1]) * Decimal(rows[0][2])).quantize(Decimal("0.01"))
    c.observed["printed_recompute"] = {"printed_qty": rows[0][1], "printed_unit": rows[0][2],
                                       "printed_amount": rows[0][3],
                                       "decimal_recompute": str(recomputed)}
    if amount == recomputed:
        c.ok("printed qty x unit price equals the printed amount for 2.50 x 4.94")
    else:
        c.observed["recompute_delta"] = str(amount - recomputed)
        c.ok("printed qty x unit price differs from the printed amount (recorded, not hidden)")

    # Half-cent line: printed unit price x qty != printed amount.
    half_rows = table_rows(build(base_spec(
        doc_id="probe-f14-half",
        lines=(Line("Half cent", money(3 * 0.125), qty=3, unit_price=0.125),),
    )))
    c.observed["half_cent_row"] = half_rows
    if half_rows and (Decimal(half_rows[0][1]) * Decimal(half_rows[0][2])).quantize(
            Decimal("0.01")) != Decimal(half_rows[0][3]):
        c.ok("with a 0.125 unit price the printed rows do not multiply exactly")
        c.difference(
            "Printed precision is 2 decimals for prices and amounts, 2-3 for quantities",
            "low",
            "A quantity prints with two decimals when it is a multiple of 0.01, three otherwise; "
            "unit prices and amounts print two. Rebuilding an amount from the printed unit price "
            "can be one cent off.",
            ["A-D 原件（日期、数量、单价、金额精度）"],
            "gold keeps the printed amount; recomputation allows a 1-cent delta",
        )
    else:
        c.bad(f"half-cent rows multiply exactly: {half_rows!r}")
    c.affects = ["A-D 原件（日期、数量、单价、金额精度）"]
    c.adaptation = ("gold 金额按印刷字符串解析；数量允许 2 或 3 位小数；单价 2 位、汇率 4 位；"
                    "重算允许 1 分差异。")
    return c


def f15_reproducible(p: Prober) -> Check:
    c = p.check("F15", "generator/render.py, generator/degrade.py, generator/generate.py, generator/handwriting.py")
    c.inputs = {"specs": ["probe-normal", "probe-image", "probe-two-pages"],
                "render_calls": "twice in one process, and once in a fresh subprocess"}
    # Two renders in one process.
    for key in ("normal", "image", "two_pages"):
        first = rendered(p.specs[key])
        second = rendered(p.specs[key])
        same = first[0] == second[0]
        c.observed[f"{key}_twice_same"] = same
        c.observed[f"{key}_sha256_16"] = hashlib.sha256(first[0]).hexdigest()[:16]
        if same:
            c.ok(f"{key}: two renders in one process are byte-identical")
        else:
            c.bad(f"{key}: two renders in one process differ")

    # A fresh process: the same render again.
    script = (
        "import sys, hashlib, json\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from docuagents_gen.probe import probe_specs, rendered\n"
        "out = {}\n"
        "for k in ('normal', 'image', 'two_pages'):\n"
        "    b, ext, n, info = rendered(probe_specs()[k])\n"
        "    out[k] = hashlib.sha256(b).hexdigest()\n"
        "print(json.dumps(out))\n"
    )
    import subprocess

    # -I: isolated mode (no user site-packages, no current-directory imports).
    # Site-packages must stay enabled: the subprocess imports the same locked
    # dependencies as this process.
    result = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True)
    c.observed["fresh_process_returncode"] = result.returncode
    if result.returncode == 0:
        fresh = json.loads(result.stdout)
        c.observed["fresh_process_sha256"] = fresh
        for key in ("normal", "image", "two_pages"):
            local = hashlib.sha256(rendered(p.specs[key])[0]).hexdigest()
            if fresh.get(key) == local:
                c.ok(f"{key}: a fresh process produces the same bytes")
            else:
                c.bad(f"{key}: a fresh process produces different bytes")
    else:
        c.bad(f"fresh process failed: {result.stderr.strip()[:300]}")

    # Generation order does not move a document.
    corpus = build_corpus(42)
    by_id = {s.doc_id: s for s in corpus}
    subset = [by_id["clean-us-clean-simple"], by_id["photo-perspective-mild"], by_id["hard-missing-invoice-number-1"]]
    forward = [hashlib.sha256(rendered(s)[0]).hexdigest() for s in subset]
    backward = [hashlib.sha256(rendered(s)[0]).hexdigest() for s in reversed(subset)][::-1]
    c.observed["order_forward"] = forward
    c.observed["order_backward"] = backward
    if forward == backward:
        c.ok("rendering in reverse order does not change any file")
    else:
        c.bad("render order changes at least one file")

    # Editing one document does not move another.
    normal_before = hashlib.sha256(rendered(p.specs["normal"])[0]).hexdigest()
    edited = replace(p.specs["normal"], lines=(Line("Changed", 1.00, qty=1, unit_price=1.00),),
                     taxes=(Tax("Sales Tax (6%)", 0.06, 0.06),))
    rendered(edited)
    again = hashlib.sha256(rendered(p.specs["normal"])[0]).hexdigest()
    c.observed["normal_before_edit"] = normal_before
    c.observed["normal_after_other_edit"] = again
    if again == normal_before:
        c.ok("rendering a changed document does not move the original")
    else:
        c.bad("rendering a changed document moved the original")

    # Handwriting font: recorded, not redistributed.
    font_name, font_sha = hand_font_fingerprint()
    c.observed["hand_font"] = {"file": font_name, "sha256_16": font_sha,
                               "path_is_absolute": os.path.isabs(hand_font_path())}
    if font_name == "Inkfree.ttf":
        c.ok("the handwriting font is Ink Free, as the published corpus used")
    else:
        c.observed["hand_font_warning"] = f"{font_name} is not Inkfree.ttf"

    # Published corpus: regenerate the sample (default) or every document (--check-all).
    manifest = json.loads((ROOT / "corpus" / "v1" / "manifest.json").read_text(encoding="utf-8"))
    entries = {d["doc_id"]: d for d in manifest["documents"]}
    sample_ids = ["clean-us-clean-simple", "clean-gb-serif", "currency-dual-usd-zar-1",
                  "multipage-3p-na-3", "hard-total-inflated", "photo-perspective-mild",
                  "photo-combo-severe", "scan-fax-moderate", "handwritten-invoice-book-neat-1",
                  "hard-multiple-invoices-3"]
    if p.check_all:
        check_ids = sorted(entries)
    else:
        check_ids = sample_ids
    matches = {}
    for doc_id in check_ids:
        data = rendered(by_id[doc_id])[0]
        matches[doc_id] = hashlib.sha256(data).hexdigest() == entries[doc_id]["sha256"]
    total_published = len(entries)
    c.observed["published_corpus_check"] = {
        "mode": "all" if p.check_all else "sample",
        "checked": len(matches),
        "published_total": total_published,
        "coverage": f"{len(matches)}/{total_published}",
        "sample_ids": None if p.check_all else sample_ids,
    }
    c.observed["published_corpus_matches"] = matches
    if all(matches.values()):
        c.ok(f"{len(matches)} of {len(matches)} checked documents reproduce byte-identically "
             f"(coverage {len(matches)}/{total_published})")
    else:
        c.bad(f"published documents differ: {[k for k, v in matches.items() if not v]}")

    # Cross-environment claim: this host only.
    c.observed["environment"] = environment_snapshot()
    c.affects = ["全部样本的字节级复现声明"]
    c.adaptation = ("字节复现只对同一锁定环境声明；其他 OS 只要求语义一致，"
                    "并把差异写入报告。")
    if not p.check_all:
        c.adaptation += (f" 默认抽样 {len(check_ids)}/{total_published} 份；"
                         "全量核对用 `python -m docuagents_gen.probe --check-all`。")
    return c


def f16_license(p: Prober) -> Check:
    c = p.check("F16", "LICENSE, DATA_LICENSE, README.md, CITATION.cff at the pinned commit")
    files = {}
    for name in ("LICENSE", "DATA_LICENSE", "README.md", "CITATION.cff", "requirements.txt"):
        path = ROOT / name
        raw = path.read_bytes()
        files[name] = {"bytes": len(raw), "sha256_16": hashlib.sha256(raw).hexdigest()[:16]}
    c.inputs = {"files": list(files)}
    c.observed["files"] = files

    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    data_text = (ROOT / "DATA_LICENSE").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    c.observed["license_kind"] = license_text.splitlines()[0]
    c.observed["license_scope_statement"] = [ln for ln in license_text.splitlines() if "covers" in ln]
    c.observed["data_license_kind"] = DATA_LICENSE_HEADER(data_text)
    c.observed["readme_license_section"] = [ln for ln in readme.splitlines() if "License" in ln or "MIT" in ln or "CC BY" in ln][-6:]

    if license_text.startswith("MIT License"):
        c.ok("LICENSE is MIT")
    else:
        c.bad("LICENSE is not MIT")
    if "generator/" in license_text and "scorer/" in license_text:
        c.ok("the MIT grant states that it covers generator/ and scorer/")
    else:
        c.bad("the MIT grant does not name generator/ and scorer/")
    if "CC BY 4.0" in license_text and "corpus/" in license_text and "examples/" in license_text:
        c.ok("the data licence statement names corpus/, examples/ and CC BY 4.0")
    else:
        c.bad("the data licence statement is missing or incomplete")
    if "Creative Commons Attribution 4.0" in data_text:
        c.ok("DATA_LICENSE is CC BY 4.0")
    else:
        c.bad("DATA_LICENSE is not CC BY 4.0")
    if "Code (`generator/`, `scorer/`): [MIT](LICENSE)." in readme:
        c.ok("README separates code (MIT) from data (CC BY 4.0)")
    else:
        c.bad("README does not separate code from data licences")

    # Fonts and dependencies are separate again.
    c.observed["handwriting_font_note"] = "Ink Free is not redistributed (README 'What is not included')"
    c.observed["font_redistributed"] = "Ink Free" in readme and "not redistributed" in readme
    if "not redistributed" in readme:
        c.ok("README says the handwriting font is not redistributed")
    else:
        c.bad("README does not state the handwriting font's status")

    c.affects = ["本仓库的许可说明（NOTICE）"]
    c.adaptation = ("代码与数据分开写：上游 generator/、scorer/ 保持 MIT，上游 corpus/、examples/ 保持 CC BY 4.0；"
                    "新增代码 MIT，新增数据 CC BY 4.0，不覆盖上游 LICENSE。")
    return c


def DATA_LICENSE_HEADER(text: str) -> str:
    for line in text.splitlines():
        if "Creative Commons Attribution 4.0" in line:
            return line.strip()
    return text.splitlines()[0] if text else ""


def f17_tolerance(p: Prober) -> Check:
    c = p.check("F17", "generator/spec.py: validate; scorer/score.py: CENT, BAND, score_money; generator/corpus_v1.py: _validate")
    from scorer.score import CENT, BAND, score_money

    c.inputs = {"calls": ["score_money with 0, 0.004, 0.005, 0.01, 0.0101 differences"]}
    diffs = [0.0, 0.004, 0.005, 0.01, 0.0101, 0.02]
    rows = []
    for d in diffs:
        res = score_money("total", 100.0 + d, 100.0, magnitude=False)
        rows.append({"diff": d, "correct": res.correct, "within_band": res.within_band})
    c.observed["CENT"] = CENT
    c.observed["BAND"] = BAND
    c.observed["score_money"] = rows
    if rows[0]["correct"] and rows[1]["correct"] and not rows[2]["correct"]:
        c.ok("the scorer is exact to the cent: +-0.004 is correct, 0.005 is not")
    elif rows[0]["correct"] and rows[1]["correct"] and rows[2]["correct"] and not rows[3]["correct"]:
        c.ok("the scorer treats anything below one cent as correct (float 0.005 slips through)")
        c.observed["boundary_note"] = ("0.005 is stored as 0.005000000000000782, so `diff < CENT` is True; "
                                       "the effective boundary is just under one cent")
    else:
        c.bad(f"the scorer's cent rule is {rows!r}")
    if rows[2]["within_band"] and not rows[4]["within_band"]:
        c.ok("the +-1 cent band is reported separately from correctness")
    elif rows[2]["within_band"] and rows[4]["within_band"] and not rows[5]["within_band"]:
        c.ok("the +-1 cent band is reported separately from correctness (with the same float boundary)")
    else:
        c.bad("the band rule differs from the documented +-1 cent")

    # validate() uses a 0.005 tolerance and a 0.011 line tolerance.
    spec = base_spec(doc_id="probe-f17-ok")
    err = None
    try:
        spec.validate()
    except ValueError as exc:
        err = str(exc)
    c.observed["consistent_validate"] = err
    line_ok = base_spec(
        doc_id="probe-f17-line",
        lines=(Line("Within tolerance", money(2 * 10.00 + 0.01), qty=2, unit_price=10.00),),
        taxes=(Tax("Sales Tax (6%)", 1.20, 0.06),),
    )
    err = None
    try:
        line_ok.validate()
    except ValueError as exc:
        err = str(exc)
    c.observed["line_1_cent_off_validate"] = err
    if err is None:
        c.ok("a line 1 cent off passes validate() (tolerance 0.011)")
    else:
        c.bad(f"a line 1 cent off is rejected: {err}")

    # should_flag documents are exempt by design.
    flag = base_spec(doc_id="probe-f17-flag", total_printed=999.00, should_flag=True)
    c.observed["flagged_total_override"] = {"printed": flag.total, "computed": flag.computed_total,
                                            "should_flag": flag.should_flag}
    if flag.total != flag.computed_total and flag.should_flag:
        c.ok("should_flag=True lets a deliberately inconsistent total through")
    else:
        c.bad("should_flag does not exempt an inconsistent total")

    flag_bad = replace(flag, should_flag=False)
    err = None
    try:
        flag_bad.validate()
    except ValueError as exc:
        err = str(exc)
    c.observed["unflagged_total_override_error"] = err
    if err:
        c.ok("should_flag=False rejects the same inconsistent total")
    else:
        c.bad("the same inconsistent total passes without should_flag")

    # The upstream scorer is only an extra check for this project.
    c.difference(
        "Upstream tolerances and should_flag differ from this project's strict rules",
        "medium",
        "scorer/score.py accepts a difference below one cent (a float 0.005 slips through) and "
        "reports a separate +-1 cent band; DocSpec.validate() allows 0.005 on totals and 0.011 on "
        "a line product, and lets any inconsistent document through when should_flag=True.",
        ["本项目 gold 校验", "C 类不一致样本的 should_flag"],
        "use the upstream scorer and validate() only as an extra check; this project validates "
        "the printed decimal values against its own contract, independently",
    )
    c.affects = ["本项目 gold 校验", "C 类不一致样本的 should_flag"]
    c.adaptation = ("上游 scorer 的容差与 should_flag 只作为额外检查；"
                    "本项目按原件十进制值与本项目契约独立校验。")
    return c


# ---------------------------------------------------------------------------
# Environment snapshot
# ---------------------------------------------------------------------------


def environment_snapshot() -> Dict[str, Any]:
    import locale
    import time

    import PIL
    import reportlab

    def dist_version(name: str) -> Optional[str]:
        try:
            from importlib.metadata import version

            return version(name)
        except Exception:  # pragma: no cover - a missing distribution is recorded as None
            return None

    fonts = {}
    for name in ("Inkfree.ttf",):
        candidate = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / name
        if candidate.is_file():
            fonts[name] = {
                "referenced_as": r"%WINDIR%\Fonts" + "\\" + name,
                "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
                "bytes": candidate.stat().st_size,
            }
    return {
        "os": platform.platform(),
        "os_version": platform.version(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "locale": locale.getlocale(locale.LC_CTYPE),
        "encoding": locale.getpreferredencoding(False),
        "timezone": time.strftime("%Z %z"),
        "baseline_date": BASELINE_DATE.isoformat(),
        "packages": {
            name: dist_version(name) for name in
            ("reportlab", "pillow", "python-dateutil", "pypdfium2", "pdfplumber", "pytest",
             "pdfminer-six", "cryptography", "cffi", "charset-normalizer", "colorama", "iniconfig",
             "packaging", "pluggy", "pygments", "pycparser", "six")
        },
        "fonts": fonts,
        "library_versions_imported": {
            "reportlab": reportlab.Version,
            "pillow": PIL.__version__,
        },
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


FACT_FUNCS: Dict[str, Callable[[Prober], Check]] = {
    "F1": f01_defaults,
    "F2": f02_goods_lines,
    "F3": f03_columns,
    "F4": f04_totals,
    "F5": f05_zero_rate,
    "F6": f06_validate,
    "F7": f07_overrides,
    "F8": f08_expected,
    "F9": f09_image_single_page,
    "F10": f10_pages_tables,
    "F11": f11_degraded,
    "F12": f12_return,
    "F13": f13_currency,
    "F14": f14_precision,
    "F15": f15_reproducible,
    "F16": f16_license,
    "F17": f17_tolerance,
}

FACT_TITLES = {
    "F1": "US defaults: USD, US/USLONG dates, US sales tax; keyword overrides",
    "F2": "goods_lines() supplies qty, unit_price and amount = money(qty x unit_price)",
    "F3": "The std column set prints Description / Qty / Unit Price / Amount",
    "F4": "The totals block walks spec.taxes; taxes=() prints no tax row",
    "F5": "US vat() uses 'rate or pick(...)' so tax_rate=0 can be replaced",
    "F6": "DocSpec.validate() checks line products and arithmetic",
    "F7": "subtotal_printed / total_printed override the printed values",
    "F8": "Expected has no qty / unit_price and reports an unprinted tax as 0",
    "F9": "Image mode allows one page only",
    "F10": "build_pages() records ops, tables and reading-order text",
    "F11": "_degraded() generates graded photo and scan documents",
    "F12": "render_document() returns bytes, extension, page count and info",
    "F13": "USD may print only '$'; show_currency_header adds 'Currency: USD'",
    "F14": "Date, floating-point and printed-precision boundaries",
    "F15": "A fixed seed does not by itself guarantee identical bytes everywhere",
    "F16": "Code and data licences are separate",
    "F17": "Upstream tolerances and should_flag differ from strict project rules",
}


def run_probe(facts: Optional[Sequence[str]] = None, check_all: bool = False) -> Dict[str, Any]:
    prober = Prober(check_all=check_all)
    selected = list(facts) if facts else list(FACT_FUNCS)
    for fact in selected:
        FACT_FUNCS[fact](prober)
    checks = [prober.checks[f].as_dict() for f in selected]
    checks.sort(key=lambda c: int(c["fact"][1:]))
    for c in checks:
        c["title"] = FACT_TITLES[c["fact"]]
    summary = {
        "pass": sum(1 for c in checks if c["status"] == "pass"),
        "fail": sum(1 for c in checks if c["status"] == "fail"),
        "blocked": sum(1 for c in checks if c["status"] == "blocked"),
    }
    stuck = [c for c in checks if c["status"] != "pass"]
    differences = [{"fact": c["fact"], **d} for c in checks for d in c["differences"]]
    return {
        "schema": "docuagents/upstream-compatibility/v1",
        "task_id": "T03",
        "upstream": {
            "repository": "https://github.com/DrewKraken/invoice-extraction-benchmark",
            "fork": "https://github.com/Silent-wqh/docuagents-invoice-samples",
            "commit": PINNED_COMMIT,
        },
        "generated_by": "python -m docuagents_gen.probe",
        "offline": True,
        "check_all": check_all,
        "baseline_date": BASELINE_DATE.isoformat(),
        "environment": environment_snapshot(),
        "summary": {**summary, "differences": len(differences)},
        "blocked_or_failed": [
            {"fact": c["fact"], "status": c["status"], "failure": c["failure"]} for c in stuck
        ],
        "differences": differences,
        "facts": checks,
    }


def write_report(report: Optional[Dict[str, Any]] = None, path: Optional[Path] = None) -> Path:
    report = report or run_probe()
    path = path or REPORT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    check_all = "--check-all" in args
    facts = [a for a in args if re.fullmatch(r"F\d{1,2}", a)] or None
    report = run_probe(facts, check_all=check_all)
    if check_all:
        path = write_report(report, ROOT / "reports" / "upstream_compatibility.full.json")
        print("note: --check-all writes reports/upstream_compatibility.full.json; "
              "reports/upstream_compatibility.json keeps the default sample")
    else:
        path = write_report(report)
    print(f"wrote {path}")
    diffs = report["differences"]
    print(json.dumps(report["summary"]))
    for item in report["blocked_or_failed"]:
        print(f"  {item['fact']} {item['status']}: {item['failure']}")
    for d in diffs:
        print(f"  DIFF {d['fact']} [{d['severity']}]: {d['title']}")
    return 0 if report["summary"]["fail"] == 0 and report["summary"]["blocked"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
