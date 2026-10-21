"""Tests for the T17 annotation lock, review and dataset README.

The test suite reads the frozen dataset. It never writes the reference: every
mutation runs on a clone in ``tmp_path``. The heavy pre-check and the
end-to-end check carry ``@pytest.mark.slow``.

Run:

    pytest tests/test_review.py -q
    pytest -q -m "not slow"
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, Mapping

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

from docuagents_gen import fixtures, generate, inject, matrix, offline, review  # noqa: E402
from test_generate import clone_tree, replace_bytes  # noqa: E402

DATASET = ROOT / "datasets" / "docuagents_v1"
FIXTURE_DIR = DATASET / generate.FIXTURE_DIR_NAME


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clone(tmp_path: Path) -> Path:
    destination = tmp_path / "dataset"
    clone_tree(DATASET, destination)
    return destination


def frozen_manifest_entries() -> Dict[str, Mapping[str, Any]]:
    manifest = load(DATASET / "manifest.json")
    return {
        entry["path"]: entry
        for entry in manifest["files"]
        if entry["artifact_role"] == "frozen_review"
    }


# ---------------------------------------------------------------------------
# The manifest and the controlled re-freeze
# ---------------------------------------------------------------------------


def test_the_manifest_declares_exactly_the_three_frozen_files() -> None:
    entries = frozen_manifest_entries()
    assert set(entries) == {review.LOCK_NAME, review.REVIEW_NAME, review.README_NAME}
    assert entries[review.LOCK_NAME]["frozen_kind"] == "annotation_lock"
    assert entries[review.REVIEW_NAME]["frozen_kind"] == "annotation_review"
    assert entries[review.README_NAME]["frozen_kind"] == "dataset_readme"
    assert entries[review.README_NAME]["media_type"] == "text/markdown"
    for path, entry in entries.items():
        data = (DATASET / path).read_bytes()
        assert entry["sha256"] == sha256_bytes(data), path
        assert entry["bytes"] == len(data), path


def test_the_generated_counts_are_not_reduced() -> None:
    manifest = load(DATASET / "manifest.json")
    generated = [entry for entry in manifest["files"] if entry["artifact_role"] == "generated"]
    main = [entry for entry in generated if not entry["path"].startswith(inject.DIR_NAME + "/")]
    injection = [entry for entry in generated if entry["path"].startswith(inject.DIR_NAME + "/")]
    assert len(main) == 142
    assert len(injection) == 100
    assert manifest["counts"]["total"] == 30
    assert manifest["counts"]["pages"] == 35
    assert manifest["counts"]["dev"] == 10
    assert manifest["counts"]["test"] == 20
    assert manifest["counts"]["fault_injection"] == {
        "cases": 50, "dev": 15, "test": 35, "envelopes": 50, "candidates": 50,
    }
    fixture_manifest = load(FIXTURE_DIR / "manifest.json")
    assert len(fixture_manifest["files"]) == 64
    assert fixture_manifest["counts"] == {"content": 18, "technical": 4, "file": 22}


def test_the_source_digest_covers_the_review_module() -> None:
    manifest = load(DATASET / "manifest.json")
    files = {entry["path"]: entry["sha256"] for entry in manifest["generator_source_digest"]["files"]}
    assert "docuagents_gen/review.py" in files
    assert files["docuagents_gen/review.py"] == sha256_bytes(
        (ROOT / "docuagents_gen" / "review.py").read_bytes()
    )


# ---------------------------------------------------------------------------
# The annotation lock
# ---------------------------------------------------------------------------


def test_the_lock_binds_every_source_and_gold_record() -> None:
    lock = load(DATASET / review.LOCK_NAME)
    assert lock["status"] == "pending_human_confirmation"
    assert lock["reviewed_by"] is None
    assert lock["reviewed_at"] is None
    assert [entry["doc_id"] for entry in lock["entries"]] == [row.doc_id for row in matrix.ROWS]
    for entry in lock["entries"]:
        document = DATASET / entry["file"]["path"]
        gold_path = DATASET / entry["gold"]["path"]
        assert entry["file"]["byte_sha256"] == sha256_bytes(document.read_bytes())
        assert entry["file"]["bytes"] == document.stat().st_size
        assert entry["gold"]["sha256"] == sha256_bytes(gold_path.read_bytes())
        assert entry["gold"]["bytes"] == gold_path.stat().st_size
        assert entry["annotation_status"] == "pending_human_confirmation"
        assert entry["readability"]["verified"] is False


def test_the_lock_decisions_match_the_gold_records() -> None:
    lock = load(DATASET / review.LOCK_NAME)
    by_id = {entry["doc_id"]: entry for entry in lock["entries"]}
    for row in matrix.ROWS:
        gold = load(DATASET / "gold" / f"{row.doc_id}.json")
        assert by_id[row.doc_id]["decisions"] == review._route_of(gold), row.doc_id


def test_the_lock_records_the_deliberate_cases() -> None:
    lock = load(DATASET / review.LOCK_NAME)
    by_id = {entry["doc_id"]: entry for entry in lock["entries"]}
    d1 = by_id["da-d1-01"]["decisions"]
    assert "tax_amount" in d1["not_found_fields"]
    assert d1["must_not_auto_accept"] is False
    d2 = by_id["da-d2-01"]["decisions"]
    assert "invoice_number" in d2["not_found_fields"]
    assert d2["must_not_auto_accept"] is True
    for doc_id in ("da-c1-01", "da-c2-01", "da-c3-01"):
        assert by_id[doc_id]["decisions"]["must_not_auto_accept"] is True
    for doc_id in ("da-e-01", "da-e-06"):
        decisions = by_id[doc_id]["decisions"]
        assert decisions["scope_status"] == "UNSUPPORTED"
        assert decisions["must_not_auto_accept"] is True
    a1 = by_id["da-a1-01"]["decisions"]
    assert a1["route"] == "auto_accepted"
    assert a1["must_not_auto_accept"] is False


def test_the_lock_verifies_and_detects_a_changed_source(tmp_path: Path) -> None:
    assert review.verify_annotation_lock(DATASET)["ok"]
    tree = clone(tmp_path)
    document = next((tree / "documents").glob("da-a1-01.*"))
    replace_bytes(document, document.read_bytes() + b"\n")
    result = review.verify_annotation_lock(tree)
    assert not result["ok"]
    assert any("not bound to the final source file" in item for item in result["failures"])


def test_the_lock_detects_a_changed_gold_record(tmp_path: Path) -> None:
    tree = clone(tmp_path)
    gold_path = tree / "gold" / "da-a1-01.json"
    replace_bytes(gold_path, gold_path.read_bytes() + b"\n")
    result = review.verify_annotation_lock(tree)
    assert not result["ok"]
    assert any("not bound to the final gold record" in item for item in result["failures"])


def test_two_lock_builds_are_byte_identical() -> None:
    first = review.lock_bytes(review.build_annotation_lock(DATASET))
    second = review.lock_bytes(review.build_annotation_lock(DATASET))
    assert first == second
    assert first == (DATASET / review.LOCK_NAME).read_bytes()


# ---------------------------------------------------------------------------
# The frozen review
# ---------------------------------------------------------------------------


def test_the_review_covers_every_document_and_page() -> None:
    report = load(DATASET / review.REVIEW_NAME)
    assert report["artifact_role"] == "frozen_review"
    assert [item["doc_id"] for item in report["documents"]] == [row.doc_id for row in matrix.ROWS]
    pages = 0
    for item in report["documents"]:
        assert item["page_count"] == len(item["pages"])
        for page in item["pages"]:
            assert (DATASET / page["preview"]).is_file()
            assert (DATASET / page["text"]).is_file()
            if item["text_layer"] == "absent_image":
                assert page["status"] == "requires_human_visual_check"
            else:
                assert page["status"] == "pass"
        pages += len(item["pages"])
    assert pages == 35
    assert report["machine_precheck"]["documents"] == 30
    assert report["machine_precheck"]["pages"] == 35


def test_the_review_covers_the_fixtures_and_the_unreadable_case() -> None:
    report = load(DATASET / review.REVIEW_NAME)
    fixtures_section = report["fixtures"]
    assert fixtures_section["content"] == 18
    assert fixtures_section["technical"] == 4
    assert fixtures_section["object_level"] == 4
    assert len(fixtures_section["documents"]) == 18
    assert len(fixtures_section["object_level_cases"]) == 4
    carry = report["t15_carryover"]
    assert carry["fixture_id"] == "da-fx-unread"
    assert carry["fields"] == ["subtotal_amount", "tax_amount", "total_amount"]
    assert carry["evidence_status"] == "unreadable"
    assert carry["must_not_auto_accept"] is True
    gold = load(FIXTURE_DIR / "gold" / "da-fx-unread.json")
    assert gold["invoice"]["subtotal_amount"] is None
    assert gold["invoice"]["tax_amount"] is None
    assert gold["invoice"]["total_amount"] is None
    assert gold["acceptance_policy"]["decisions"] == ["UNREADABLE_FIELD"]


def test_the_review_records_zero_disputes_and_stays_pending() -> None:
    report = load(DATASET / review.REVIEW_NAME)
    assert report["differences"] == []
    assert report["disputes"] == []
    assert report["status"] == "pending_human_confirmation"
    assert report["reviewer"] == "Project Owner"
    assert report["reviewed_by"] is None
    assert report["reviewed_at"] is None
    assert report["machine_precheck"]["differences"] == []
    assert report["annotation_lock"]["sha256"] == sha256_bytes(
        (DATASET / review.LOCK_NAME).read_bytes()
    )
    assert report["dataset_readme"]["sha256"] == sha256_bytes(
        (DATASET / review.README_NAME).read_bytes()
    )


def test_the_review_carries_the_nine_completion_criteria() -> None:
    report = load(DATASET / review.REVIEW_NAME)
    criteria = report["completion_criteria"]
    assert [item["id"] for item in criteria] == [f"C{index}" for index in range(1, 10)]
    for item in criteria:
        assert item["criterion"]
        assert item["evidence"]
        assert item["status"] in (
            "PASS", "PENDING_FINAL_ACCEPTANCE_RUN", "PENDING_HUMAN_CONFIRMATION",
        )
    assert criteria[-1]["status"] == "PENDING_HUMAN_CONFIRMATION"


def test_the_review_lists_the_manual_check_items() -> None:
    report = load(DATASET / review.REVIEW_NAME)
    categories = {item["category"] for item in report["manual_check_items"]}
    assert {
        "image_samples", "b3_degraded_readability", "c1_scanned_conflict",
        "routing_and_scope", "multi_page", "duplicate_amount_lines",
        "unreadable_fixture",
    } <= categories
    checklist = report["manual_checklist"]
    assert checklist["signing_steps"]
    assert "contact_sheet.png" in checklist["contact_sheet_note"]
    assert checklist["preview_index"]["da-a1-01"] == ["previews/pages/da-a1-01.p1.png"]


def test_the_review_verifies_and_detects_drift(tmp_path: Path) -> None:
    assert review.verify_annotation_review(DATASET)["ok"]
    tree = clone(tmp_path)
    lock_path = tree / review.LOCK_NAME
    lock = load(lock_path)
    lock["status"] = "confirmed"
    replace_bytes(lock_path, generate.canonical_bytes(lock))
    result = review.verify_annotation_review(tree)
    assert not result["ok"]
    assert any("not bound to the current annotation lock" in item for item in result["failures"])


def test_two_review_builds_are_byte_identical() -> None:
    first = review.review_bytes(review.build_annotation_review(DATASET))
    second = review.review_bytes(review.build_annotation_review(DATASET))
    assert first == second
    assert first == (DATASET / review.REVIEW_NAME).read_bytes()


def test_the_frozen_documents_hold_no_clock_or_absolute_path() -> None:
    for path in (DATASET / review.LOCK_NAME, DATASET / review.REVIEW_NAME):
        text = path.read_text(encoding="utf-8")
        for forbidden in ("generated_at", "timestamp", "duration_seconds", "C:\\\\"):
            assert forbidden not in text, (path.name, forbidden)


# ---------------------------------------------------------------------------
# The dataset README
# ---------------------------------------------------------------------------


def test_the_readme_has_every_required_section() -> None:
    text = (DATASET / review.README_NAME).read_text(encoding="utf-8")
    for heading in review.README_SECTIONS:
        assert heading in text, heading


def test_the_readme_states_the_scoring_range_and_the_limitations() -> None:
    text = (DATASET / review.README_NAME).read_text(encoding="utf-8")
    scoring = review.scoring_range(DATASET)
    assert scoring["fields_scored"] == 1003
    assert scoring["explicit_null_decisions"] == 28
    assert scoring["non_null_decisions"] == 975
    assert scoring["documents"] == 28
    assert scoring["excluded"] == {"b3_slots": 42, "multi_invoice_level_slots": 11}
    for value in ("1003", "975", "28", "42", "11"):
        assert value in text
    for phrase in (
        "All documents come from one layout engine",
        "not validated against real scans",
        "public test set",
        "test visibility",
    ):
        assert phrase.lower() in text.lower()


def test_the_readme_separates_the_licences_and_the_model_inputs() -> None:
    text = (DATASET / review.README_NAME).read_text(encoding="utf-8")
    for phrase in ("MIT", "CC BY 4.0", "LICENSE", "DATA_LICENSE", "NOTICE"):
        assert phrase in text
    for forbidden in ("gold/", "text/", "bases/", "annotations.lock.json"):
        assert forbidden in text  # named in the prohibited list


def test_the_notice_carries_the_t17_declarations() -> None:
    text = (ROOT / "NOTICE").read_text(encoding="utf-8")
    for phrase in (
        "annotations.lock.json",
        "reports/annotation_review.json",
        "docuagents_gen/review.py",
        "CC BY 4.0",
        "MIT",
    ):
        assert phrase in text


# ---------------------------------------------------------------------------
# The end-to-end check
# ---------------------------------------------------------------------------


def test_the_check_accepts_the_frozen_dataset(tmp_path: Path) -> None:
    tree = clone(tmp_path)
    result = generate.compare_datasets(tree, DATASET)
    assert result["ok"], result["failures"][:10]
    assert result["counts"]["generated_compared"] == 142
    assert result["counts"]["fixture_total"] == 64
    assert result["counts"]["injection_total"] == 100
    assert result["counts"]["frozen_total"] == 3
    assert result["counts"]["frozen_verified"] == 3
    assert result["counts"]["frozen_compared"] == 3
    assert result["counts"]["lock_verified"] == 1
    assert result["counts"]["review_verified"] == 1
    assert result["counts"]["readme_verified"] == 1


@pytest.mark.parametrize(
    "relative",
    [review.LOCK_NAME, review.REVIEW_NAME, review.README_NAME],
)
def test_the_check_detects_drift_in_each_frozen_file(tmp_path: Path, relative: str) -> None:
    tree = clone(tmp_path)
    path = tree / relative
    replace_bytes(path, path.read_bytes() + b"\n")
    manifest = load(DATASET / "manifest.json")
    result = review.check_frozen_artifacts(tree, DATASET, manifest)
    assert not result["ok"]
    assert any(relative in failure for failure in result["failures"])


@pytest.mark.slow
def test_the_precheck_finds_no_substantive_difference() -> None:
    result = review.machine_precheck(DATASET)
    assert result["differences"] == []
    assert result["documents"] == 30
    assert result["pages"] == 35
    assert result["evidence"]["located"] > 0
    assert result["evidence"]["boxes_checked"] > 0
    assert result["evidence"]["not_found"] > 0
    assert result["manifest"]["main_generated"] == 142
    assert result["manifest"]["main_verified"] == 142
    assert result["manifest"]["injection"] == 100
    assert result["manifest"]["injection_verified"] == 100
    assert result["fixtures"]["manifest_verified"] == 64
    assert result["fixtures"]["content"] == 18
    assert result["fixtures"]["technical"] == 4
    assert result["fixtures"]["object_level"] == 4
    assert result["scoring"]["fields_scored"] == 1003


@pytest.mark.slow
def test_the_check_command_fails_on_a_drifted_reference(tmp_path: Path) -> None:
    reference = clone(tmp_path)
    lock_path = reference / review.LOCK_NAME
    replace_bytes(lock_path, lock_path.read_bytes() + b"\n")
    out = tmp_path / "fresh"
    try:
        code = generate.main([
            "--seed", "2026",
            "--out", str(out),
            "--check",
            "--reference", str(reference / "manifest.json"),
            "--quiet",
        ])
    finally:
        offline.uninstall_network_guard()
    assert code == 1
