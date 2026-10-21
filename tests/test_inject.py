"""Tests for the fault injection suite (T16, sample plan §7 P6).

The suite is data, not a pipeline: for each of the ten class-A gold records it
writes five synthetic first-candidate faults; every fault comes with one
evaluation envelope (``fault_injection/<case_id>.json``) and one neutral
runtime candidate (``fault_injection/candidates/inj-NNNN.json``).  The
candidate is a complete v1.1 ``ExtractionEnvelope``; the envelope is the only
place that names the base document, the fault and the expected behaviour.

Everything here runs offline: ``tests/conftest.py`` installs the session
network guard.  The heavy end-to-end check carries ``@pytest.mark.slow``.

Run:

    pytest tests/test_inject.py -q
    pytest -q -m "not slow"
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import json
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Mapping

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

from docuagents_gen import generate, inject, matrix, offline, split  # noqa: E402

# Imported for the shared tree helpers; the module only defines fixtures and
# helpers at import time and generates nothing.
from test_generate import clone_tree, replace_bytes, tree_hashes  # noqa: E402

DATASET = ROOT / "datasets" / "docuagents_v1"
GOLD_DIR = DATASET / "gold"
INJECTION_DIR = DATASET / inject.DIR_NAME

A_DOC_IDS = (
    "da-a1-01", "da-a1-02", "da-a1-03", "da-a1-04", "da-a1-05",
    "da-a1-06", "da-a2-01", "da-a3-01", "da-a3-02", "da-a4-01",
)
FAULT_TYPES = (
    "unit_decimal_shift", "line_digit_transpose", "missing_row",
    "duplicate_row", "tax_null",
)
STRUCTURAL_FAULTS = {"missing_row", "duplicate_row"}


# ---------------------------------------------------------------------------
# A minimal JSON Schema validator (draft 2020-12 subset).
#
# ``jsonschema`` is not in the locked environment and this task may not add a
# dependency, so the tests carry the subset that the two schemas use.  An
# unknown keyword fails the run: a schema edit cannot turn validation into a
# no-op.  This is the same approach as tests/test_docuagents_gen.py, extended
# with the keywords of the copied runtime envelope schema (anyOf, prefixItems,
# maxLength, format).
# ---------------------------------------------------------------------------


class SchemaError(AssertionError):
    """Raised when a value does not match a schema."""


_IGNORED = {"$schema", "$id", "title", "description", "$defs", "default", "examples"}
_KNOWN = {
    "type", "const", "enum", "pattern", "minLength", "maxLength", "minItems",
    "maxItems", "uniqueItems", "minimum", "maximum", "items", "prefixItems",
    "properties", "required", "additionalProperties", "propertyNames", "oneOf",
    "anyOf", "allOf", "if", "then", "else", "minProperties", "$ref", "format",
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
    if name == "integer" and isinstance(value, float):
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
    if isinstance(value, str):
        if "minLength" in node and len(value) < node["minLength"]:
            _fail(path, f"string is shorter than {node['minLength']}")
        if "maxLength" in node and len(value) > node["maxLength"]:
            _fail(path, f"string is longer than {node['maxLength']}")
        if "format" in node:
            if node["format"] != "date":
                raise SchemaError(f"{path}: unsupported format {node['format']!r}")
            try:
                datetime.date.fromisoformat(value)
            except ValueError:
                _fail(path, f"{value!r} is not a date")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
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
        for index, item in enumerate(node.get("prefixItems", [])):
            if index < len(value):
                _validate(value[index], item, root, f"{path}[{index}]")
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

    if "anyOf" in node:
        failures = []
        for option in node["anyOf"]:
            try:
                _validate(value, option, root, path)
            except SchemaError as exc:
                failures.append(str(exc))
            else:
                break
        else:
            _fail(path, "matched none of the anyOf options; first: "
                       + (failures[0] if failures else ""))

    if "oneOf" in node:
        matches = 0
        failures = []
        for option in node["oneOf"]:
            try:
                _validate(value, option, root, path)
            except SchemaError as exc:
                failures.append(str(exc))
                continue
            matches += 1
        if matches != 1:
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
# fixtures
# ---------------------------------------------------------------------------


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(scope="session")
def golds() -> Dict[str, Dict[str, Any]]:
    return {doc_id: load(GOLD_DIR / f"{doc_id}.json") for doc_id in matrix.BY_ID}


@pytest.fixture(scope="session")
def injection_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    parent = tmp_path_factory.mktemp("injection")
    out = parent / inject.DIR_NAME
    inject.inject_faults(GOLD_DIR, out)
    return out


@pytest.fixture(scope="session")
def cases() -> Dict[str, inject.InjectionCase]:
    return {case.case_id: case for case in inject.INJECTION_PLAN}


@pytest.fixture(scope="session")
def envelopes(injection_tree: Path) -> Dict[str, Dict[str, Any]]:
    return {
        path.stem: load(path)
        for path in sorted(injection_tree.glob("*.json"))
    }


@pytest.fixture(scope="session")
def candidates(
    envelopes: Mapping[str, Mapping[str, Any]], injection_tree: Path
) -> Dict[str, Dict[str, Any]]:
    return {
        case_id: load(injection_tree / envelope["initial_candidate_file"])
        for case_id, envelope in envelopes.items()
    }


@pytest.fixture(scope="session")
def envelope_schema() -> Dict[str, Any]:
    return load(ROOT / "schemas" / "extraction_envelope.v1.1.schema.json")


@pytest.fixture(scope="session")
def fault_schema() -> Dict[str, Any]:
    return load(ROOT / "schemas" / "fault_injection.schema.json")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def walk_keys(value: Any) -> List[str]:
    keys: List[str] = []
    if isinstance(value, dict):
        for name, item in value.items():
            keys.append(name)
            keys.extend(walk_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.extend(walk_keys(item))
    return keys


def walk_strings(value: Any) -> List[str]:
    found: List[str] = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, dict):
        for name, item in value.items():
            found.append(name)
            found.extend(walk_strings(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(walk_strings(item))
    return found


def candidate_paths_for(case: inject.InjectionCase) -> tuple[str, str]:
    """The affected invoice path and its evidence/raw-field path."""
    if case.fault_type == "unit_decimal_shift":
        path = f"items[{case.target_row}].unit_price"
        return path, path
    if case.fault_type == "line_digit_transpose":
        path = f"items[{case.target_row}].line_net_amount"
        return path, path
    if case.fault_type == "tax_null":
        return "tax_amount", "tax_amount"
    return f"items[{case.target_row}]", f"items[{case.target_row}]"


# ---------------------------------------------------------------------------
# plan and output tree
# ---------------------------------------------------------------------------


def test_the_plan_holds_fifty_cases_in_the_fixed_splits(
    cases: Mapping[str, inject.InjectionCase], golds: Mapping[str, Dict[str, Any]]
) -> None:
    assert len(inject.INJECTION_PLAN) == 50 == len(cases)
    assert len({case.case_id for case in cases.values()}) == 50
    expected = {
        f"{doc_id}__{fault}"
        for doc_id in A_DOC_IDS
        for fault in FAULT_TYPES
    }
    assert set(cases) == expected
    assert {case.fault_type for case in cases.values()} == set(FAULT_TYPES)
    assert all(sum(1 for c in cases.values() if c.fault_type == fault) == 10
               for fault in FAULT_TYPES)
    assert all(sum(1 for c in cases.values() if c.base_doc_id == doc_id) == 5
               for doc_id in A_DOC_IDS)

    splits = load(DATASET / "splits.json")
    split_by_id = {entry["doc_id"]: entry for entry in splits["documents"]}
    for case in cases.values():
        source = split_by_id[case.base_doc_id]
        gold = golds[case.base_doc_id]
        assert source["class"] == "A" == matrix.BY_ID[case.base_doc_id].klass
        assert case.split == source["split"] == gold["split"]
        assert case.group_id == source["group_id"] == f"source:{case.base_doc_id}"
        assert case.source_family == source["source_family"] == gold["source_family"]

    assert sum(1 for case in cases.values() if case.split == "dev") == 15
    assert sum(1 for case in cases.values() if case.split == "test") == 35
    assert [case.candidate_id for case in inject.INJECTION_PLAN] == [
        f"inj-{index:04d}" for index in range(1, 51)
    ]


def test_the_output_tree_holds_fifty_envelopes_and_fifty_candidates(
    injection_tree: Path, envelopes: Mapping[str, Any], candidates: Mapping[str, Any]
) -> None:
    assert len(list(injection_tree.glob("*.json"))) == 50
    assert len(list((injection_tree / "candidates").glob("*.json"))) == 50
    assert len(envelopes) == 50 == len(candidates)
    for case_id, envelope in envelopes.items():
        candidate_id = envelope["candidate_id"]
        assert re.fullmatch(r"inj-\d{4}", candidate_id), case_id
        assert envelope["initial_candidate_file"] == f"candidates/{candidate_id}.json"
        assert (injection_tree / envelope["initial_candidate_file"]).is_file()
        assert candidates[case_id]["document_id"] == candidate_id


def test_injection_refuses_a_non_empty_output(tmp_path: Path) -> None:
    out = tmp_path / inject.DIR_NAME
    out.mkdir()
    (out / "keep.json").write_text("{}", encoding="utf-8")
    with pytest.raises(inject.InjectError):
        inject.inject_faults(GOLD_DIR, out)
    assert (out / "keep.json").read_text(encoding="utf-8") == "{}"


# ---------------------------------------------------------------------------
# fault authenticity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case_id", [case.case_id for case in inject.INJECTION_PLAN])
def test_each_case_injects_the_declared_fault(
    case_id: str,
    cases: Mapping[str, inject.InjectionCase],
    golds: Mapping[str, Dict[str, Any]],
    envelopes: Mapping[str, Dict[str, Any]],
    candidates: Mapping[str, Dict[str, Any]],
) -> None:
    case = cases[case_id]
    gold = golds[case.base_doc_id]["invoice"]
    candidate = candidates[case_id]
    envelope = envelopes[case_id]

    assert envelope["case_id"] == case_id
    assert envelope["base_doc_id"] == case.base_doc_id
    assert envelope["split"] == case.split
    assert envelope["injected"]["fault_type"] == case.fault_type
    assert envelope["document_sha256"] == golds[case.base_doc_id]["file"]["sha256"]
    assert envelope["gold_sha256"] == sha256_bytes(
        (GOLD_DIR / f"{case.base_doc_id}.json").read_bytes()
    )

    if case.fault_type == "unit_decimal_shift":
        item = candidate["invoice"]["items"][case.target_row]
        original = gold["line_items"][case.target_row]["unit_price"]
        assert item["unit_price"] != original
        assert Decimal(item["unit_price"]) == Decimal(original) * 10
        assert envelope["injected"]["original"] == original
        assert envelope["injected"]["injected"] == item["unit_price"]
        assert candidate["raw_fields"][f"items[{case.target_row}].unit_price"] == item["unit_price"]
        assert candidate["evidence"][f"items[{case.target_row}].unit_price"]["text"] == item["unit_price"]

    elif case.fault_type == "line_digit_transpose":
        item = candidate["invoice"]["items"][case.target_row]
        original = gold["line_items"][case.target_row]["amount"]
        assert item["line_net_amount"] != original
        assert Decimal(item["line_net_amount"]) != Decimal(original)
        # A transposition moves the same digits, so the multiset is unchanged.
        assert sorted(digits(item["line_net_amount"])) == sorted(digits(original))
        assert envelope["injected"]["original"] == original
        assert envelope["injected"]["injected"] == item["line_net_amount"]
        assert candidate["raw_fields"][f"items[{case.target_row}].line_net_amount"] == item["line_net_amount"]
        assert candidate["evidence"][f"items[{case.target_row}].line_net_amount"]["text"] == item["line_net_amount"]

    elif case.fault_type == "missing_row":
        assert len(candidate["invoice"]["items"]) == len(gold["line_items"]) - 1
        expected = [entry["amount"] for index, entry in enumerate(gold["line_items"])
                    if index != case.target_row]
        assert [entry["line_net_amount"] for entry in candidate["invoice"]["items"]] == expected
        assert envelope["injected"]["original"] == gold["line_items"][case.target_row]["amount"]
        assert envelope["injected"]["injected"] is None

    elif case.fault_type == "duplicate_row":
        items = candidate["invoice"]["items"]
        assert len(items) == len(gold["line_items"]) + 1
        assert items[case.target_row] == items[case.target_row + 1]
        assert envelope["injected"]["original"] == gold["line_items"][case.target_row]["amount"]

    elif case.fault_type == "tax_null":
        assert candidate["invoice"]["tax_amount"] is None
        assert candidate["raw_fields"]["tax_amount"] is None
        evidence = candidate["evidence"]["tax_amount"]
        assert evidence["status"] == "not_found"
        assert evidence["page"] is None and evidence["text"] is None
        assert evidence["searched_pages"]
        assert envelope["injected"]["original"] == gold["tax_amount"]
        assert envelope["injected"]["injected"] is None
    else:  # pragma: no cover - the plan is closed
        raise AssertionError(case.fault_type)


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------


def test_all_candidates_pass_the_runtime_envelope_schema(
    candidates: Mapping[str, Dict[str, Any]], envelope_schema: Dict[str, Any]
) -> None:
    assert len(candidates) == 50
    for case_id, candidate in sorted(candidates.items()):
        try:
            validate_schema(candidate, envelope_schema)
        except SchemaError as exc:  # add the case to the message
            raise AssertionError(f"{case_id}: {exc}") from exc


def test_all_envelopes_pass_the_fault_injection_schema(
    envelopes: Mapping[str, Dict[str, Any]], fault_schema: Dict[str, Any]
) -> None:
    assert len(envelopes) == 50
    for case_id, envelope in sorted(envelopes.items()):
        try:
            validate_schema(envelope, fault_schema)
        except SchemaError as exc:
            raise AssertionError(f"{case_id}: {exc}") from exc


def test_the_copied_envelope_schema_matches_the_main_project_record(
    envelope_schema: Dict[str, Any]
) -> None:
    # The copy is verbatim; the digest is recorded in HANDOFF.md and the
    # completion report.  A byte drift here would invalidate that record.
    data = (ROOT / "schemas" / "extraction_envelope.v1.1.schema.json").read_bytes()
    assert sha256_bytes(data) == "6d1d6610bd1990c177af8e9b528a10e87889ed8792f5684c048b11af0b69160a"
    assert envelope_schema["title"] == "ExtractionEnvelope"
    assert envelope_schema["properties"]["schema_version"]["const"] == "1.1"


# ---------------------------------------------------------------------------
# evidence reconstruction (QA28 data layer)
# ---------------------------------------------------------------------------


def test_evidence_is_rebuilt_at_runtime_level(
    candidates: Mapping[str, Dict[str, Any]]
) -> None:
    for case_id, candidate in candidates.items():
        keys = walk_keys(candidate)
        assert "bbox_pt" not in keys, case_id
        assert "source_reference" not in keys, case_id
        strings = walk_strings(candidate)
        for forbidden in ("generator_recorded", "text_verified", "human_reviewed",
                          "human_verified", "expected", "fault_type", "gold_path",
                          "must_not_auto_accept", "initial_candidate_file"):
            assert forbidden not in strings, (case_id, forbidden)
        for path, evidence in candidate["evidence"].items():
            assert evidence["verification"] == "model_cited", (case_id, path)
            assert evidence["context_binding"] == "unverified", (case_id, path)
            assert evidence["bbox_precision"] == "unavailable", (case_id, path)
            if evidence["status"] == "observed":
                assert isinstance(evidence["page"], int) and evidence["page"] >= 1
                assert isinstance(evidence["text"], str) and evidence["text"]
                assert evidence["searched_pages"]
            elif evidence["status"] == "not_found":
                assert evidence["page"] is None and evidence["text"] is None
                assert evidence["searched_pages"]
        for path, raw in candidate["raw_fields"].items():
            assert isinstance(raw, (str, type(None))), (case_id, path)
            if isinstance(raw, str):
                assert raw, (case_id, path)
        for name in ("schema_version", "document_id", "invoice", "raw_fields",
                     "scope_status", "scope_signals", "out_of_scope_reasons",
                     "evidence", "row_references", "uncertainties", "extras"):
            assert name in candidate, (case_id, name)
        assert set(candidate) == set(inject.ENVELOPE_KEYS), case_id


@pytest.mark.parametrize("fault", ["unit_decimal_shift", "line_digit_transpose", "tax_null"])
def test_affected_paths_keep_no_correct_value(
    fault: str,
    cases: Mapping[str, inject.InjectionCase],
    golds: Mapping[str, Dict[str, Any]],
    candidates: Mapping[str, Dict[str, Any]],
    envelopes: Mapping[str, Dict[str, Any]],
) -> None:
    selected = [case for case in cases.values() if case.fault_type == fault]
    assert len(selected) == 10
    for case in selected:
        candidate = candidates[case.case_id]
        envelope = envelopes[case.case_id]
        invoice = golds[case.base_doc_id]["invoice"]
        raw_path, evidence_path = candidate_paths_for(case)
        if fault == "unit_decimal_shift":
            original = invoice["line_items"][case.target_row]["unit_price"]
            actual = candidate["invoice"]["items"][case.target_row]["unit_price"]
        elif fault == "line_digit_transpose":
            original = invoice["line_items"][case.target_row]["amount"]
            actual = candidate["invoice"]["items"][case.target_row]["line_net_amount"]
        else:
            original = invoice["tax_amount"]
            actual = candidate["invoice"]["tax_amount"]
        assert actual != original
        assert candidate["raw_fields"][raw_path] != original
        assert candidate["evidence"][evidence_path].get("text") != original
        # The correct value survives only on the evaluation side.  A repeated
        # amount on an unaffected row of the same invoice is not a residue.
        assert envelope["injected"]["original"] == original
        assert candidate["raw_fields"][raw_path] != original


def test_missing_rows_rebuild_indices_and_references(
    cases: Mapping[str, inject.InjectionCase],
    candidates: Mapping[str, Dict[str, Any]],
) -> None:
    selected = [case for case in cases.values() if case.fault_type == "missing_row"]
    assert len(selected) == 10
    for case in selected:
        candidate = candidates[case.case_id]
        count = len(candidate["invoice"]["items"])
        assert [row["item_index"] for row in candidate["row_references"]] == list(range(count))
        assert [row["row_id"] for row in candidate["row_references"]] == [
            f"row-{index + 1}" for index in range(count)
        ]
        evidence_rows = {
            int(match.group(1))
            for key in candidate["evidence"]
            if (match := re.fullmatch(r"items\[(\d+)\]\.\w+", key))
        }
        assert evidence_rows == set(range(count))
        assert not any(key.startswith(f"items[{count}]") for key in candidate["evidence"])


def test_duplicate_rows_stay_identical_and_rebuild_references(
    cases: Mapping[str, inject.InjectionCase],
    candidates: Mapping[str, Dict[str, Any]],
) -> None:
    selected = [case for case in cases.values() if case.fault_type == "duplicate_row"]
    assert len(selected) == 10
    for case in selected:
        candidate = candidates[case.case_id]
        items = candidate["invoice"]["items"]
        assert items[case.target_row] == items[case.target_row + 1]
        assert [row["item_index"] for row in candidate["row_references"]] == list(range(len(items)))
        for field in ("quantity", "unit_price", "line_net_amount"):
            first = candidate["evidence"][f"items[{case.target_row}].{field}"]
            second = candidate["evidence"][f"items[{case.target_row + 1}].{field}"]
            assert first["text"] == second["text"]
        assert candidate["scope_signals"]["observed_row_count"] == len(items)


def test_row_references_agree_with_evidence_and_pages(
    candidates: Mapping[str, Dict[str, Any]]
) -> None:
    for case_id, candidate in candidates.items():
        document_id = candidate["document_id"]
        assert candidate["row_references"], case_id
        for row in candidate["row_references"]:
            assert row["page_id"].startswith(f"{document_id}:p")
            page_number = int(row["page_id"].rsplit(":p", 1)[1])
            assert page_number >= 1
            assert row["field_evidence_keys"], (case_id, row["item_index"])
            for key in row["field_evidence_keys"]:
                assert key in candidate["evidence"], (case_id, key)
                assert key.startswith(f"items[{row['item_index']}]."), (case_id, key)
                assert candidate["evidence"][key]["page"] == page_number, (case_id, key)


def test_tax_null_keeps_the_printed_value_on_the_evaluation_side(
    cases: Mapping[str, inject.InjectionCase],
    envelopes: Mapping[str, Dict[str, Any]],
    golds: Mapping[str, Dict[str, Any]],
) -> None:
    selected = {case.base_doc_id: case for case in cases.values()
                if case.fault_type == "tax_null"}
    assert set(selected) == set(A_DOC_IDS)
    for doc_id, case in selected.items():
        envelope = envelopes[case.case_id]
        assert envelope["injected"]["original"] == golds[doc_id]["invoice"]["tax_amount"]
    assert envelopes[selected["da-a4-01"].case_id]["injected"]["original"] == "0.00"


# ---------------------------------------------------------------------------
# neutral naming and input boundary
# ---------------------------------------------------------------------------


_LEAK_PATTERNS = (
    re.compile(r"da-[a-z0-9]"),
    re.compile(r"unit_decimal_shift|line_digit_transpose|missing_row|duplicate_row|tax_null"),
    re.compile(r"\bfault", re.IGNORECASE),
    re.compile(r"\bclass\b|\bsubclass\b", re.IGNORECASE),
    re.compile(r"\brout(e|ing)\b", re.IGNORECASE),
    re.compile(r"\bexpected\b", re.IGNORECASE),
    re.compile(r"\ba[1-4]\b", re.IGNORECASE),
)


def test_candidate_names_ids_and_paths_are_neutral(
    injection_tree: Path, candidates: Mapping[str, Dict[str, Any]]
) -> None:
    for path in sorted((injection_tree / "candidates").glob("*.json")):
        assert re.fullmatch(r"inj-\d{4}\.json", path.name), path.name
    for case_id, candidate in candidates.items():
        assert re.fullmatch(r"inj-\d{4}", candidate["document_id"]), case_id
        # The names, ids and paths are structural.  Printed text such as the
        # paper size "A4" is document content, not a class label, so it is
        # not scanned here.
        structural = [candidate["document_id"]]
        structural.extend(candidate["raw_fields"])
        structural.extend(candidate["evidence"])
        structural.extend(row["row_id"] for row in candidate["row_references"])
        structural.extend(row["page_id"] for row in candidate["row_references"])
        structural.extend(walk_keys(candidate))
        for text in structural:
            for pattern in _LEAK_PATTERNS:
                assert not pattern.search(text), (case_id, pattern.pattern, text)
        for row in candidate["row_references"]:
            assert row["page_id"].startswith(candidate["document_id"] + ":p")


def test_unique_constraints_are_kept_by_the_candidate(
    candidates: Mapping[str, Dict[str, Any]]
) -> None:
    # A duplicated row is deliberate; the evaluation envelope, not the
    # candidate, explains it.  No other field may claim a classification.
    evaluation_only = {"case_id", "base_doc_id", "split", "group_id", "fault_type",
                       "injected", "expected_behavior", "synthetic_fault"}
    for candidate in candidates.values():
        assert not evaluation_only & set(walk_keys(candidate))


# ---------------------------------------------------------------------------
# immutability and determinism
# ---------------------------------------------------------------------------


def test_injection_changes_no_gold_record_and_no_document(
    tmp_path: Path, golds: Mapping[str, Dict[str, Any]]
) -> None:
    document_ids = [row.doc_id for row in matrix.ROWS if row.klass == "A"]
    before_gold = {
        doc_id: sha256_bytes((GOLD_DIR / f"{doc_id}.json").read_bytes())
        for doc_id in document_ids
    }
    before_documents = {
        doc_id: sha256_bytes((DATASET / golds[doc_id]["file"]["file_name"]).read_bytes())
        for doc_id in document_ids
    }
    out = tmp_path / inject.DIR_NAME
    inject.inject_faults(GOLD_DIR, out)
    after_gold = {
        doc_id: sha256_bytes((GOLD_DIR / f"{doc_id}.json").read_bytes())
        for doc_id in document_ids
    }
    after_documents = {
        doc_id: sha256_bytes((DATASET / golds[doc_id]["file"]["file_name"]).read_bytes())
        for doc_id in document_ids
    }
    assert before_gold == after_gold
    assert before_documents == after_documents
    for doc_id in document_ids:
        record = golds[doc_id]
        assert record["file"]["sha256"] == after_documents[doc_id]


def test_two_generations_are_byte_identical(tmp_path: Path) -> None:
    first = tmp_path / "first" / inject.DIR_NAME
    second = tmp_path / "second" / inject.DIR_NAME
    inject.inject_faults(GOLD_DIR, first)
    inject.inject_faults(GOLD_DIR, second)
    assert tree_hashes(first) == tree_hashes(second)


# ---------------------------------------------------------------------------
# manifest integration and --check
# ---------------------------------------------------------------------------


def test_the_main_manifest_declares_every_injection_file() -> None:
    manifest = load(DATASET / "manifest.json")
    declared = {
        entry["path"]: entry
        for entry in manifest["files"]
        if entry["path"].startswith(inject.DIR_NAME + "/")
    }
    assert len(declared) == 100
    on_disk = {
        path.relative_to(DATASET).as_posix()
        for path in (DATASET / inject.DIR_NAME).rglob("*")
        if path.is_file()
    }
    assert set(declared) == on_disk
    for relative, entry in declared.items():
        data = (DATASET / relative).read_bytes()
        assert entry["artifact_role"] == "generated", relative
        assert entry["media_type"] == "application/json", relative
        assert entry["sha256"] == sha256_bytes(data), relative
        assert entry["bytes"] == len(data), relative
    counts = manifest["counts"]["fault_injection"]
    assert counts == {"cases": 50, "dev": 15, "test": 35,
                      "envelopes": 50, "candidates": 50}


def test_the_suite_does_not_enter_the_thirty_document_denominator() -> None:
    manifest = load(DATASET / "manifest.json")
    assert manifest["counts"]["total"] == 30
    generated = [entry for entry in manifest["files"] if entry["artifact_role"] == "generated"]
    assert len([entry for entry in generated
                if entry["path"].startswith("gold/")]) == 30


def test_check_counts_and_compares_the_injection_files(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone_tree(DATASET, clone)
    result = generate.compare_datasets(clone, DATASET)
    assert result["ok"], result["failures"]
    assert result["counts"]["injection_total"] == 100
    assert result["counts"]["injection_compared"] == 100


def _damage_changed_envelope(tree: Path) -> None:
    path = tree / inject.DIR_NAME / "da-a1-01__unit_decimal_shift.json"
    payload = load(path)
    payload["injected"]["injected"] = "0.00"
    replace_bytes(path, generate.canonical_bytes(payload))


def _damage_changed_candidate(tree: Path) -> None:
    path = tree / inject.DIR_NAME / "candidates" / "inj-0001.json"
    payload = load(path)
    payload["invoice"]["items"][0]["unit_price"] = "0.00"
    replace_bytes(path, generate.canonical_bytes(payload))


def _damage_fault_leak(tree: Path) -> None:
    path = tree / inject.DIR_NAME / "candidates" / "inj-0002.json"
    payload = load(path)
    payload["document_id"] = "da-a1-01"
    replace_bytes(path, generate.canonical_bytes(payload))


@pytest.mark.parametrize(
    "damage",
    [_damage_changed_envelope, _damage_changed_candidate, _damage_fault_leak],
    ids=["changed-envelope", "changed-candidate", "leaked-base-id"],
)
def test_check_rejects_injection_drift(tmp_path: Path, damage) -> None:
    clone = tmp_path / "clone"
    clone_tree(DATASET, clone)
    assert generate.compare_datasets(clone, DATASET)["ok"]
    damage(clone)
    result = generate.compare_datasets(clone, DATASET)
    assert not result["ok"]
    assert result["failures"]


@pytest.mark.slow
def test_check_end_to_end_regenerates_the_injection_suite(tmp_path: Path) -> None:
    fresh = tmp_path / "fresh"
    # ``generate.main`` installs the CLI offline guard process-wide, as the CLI
    # tests in test_generate.py expect.  This test runs in the same process, so
    # it must remove the guard; otherwise the later offline tests in
    # test_offline.py see the CLI guard instead of the session guard.
    try:
        code = generate.main(
            ["--seed", str(matrix.SEED), "--out", str(fresh), "--check",
             "--reference", str(DATASET / "manifest.json"), "--quiet"]
        )
    finally:
        offline.uninstall_network_guard()
    assert code == 0
    manifest = load(fresh / "manifest.json")
    paths = [entry["path"] for entry in manifest["files"]
             if entry["path"].startswith(inject.DIR_NAME + "/")]
    assert len(paths) == 100
    assert manifest["counts"]["fault_injection"]["cases"] == 50
