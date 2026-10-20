"""Offline tests for the sample matrix, the gold builder and the contract adapter (T07).

The tests work on objects only.  They build every ``DocSpec`` and its printed
pages in memory, then check the matrix, the gold record and the schema.  No
file is rendered, no model is called and the session-wide network guard in
``tests/conftest.py`` stays installed.

Run:

    pytest tests/test_docuagents_gen.py -q
"""

from __future__ import annotations

import copy
import json
import re
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List

import pytest

# Self-contained import path: this file must run under a bare
# `pytest tests/test_docuagents_gen.py` with no pytest.ini and no PYTHONPATH.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from generator.layout import build_pages  # noqa: E402  (after the path fix)
from generator.spec import DocSpec  # noqa: E402

from docuagents_gen import build, codes, gold, matrix  # noqa: E402


# ---------------------------------------------------------------------------
# session fixtures: one spec and one gold per document
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def specs() -> Dict[str, DocSpec]:
    return {row.doc_id: build.build_spec(row) for row in matrix.ROWS}


@pytest.fixture(scope="session")
def pages(specs: Dict[str, DocSpec]) -> Dict[str, Any]:
    return {doc_id: build_pages(spec) for doc_id, spec in specs.items()}


@pytest.fixture(scope="session")
def golds(specs: Dict[str, DocSpec], pages: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        doc_id: gold.build_gold(spec, pages[doc_id], gold.placeholder_file_meta(spec, len(pages[doc_id])))
        for doc_id, spec in specs.items()
    }


@pytest.fixture(scope="session")
def schema() -> Dict[str, Any]:
    return json.loads((ROOT / "schemas" / "gold.schema.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# A minimal JSON Schema validator.
#
# `jsonschema` is not in the locked environment (environment.lock.json) and
# this task may not add a dependency, so the tests carry the subset of
# draft 2020-12 that `schemas/gold.schema.json` uses.  An unknown keyword
# fails the run: a schema change cannot turn validation into a no-op.
# ---------------------------------------------------------------------------


class SchemaError(AssertionError):
    """Raised when a value does not match the gold schema."""


_IGNORED = {"$schema", "$id", "title", "description", "$defs"}
_KNOWN = {
    "type", "const", "enum", "pattern", "minLength", "minItems", "maxItems",
    "uniqueItems", "minimum", "maximum", "items", "properties", "required",
    "additionalProperties", "propertyNames", "oneOf", "allOf", "if", "then",
    "else", "minProperties", "$ref",
}

_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def _type_ok(value: Any, name: str) -> bool:
    expected = _TYPES[name]
    if name in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, expected)


def _resolve(root: Dict[str, Any], ref: str) -> Dict[str, Any]:
    if not ref.startswith("#/$defs/"):
        raise SchemaError(f"unsupported $ref {ref!r}")
    name = ref.split("/")[-1]
    if name not in root["$defs"]:
        raise SchemaError(f"$ref points at an unknown definition {name!r}")
    return root["$defs"][name]


def _fail(path: str, message: str) -> None:
    raise SchemaError(f"{path or '<root>'}: {message}")


def _validate(value: Any, node: Dict[str, Any], root: Dict[str, Any], path: str) -> None:
    unknown = set(node) - _KNOWN - _IGNORED
    if unknown:
        raise SchemaError(f"{path}: unsupported schema keywords {sorted(unknown)}")

    if "$ref" in node:
        _validate(value, _resolve(root, node["$ref"]), root, path)
        return

    if "type" in node:
        types = node["type"] if isinstance(node["type"], list) else [node["type"]]
        if not any(_type_ok(value, name) for name in types):
            _fail(path, f"expected type {types}, got {type(value).__name__}")
    if "const" in node and value != node["const"]:
        _fail(path, f"expected const {node['const']!r}, got {value!r}")
    if "enum" in node and value not in node["enum"]:
        _fail(path, f"{value!r} is not one of {node['enum']}")
    if "pattern" in node:
        if not isinstance(value, str) or not re.search(node["pattern"], value):
            _fail(path, f"{value!r} does not match {node['pattern']!r}")
    if "minLength" in node and len(value) < node["minLength"]:
        _fail(path, f"string is shorter than {node['minLength']}")
    if "minimum" in node and value < node["minimum"]:
        _fail(path, f"{value!r} is below {node['minimum']}")
    if "maximum" in node and value > node["maximum"]:
        _fail(path, f"{value!r} is above {node['maximum']}")

    if isinstance(value, list):
        if "minItems" in node and len(value) < node["minItems"]:
            _fail(path, f"only {len(value)} items, expected {node['minItems']}")
        if "maxItems" in node and len(value) > node["maxItems"]:
            _fail(path, f"{len(value)} items, expected at most {node['maxItems']}")
        if node.get("uniqueItems"):
            encoded = [json.dumps(item, sort_keys=True) for item in value]
            if len(encoded) != len(set(encoded)):
                _fail(path, "items are not unique")
        if "items" in node:
            for index, item in enumerate(value):
                _validate(item, node["items"], root, f"{path}[{index}]")

    if isinstance(value, dict):
        if "minProperties" in node and len(value) < node["minProperties"]:
            _fail(path, f"only {len(value)} properties, expected {node['minProperties']}")
        properties = node.get("properties", {})
        for name in node.get("required", []):
            if name not in value:
                _fail(path, f"missing required property {name!r}")
        extra = node.get("additionalProperties", True)
        for name, item in value.items():
            if name in properties:
                _validate(item, properties[name], root, f"{path}.{name}")
                continue
            if "propertyNames" in node:
                _validate(name, node["propertyNames"], root, f"{path}.<name {name}>")
            if extra is False:
                _fail(path, f"unexpected property {name!r}")
            elif isinstance(extra, dict):
                _validate(item, extra, root, f"{path}.{name}")

    for name in node.get("allOf", []):
        _validate(value, name, root, path)

    if "oneOf" in node:
        matches = 0
        failures: List[str] = []
        for index, option in enumerate(node["oneOf"]):
            try:
                _validate(value, option, root, path)
            except SchemaError as exc:
                failures.append(str(exc))
                continue
            matches += 1
        if matches != 1:
            # Name the inner reason: a bare "matched 0" hides which field the
            # document actually got wrong.
            detail = f"; first option failed: {failures[0]}" if failures else ""
            _fail(path, f"matched {matches} of the oneOf options, expected exactly one{detail}")

    if "if" in node:
        try:
            _validate(value, node["if"], root, path)
        except SchemaError:
            branch = node.get("else")
        else:
            branch = node.get("then")
        if branch is not None:
            _validate(value, branch, root, path)


def validate_schema(instance: Any, schema: Dict[str, Any]) -> None:
    _validate(instance, schema, schema, "")


# ---------------------------------------------------------------------------
# Contract adapter and matrix
# ---------------------------------------------------------------------------


def test_contract_digest_matches_the_pinned_value() -> None:
    raw = codes.CONTRACT_PATH.read_bytes()
    assert len(raw) == 2061
    assert codes.digest(raw) == "7fb40fa01668dadcbfa24b4cdad8b45859b8177e5a1c3c12688f76d456bbcb56"
    assert codes.digest(raw) == codes.CONTRACT_SHA256
    assert codes.canonical_bytes(dict(codes.CONTRACT)) == raw
    assert codes.CONTRACT["contract_version"] == codes.CONTRACT_VERSION == "1.1"


def test_contract_namespaces_are_exposed_without_new_values() -> None:
    assert len(codes.ISSUE) == 13
    assert len(codes.SCOPE_REASON) == 16
    assert codes.CHECKS_RESULT == ("PASS", "FAIL", "NOT_APPLICABLE", "BLOCKED_MISSING_INPUT")
    assert codes.GOLD_ANNOTATION_SOURCE == ("generator_recorded", "human_reviewed")
    # The runtime and gold provenance namespaces must not overlap.
    assert not set(codes.GOLD_ANNOTATION_SOURCE) & set(codes.CONTRACT["evidence_verification"])


def test_matrix_holds_thirty_documents_with_the_planned_counts() -> None:
    assert len(matrix.ROWS) == 30
    assert len({row.doc_id for row in matrix.ROWS}) == 30
    classes: Dict[str, int] = {}
    subclasses: Dict[str, int] = {}
    for row in matrix.ROWS:
        classes[row.klass] = classes.get(row.klass, 0) + 1
        subclasses[row.subclass] = subclasses.get(row.subclass, 0) + 1
    assert classes == dict(matrix.CLASS_COUNTS)
    assert subclasses == dict(matrix.SUBCLASS_COUNTS)


def test_split_is_fixed_at_ten_dev_and_twenty_test() -> None:
    dev = [row.doc_id for row in matrix.ROWS if row.split == "dev"]
    test = [row.doc_id for row in matrix.ROWS if row.split == "test"]
    assert len(dev) == 10
    assert len(test) == 20
    assert set(dev) | set(test) == set(matrix.SPLIT)


def test_no_vendor_crosses_the_split_boundary() -> None:
    """Sample plan §6.3: no vendor entity appears in both dev and test.

    The child invoices of the multi-invoice document print their own vendor
    too, so they count as part of that document's split.
    """
    by_split: Dict[str, set] = {"dev": set(), "test": set()}
    for row in matrix.ROWS:
        by_split[row.split].add(matrix.VENDORS[row.vendor].name)
        for child in row.children:
            by_split[row.split].add(matrix.VENDORS[child.vendor].name)

    assert not by_split["dev"] & by_split["test"], sorted(by_split["dev"] & by_split["test"])
    # Two suppliers per split at least: a single supplier would hide leaks.
    assert len(by_split["dev"]) >= 2
    assert len(by_split["test"]) >= 2


def test_classes_a_to_d_are_supported_and_e_is_unsupported() -> None:
    for row in matrix.ROWS:
        if row.klass in "ABCD":
            assert row.scope_status == "SUPPORTED", row.doc_id
            assert row.scope_reasons == (), row.doc_id
        else:
            assert row.scope_status == "UNSUPPORTED", row.doc_id
            assert row.scope_reasons, row.doc_id


def test_e_documents_cover_all_six_scope_reasons() -> None:
    reasons = {
        "da-e-01": ["HAS_DISCOUNT"],
        "da-e-02": ["HAS_EXTRA_CHARGES"],
        "da-e-03": ["NON_USD_CURRENCY"],
        "da-e-04": ["TABLE_SPANS_PAGES", "TOO_MANY_PAGES"],
        "da-e-05": ["CREDIT_NOTE"],
        "da-e-06": ["MULTIPLE_INVOICES"],
    }
    for doc_id, expected in reasons.items():
        assert list(matrix.BY_ID[doc_id].scope_reasons) == expected, doc_id
    # Six E documents, one target reason family each.  E-04 carries a second
    # reason because its table spans pages *and* it passes the page limit.
    assert len(reasons) == 6
    union = {reason for row in matrix.ROWS if row.klass == "E" for reason in row.scope_reasons}
    assert union == {reason for values in reasons.values() for reason in values}
    assert len(union) == 7


# ---------------------------------------------------------------------------
# Specs: construction rules
# ---------------------------------------------------------------------------


def test_every_spec_builds_and_has_its_required_printed_facts(specs: Dict[str, DocSpec]) -> None:
    assert set(specs) == {row.doc_id for row in matrix.ROWS}
    for row in matrix.ROWS:
        spec = specs[row.doc_id]
        assert spec.date_style == "USLONG", row.doc_id
        assert spec.layout.show_currency_header is True, row.doc_id
        if row.klass in "ABCD":
            assert spec.currency == "USD", row.doc_id
            assert "Prices exclude tax." in spec.notes_text, row.doc_id


def test_a3_construction_matches_its_frozen_definition(pages: Dict[str, Any]) -> None:
    for doc_id in ("da-a3-01", "da-a3-02"):
        rendered = pages[doc_id]
        row = matrix.BY_ID[doc_id]
        assert len(rendered) == 2, doc_id
        rows_by_page = [sum(len(table.rows) for table in page.tables) for page in rendered]
        assert rows_by_page == [row.line_count, 0], doc_id
        page_two_text = " ".join(
            op.text for op in rendered[1].ops if hasattr(op, "text")
        )
        assert "Subtotal" in page_two_text
        assert "TOTAL" in page_two_text
        assert row.line_count == matrix.A3_LINE_COUNT <= 20
        assert row.layout.rows_per_page == matrix.A3_ROWS_PER_PAGE
        # The controlled lever is real: every description prints on two lines.
        wrapped = [op.text for op in rendered[0].ops if getattr(op, "text", "") == "board"]
        assert len(wrapped) == row.line_count, doc_id


def test_a3_candidate_enumeration_is_bounded_and_names_the_frozen_parameters() -> None:
    candidates = build.a3_candidates()
    assert 1 <= len(candidates) <= 200
    assert all(c["line_count"] <= 20 for c in candidates)
    frozen = [
        c for c in candidates
        if c["rows_per_page"] == matrix.A3_ROWS_PER_PAGE
        and c["line_count"] == matrix.A3_LINE_COUNT
        and c["description_form"] == "wrapped"
    ]
    assert len(frozen) == 1
    assert frozen[0]["passed"] is True
    assert frozen[0]["pages"] == 2
    assert frozen[0]["rows_per_page_measured"] == [matrix.A3_LINE_COUNT, 0]
    assert frozen[0]["totals_page"] == 2
    # The frozen parameters are the first candidate, in the fixed order, that
    # passes the whole A3 definition.
    first_passing = next(index for index, c in enumerate(candidates) if c["passed"])
    assert candidates[first_passing] is frozen[0]


def test_subseeds_are_stable_and_do_not_use_the_process_hash() -> None:
    first = build.subseed("da-a1-01", "render")
    second = build.subseed("da-a1-01", "render")
    assert first == second
    assert first == build.subseed("da-a1-01", "render", seed=matrix.SEED)
    assert build.subseed("da-a1-01", "render") != build.subseed("da-a1-01", "degrade")
    assert build.subseed("da-a1-01", "render") != build.subseed("da-a1-02", "render")
    assert 0 <= first < 2**64


def test_upstream_validate_accepts_a_b_and_d_and_rejects_the_deliberate_c() -> None:
    for row in matrix.ROWS:
        spec = build.build_spec(row)
        if row.klass == "C":
            with pytest.raises(ValueError):
                spec.validate()
        else:
            spec.validate()


# ---------------------------------------------------------------------------
# Gold: the whole set
# ---------------------------------------------------------------------------


def test_all_gold_passes_the_schema(golds: Dict[str, Dict[str, Any]], schema: Dict[str, Any]) -> None:
    assert len(golds) == 30
    for doc_id, record in golds.items():
        try:
            validate_schema(record, schema)
        except SchemaError as exc:  # pragma: no cover - the message carries the path
            pytest.fail(f"{doc_id}: {exc}")


# ---------------------------------------------------------------------------
# The validator must reject a corrupted gold, not only accept a good one.
#
# These cases prove the checker is live.  A validator that silently accepted
# everything would let test_all_gold_passes_the_schema pass on a broken gold,
# so each case damages one field of a valid record and asserts a SchemaError
# whose message names the damaged path.
# ---------------------------------------------------------------------------


def _corrupted(record: Dict[str, Any], damage) -> Dict[str, Any]:
    """A deep copy of a valid gold with one deliberate defect."""
    broken = copy.deepcopy(record)
    damage(broken)
    return broken


def _rejection(broken: Dict[str, Any], schema: Dict[str, Any]) -> str:
    """Assert that the validator rejects ``broken`` and return its message."""
    try:
        validate_schema(broken, schema)
    except SchemaError as exc:
        return str(exc)
    pytest.fail("the validator accepted a corrupted gold")


def test_validator_rejects_a_missing_required_field(
    golds: Dict[str, Dict[str, Any]], schema: Dict[str, Any]
) -> None:
    record = golds["da-a1-01"]
    validate_schema(record, schema)  # the unmodified record is valid

    cases = {
        "invoice.vendor_name": (lambda r: r["invoice"].pop("vendor_name"), "'vendor_name'"),
        "scope.reasons": (lambda r: r["scope"].pop("reasons"), "'reasons'"),
        "root.evidence": (lambda r: r.pop("evidence"), "'evidence'"),
    }
    for name, (damage, expected) in cases.items():
        message = _rejection(_corrupted(record, damage), schema)
        assert "missing required property" in message, (name, message)
        assert expected in message, (name, message)


def test_validator_rejects_a_wrong_field_type(
    golds: Dict[str, Dict[str, Any]], schema: Dict[str, Any]
) -> None:
    record = golds["da-a1-01"]
    validate_schema(record, schema)

    cases = {
        "invoice.invoice_number": (
            lambda r: r["invoice"].__setitem__("invoice_number", 1101), "invoice_number"
        ),
        "invoice.tax_amount": (
            lambda r: r["invoice"].__setitem__("tax_amount", 12.33), "tax_amount"
        ),
        "file.page_count": (
            lambda r: r["file"].__setitem__("page_count", "1"), "page_count"
        ),
        "readability.verified": (
            lambda r: r["readability"].__setitem__("verified", "no"), "verified"
        ),
    }
    for name, (damage, expected) in cases.items():
        message = _rejection(_corrupted(record, damage), schema)
        assert "expected type" in message, (name, message)
        assert expected in message, (name, message)


def test_validator_rejects_an_undefined_enum_or_constant(
    golds: Dict[str, Dict[str, Any]], schema: Dict[str, Any]
) -> None:
    record = golds["da-a1-01"]
    validate_schema(record, schema)

    cases = {
        "schema_version": (lambda r: r.__setitem__("schema_version", 3), "expected const 2"),
        "contract_version": (
            lambda r: r.__setitem__("contract_version", "1.0"), "expected const '1.1'"
        ),
        "split": (lambda r: r.__setitem__("split", "train"), "'train' is not one of"),
        "document_type": (
            lambda r: r["invoice"].__setitem__("document_type", "receipt"),
            "'receipt' is not one of",
        ),
        "evidence.status": (
            lambda r: r["evidence"]["currency"].__setitem__("status", "seen"),
            "'seen' is not one of",
        ),
    }
    for name, (damage, expected) in cases.items():
        message = _rejection(_corrupted(record, damage), schema)
        assert expected in message, (name, message)


def test_validator_keeps_its_cross_field_guards_live(
    golds: Dict[str, Dict[str, Any]], schema: Dict[str, Any]
) -> None:
    """A not_found entry must not carry a page; an unknown property is refused."""
    record = golds["da-a1-01"]
    validate_schema(record, schema)

    page = _rejection(
        _corrupted(record, lambda r: r["evidence"]["purchase_order_number"].__setitem__("page", 1)),
        schema,
    )
    assert ".evidence.purchase_order_number.page" in page
    assert "expected type ['null']" in page

    extra = _rejection(_corrupted(record, lambda r: r.__setitem__("extra", 1)), schema)
    assert "unexpected property 'extra'" in extra


def test_gold_header_matches_the_matrix(golds: Dict[str, Dict[str, Any]]) -> None:
    for row in matrix.ROWS:
        record = golds[row.doc_id]
        assert record["schema_version"] == 2
        assert record["contract_version"] == "1.1"
        assert record["split"] == matrix.SPLIT[row.doc_id]
        assert record["class"] == row.klass
        assert record["subclass"] == row.subclass
        assert record["target_question"] == row.target_question
        assert record["source_family"] == row.source_family
        assert record["scope"]["status"] == row.scope_status
        assert record["scope"]["reasons"] == list(row.scope_reasons)
        assert record["expected_issues"] == list(row.expected_issues)


def test_gold_amounts_are_two_decimal_strings_and_never_non_finite(
    golds: Dict[str, Dict[str, Any]],
) -> None:
    money = re.compile(r"^-?(0|[1-9][0-9]*)\.[0-9]{2}$")
    for doc_id, record in golds.items():
        text = json.dumps(record)
        assert "NaN" not in text and "Infinity" not in text, doc_id
        invoice = record["invoice"]
        if invoice is None:
            continue
        for field in ("subtotal_amount", "tax_amount", "total_amount"):
            value = invoice[field]
            assert value is None or money.match(value), (doc_id, field, value)
        for item in invoice["line_items"]:
            assert money.match(item["amount"]), (doc_id, item)
            if item["unit_price"] is not None:
                assert money.match(item["unit_price"]), (doc_id, item["unit_price"])


def test_evidence_references_real_pages_and_covers_every_gold_field(
    golds: Dict[str, Dict[str, Any]],
) -> None:
    for doc_id, record in golds.items():
        page_count = record["file"]["page_count"]
        evidence = record["evidence"]
        assert evidence, doc_id
        for key, entry in evidence.items():
            assert entry["verification"] == "generator_recorded", (doc_id, key)
            if entry["status"] == "not_found":
                assert entry["page"] is None and entry["text"] is None, (doc_id, key)
                assert entry["searched_pages"], (doc_id, key)
            else:
                assert entry["status"] == "observed", (doc_id, key)
                assert isinstance(entry["page"], int), (doc_id, key)
                assert 1 <= entry["page"] <= page_count, (doc_id, key)
                assert isinstance(entry["text"], str) and entry["text"], (doc_id, key)
            for page in entry["searched_pages"]:
                assert 1 <= page <= page_count, (doc_id, key)

        required = [
            "vendor_name", "customer_name", "invoice_number", "invoice_date", "due_date",
            "currency", "purchase_order_number", "subtotal_amount", "tax_amount",
            "total_amount",
        ]
        invoice = record["invoice"]
        if invoice is not None:
            required += [
                f"line_items[{item['index']}].{part}"
                for item in invoice["line_items"]
                for part in ("amount", "quantity", "unit_price")
            ]
        for key in required:
            assert key in evidence, (doc_id, key)

        signals = record["scope"]["signals"]
        for name in signals:
            assert f"scope_signals.{name}" in evidence, (doc_id, name)


def test_gold_recomputes_the_scope_gates_from_the_matrix(golds: Dict[str, Dict[str, Any]]) -> None:
    for row in matrix.ROWS:
        record = golds[row.doc_id]
        assert record["scope"]["status"] == row.scope_status, row.doc_id
        assert record["scope"]["reasons"] == list(row.scope_reasons), row.doc_id
        assert record["expected_issues"] == list(row.expected_issues), row.doc_id


# ---------------------------------------------------------------------------
# A: clean documents
# ---------------------------------------------------------------------------


def test_a4_records_a_printed_zero_as_zero_not_missing(golds: Dict[str, Dict[str, Any]]) -> None:
    record = golds["da-a4-01"]
    assert matrix.BY_ID["da-a4-01"].tax_label == "Sales Tax (Exempt)"
    assert matrix.BY_ID["da-a4-01"].tax_rate == Decimal("0.00")
    assert record["invoice"]["tax_amount"] == "0.00"
    assert record["invoice"]["total_amount"] == record["invoice"]["subtotal_amount"]
    assert record["acceptance_policy"]["tax_rule"] == "APPLICABLE"
    entry = record["evidence"]["tax_amount"]
    assert entry["status"] == "observed"
    assert entry["text"] == "$0.00"
    assert record["scope"]["signals"]["tax_presentation"] == "single"


def test_a2_is_twenty_lines_on_one_page(pages: Dict[str, Any], golds: Dict[str, Dict[str, Any]]) -> None:
    row = matrix.BY_ID["da-a2-01"]
    assert row.line_count == 20
    rendered = pages["da-a2-01"]
    assert len(rendered) == 1
    assert sum(len(table.rows) for table in rendered[0].tables) == 20
    record = golds["da-a2-01"]
    assert len(record["invoice"]["line_items"]) == 20
    assert record["scope"]["signals"]["table_layout"] == "single_page"
    assert record["acceptance_policy"]["rules"]["A_line_amount"] == "PASS"


def test_at_least_one_a1_references_a_purchase_order(golds: Dict[str, Dict[str, Any]]) -> None:
    rows = [row for row in matrix.ROWS if row.subclass == "A1" and row.po_number]
    assert rows, "no A1 document prints a normal purchase order reference"
    assert matrix.BY_ID["da-d2-01"].po_number not in {row.po_number for row in rows}
    for row in rows:
        entry = golds[row.doc_id]["evidence"]["purchase_order_number"]
        assert entry["status"] == "observed", row.doc_id
        assert entry["text"] == row.po_number, row.doc_id
        assert golds[row.doc_id]["invoice"]["purchase_order_number"] == row.po_number


# ---------------------------------------------------------------------------
# Specs: degradation and medium per subclass (sample plan §6.1)
# ---------------------------------------------------------------------------


def test_c1_has_one_pdf_and_one_mild_scan(pages: Dict[str, Any], golds: Dict[str, Dict[str, Any]]) -> None:
    from generator.degrade import SCAN_KINDS

    pdf_row = matrix.BY_ID["da-c1-01"]
    scan_row = matrix.BY_ID["da-c1-02"]
    assert pdf_row.render_mode == "pdf"
    assert len(pages["da-c1-01"]) == 1
    assert golds["da-c1-01"]["file"]["media_type"] == "application/pdf"

    assert scan_row.render_mode == "image"
    assert scan_row.render_fmt == "png"
    assert len(scan_row.degradations) == 1
    kind, severity = scan_row.degradations[0]
    assert kind in SCAN_KINDS
    assert severity == "mild"
    assert scan_row.legibility == "image_readable"
    assert golds["da-c1-02"]["file"]["media_type"] == "image/png"

    # The deliberate conflict survives the change of medium.
    record = golds["da-c1-02"]
    failing = [name for name, value in record["acceptance_policy"]["rules"].items() if value == "FAIL"]
    assert failing == ["A_line_amount"]
    first = record["arithmetic"]["lines"][0]
    assert first["printed_amount"] == "81.00"
    assert first["recomputed_amount"] == "18.00"
    assert first["delta"] == "-63.00"
    assert record["expected_issues"] == ["LINE_AMOUNT_MISMATCH"]


def test_b1_is_scan_and_b2_is_photo_with_plan_grades(specs: Dict[str, DocSpec]) -> None:
    from generator.degrade import PHOTO_KINDS, SCAN_KINDS

    def severities(subclass: str) -> List[str]:
        return [
            row.degradations[0][1]
            for row in matrix.ROWS
            if row.subclass == subclass
        ]

    for row in matrix.ROWS:
        if row.subclass == "B1":
            spec = specs[row.doc_id]
            assert spec.render.mode == "image", row.doc_id
            assert spec.render.fmt == "png", row.doc_id
            kind, severity = row.degradations[0]
            assert kind in SCAN_KINDS, (row.doc_id, kind)
            assert severity in ("mild", "moderate"), (row.doc_id, severity)
            assert row.source_family == "degraded_scan", row.doc_id
            assert row.legibility == "image_readable", row.doc_id
        elif row.subclass == "B2":
            spec = specs[row.doc_id]
            assert spec.render.mode == "image", row.doc_id
            assert spec.render.fmt == "jpg", row.doc_id
            kind, severity = row.degradations[0]
            assert kind in PHOTO_KINDS, (row.doc_id, kind)
            assert severity in ("mild", "moderate"), (row.doc_id, severity)
            assert row.source_family == "degraded_photo", row.doc_id
            assert row.legibility == "image_readable", row.doc_id

    # Three documents per subclass, covering both plan grades.
    assert len(severities("B1")) == 3 and set(severities("B1")) == {"mild", "moderate"}
    assert len(severities("B2")) == 3 and set(severities("B2")) == {"mild", "moderate"}


def test_b3_keeps_the_two_severe_renders() -> None:
    from generator.degrade import PHOTO_KINDS, SCAN_KINDS

    b3 = [row for row in matrix.ROWS if row.subclass == "B3"]
    assert len(b3) == 2
    assert all(row.degradations[0][1] == "severe" for row in b3)
    assert all(row.legibility == "image_degraded_unverified" for row in b3)
    photo = [row for row in b3 if row.degradations[0][0] in PHOTO_KINDS]
    scan = [row for row in b3 if row.degradations[0][0] in SCAN_KINDS]
    assert len(photo) == 1 and len(scan) == 1
    assert photo[0].render_fmt == "jpg"
    assert scan[0].render_fmt == "png"


def test_a3_places_all_lines_on_page_one_and_the_totals_on_page_two(
    golds: Dict[str, Dict[str, Any]],
) -> None:
    record = golds["da-a3-01"]
    assert record["file"]["page_count"] == 2
    assert len(record["invoice"]["line_items"]) == matrix.A3_LINE_COUNT
    assert len(record["invoice"]["line_items"]) <= 20
    for item in record["invoice"]["line_items"]:
        assert record["evidence"][f"line_items[{item['index']}].amount"]["page"] == 1
    for field in ("subtotal_amount", "tax_amount", "total_amount"):
        assert record["evidence"][field]["page"] == 2
    assert record["scope"]["signals"]["table_layout"] == "single_page"


# ---------------------------------------------------------------------------
# C: deliberate conflicts
# ---------------------------------------------------------------------------


def _failing_rules(record: Dict[str, Any]) -> List[str]:
    return [name for name, value in record["acceptance_policy"]["rules"].items() if value == "FAIL"]


def test_each_c_document_fails_exactly_one_target_rule(golds: Dict[str, Dict[str, Any]]) -> None:
    expected_rule = {
        "da-c1-01": "A_line_amount",
        "da-c1-02": "A_line_amount",
        "da-c2-01": "B_subtotal",
        "da-c3-01": "C_total",
    }
    c_rows = [row for row in matrix.ROWS if row.klass == "C"]
    assert len(c_rows) == 4
    for row in c_rows:
        record = golds[row.doc_id]
        assert _failing_rules(record) == [expected_rule[row.doc_id]], row.doc_id
        assert len(record["acceptance_policy"]["decisions"]) == 1, row.doc_id
        assert record["acceptance_policy"]["upstream_validate"] == "SKIPPED_DELIBERATE_CONFLICT"
        assert record["acceptance_policy"]["rules"]["D_required_fields"] == "PASS", row.doc_id


def test_c1_keeps_the_printed_amount_and_shows_the_delta(golds: Dict[str, Dict[str, Any]]) -> None:
    record = golds["da-c1-01"]
    first = record["arithmetic"]["lines"][0]
    assert first["printed_amount"] == "51.00"
    assert first["recomputed_amount"] == "15.00"
    assert first["delta"] == "-36.00"
    assert first["status"] == "FAIL"
    assert record["arithmetic"]["line_sum"] == record["arithmetic"]["printed_subtotal"]
    assert record["arithmetic"]["delta_subtotal"] == "0.00"


def test_c2_and_c3_keep_the_printed_wrong_totals(golds: Dict[str, Dict[str, Any]]) -> None:
    c2 = golds["da-c2-01"]
    assert c2["arithmetic"]["line_sum"] == "100.00"
    assert c2["arithmetic"]["printed_subtotal"] == "110.00"
    assert c2["arithmetic"]["delta_subtotal"] == "-10.00"
    assert c2["arithmetic"]["delta_total"] == "0.00"

    c3 = golds["da-c3-01"]
    assert c3["arithmetic"]["printed_total"] == "118.00"
    assert c3["arithmetic"]["recomputed_total"] == "106.00"
    assert c3["arithmetic"]["delta_total"] == "-12.00"
    assert c3["arithmetic"]["delta_subtotal"] == "0.00"


# ---------------------------------------------------------------------------
# D: missing data
# ---------------------------------------------------------------------------


def test_d1_reports_an_unprinted_tax_as_unknown(golds: Dict[str, Dict[str, Any]]) -> None:
    record = golds["da-d1-01"]
    invoice = record["invoice"]
    assert invoice["tax_amount"] is None
    assert record["arithmetic"]["tax_printed"] is None
    assert record["acceptance_policy"]["tax_rule"] == "NOT_APPLICABLE"
    assert record["acceptance_policy"]["rules"]["C_total"] == "PASS"
    entry = record["evidence"]["tax_amount"]
    assert entry["status"] == "not_found"
    assert entry["page"] is None and entry["text"] is None
    assert entry["searched_pages"] == [1]
    assert record["scope"]["signals"]["tax_presentation"] == "missing"


def test_d2_does_not_treat_the_purchase_order_number_as_the_invoice_number(
    golds: Dict[str, Dict[str, Any]],
) -> None:
    record = golds["da-d2-01"]
    invoice = record["invoice"]
    assert invoice["invoice_number"] is None
    assert invoice["purchase_order_number"] == "PO-48213"
    assert record["evidence"]["invoice_number"]["status"] == "not_found"
    assert record["evidence"]["purchase_order_number"]["status"] == "observed"
    assert record["evidence"]["purchase_order_number"]["text"] == "PO-48213"
    assert record["expected_issues"] == ["MISSING_REQUIRED_FIELD"]
    assert record["acceptance_policy"]["rules"]["D_required_fields"] == "FAIL"


# ---------------------------------------------------------------------------
# E: out-of-scope documents
# ---------------------------------------------------------------------------


def test_e_discount_and_surcharge_are_read_from_the_totals_block(
    golds: Dict[str, Dict[str, Any]],
) -> None:
    discount = golds["da-e-01"]
    assert discount["scope"]["signals"]["discount"] == "present"
    assert discount["evidence"]["scope_signals.discount"]["status"] == "observed"
    assert discount["invoice"]["charges"] == [
        {"label": "Discount", "amount": "-50.00", "kind": "discount"}
    ]
    assert discount["acceptance_policy"]["upstream_validate"] == "OPTIONAL"

    surcharge = golds["da-e-02"]
    assert surcharge["scope"]["signals"]["additional_charges"] == "present"
    assert surcharge["invoice"]["charges"] == [
        {"label": "Freight surcharge", "amount": "35.00", "kind": "charge"}
    ]


def test_e_non_usd_keeps_the_printed_currency(golds: Dict[str, Dict[str, Any]]) -> None:
    record = golds["da-e-03"]
    assert record["invoice"]["currency"] == "CAD"
    assert record["printing"]["currency_header"] == "Currency: CAD"
    assert record["evidence"]["currency"]["text"] == "CAD"
    assert record["scope"]["signals"]["currency_evidence"] == "explicit"
    assert record["scope"]["reasons"] == ["NON_USD_CURRENCY"]


def test_e_pages_document_spans_three_pages(golds: Dict[str, Dict[str, Any]]) -> None:
    record = golds["da-e-04"]
    assert record["file"]["page_count"] == 3
    assert record["scope"]["reasons"] == ["TABLE_SPANS_PAGES", "TOO_MANY_PAGES"]
    assert record["scope"]["signals"]["table_layout"] == "spans_pages"
    assert record["scope"]["signals"]["table_pages"] == [1, 2, 3]
    assert record["scope"]["signals"]["observed_row_count"] == 60


def test_e_credit_note_keeps_the_printed_negative_amounts(
    golds: Dict[str, Dict[str, Any]],
) -> None:
    record = golds["da-e-05"]
    assert record["invoice"]["document_type"] == "credit_note"
    assert record["invoice"]["total_amount"].startswith("-")
    assert record["acceptance_policy"]["rules"]["A_line_amount"] == "PASS"
    assert record["acceptance_policy"]["rules"]["C_total"] == "PASS"
    assert record["scope"]["reasons"] == ["CREDIT_NOTE"]
    assert record["evidence"]["due_date"]["status"] == "not_found"


def test_e_multi_invoice_has_no_single_invoice_level_value(
    golds: Dict[str, Dict[str, Any]],
) -> None:
    record = golds["da-e-06"]
    assert record["invoice"] is None
    assert record["scope"]["signals"]["invoice_count"] == 2
    assert record["scope"]["signals"]["table_layout"] == "single_page"
    for field in ("invoice_number", "invoice_date", "due_date", "subtotal_amount",
                  "tax_amount", "total_amount", "purchase_order_number"):
        assert record["evidence"][field]["status"] == "not_found", field
    assert record["acceptance_policy"]["upstream_validate"] == "NOT_APPLICABLE"
    assert set(record["acceptance_policy"]["rules"].values()) == {"NOT_APPLICABLE"}


# ---------------------------------------------------------------------------
# B: degraded images
# ---------------------------------------------------------------------------


def test_b_documents_are_single_page_images_with_a_fixed_degradation(
    specs: Dict[str, DocSpec], pages: Dict[str, Any], golds: Dict[str, Dict[str, Any]],
) -> None:
    for row in matrix.ROWS:
        if row.klass != "B":
            continue
        spec = specs[row.doc_id]
        assert spec.render.mode == "image", row.doc_id
        assert spec.render.fmt in ("jpg", "png"), row.doc_id
        assert len(spec.render.degradations) == 1, row.doc_id
        assert len(pages[row.doc_id]) == 1, row.doc_id
        record = golds[row.doc_id]
        assert record["file"]["media_type"] in ("image/jpeg", "image/png"), row.doc_id
        assert record["readability"]["gate"] == row.legibility, row.doc_id
        assert record["expected_issues"] == [], row.doc_id


# ---------------------------------------------------------------------------
# AC6: one change must not move another document
# ---------------------------------------------------------------------------


def test_changing_one_row_leaves_every_other_fingerprint_unchanged() -> None:
    before = build.fingerprints()
    assert len(before) == 30

    target = matrix.BY_ID["da-a1-03"]
    changed = replace(target, items=target.items[:-1])
    after_spec = build.spec_fingerprint(build.build_spec(changed))
    after = dict(before)
    after["da-a1-03"] = after_spec

    assert after_spec != before["da-a1-03"]
    for doc_id, digest in before.items():
        if doc_id == "da-a1-03":
            continue
        assert after[doc_id] == digest, f"{doc_id} moved when da-a1-03 changed"


def test_fingerprints_are_stable_across_calls() -> None:
    assert build.fingerprints() == build.fingerprints()


# ---------------------------------------------------------------------------
# Gold builder guards
# ---------------------------------------------------------------------------


def test_build_gold_rejects_unusable_file_meta(specs: Dict[str, DocSpec], pages: Dict[str, Any]) -> None:
    spec = specs["da-a1-01"]
    rendered = pages["da-a1-01"]
    good = gold.placeholder_file_meta(spec, len(rendered))

    with pytest.raises(gold.GoldError):
        gold.build_gold(spec, rendered, {**good, "sha256": "not-a-digest"})
    with pytest.raises(gold.GoldError):
        gold.build_gold(spec, rendered, {**good, "media_type": "text/plain"})
    with pytest.raises(gold.GoldError):
        gold.build_gold(spec, rendered, {**good, "page_count": len(rendered) + 1})
    with pytest.raises(gold.GoldError):
        gold.build_gold(spec, rendered, {"file_name": "x.pdf"})


def test_build_gold_rejects_a_page_without_a_title(specs: Dict[str, DocSpec]) -> None:
    spec = specs["da-a1-01"]
    with pytest.raises(gold.GoldError):
        gold.build_gold(spec, [], {"file_name": "x.pdf", "sha256": "0" * 64,
                                   "media_type": "application/pdf", "page_count": 0})


def test_printed_money_parser_reads_the_forms_the_layout_prints() -> None:
    assert gold.parse_printed_money("$1,234.50") == Decimal("1234.50")
    assert gold.parse_printed_money("-117.66") == Decimal("-117.66")
    assert gold.parse_printed_money("(45.00)") == Decimal("-45.00")
    assert gold.parse_printed_money("45.00CR") == Decimal("-45.00")
    assert gold.parse_printed_money("Subtotal") is None
    assert gold.parse_printed_money("") is None


def test_gold_uses_the_printed_cell_text_for_quantity_and_unit_price(
    golds: Dict[str, Dict[str, Any]],
) -> None:
    record = golds["da-a1-01"]
    quantities = [item["quantity"] for item in record["invoice"]["line_items"]]
    unit_prices = [item["unit_price"] for item in record["invoice"]["line_items"]]
    assert "2.40" in unit_prices
    assert "34.50" in unit_prices
    assert "2.4" not in unit_prices
    assert all(value is not None for value in quantities)
    for item in record["invoice"]["line_items"]:
        entry = record["evidence"][f"line_items[{item['index']}].unit_price"]
        assert entry["text"] == item["unit_price"]


def test_scope_signal_evidence_states_absence_honestly(golds: Dict[str, Dict[str, Any]]) -> None:
    record = golds["da-a1-01"]
    for name in ("discount", "additional_charges", "prepayment_or_prior_balance"):
        entry = record["evidence"][f"scope_signals.{name}"]
        assert entry["status"] == "not_found", name
        assert entry["page"] is None and entry["text"] is None, name
        assert entry["searched_pages"] == [1], name
    assert record["evidence"]["scope_signals.currency_evidence"]["status"] == "observed"
    assert record["evidence"]["scope_signals.currency_evidence"]["text"] == "Currency: USD"
