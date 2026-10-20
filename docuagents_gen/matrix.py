"""The 30-document main sample matrix (DocuAgentS sample plan, P2-P3).

This module is the **only** configuration source for the main sample set of
the invoice-extraction benchmark fork.  It answers, for every document:

* its ``doc_id`` and fixed ``split`` (dev / test) from sample plan §6.3;
* its class and subclass (A-E, with subclasses from §6.1);
* its construction parameters (dates, lines, tax, charges, layout, render);
* the question the document is there to answer;
* its scope gate (``scope_status`` and the true ``scope_reason`` codes);
* its legibility gate (what a reader may expect to read, and what T17 still
  has to confirm);
* its expected issue codes (what a correct pipeline must report).

Nothing here renders a file, reads a PDF or calls a model.  ``build.py``
turns a row into an upstream ``DocSpec``; ``gold.py`` turns the printed
pages and the injected file metadata into the gold record.

Rules this module keeps:

* No randomness.  Parties come from a fixed catalogue, and every line, date,
  number and amount is written out or derived by a closed formula.
* The matrix is checked when the module is imported.  A wrong count, a
  duplicate id, an unknown scope reason or a split that departs from §6.3
  raises ``MatrixError`` instead of producing a quietly different set.
* Values declared here are the sample plan's; they are never derived from
  the generated documents (that would make the check circular).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, Mapping, Tuple

from . import codes

SEED = 2026

#: Scope limits that the matrix is built against.  A document above a limit
#: is out of scope for that reason.  ``A3`` has two pages and 16 lines, so the
#: limits must stay at or above those values; ``max_lines`` stays above the
#: 60-line page-limit construction of ``da-e-04``.
LIMITS: Mapping[str, int] = {
    "max_pages": 2,
    "max_lines": 80,
}

#: Sample plan §6.3, fixed split table.  ``dev`` and ``test`` are frozen per
#: document id; they are not derived from the class or from a random draw.
SPLIT: Mapping[str, str] = {
    "da-a1-01": "dev",
    "da-a1-02": "dev",
    "da-a1-03": "test",
    "da-a1-04": "test",
    "da-a1-05": "test",
    "da-a1-06": "test",
    "da-a2-01": "test",
    "da-a3-01": "dev",
    "da-a3-02": "test",
    "da-a4-01": "test",
    "da-b1-01": "dev",
    "da-b1-02": "test",
    "da-b1-03": "test",
    "da-b2-01": "dev",
    "da-b2-02": "test",
    "da-b2-03": "test",
    "da-b3-01": "dev",
    "da-b3-02": "test",
    "da-c1-01": "dev",
    "da-c1-02": "test",
    "da-c2-01": "test",
    "da-c3-01": "test",
    "da-d1-01": "test",
    "da-d2-01": "dev",
    "da-e-01": "dev",
    "da-e-02": "test",
    "da-e-03": "test",
    "da-e-04": "test",
    "da-e-05": "dev",
    "da-e-06": "test",
}

#: Sample plan §6.1: subclass counts.
CLASS_COUNTS: Mapping[str, int] = {"A": 10, "B": 8, "C": 4, "D": 2, "E": 6}
SUBCLASS_COUNTS: Mapping[str, int] = {
    "A1": 6,
    "A2": 1,
    "A3": 2,
    "A4": 1,
    "B1": 3,
    "B2": 3,
    "B3": 2,
    "C1": 2,
    "C2": 1,
    "C3": 1,
    "D1": 1,
    "D2": 1,
    "E": 6,
}


class MatrixError(RuntimeError):
    """Raised when the matrix contradicts the sample plan or the contract."""


def round_money(value: Decimal) -> Decimal:
    """Quantize to the cent, half up (the printed amount rule of the plan)."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Fixed party catalogue.  No random draws: the same row always gets the same
# vendor and customer.  Names, streets and tax ids are invented.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Vendor:
    key: str
    name: str
    address: Tuple[str, ...]
    phone: str
    email: str
    tax_id: str
    tax_label: str = "EIN"


VENDORS: Tuple[Vendor, ...] = (
    Vendor("northline", "Northline Supply LLC", ("4100 Commerce Parkway", "Columbus, OH 43228"),
           "(614) 555-0142", "accounts@northlinesupply.example", "31-4820195"),
    Vendor("harborline", "Harborline Industrial Co", ("27 Depot Lane", "Savannah, GA 31401"),
           "(912) 555-0188", "billing@harborlineindustrial.example", "58-2214907"),
    Vendor("ridgeline", "Ridgeline Paper & Packaging", ("900 Elm Street", "Denver, CO 80204"),
           "(303) 555-0117", "invoices@ridgelinepaper.example", "84-3902116"),
    Vendor("summit", "Summit Fastener Supply", ("1450 Market Street", "Omaha, NE 68102"),
           "(402) 555-0163", "ar@summitfastener.example", "47-1180332"),
    Vendor("cedarworks", "Cedarworks Tools Inc", ("63 Riverside Avenue", "Spokane, WA 99201"),
           "(509) 555-0129", "accounts@cedarworkstools.example", "91-2455018"),
    Vendor("bluewater", "Bluewater Carton Co", ("210 Harbor Boulevard", "Memphis, TN 38103"),
           "(901) 555-0174", "billing@bluewatercarton.example", "62-7743019"),
)


@dataclass(frozen=True)
class Customer:
    key: str
    name: str
    address: Tuple[str, ...]


CUSTOMERS: Tuple[Customer, ...] = (
    Customer("redwood", "Redwood Assembly Inc", ("88 Industrial Drive", "Dallas, TX 75207")),
    Customer("kestrel", "Kestrel Fabrication LLC", ("512 Commerce Parkway", "Reno, NV 89502")),
    Customer("larkspur", "Larkspur Manufacturing Co", ("19 Freight Road", "Columbus, OH 43215")),
    Customer("tandem", "Tandem Works Inc", ("600 Market Street", "Denver, CO 80202")),
    Customer("alder", "Alder Creek Mills", ("35 Depot Lane", "Savannah, GA 31405")),
    Customer("quarry", "Quarry Lane Systems", ("240 Elm Street", "Omaha, NE 68105")),
)


#: Sample plan §6.3: a supplier entity belongs to one split only.  The catalogue
#: is split in two halves, and the two halves do not intersect:
#:
#: * ``dev``  -- ``VENDORS[0..2]`` (Northline, Harborline, Ridgeline);
#: * ``test`` -- ``VENDORS[3..5]`` (Summit, Cedarworks, Bluewater).
#:
#: A row picks a slot inside its own half, so the same supplier can serve
#: several documents of one split but never a document of the other split.
DEV_VENDOR_SLOTS: Tuple[int, ...] = (0, 1, 2)
TEST_VENDOR_SLOTS: Tuple[int, ...] = (3, 4, 5)


def _vendor_slot(doc_id: str, slot: int) -> int:
    """The vendor index of ``slot`` inside the split that holds ``doc_id``."""
    slots = DEV_VENDOR_SLOTS if SPLIT[doc_id] == "dev" else TEST_VENDOR_SLOTS
    return slots[slot % len(slots)]


# ---------------------------------------------------------------------------
# Row model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    """One printed line: description, quantity and unit price.

    ``amount_override`` is a deliberate printed amount that differs from
    ``quantity x unit_price``; only the C1 construction uses it.
    """

    description: str
    quantity: Decimal
    unit_price: Decimal
    amount_override: Decimal | None = None

    @property
    def amount(self) -> Decimal:
        if self.amount_override is not None:
            return round_money(self.amount_override)
        return round_money(self.quantity * self.unit_price)


@dataclass(frozen=True)
class ChargeSpec:
    label: str
    amount: Decimal
    kind: str  # shipping | fees | discount


@dataclass(frozen=True)
class LayoutSpec:
    family: str = "sans"
    header: str = "left"
    table: str = "band"
    paper: str = "LETTER"
    rows_per_page: int = 24
    logo: bool = False


@dataclass(frozen=True)
class ChildSpec:
    """One invoice inside a multi-invoice file (E's multiple_invoices)."""

    number: str
    vendor: int
    items: Tuple[Item, ...]


@dataclass(frozen=True)
class Row:
    doc_id: str
    klass: str
    subclass: str
    target_question: str
    source_family: str
    vendor: int
    customer: int
    issue_date: date
    items: Tuple[Item, ...]
    number: str | None
    number_label: str = "Invoice #"
    po_number: str | None = None
    po_label: str = "PO Number"
    due_days: int | None = 30
    title: str = "INVOICE"
    doc_kind: str = "invoice"
    columns: str = "std"
    charges: Tuple[ChargeSpec, ...] = ()
    tax_rate: Decimal | None = Decimal("0.06")
    tax_label: str = "Sales Tax (6%)"
    currency: str = "USD"
    money_symbol: str = "$"
    layout: LayoutSpec = LayoutSpec()
    render_mode: str = "pdf"
    render_fmt: str = "pdf"
    degradations: Tuple[Tuple[str, str], ...] = ()
    subtotal_printed: Decimal | None = None
    total_printed: Decimal | None = None
    children: Tuple[ChildSpec, ...] = ()
    scope_status: str = "SUPPORTED"
    scope_reasons: Tuple[str, ...] = ()
    expected_issues: Tuple[str, ...] = ()
    legibility: str = "clean_text"
    notes: str = ""

    # -- derived ------------------------------------------------------------

    @property
    def split(self) -> str:
        return SPLIT[self.doc_id]

    @property
    def line_count(self) -> int:
        return len(self.items)

    def label(self) -> Dict[str, str]:
        """The matrix facts that travel with the gold record."""
        return {
            "split": self.split,
            "class": self.klass,
            "subclass": self.subclass,
            "target_question": self.target_question,
            "source_family": self.source_family,
            "legibility": self.legibility,
        }

    def declaration(self) -> Dict[str, object]:
        """What ``gold.build_gold`` must reproduce from the printed pages."""
        return {
            "scope_status": self.scope_status,
            "scope_reasons": list(self.scope_reasons),
            "expected_issues": list(self.expected_issues),
            "tax_rule": "APPLICABLE" if self.tax_rate is not None else "NOT_APPLICABLE",
        }


# ---------------------------------------------------------------------------
# Catalogue of printed lines.  Every product is exact at the cent.
# ---------------------------------------------------------------------------

PACKING_TAPE = Item("Packing tape, 48 mm x 66 m", Decimal("6"), Decimal("4.25"))
LABELS = Item("Shipping labels, 100 sheets", Decimal("4"), Decimal("12.75"))
CARTONS = Item("Corrugated cartons, 400 x 300 x 300 mm", Decimal("25"), Decimal("2.40"))
STRETCH_FILM = Item("Stretch film, 500 mm x 300 m", Decimal("2"), Decimal("34.50"))
BUBBLE_WRAP = Item("Bubble wrap, 1 m x 100 m", Decimal("3"), Decimal("28.20"))
GLOVES = Item("Nitrile gloves, size L, box of 100", Decimal("5"), Decimal("18.40"))
STRAPPING = Item("Pallet strapping kit", Decimal("1"), Decimal("44.00"))
TONER = Item("Toner cartridge, black", Decimal("2"), Decimal("61.50"))
KNIFE = Item("Safety knife, retractable", Decimal("10"), Decimal("7.35"))
WALLET = Item("Document wallet, A4, pack of 25", Decimal("8"), Decimal("9.60"))


def insert_lines(count: int) -> Tuple[Item, ...]:
    """A table of ``count`` short descriptions that never wrap.

    Used by E's page-limit construction (60 lines, 24 rows per page).
    """
    if count < 1:
        raise MatrixError("insert_lines needs at least one line")
    return tuple(
        Item(
            f"Corrugated insert, size {i + 1:02d}",
            Decimal("2"),
            Decimal("3.10") + Decimal(i) * Decimal("0.10"),
        )
        for i in range(count)
    )


#: A3 line descriptions wrap to two printed lines inside the description
#: column.  The extra row height is the controlled lever that pushes the
#: totals block to page 2 while every line item stays on page 1 (sample plan
#: §6.1 "n_lines <= 20", §7 P5).  The text is only longer; no upstream layout
#: parameter is changed.
_A3_DESCRIPTION = (
    "Corrugated insert, size {n:02d}, double wall, stackable, "
    "moisture resistant, recycled board"
)


def a3_items(count: int) -> Tuple[Item, ...]:
    """The A3 table: ``count`` lines, every description wrapping to two lines."""
    if count < 1:
        raise MatrixError("a3_items needs at least one line")
    return tuple(
        Item(
            _A3_DESCRIPTION.format(n=i + 1),
            Decimal("2"),
            Decimal("3.10") + Decimal(i) * Decimal("0.10"),
        )
        for i in range(count)
    )


#: The A3 construction, frozen after the bounded candidate enumeration in
#: ``build.a3_candidates`` (see the T07a completion report for the run).
#: The pair is the first passing candidate in the fixed enumeration order
#: (``rows_per_page`` ascending, ``line_count`` ascending, one-line before
#: wrapped): 182 candidates, 26 pass, and candidate 6
#: (``rows_per_page=18``, ``line_count=16``, wrapped descriptions) is first.
A3_ROWS_PER_PAGE = 18
A3_LINE_COUNT = 16


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


def _a1(index: int) -> Row:
    dates = (
        date(2026, 1, 12),
        date(2026, 2, 3),
        date(2026, 2, 17),
        date(2026, 3, 2),
        date(2026, 3, 16),
        date(2026, 4, 1),
    )
    items = (
        (PACKING_TAPE, LABELS, CARTONS, STRETCH_FILM),
        (BUBBLE_WRAP, GLOVES, STRAPPING),
        (TONER, KNIFE, WALLET, LABELS, PACKING_TAPE),
        (CARTONS, STRETCH_FILM, GLOVES, KNIFE),
        (WALLET, TONER, BUBBLE_WRAP),
        (STRAPPING, CARTONS, LABELS, GLOVES),
    )[index]
    layouts = (
        LayoutSpec(header="left", table="band"),
        LayoutSpec(header="right", table="plain", family="serif"),
        LayoutSpec(header="banner", table="grid", family="sans"),
        LayoutSpec(header="center", table="zebra", family="serif"),
        LayoutSpec(header="left", table="zebra", family="mono"),
        LayoutSpec(header="right", table="band", logo=True),
    )
    doc_id = f"da-a1-{index + 1:02d}"
    # da-a1-04 is the normal purchase-order reference of the plan (§6.1); the
    # near-due-date interference stays on da-a1-05 and da-a1-06.
    po_number = "PO-2026-1177" if index == 3 else None
    return Row(
        doc_id=doc_id,
        klass="A",
        subclass="A1",
        target_question="正常单页发票的字段与金额是否被完整、准确抽取？",
        source_family="clean_pdf",
        vendor=_vendor_slot(doc_id, index),
        customer=(index + 1) % len(CUSTOMERS),
        issue_date=dates[index],
        items=items,
        number=f"INV-2026-{1101 + index}",
        po_number=po_number,
        layout=layouts[index],
        notes=(
            "单页、明细完整、税额印出、算术一致；票面印正常 PO 引用 \"PO-2026-1177\"。"
            if po_number
            else "单页、明细完整、税额印出、算术一致。"
        ),
    )


def _a2() -> Row:
    # Sample plan §6.1: n_lines = 20, one page, every line readable.
    items = (
        PACKING_TAPE, LABELS, CARTONS, STRETCH_FILM, BUBBLE_WRAP, GLOVES,
        STRAPPING, TONER, KNIFE, WALLET, LABELS, PACKING_TAPE,
        CARTONS, STRETCH_FILM, BUBBLE_WRAP, GLOVES, STRAPPING, TONER,
        KNIFE, WALLET,
    )
    return Row(
        doc_id="da-a2-01",
        klass="A",
        subclass="A2",
        target_question="高行数单页发票的行项目是否完整且金额正确？",
        source_family="clean_pdf",
        vendor=_vendor_slot("da-a2-01", 1),
        customer=2,
        issue_date=date(2026, 5, 6),
        items=items,
        number="INV-2026-1201",
        layout=LayoutSpec(header="left", table="band"),
        notes="单页 20 行；全部明细在一页内且可读，不与 A3 的分页构造重叠。",
    )


def _a3(index: int) -> Row:
    doc_id = f"da-a3-{index + 1:02d}"
    return Row(
        doc_id=doc_id,
        klass="A",
        subclass="A3",
        target_question="明细与合计分页时，合计区是否仍被正确绑定？",
        source_family="clean_pdf_multipage",
        vendor=_vendor_slot(doc_id, index),
        customer=index,
        issue_date=date(2026, 5, 20 + 7 * index),
        items=a3_items(A3_LINE_COUNT),
        number=f"INV-2026-{1301 + index}",
        layout=LayoutSpec(header="left", table="band", rows_per_page=A3_ROWS_PER_PAGE),
        notes="两页；全部明细在第 1 页；第 2 页只有完整小计/税额/总额区。",
    )


def _a4() -> Row:
    return Row(
        doc_id="da-a4-01",
        klass="A",
        subclass="A4",
        target_question="印出的 0.00 税额是否被当作已知值而不是缺失？",
        source_family="clean_pdf",
        vendor=_vendor_slot("da-a4-01", 2),
        customer=4,
        issue_date=date(2026, 6, 2),
        items=(WALLET, KNIFE),
        number="INV-2026-1401",
        tax_rate=Decimal("0.00"),
        tax_label="Sales Tax (Exempt)",
        layout=LayoutSpec(header="right", table="band"),
        notes="免税标签印出明确的税额行 0.00；gold 为 \"0.00\"，不是 null。",
    )


def _b_rows() -> Tuple[Row, ...]:
    """B1 scan, B2 photo, B3 heavy (one photo, one scan).

    Sample plan §6.1: B1 "扫描图像（mild/moderate）", B2 "照片图像
    （mild/moderate）", severe belongs to B3.
    """
    plan = (
        ("da-b1-01", "B1", "scan", "grayscale_scan", "mild", "png"),
        ("da-b1-02", "B1", "scan", "noise", "moderate", "png"),
        ("da-b1-03", "B1", "scan", "grayscale_scan", "moderate", "png"),
        ("da-b2-01", "B2", "photo", "perspective", "mild", "jpg"),
        ("da-b2-02", "B2", "photo", "rotate_small", "moderate", "jpg"),
        ("da-b2-03", "B2", "photo", "perspective", "moderate", "jpg"),
        ("da-b3-01", "B3", "photo", "low_res", "severe", "jpg"),
        ("da-b3-02", "B3", "scan", "fax", "severe", "png"),
    )
    rows = []
    for i, (doc_id, subclass, category, kind, severity, fmt) in enumerate(plan):
        question = {
            "B1": "扫描降质下核心字段是否仍可读？",
            "B2": "照片降质下核心字段是否仍可读？",
        }.get(subclass, "重降质下不可读字段是否被如实标记而不是猜测？")
        note = {
            "B1": "扫描降质（mild/moderate）；核心字段按构造保持可读。",
            "B2": "照片降质（mild/moderate）；核心字段按构造保持可读。",
        }.get(subclass, "按构造参数预填可读性；最终冻结核对属 T17 全量标注核对。")
        rows.append(
            Row(
                doc_id=doc_id,
                klass="B",
                subclass=subclass,
                target_question=question,
                source_family="degraded_photo" if category == "photo" else "degraded_scan",
                vendor=_vendor_slot(doc_id, i),
                customer=(i + 3) % len(CUSTOMERS),
                issue_date=date(2026, 6, 10 + i),
                items=(PACKING_TAPE, CARTONS, TONER)[: 3 if i % 2 == 0 else 2],
                number=f"INV-2026-{1501 + i}",
                layout=LayoutSpec(header="left", table="band"),
                render_mode="image",
                render_fmt=fmt,
                degradations=((kind, severity),),
                legibility="image_readable" if subclass != "B3" else "image_degraded_unverified",
                notes=note,
            )
        )
    return tuple(rows)


def _c1(index: int) -> Row:
    """Two deliberate line-amount conflicts: one PDF, one mild scan (§6.1)."""
    if index == 0:
        items = (
            Item("Pallet strapping kit", Decimal("3"), Decimal("5.00"), amount_override=Decimal("51.00")),
            Item("Shipping labels, 100 sheets", Decimal("1"), Decimal("25.00")),
        )
        number = "INV-2026-1601"
        date_ = date(2026, 7, 7)
    else:
        items = (
            Item("Nitrile gloves, size L, box of 100", Decimal("2"), Decimal("9.00"), amount_override=Decimal("81.00")),
            Item("Document wallet, A4, pack of 25", Decimal("1"), Decimal("50.00")),
        )
        number = "INV-2026-1602"
        date_ = date(2026, 7, 14)
    doc_id = f"da-c1-{index + 1:02d}"
    scanned = index == 1
    return Row(
        doc_id=doc_id,
        klass="C",
        subclass="C1",
        target_question="行金额与数量×单价不一致时是否只报 LINE_AMOUNT_MISMATCH？",
        source_family="clean_pdf_inconsistent",
        vendor=_vendor_slot(doc_id, index),
        customer=5 - index,
        issue_date=date_,
        items=items,
        number=number,
        layout=LayoutSpec(header="left", table="band"),
        render_mode="pdf" if not scanned else "image",
        render_fmt="pdf" if not scanned else "png",
        degradations=() if not scanned else (("grayscale_scan", "mild"),),
        legibility="clean_text" if not scanned else "image_readable",
        expected_issues=("LINE_AMOUNT_MISMATCH",),
        notes=(
            "数字换位：3 × 5.00 = 15.00 印成 51.00；2 × 9.00 = 18.00 印成 81.00。"
            + ("第二份为 mild 灰度扫描。" if scanned else "")
        ),
    )


def _c2() -> Row:
    return Row(
        doc_id="da-c2-01",
        klass="C",
        subclass="C2",
        target_question="小计与行金额之和不一致时是否只报 SUBTOTAL_MISMATCH？",
        source_family="clean_pdf_inconsistent",
        vendor=_vendor_slot("da-c2-01", 0),
        customer=5,
        issue_date=date(2026, 7, 21),
        items=(
            Item("Stretch film, 500 mm x 300 m", Decimal("2"), Decimal("25.00")),
            Item("Pallet strapping kit", Decimal("1"), Decimal("50.00")),
        ),
        number="INV-2026-1603",
        layout=LayoutSpec(header="left", table="band"),
        subtotal_printed=Decimal("110.00"),
        total_printed=Decimal("116.00"),
        expected_issues=("SUBTOTAL_MISMATCH",),
        notes="行金额之和 100.00，印出小计 110.00；总额 116.00 与小计+税额一致，故只违反规则 B。",
    )


def _c3() -> Row:
    return Row(
        doc_id="da-c3-01",
        klass="C",
        subclass="C3",
        target_question="总计关系不一致时是否只报 TOTAL_MISMATCH？",
        source_family="clean_pdf_inconsistent",
        vendor=_vendor_slot("da-c3-01", 2),
        customer=2,
        issue_date=date(2026, 7, 28),
        items=(
            Item("Bubble wrap, 1 m x 100 m", Decimal("2"), Decimal("25.00")),
            Item("Safety knife, retractable", Decimal("1"), Decimal("50.00")),
        ),
        number="INV-2026-1604",
        layout=LayoutSpec(header="left", table="band"),
        total_printed=Decimal("118.00"),
        expected_issues=("TOTAL_MISMATCH",),
        notes="小计 100.00、税额 6.00 都正确；总额印成 118.00，故只违反规则 C。",
    )


def _d1() -> Row:
    return Row(
        doc_id="da-d1-01",
        klass="D",
        subclass="D1",
        target_question="税额未印时是否记录为未知（null）而不是 0？",
        source_family="clean_pdf_missing_tax",
        vendor=_vendor_slot("da-d1-01", 1),
        customer=0,
        issue_date=date(2026, 8, 4),
        items=(PACKING_TAPE, LABELS, CARTONS),
        number="INV-2026-1701",
        tax_rate=None,
        layout=LayoutSpec(header="left", table="band"),
        notes="税额没有印在票面上；gold 的 tax_amount 为 null，evidence.status 为 not_found。",
    )


def _d2() -> Row:
    return Row(
        doc_id="da-d2-01",
        klass="D",
        subclass="D2",
        target_question="只印 PO 号时是否避免把 PO 号当作发票号？",
        source_family="clean_pdf_po_only",
        vendor=_vendor_slot("da-d2-01", 2),
        customer=3,
        issue_date=date(2026, 8, 11),
        items=(GLOVES, TONER, WALLET),
        number=None,
        po_number="PO-48213",
        po_label="Order No",
        layout=LayoutSpec(header="right", table="band", logo=True),
        expected_issues=("MISSING_REQUIRED_FIELD",),
        notes="票面只印 Order No 与 Delivery Note，没有发票号；gold 的 invoice_number 为 null。",
    )


def _e_discount() -> Row:
    return Row(
        doc_id="da-e-01",
        klass="E",
        subclass="E",
        target_question="含折扣的发票是否被判定为超范围并给出 HAS_DISCOUNT？",
        source_family="clean_pdf_out_of_scope",
        vendor=_vendor_slot("da-e-01", 0),
        customer=1,
        issue_date=date(2026, 8, 18),
        items=(CARTONS, STRETCH_FILM, GLOVES),
        number="INV-2026-1801",
        charges=(ChargeSpec("Discount", Decimal("50.00"), "discount"),),
        layout=LayoutSpec(header="left", table="band"),
        scope_status="UNSUPPORTED",
        scope_reasons=("HAS_DISCOUNT",),
        notes="折扣以负数行印在合计区。",
    )


def _e_surcharge() -> Row:
    return Row(
        doc_id="da-e-02",
        klass="E",
        subclass="E",
        target_question="含额外费用的发票是否被判定为超范围并给出 HAS_EXTRA_CHARGES？",
        source_family="clean_pdf_out_of_scope",
        vendor=_vendor_slot("da-e-02", 0),
        customer=4,
        issue_date=date(2026, 8, 25),
        items=(LABELS, TONER),
        number="INV-2026-1802",
        charges=(ChargeSpec("Freight surcharge", Decimal("35.00"), "shipping"),),
        layout=LayoutSpec(header="left", table="band"),
        scope_status="UNSUPPORTED",
        scope_reasons=("HAS_EXTRA_CHARGES",),
        notes="运费附加费以正数行印在合计区。",
    )


def _e_non_usd() -> Row:
    return Row(
        doc_id="da-e-03",
        klass="E",
        subclass="E",
        target_question="非美元发票是否被判定为超范围并给出 NON_USD_CURRENCY？",
        source_family="clean_pdf_out_of_scope",
        vendor=_vendor_slot("da-e-03", 1),
        customer=5,
        issue_date=date(2026, 9, 1),
        items=(PACKING_TAPE, BUBBLE_WRAP),
        number="INV-2026-1803",
        currency="CAD",
        tax_rate=Decimal("0.05"),
        tax_label="GST (5%)",
        layout=LayoutSpec(header="left", table="band"),
        scope_status="UNSUPPORTED",
        scope_reasons=("NON_USD_CURRENCY",),
        notes="票面印 Currency: CAD，金额仍是两位小数。",
    )


def _e_pages() -> Row:
    return Row(
        doc_id="da-e-04",
        klass="E",
        subclass="E",
        target_question="明细跨页且超过页数上限时是否给出 TABLE_SPANS_PAGES 与 TOO_MANY_PAGES？",
        source_family="clean_pdf_out_of_scope",
        vendor=_vendor_slot("da-e-04", 2),
        customer=0,
        issue_date=date(2026, 9, 8),
        items=insert_lines(60),
        number="INV-2026-1804",
        layout=LayoutSpec(header="left", table="band", rows_per_page=24),
        scope_status="UNSUPPORTED",
        scope_reasons=("TABLE_SPANS_PAGES", "TOO_MANY_PAGES"),
        notes="60 行、24 行/页：三页，明细跨页。",
    )


def _e_credit() -> Row:
    return Row(
        doc_id="da-e-05",
        klass="E",
        subclass="E",
        target_question="贷项通知单是否被判定为超范围并给出 CREDIT_NOTE？",
        source_family="clean_pdf_out_of_scope",
        vendor=_vendor_slot("da-e-05", 1),
        customer=1,
        issue_date=date(2026, 9, 15),
        items=(CARTONS, LABELS),
        number="CN-2026-0714",
        number_label="Credit Note No",
        due_days=None,
        title="CREDIT NOTE",
        doc_kind="credit_note",
        layout=LayoutSpec(header="left", table="band"),
        scope_status="UNSUPPORTED",
        scope_reasons=("CREDIT_NOTE",),
        notes="金额以负号印出；规则按绝对值复核。",
    )


def _e_multiple() -> Row:
    return Row(
        doc_id="da-e-06",
        klass="E",
        subclass="E",
        target_question="一个文件含多张发票时是否被判定为超范围并给出 MULTIPLE_INVOICES？",
        source_family="clean_pdf_out_of_scope",
        vendor=_vendor_slot("da-e-06", 2),
        customer=2,
        issue_date=date(2026, 9, 22),
        items=(),
        number=None,
        doc_kind="multi_invoice",
        layout=LayoutSpec(header="left", table="band"),
        children=(
            # Test-split suppliers only: the vendor catalogue is split-isolated.
            ChildSpec("INV-2026-1901", 3, (PACKING_TAPE, LABELS)),
            ChildSpec("INV-2026-1902", 5, (TONER, WALLET)),
        ),
        scope_status="UNSUPPORTED",
        scope_reasons=("MULTIPLE_INVOICES",),
        notes="两页，每页一张发票。",
    )


def _build_rows() -> Tuple[Row, ...]:
    rows = [_a1(i) for i in range(6)]
    rows.append(_a2())
    rows.extend(_a3(i) for i in range(2))
    rows.append(_a4())
    rows.extend(_b_rows())
    rows.extend(_c1(i) for i in range(2))
    rows.append(_c2())
    rows.append(_c3())
    rows.append(_d1())
    rows.append(_d2())
    rows.extend((_e_discount(), _e_surcharge(), _e_non_usd(), _e_pages(), _e_credit(), _e_multiple()))
    return tuple(rows)


ROWS: Tuple[Row, ...] = _build_rows()
BY_ID: Dict[str, Row] = {row.doc_id: row for row in ROWS}


# ---------------------------------------------------------------------------
# Configuration checks.  These run at import so a typo cannot slip into the
# generated set.
# ---------------------------------------------------------------------------


def _check_configuration() -> None:
    ids = [row.doc_id for row in ROWS]
    if len(ids) != len(set(ids)):
        raise MatrixError("duplicate doc_id in the matrix")
    if set(ids) != set(SPLIT):
        unknown = sorted(set(ids) - set(SPLIT))
        missing = sorted(set(SPLIT) - set(ids))
        raise MatrixError(f"matrix and split table disagree: unknown={unknown} missing={missing}")
    if len(ROWS) != 30:
        raise MatrixError(f"the main matrix must hold exactly 30 documents, got {len(ROWS)}")

    for name, expected in CLASS_COUNTS.items():
        actual = sum(1 for row in ROWS if row.klass == name)
        if actual != expected:
            raise MatrixError(f"class {name}: expected {expected}, got {actual}")
    for name, expected in SUBCLASS_COUNTS.items():
        actual = sum(1 for row in ROWS if row.subclass == name)
        if actual != expected:
            raise MatrixError(f"subclass {name}: expected {expected}, got {actual}")

    dev = [row.doc_id for row in ROWS if row.split == "dev"]
    test = [row.doc_id for row in ROWS if row.split == "test"]
    if len(dev) != 10 or len(test) != 20:
        raise MatrixError(f"split must be 10 dev / 20 test, got {len(dev)} / {len(test)}")

    for row in ROWS:
        if row.klass in ("A", "B", "C", "D") and row.scope_status != "SUPPORTED":
            raise MatrixError(f"{row.doc_id}: classes A-D must be SUPPORTED")
        if row.klass == "E" and row.scope_status != "UNSUPPORTED":
            raise MatrixError(f"{row.doc_id}: class E must be UNSUPPORTED")
        for reason in row.scope_reasons:
            if reason not in codes.SCOPE_REASON:
                raise MatrixError(f"{row.doc_id}: {reason!r} is not a contract scope_reason")
        for issue in row.expected_issues:
            if issue not in codes.ISSUE:
                raise MatrixError(f"{row.doc_id}: {issue!r} is not a contract issue code")
        if row.klass != "E" and row.scope_reasons:
            raise MatrixError(f"{row.doc_id}: only E documents carry scope reasons")
        if row.klass == "C" and len(row.expected_issues) != 1:
            raise MatrixError(f"{row.doc_id}: a C document has exactly one target issue")
        if row.doc_kind == "multi_invoice" and not row.children:
            raise MatrixError(f"{row.doc_id}: multi_invoice needs children")
        if row.render_mode == "image" and row.render_fmt == "pdf":
            raise MatrixError(f"{row.doc_id}: an image document needs a raster format")
        for item in row.items:
            if item.amount <= 0:
                raise MatrixError(f"{row.doc_id}: line amounts must be positive")

    vendors = [vendor.name for vendor in VENDORS]
    if len(vendors) != len(set(vendors)):
        raise MatrixError("the vendor catalogue holds a duplicate name")
    customers = [customer.name for customer in CUSTOMERS]
    if len(customers) != len(set(customers)):
        raise MatrixError("the customer catalogue holds a duplicate name")

    for row in ROWS:
        if row.vendor >= len(VENDORS) or row.customer >= len(CUSTOMERS):
            raise MatrixError(f"{row.doc_id}: vendor/customer index out of range")
        for child in row.children:
            if child.vendor >= len(VENDORS):
                raise MatrixError(f"{row.doc_id}: child vendor index out of range")

    # Sample plan §6.3: a supplier entity belongs to one split only.
    vendor_splits: Dict[str, set] = {}
    for row in ROWS:
        names = [VENDORS[row.vendor].name] + [VENDORS[child.vendor].name for child in row.children]
        for name in names:
            vendor_splits.setdefault(name, set()).add(row.split)
    crossing = sorted(name for name, splits in vendor_splits.items() if len(splits) > 1)
    if crossing:
        raise MatrixError(f"vendors cross the dev/test boundary: {crossing}")
    for split, slots in (("dev", DEV_VENDOR_SLOTS), ("test", TEST_VENDOR_SLOTS)):
        used = {row.vendor for row in ROWS if row.split == split}
        if used - set(slots):
            raise MatrixError(f"{split}: a row uses a vendor outside its split half")


_check_configuration()

__all__ = [
    "A3_LINE_COUNT",
    "A3_ROWS_PER_PAGE",
    "a3_items",
    "BY_ID",
    "CLASS_COUNTS",
    "CUSTOMERS",
    "ChargeSpec",
    "ChildSpec",
    "Customer",
    "DEV_VENDOR_SLOTS",
    "Item",
    "insert_lines",
    "LIMITS",
    "LayoutSpec",
    "MatrixError",
    "ROWS",
    "Row",
    "SEED",
    "SPLIT",
    "SUBCLASS_COUNTS",
    "TEST_VENDOR_SLOTS",
    "VENDORS",
    "Vendor",
    "round_money",
]
