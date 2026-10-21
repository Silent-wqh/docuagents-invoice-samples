"""Tests for the boundary and technical fixtures (T15).

The fixtures live beside the main sample set, not inside it:

* ``datasets/docuagents_v1/boundary_fixtures/`` holds the file-level fixtures,
  their gold records, their previews and their own manifest;
* ``tests/fixtures/numeric/`` holds the object-level rounding cases, which are
  covered by pytest only and never enter the dataset manifest.

Everything here runs offline: ``tests/conftest.py`` installs the session
network guard.  The heavy tests carry ``@pytest.mark.slow`` so a run can
deselect them with ``-m "not slow"``.

Run:

    pytest tests/test_fixtures.py -q
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sys
from dataclasses import replace
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # pragma: no cover - depends on the pytest version
    import _pytest.mark

    _config = getattr(_pytest.mark.MARK_GEN, "_config", None)
    if _config is not None:
        _config.addinivalue_line("markers", "slow: a full dataset generation; deselect with -m 'not slow'")
except (ImportError, AttributeError):  # pragma: no cover
    pass

from generator.layout import build_pages  # noqa: E402
from generator.spec import DocSpec  # noqa: E402

from docuagents_gen import build, codes, fixtures, generate, gold, matrix  # noqa: E402

from test_docuagents_gen import SchemaError, validate_schema  # noqa: E402

#: The §6.4 list: 18 content fixtures.
CONTENT_FIXTURE_IDS = (
    "da-fx-po", "da-fx-poref", "da-fx-cur", "da-fx-date", "da-fx-lang",
    "da-fx-taxunk", "da-fx-incl", "da-fx-prepay", "da-fx-21ln", "da-fx-empty",
    "da-fx-span", "da-fx-3pg", "da-fx-unread", "da-fx-scan", "da-fx-mixed",
    "da-fx-blank", "da-fx-dup", "da-fx-prec",
)

#: The four technical fixtures.
TECHNICAL_FIXTURE_IDS = ("da-fx-crc", "da-fx-zero", "da-fx-enc", "da-fx-fake")

#: The fixtures that reach auto_accepted: SUPPORTED and no issue.
SUPPORTED_AUTO = {
    "da-fx-poref", "da-fx-scan", "da-fx-mixed", "da-fx-blank", "da-fx-dup", "da-fx-prec",
}

#: The fixtures that are in scope.  Three of them carry an issue, so they go to
#: review even though the scope is SUPPORTED (sample plan §6.3, §6.4 items 4,
#: 10 and 13).
SUPPORTED = SUPPORTED_AUTO | {"da-fx-date", "da-fx-empty", "da-fx-unread"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def clone_tree(source: Path, target: Path) -> None:
    shutil.copytree(source, target, copy_function=_link_or_copy)


def _link_or_copy(src: str, dst: str) -> str:
    try:
        os.link(src, dst)
        return dst
    except OSError:
        return shutil.copy2(src, dst)


def replace_bytes(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".new")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def tree_hashes(root: Path) -> Dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def numeric_cases() -> List[Dict[str, Any]]:
    directory = ROOT / "tests" / "fixtures" / "numeric"
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))]


# ---------------------------------------------------------------------------
# session fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def fixture_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    parent = tmp_path_factory.mktemp("fixtures")
    out = parent / "boundary_fixtures"
    fixtures.generate_boundary_fixtures(out)
    return out


@pytest.fixture(scope="session")
def fixture_manifest(fixture_dir: Path) -> Dict[str, Any]:
    return json.loads((fixture_dir / "manifest.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def schema() -> Dict[str, Any]:
    return json.loads((ROOT / "schemas" / "gold.schema.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# the §6.4 list: the fixture table is complete and its fields are usable
# ---------------------------------------------------------------------------


def test_the_fixture_table_covers_the_whole_6_4_list() -> None:
    ids = [row["doc_id"] for row in fixtures.FIXTURES]
    assert len(ids) == len(set(ids)), "duplicate fixture id"
    content = [row["doc_id"] for row in fixtures.FIXTURES if row["coverage"] == "file"
               and row["fixture_kind"] == "content"]
    technical = [row["doc_id"] for row in fixtures.FIXTURES if row["fixture_kind"] == "technical"]
    assert tuple(content) == CONTENT_FIXTURE_IDS
    assert tuple(technical) == TECHNICAL_FIXTURE_IDS
    assert len(fixtures.FIXTURES) == 22


def test_every_fixture_row_carries_the_required_fields() -> None:
    required = {
        "doc_id", "slug", "plan_item", "coverage", "fixture_kind", "expected_status",
        "must_not_auto_accept", "expected_scope_status", "expected_issues", "notes",
    }
    for row in fixtures.FIXTURES:
        missing = required - set(row)
        assert not missing, (row["doc_id"], sorted(missing))
        assert row["plan_item"].startswith("§6.4"), row["doc_id"]
        assert row["expected_status"] in ("auto_accepted", "needs_review", "failed")
        assert isinstance(row["must_not_auto_accept"], bool)
        assert row["expected_scope_status"] in ("SUPPORTED", "UNSUPPORTED", "UNKNOWN")
        if row["fixture_kind"] == "technical":
            assert row.get("expected_technical_error"), row["doc_id"]
            assert row["expected_status"] == "failed", row["doc_id"]
        else:
            for reason in row["expected_scope_reasons"]:
                assert reason in codes.SCOPE_REASON, (row["doc_id"], reason)
            for issue in row["expected_issues"]:
                assert issue in codes.ISSUE, (row["doc_id"], issue)


def test_the_manifest_counts_only_the_file_level_fixtures(
    fixture_manifest: Dict[str, Any],
) -> None:
    counts = fixture_manifest["counts"]
    assert counts["content"] == 18
    assert counts["technical"] == 4
    assert counts["file"] == 22
    # Object-level cases are covered by pytest and never enter this manifest.
    assert "object" not in counts
    assert fixture_manifest["seed"] == fixtures.FIXTURE_SEED == 2027


# ---------------------------------------------------------------------------
# isolation (D4)
# ---------------------------------------------------------------------------


def _fixture_vendor_ids() -> set:
    return {f"vendor:{v.key}" for v in fixtures.FIXTURE_VENDORS}


def _fixture_source_families() -> set:
    return {row["source_family"] for row in fixtures.FIXTURES if row["fixture_kind"] == "content"}


def _main_vendor_ids() -> set:
    ids = {f"vendor:{matrix.VENDORS[row.vendor].key}" for row in matrix.ROWS}
    ids |= {f"vendor:{matrix.VENDORS[c.vendor].key}" for row in matrix.ROWS for c in row.children}
    return ids


def test_fixture_vendors_do_not_intersect_the_main_set() -> None:
    assert _fixture_vendor_ids() & _main_vendor_ids() == set()


def test_fixture_source_families_do_not_intersect_the_main_set() -> None:
    main = {row.source_family for row in matrix.ROWS}
    assert _fixture_source_families() & main == set()
    # The stronger statement: no test-split family either.
    main_test = {row.source_family for row in matrix.ROWS if row.split == "test"}
    assert _fixture_source_families() & main_test == set()


def test_fixture_doc_ids_do_not_collide_and_the_seed_differs() -> None:
    assert {row["doc_id"] for row in fixtures.FIXTURES} & set(matrix.BY_ID) == set()
    assert fixtures.FIXTURE_SEED != matrix.SEED


def test_fixture_party_names_do_not_reuse_a_main_name() -> None:
    main_names = {v.name for v in matrix.VENDORS} | {c.name for c in matrix.CUSTOMERS}
    fixture_names = {v.name for v in fixtures.FIXTURE_VENDORS}
    fixture_names |= {c.name for c in fixtures.FIXTURE_CUSTOMERS}
    assert fixture_names & main_names == set()


def test_isolation_is_reported_in_the_fixture_manifest(fixture_manifest: Dict[str, Any]) -> None:
    isolation = fixture_manifest["isolation"]
    assert isolation["vendor_overlap"] == []
    assert isolation["source_family_overlap"] == []
    assert isolation["doc_id_overlap"] == []


# ---------------------------------------------------------------------------
# object-level rounding (D8)
# ---------------------------------------------------------------------------


def test_the_object_level_cases_are_at_least_three_and_hand_computed() -> None:
    cases = numeric_cases()
    assert len(cases) >= 3
    ids = {case["case_id"] for case in cases}
    assert "half-cent-3x0.335" in ids
    assert "half-cent-1.5x0.125" in ids
    expected = {case["case_id"]: case["expected_line_amount"] for case in cases}
    assert expected["half-cent-3x0.335"] == "1.01"
    assert expected["half-cent-1.5x0.125"] == "0.19"


def test_every_object_level_case_rounds_half_up() -> None:
    for case in numeric_cases():
        assert case["coverage"] == "object_level", case["case_id"]
        assert case["rounding"] == "ROUND_HALF_UP", case["case_id"]
        for field in ("quantity", "unit_price", "expected_line_amount"):
            assert isinstance(case[field], str), (case["case_id"], field)
        product = Decimal(case["quantity"]) * Decimal(case["unit_price"])
        assert matrix.round_money(product) == Decimal(case["expected_line_amount"]), case["case_id"]
        assert product.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) == Decimal(
            case["expected_line_amount"]
        ), case["case_id"]


def test_the_tie_case_discriminates_half_up_from_half_even() -> None:
    case = next(c for c in numeric_cases() if c["case_id"] == "half-cent-1x0.125")
    product = Decimal(case["quantity"]) * Decimal(case["unit_price"])
    assert product == Decimal("0.125")
    assert matrix.round_money(product) == Decimal("0.13")
    assert product.quantize(Decimal("0.01"), rounding="ROUND_HALF_EVEN") == Decimal("0.12")


def test_object_level_cases_never_enter_the_file_level_counts(
    fixture_manifest: Dict[str, Any],
) -> None:
    declared = {entry["path"] for entry in fixture_manifest["files"]}
    assert not any("numeric" in path for path in declared)
    assert fixture_manifest["counts"]["file"] == 22
    assert all(entry.get("fixture_id", "").startswith("da-fx-")
               for entry in fixture_manifest["files"] if entry.get("fixture_id"))


# ---------------------------------------------------------------------------
# generation and structure (D1, D3, D7)
# ---------------------------------------------------------------------------


def test_the_fixture_tree_has_the_planned_directories(fixture_dir: Path) -> None:
    for name in ("documents", "gold", "previews", "reports", "technical"):
        assert (fixture_dir / name).is_dir(), name
    assert (fixture_dir / "previews" / "pages").is_dir()
    assert (fixture_dir / "reports" / "legibility_review.json").is_file()
    assert (fixture_dir / "manifest.json").is_file()


def test_every_content_fixture_has_a_document_a_gold_and_previews(fixture_dir: Path,
                                                                  fixture_manifest: Dict[str, Any]) -> None:
    gold_files = {p.stem for p in (fixture_dir / "gold").iterdir()}
    assert gold_files == set(CONTENT_FIXTURE_IDS)
    for doc_id in CONTENT_FIXTURE_IDS:
        record = json.loads((fixture_dir / "gold" / f"{doc_id}.json").read_text(encoding="utf-8"))
        document = fixture_dir / record["file"]["file_name"]
        assert document.is_file(), (doc_id, record["file"]["file_name"])
        for page in range(1, record["file"]["page_count"] + 1):
            assert (fixture_dir / "previews" / "pages" / f"{doc_id}.p{page}.png").is_file(), (doc_id, page)


def test_the_four_technical_fixtures_have_files_and_no_gold(fixture_dir: Path) -> None:
    for doc_id in TECHNICAL_FIXTURE_IDS:
        matches = sorted((fixture_dir / "technical").glob(f"{doc_id}.*"))
        assert len(matches) == 1, (doc_id, matches)
        assert not (fixture_dir / "gold" / f"{doc_id}.json").exists(), doc_id


def test_the_technical_fixtures_carry_the_expected_error(fixture_manifest: Dict[str, Any]) -> None:
    expected = {
        "da-fx-crc": "UNREADABLE_FILE",
        "da-fx-zero": "EMPTY_FILE",
        "da-fx-enc": "ENCRYPTED_FILE",
        "da-fx-fake": "INVALID_FILE_TYPE",
    }
    entries = {entry["fixture_id"]: entry for entry in fixture_manifest["files"]
               if entry.get("fixture_id") in TECHNICAL_FIXTURE_IDS}
    assert set(entries) == set(expected)
    for doc_id, code in expected.items():
        assert entries[doc_id]["expected"]["technical_error"] == code, doc_id
        assert entries[doc_id]["expected"]["business_status"] == "failed", doc_id
        assert entries[doc_id]["artifact_role"] == "generated", doc_id


def test_the_technical_fixture_bytes_cannot_be_read_as_an_invoice(fixture_dir: Path) -> None:
    import pypdfium2 as pdfium
    import pdfplumber

    zero = fixture_dir / "technical" / "da-fx-zero.pdf"
    assert zero.stat().st_size == 0
    with pytest.raises(Exception):
        pdfplumber.open(str(zero))

    fake = fixture_dir / "technical" / "da-fx-fake.pdf"
    assert fake.read_bytes().startswith(b"This is not a PDF")
    with pytest.raises(Exception):
        pdfplumber.open(str(fake))

    enc = fixture_dir / "technical" / "da-fx-enc.pdf"
    with pytest.raises(Exception):
        pdfium.PdfDocument(enc.read_bytes())

    crc = fixture_dir / "technical" / "da-fx-crc.pdf"
    with pytest.raises(Exception):
        pdfplumber.open(str(crc))


def test_the_manifest_covers_every_fixture_file_and_keeps_its_own_hash_out(
    fixture_dir: Path, fixture_manifest: Dict[str, Any]
) -> None:
    declared = {entry["path"] for entry in fixture_manifest["files"]}
    on_disk = {p.relative_to(fixture_dir).as_posix() for p in fixture_dir.rglob("*") if p.is_file()}
    assert declared | {"manifest.json"} == on_disk
    assert "manifest.json" not in declared
    roles = {entry["artifact_role"] for entry in fixture_manifest["files"]}
    assert roles <= {"generated", "frozen_review", "runtime_report"}


def test_the_fixture_manifest_carries_the_required_header_fields(
    fixture_manifest: Dict[str, Any],
) -> None:
    for field in (
        "schema_version", "contract_version", "seed", "upstream_commit", "generator_commit",
        "generator_source_digest", "fixtures_digest", "environment_digest",
        "digest_method_version", "counts", "files",
    ):
        assert field in fixture_manifest, field
    assert fixture_manifest["seed"] == fixtures.FIXTURE_SEED
    assert fixture_manifest["upstream_commit"] == generate.UPSTREAM_COMMIT
    assert fixture_manifest["fixtures_digest"] == fixtures.fixtures_digest()
    assert len(fixture_manifest["fixtures_digest"]) == 64


def test_the_fixture_paths_are_sorted(fixture_manifest: Dict[str, Any]) -> None:
    paths = [entry["path"] for entry in fixture_manifest["files"]]
    assert paths == sorted(paths)


# ---------------------------------------------------------------------------
# gold: schema and the routing table (D3, D5)
# ---------------------------------------------------------------------------


def test_every_fixture_gold_passes_the_schema(fixture_dir: Path,
                                              schema: Dict[str, Any]) -> None:
    for doc_id in CONTENT_FIXTURE_IDS:
        record = json.loads((fixture_dir / "gold" / f"{doc_id}.json").read_text(encoding="utf-8"))
        try:
            validate_schema(record, schema)
        except SchemaError as exc:  # pragma: no cover - the message carries the path
            pytest.fail(f"{doc_id}: {exc}")


def test_every_fixture_gold_carries_the_routing_expectation(fixture_dir: Path) -> None:
    for doc_id in CONTENT_FIXTURE_IDS:
        record = json.loads((fixture_dir / "gold" / f"{doc_id}.json").read_text(encoding="utf-8"))
        assert record["expected_status"] in ("auto_accepted", "needs_review", "failed"), doc_id
        assert isinstance(record["must_not_auto_accept"], bool), doc_id
        assert record["split"] == "unassigned", doc_id
        assert record["class"] == "?", doc_id


def test_the_routing_table_matches_the_6_4_list(fixture_dir: Path) -> None:
    for row in fixtures.FIXTURES:
        if row["fixture_kind"] != "content":
            continue
        doc_id = row["doc_id"]
        record = json.loads((fixture_dir / "gold" / f"{doc_id}.json").read_text(encoding="utf-8"))
        assert record["scope"]["status"] == row["expected_scope_status"], doc_id
        assert record["scope"]["reasons"] == list(row["expected_scope_reasons"]), doc_id
        assert record["expected_issues"] == list(row["expected_issues"]), doc_id
        assert record["expected_status"] == row["expected_status"], doc_id
        assert record["must_not_auto_accept"] == row["must_not_auto_accept"], doc_id


def test_supported_fixtures_are_the_ones_the_6_4_list_names() -> None:
    supported = {row["doc_id"] for row in fixtures.FIXTURES
                 if row["expected_scope_status"] == "SUPPORTED"}
    assert supported == SUPPORTED
    for row in fixtures.FIXTURES:
        if row["doc_id"] in SUPPORTED_AUTO:
            assert row["expected_status"] == "auto_accepted", row["doc_id"]
            assert row["must_not_auto_accept"] is False, row["doc_id"]
        elif row["fixture_kind"] == "content":
            assert row["must_not_auto_accept"] is True, row["doc_id"]


def test_the_purchase_order_fixture_is_not_an_invoice(fixture_dir: Path) -> None:
    record = json.loads((fixture_dir / "gold" / "da-fx-po.json").read_text(encoding="utf-8"))
    assert record["scope"]["status"] == "UNSUPPORTED"
    assert record["scope"]["reasons"] == ["PURCHASE_ORDER"]
    assert record["scope"]["signals"]["document_type"] == "purchase_order"
    assert record["must_not_auto_accept"] is True


def test_the_po_reference_fixture_keeps_the_invoice_number(fixture_dir: Path) -> None:
    record = json.loads((fixture_dir / "gold" / "da-fx-poref.json").read_text(encoding="utf-8"))
    assert record["scope"]["status"] == "SUPPORTED"
    assert record["invoice"]["purchase_order_number"] == "PO-2026-7701"
    assert record["invoice"]["invoice_number"] != record["invoice"]["purchase_order_number"]
    assert record["invoice"]["invoice_number"] == "INV-2027-2101"


def test_the_ambiguous_date_keeps_the_raw_text_and_no_iso_value(fixture_dir: Path) -> None:
    record = json.loads((fixture_dir / "gold" / "da-fx-date.json").read_text(encoding="utf-8"))
    assert record["invoice"]["invoice_date"] is None
    entry = record["evidence"]["invoice_date"]
    assert entry["status"] == "ambiguous"
    assert entry["text"] == "03/04/2026"
    assert "AMBIGUOUS_DATE" in record["expected_issues"]


def test_the_currency_fixture_reports_an_unconfirmed_currency(fixture_dir: Path) -> None:
    record = json.loads((fixture_dir / "gold" / "da-fx-cur.json").read_text(encoding="utf-8"))
    assert record["scope"]["status"] == "UNKNOWN"
    assert record["invoice"]["currency"] is None
    assert record["printing"]["currency_header"] is None
    assert record["evidence"]["currency"]["status"] == "not_found"
    assert set(record["expected_issues"]) >= {"CURRENCY_UNCONFIRMED", "SCOPE_UNKNOWN"}


def test_the_non_english_fixture_records_its_language(fixture_dir: Path) -> None:
    record = json.loads((fixture_dir / "gold" / "da-fx-lang.json").read_text(encoding="utf-8"))
    assert record["scope"]["status"] == "UNSUPPORTED"
    assert "NON_ENGLISH" in record["scope"]["reasons"]
    assert record["scope"]["signals"]["language"] == "other"


def test_the_tax_basis_fixtures_read_the_printed_note(fixture_dir: Path) -> None:
    unknown = json.loads((fixture_dir / "gold" / "da-fx-taxunk.json").read_text(encoding="utf-8"))
    assert unknown["scope"]["status"] == "UNKNOWN"
    assert unknown["scope"]["signals"]["unit_price_basis"] == "unknown"
    assert "SCOPE_UNKNOWN" in unknown["expected_issues"]

    inclusive = json.loads((fixture_dir / "gold" / "da-fx-incl.json").read_text(encoding="utf-8"))
    assert inclusive["scope"]["status"] == "UNSUPPORTED"
    assert inclusive["scope"]["signals"]["unit_price_basis"] == "tax_inclusive"
    assert inclusive["scope"]["reasons"] == ["TAX_INCLUSIVE"]


def test_the_prepayment_fixture_reads_the_printed_balance_row(fixture_dir: Path) -> None:
    record = json.loads((fixture_dir / "gold" / "da-fx-prepay.json").read_text(encoding="utf-8"))
    assert record["scope"]["status"] == "UNSUPPORTED"
    assert record["scope"]["reasons"] == ["PREPAYMENT_OR_PRIOR_BALANCE"]
    assert record["scope"]["signals"]["prepayment_or_prior_balance"] == "present"


def test_the_twenty_one_line_fixture_is_the_line_limit_case(fixture_dir: Path) -> None:
    record = json.loads((fixture_dir / "gold" / "da-fx-21ln.json").read_text(encoding="utf-8"))
    assert record["scope"]["signals"]["observed_row_count"] == 21
    assert record["scope"]["reasons"] == ["TOO_MANY_LINES"]
    assert record["file"]["page_count"] == 1


def test_the_empty_table_fixture_does_not_claim_a_row_limit(fixture_dir: Path) -> None:
    record = json.loads((fixture_dir / "gold" / "da-fx-empty.json").read_text(encoding="utf-8"))
    assert record["scope"]["signals"]["observed_row_count"] == 0
    assert "TOO_MANY_LINES" not in record["scope"]["reasons"]
    assert record["scope"]["status"] == "SUPPORTED"
    assert record["expected_issues"] == ["MISSING_REQUIRED_FIELD"]


def test_the_page_shape_fixtures_route_as_named(fixture_dir: Path) -> None:
    span = json.loads((fixture_dir / "gold" / "da-fx-span.json").read_text(encoding="utf-8"))
    assert span["scope"]["reasons"] == ["TABLE_SPANS_PAGES"]
    assert span["scope"]["signals"]["table_layout"] == "spans_pages"

    three = json.loads((fixture_dir / "gold" / "da-fx-3pg.json").read_text(encoding="utf-8"))
    assert three["file"]["page_count"] == 3
    assert three["scope"]["reasons"] == ["TOO_MANY_PAGES"]
    assert three["scope"]["signals"]["table_layout"] == "single_page"


def test_the_duplicate_line_fixture_keeps_both_lines(fixture_dir: Path) -> None:
    record = json.loads((fixture_dir / "gold" / "da-fx-dup.json").read_text(encoding="utf-8"))
    items = record["invoice"]["line_items"]
    assert len(items) == 2
    assert items[0]["description"] == items[1]["description"]
    assert items[0]["amount"] == items[1]["amount"]
    assert [item["index"] for item in items] == [0, 1]


def test_the_medium_fixtures_are_supported(fixture_dir: Path) -> None:
    for doc_id in ("da-fx-scan", "da-fx-mixed", "da-fx-blank"):
        record = json.loads((fixture_dir / "gold" / f"{doc_id}.json").read_text(encoding="utf-8"))
        assert record["scope"]["status"] == "SUPPORTED", doc_id
        assert record["expected_status"] == "auto_accepted", doc_id
        assert record["must_not_auto_accept"] is False, doc_id


# ---------------------------------------------------------------------------
# the unreadable fixture (D6)
# ---------------------------------------------------------------------------


def test_the_unreadable_fixture_hides_the_true_values(fixture_dir: Path) -> None:
    record = json.loads((fixture_dir / "gold" / "da-fx-unread.json").read_text(encoding="utf-8"))
    row = fixtures.BY_ID["da-fx-unread"]
    assert record["must_not_auto_accept"] is True
    assert "UNREADABLE_FIELD" in record["expected_issues"]
    # The unreadable fields are null, and their evidence is unreadable.
    for field in row["unreadable_fields"]:
        assert record["invoice"][field] is None, field
        entry = record["evidence"][field]
        assert entry["status"] == "unreadable", field
        assert entry["text"] is None, field
    # No hidden truth leaks into the gold: the true printed amounts are absent.
    text = json.dumps(record)
    truth = json.dumps(fixtures.hidden_truth("da-fx-unread"), ensure_ascii=False)
    for value in ("151.20", "160.27"):
        assert value not in text, value
    assert truth not in text


def test_the_unreadable_fixture_has_a_preview_and_a_pending_review(fixture_dir: Path) -> None:
    record = json.loads((fixture_dir / "gold" / "da-fx-unread.json").read_text(encoding="utf-8"))
    for page in range(1, record["file"]["page_count"] + 1):
        assert (fixture_dir / "previews" / "pages" / f"da-fx-unread.p{page}.png").is_file()

    review = json.loads((fixture_dir / "reports" / "legibility_review.json").read_text(encoding="utf-8"))
    assert review["status"] == "pending_human_confirmation"
    assert review["reviewer"] is None
    assert review["confirmation_path"]
    assert review["documents"], "the review must name the documents it covers"
    entry = next(d for d in review["documents"] if d["doc_id"] == "da-fx-unread")
    assert entry["file_sha256"] == record["file"]["sha256"]
    fields = {item["field"]: item["status"] for item in entry["fields"]}
    for field in fixtures.BY_ID["da-fx-unread"]["unreadable_fields"]:
        assert fields[field] == "unreadable", field


def test_the_hidden_truth_stays_out_of_every_generated_artifact(fixture_dir: Path) -> None:
    needles = []
    for doc_id in ("da-fx-unread",):
        for value in fixtures.hidden_truth(doc_id).values():
            needles.append(str(value))
    for path in sorted(fixture_dir.rglob("*")):
        if not path.is_file() or path.suffix not in {".json", ".txt"}:
            continue
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            assert needle not in text, (path.relative_to(fixture_dir).as_posix(), needle)


# ---------------------------------------------------------------------------
# reproducibility and drift (D9, AC6)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_two_generations_are_byte_identical(fixture_dir: Path, tmp_path: Path) -> None:
    second = tmp_path / "second"
    fixtures.generate_boundary_fixtures(second)
    first_hashes = tree_hashes(fixture_dir)
    second_hashes = tree_hashes(second)
    generated = {
        entry["path"] for entry in json.loads(
            (fixture_dir / "manifest.json").read_text(encoding="utf-8"))["files"]
        if entry["artifact_role"] == "generated"
    }
    for rel in sorted(generated):
        assert first_hashes[rel] == second_hashes[rel], rel
    assert first_hashes["manifest.json"] == second_hashes["manifest.json"]


@pytest.mark.slow
def test_a_permuted_order_writes_the_same_files(fixture_dir: Path, tmp_path: Path) -> None:
    order = [row["doc_id"] for row in fixtures.FIXTURES]
    permutation = order[1::2] + order[0::2]
    assert sorted(permutation) == sorted(order)
    out = tmp_path / "permuted"
    fixtures.generate_boundary_fixtures(out, order=permutation)
    assert tree_hashes(out) == tree_hashes(fixture_dir)


def test_the_order_must_be_a_permutation(tmp_path: Path) -> None:
    with pytest.raises(fixtures.FixtureError):
        fixtures.generate_boundary_fixtures(tmp_path / "bad", order=["da-fx-po"])


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param(lambda d: replace_bytes(d / "documents" / "da-fx-poref.pdf",
                                             (d / "documents" / "da-fx-poref.pdf").read_bytes() + b"\n"),
                     id="changed-document"),
        pytest.param(lambda d: replace_bytes(d / "gold" / "da-fx-po.json", b'{"broken":1}'),
                     id="changed-gold"),
        pytest.param(lambda d: replace_bytes(d / "previews" / "pages" / "da-fx-poref.p1.png", b"\x89PNG\r\n\x1a\n"),
                     id="changed-preview"),
        pytest.param(lambda d: replace_bytes(d / "technical" / "da-fx-zero.pdf", b"not empty"),
                     id="changed-technical"),
        pytest.param(lambda d: (d / "documents" / "da-fx-extra.pdf").write_bytes(b"%PDF-1.4\n"),
                     id="undeclared-file"),
    ],
)
def test_check_rejects_fixture_drift(fixture_dir: Path, tmp_path: Path, damage) -> None:
    clone = tmp_path / "clone"
    clone_tree(fixture_dir, clone)
    clean = fixtures.check_fixtures(clone, fixture_dir / "manifest.json")
    assert clean["ok"], clean["failures"]
    damage(clone)
    result = fixtures.check_fixtures(clone, fixture_dir / "manifest.json")
    assert not result["ok"]
    assert result["failures"]


def test_check_rejects_a_changed_fixture_manifest(fixture_dir: Path, tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone_tree(fixture_dir, clone)
    path = clone / "manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["seed"] = 9999
    replace_bytes(path, generate.canonical_bytes(payload))
    result = fixtures.check_fixtures(clone, fixture_dir / "manifest.json")
    assert not result["ok"]


def test_check_reports_the_comparison_counts(fixture_dir: Path, tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone_tree(fixture_dir, clone)
    result = fixtures.check_fixtures(clone, fixture_dir / "manifest.json")
    assert result["ok"], result["failures"]
    counts = result["counts"]
    assert counts["missing"] == 0
    assert counts["extra"] == 0
    assert counts["generated_compared"] == counts["generated_total"]
    assert counts["generated_total"] >= 22


# ---------------------------------------------------------------------------
# integration with --check and the main set (D2, AC6)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_the_main_check_still_passes_and_sees_the_fixtures(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "tree"
    code = generate.main(
        ["--seed", str(matrix.SEED), "--out", str(out), "--check",
         "--reference", str(generate.DEFAULT_OUT / "manifest.json"), "--quiet"]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.out
    assert "fixture files compared:" in captured.out


@pytest.mark.slow
def test_the_main_manifest_still_covers_only_the_main_set(tmp_path: Path) -> None:
    out = tmp_path / "tree"
    generate.generate_dataset(out, quiet=True)
    manifest_doc = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest_doc["counts"]["total"] == 30
    assert len(manifest_doc["files"]) == 143
    assert not any(entry["path"].startswith("boundary_fixtures/")
                   for entry in manifest_doc["files"])
    assert (out / "boundary_fixtures" / "manifest.json").is_file()
