"""Tests for the dataset generator, the split table and the previews (T14).

Everything here runs offline: ``tests/conftest.py`` installs the session
network guard, and the generator installs its own guard at the command-line
entry point.  No model is called and no file outside ``tmp_path`` is written;
the reference tree is generated once per session into a temporary directory.

The tests are grouped as the task requires:

* generation content and the artifact roles (D4, D13);
* splits, isolation and reference integrity (D5);
* previews and the contact sheet (D6);
* ``--check`` and the comparison function (D8);
* atomic publication and directory safety (D2);
* the offline guard at the entry point and in a subprocess (D11);
* the two-branch price policy (D9) and the placeholder check (D10).

Run:

    pytest tests/test_generate.py -q
    pytest -q -m "not slow"      # skip the extra full generations
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import unicodedata
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The heavy tests carry ``@pytest.mark.slow`` so a run can deselect them with
# ``-m "not slow"``.  The marker is registered here because ``tests/conftest.py``
# is a T03 deliverable this task may not edit.  The registration goes through
# the live configuration object, which is the only place pytest accepts a new
# marker once the session has started; a missing private object is tolerated,
# and the marker then only emits an unknown-mark warning.
try:  # pragma: no cover - depends on the pytest version
    import _pytest.mark

    _config = getattr(_pytest.mark.MARK_GEN, "_config", None)
    if _config is not None:
        _config.addinivalue_line("markers", "slow: a full dataset generation; deselect with -m 'not slow'")
except (ImportError, AttributeError):  # pragma: no cover
    pass

from docuagents_gen import build, generate, gold, matrix, offline, split  # noqa: E402

DOCUMENT_COUNT = 30
PAGE_COUNT = 35
IMAGE_DOCUMENTS = 9
PREVIEW_COUNT = PAGE_COUNT


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def clone_tree(source: Path, target: Path) -> None:
    """A cheap copy of ``source``: hard links where the file system allows.

    Every test that damages a tree writes through a new file, never through an
    existing link, so the session reference tree stays untouched.
    """
    shutil.copytree(source, target, copy_function=_link_or_copy)


def _link_or_copy(src: str, dst: str) -> str:
    try:
        os.link(src, dst)
        return dst
    except OSError:
        return shutil.copy2(src, dst)


def replace_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` without writing through a hard link."""
    temporary = path.with_name(path.name + ".new")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def tree_hashes(root: Path) -> Dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def manifest(reference_dir: Path) -> Dict[str, Any]:
    return json.loads((reference_dir / "manifest.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# session fixtures: one full generation, reused by every read-only test
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def reference_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    parent = tmp_path_factory.mktemp("reference")
    out = parent / "docuagents_v1"
    generate.generate_dataset(out, seed=matrix.SEED)
    # The publication moved the temporary directory; nothing is left beside it.
    assert [p.name for p in parent.iterdir() if p.name.startswith(".tmp-docuagents-v1-")] == []
    return out


@pytest.fixture(scope="session")
def reference_manifest(reference_dir: Path) -> Dict[str, Any]:
    return manifest(reference_dir)


@pytest.fixture(scope="session")
def reference_splits(reference_dir: Path) -> Dict[str, Any]:
    return json.loads((reference_dir / "splits.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# generation: content (D3, D13)
# ---------------------------------------------------------------------------


def test_generation_writes_thirty_documents_and_thirty_gold_records(reference_dir: Path) -> None:
    documents = sorted(p.name for p in (reference_dir / "documents").iterdir())
    golds = sorted(p.name for p in (reference_dir / "gold").iterdir())
    assert len(documents) == DOCUMENT_COUNT
    assert len(golds) == DOCUMENT_COUNT
    assert {Path(name).stem for name in documents} == set(matrix.BY_ID)
    assert {Path(name).stem for name in golds} == set(matrix.BY_ID)
    for row in matrix.ROWS:
        expected = "pdf" if row.render_mode == "pdf" else row.render_fmt
        assert (reference_dir / "documents" / f"{row.doc_id}.{expected}").is_file(), row.doc_id


def test_generation_writes_the_planned_directory_structure(reference_dir: Path) -> None:
    for name in ("documents", "gold", "text", "bases", "previews", "reports"):
        assert (reference_dir / name).is_dir(), name
    for name in ("manifest.json", "splits.json", "matrix.snapshot.json"):
        assert (reference_dir / name).is_file(), name
    assert (reference_dir / "previews" / "pages").is_dir()
    assert (reference_dir / "previews" / "contact_sheet.png").is_file()
    assert (reference_dir / "reports" / "check_summary.json").is_file()


def test_counts_match_the_matrix(reference_dir: Path, reference_manifest: Dict[str, Any]) -> None:
    counts = reference_manifest["counts"]
    rows = matrix.ROWS
    assert counts["total"] == DOCUMENT_COUNT
    assert counts["dev"] == sum(1 for row in rows if row.split == "dev") == 10
    assert counts["test"] == sum(1 for row in rows if row.split == "test") == 20
    assert counts["pages"] == PAGE_COUNT
    for name, expected in matrix.SUBCLASS_COUNTS.items():
        assert counts["subclass"][name] == expected, name
    for name, expected in matrix.CLASS_COUNTS.items():
        assert counts["class"][name] == expected, name


def test_image_documents_have_one_base_each(reference_dir: Path) -> None:
    bases = sorted(p.name for p in (reference_dir / "bases").iterdir())
    image_rows = [row for row in matrix.ROWS if row.render_mode == "image"]
    assert len(image_rows) == IMAGE_DOCUMENTS
    assert bases == sorted(f"{row.doc_id}-base.json" for row in image_rows)
    record = json.loads((reference_dir / "bases" / "da-b1-01-base.json").read_text(encoding="utf-8"))
    assert record["base_id"] == "da-b1-01-base"
    assert record["doc_id"] == "da-b1-01"
    assert record["render"]["mode"] == "image"
    assert record["render"]["fmt"] == "png"
    assert record["render"]["dpi"] == 200
    assert record["render"]["degradations"] == [{"kind": "grayscale_scan", "severity": "mild"}]
    assert record["seed"] == matrix.SEED
    assert record["upstream_commit"] == generate.UPSTREAM_COMMIT
    assert record["spec_fingerprint"] == build.fingerprints()["da-b1-01"]
    # C1-02 is a raster document, so it carries a base too.
    assert (reference_dir / "bases" / "da-c1-02-base.json").is_file()


def test_manifest_covers_every_artifact_and_keeps_its_own_hash_out(
    reference_dir: Path, reference_manifest: Dict[str, Any]
) -> None:
    declared = {entry["path"] for entry in reference_manifest["files"]}
    on_disk = {
        path.relative_to(reference_dir).as_posix()
        for path in reference_dir.rglob("*")
        if path.is_file()
    }
    assert declared | {"manifest.json"} == on_disk
    assert "manifest.json" not in declared
    roles = {entry["artifact_role"] for entry in reference_manifest["files"]}
    assert roles == {"generated", "runtime_report"}
    assert reference_manifest["files"], "the manifest must list files"
    for entry in reference_manifest["files"]:
        assert entry["artifact_role"] in {"generated", "frozen_review", "runtime_report"}
        assert entry["bytes"] >= 0
        assert len(entry["sha256"]) == 64
    report = [e for e in reference_manifest["files"] if e["artifact_role"] == "runtime_report"]
    assert [entry["path"] for entry in report] == ["reports/check_summary.json"]


def test_manifest_has_the_required_header_fields(reference_manifest: Dict[str, Any]) -> None:
    for field in (
        "schema_version",
        "contract_version",
        "seed",
        "upstream_commit",
        "generator_commit",
        "generator_source_digest",
        "matrix_digest",
        "environment_digest",
        "digest_method_version",
        "counts",
        "files",
    ):
        assert field in reference_manifest, field
    assert reference_manifest["schema_version"] == 1
    assert reference_manifest["contract_version"] == "1.1"
    assert reference_manifest["digest_method_version"] == "1"
    assert reference_manifest["seed"] == matrix.SEED
    assert reference_manifest["upstream_commit"] == generate.UPSTREAM_COMMIT
    assert reference_manifest["matrix_digest"] == generate.canonical_sha256(build.fingerprints())
    assert reference_manifest["environment_digest"]["canonical_sha256"] == generate.canonical_sha256(
        {
            "lockfile_sha256": reference_manifest["environment_digest"]["lockfile_sha256"],
            "versions": reference_manifest["environment_digest"]["versions"],
        }
    )
    source = reference_manifest["generator_source_digest"]
    assert source["canonical_sha256"] == generate.canonical_sha256(source["files"])
    paths = [item["path"] for item in source["files"]]
    assert paths == sorted(paths)
    assert any(path.startswith("docuagents_gen/") for path in paths)
    assert any(path.startswith("schemas/") for path in paths)


def test_no_timestamp_is_stored_in_the_stable_artifacts(
    reference_dir: Path, reference_manifest: Dict[str, Any]
) -> None:
    stable = {
        entry["path"]
        for entry in reference_manifest["files"]
        if entry["artifact_role"] != "runtime_report"
    }
    for rel in sorted(stable):
        if not rel.endswith(".json"):
            continue
        text = (reference_dir / rel).read_text(encoding="utf-8")
        for token in ("generated_at", "timestamp", "duration_seconds"):
            assert token not in text, (rel, token)
    summary = json.loads((reference_dir / "reports" / "check_summary.json").read_text(encoding="utf-8"))
    assert summary["generated_at"]
    assert summary["duration_seconds"] >= 0


def test_json_artifacts_are_canonical_and_text_is_nfc_with_lf(reference_dir: Path) -> None:
    for path in sorted(reference_dir.rglob("*")):
        if not path.is_file() or path.suffix not in {".json", ".txt"}:
            continue
        raw = path.read_bytes()
        assert b"\r" not in raw, path
        text = raw.decode("utf-8")
        assert unicodedata.is_normalized("NFC", text), path
        assert not text.endswith("\n"), path
        if path.suffix == ".json":
            parsed = json.loads(text)
            assert generate.canonical_bytes(parsed) == raw, path


def test_gold_records_reference_the_real_file_bytes(
    reference_dir: Path, reference_manifest: Dict[str, Any]
) -> None:
    for row in matrix.ROWS:
        record = json.loads((reference_dir / "gold" / f"{row.doc_id}.json").read_text(encoding="utf-8"))
        file_name = record["file"]["file_name"]
        data = (reference_dir / file_name).read_bytes()
        assert record["file"]["sha256"] == hashlib.sha256(data).hexdigest(), row.doc_id
        assert record["file"]["page_count"] == len(
            [p for p in (reference_dir / "text").iterdir() if p.name.startswith(f"{row.doc_id}.p")]
        )
        for key, entry in record["evidence"].items():
            if entry["page"] is not None:
                assert 1 <= entry["page"] <= record["file"]["page_count"], (row.doc_id, key)
            for page in entry["searched_pages"]:
                assert 1 <= page <= record["file"]["page_count"], (row.doc_id, key)
        # The manifest and the gold record agree on the digest.
        entry = next(e for e in reference_manifest["files"] if e["path"] == file_name)
        assert entry["sha256"] == record["file"]["sha256"]


def test_gold_line_items_match_the_printed_layout(reference_dir: Path) -> None:
    for row in matrix.ROWS:
        record = json.loads((reference_dir / "gold" / f"{row.doc_id}.json").read_text(encoding="utf-8"))
        spec = build.build_spec(row)
        from generator.layout import build_pages

        pages = build_pages(spec)
        printed = sum(len(table.rows) for page in pages for table in page.tables)
        if record["invoice"] is None:
            assert printed == 0 or row.doc_kind == "multi_invoice"
            continue
        assert len(record["invoice"]["line_items"]) == printed, row.doc_id


def test_text_pages_are_written_for_every_page(reference_dir: Path) -> None:
    for row in matrix.ROWS:
        record = json.loads((reference_dir / "gold" / f"{row.doc_id}.json").read_text(encoding="utf-8"))
        pages = record["file"]["page_count"]
        for page in range(1, pages + 1):
            path = reference_dir / "text" / f"{row.doc_id}.p{page}.txt"
            assert path.is_file(), path
            assert path.read_text(encoding="utf-8").strip(), path
        assert not (reference_dir / "text" / f"{row.doc_id}.p{pages + 1}.txt").exists()


def test_no_placeholder_residue(reference_dir: Path, reference_manifest: Dict[str, Any]) -> None:
    for path in sorted((reference_dir / "gold").iterdir()) + [
        reference_dir / "manifest.json",
        reference_dir / "splits.json",
        reference_dir / "matrix.snapshot.json",
    ] + sorted((reference_dir / "bases").iterdir()):
        text = path.read_text(encoding="utf-8")
        assert "placeholder" not in text.lower(), path
    # The generator must not use the T07 test helper.
    source = (ROOT / "docuagents_gen" / "generate.py").read_text(encoding="utf-8")
    assert "placeholder_file_meta" not in source


# ---------------------------------------------------------------------------
# splits and isolation (D5)
# ---------------------------------------------------------------------------


def test_splits_follow_the_matrix_order_and_carry_the_isolation_keys(
    reference_splits: Dict[str, Any],
) -> None:
    documents = reference_splits["documents"]
    assert [d["doc_id"] for d in documents] == [row.doc_id for row in matrix.ROWS]
    for document, row in zip(documents, matrix.ROWS):
        assert document["split"] == matrix.SPLIT[row.doc_id]
        assert document["class"] == row.klass
        assert document["subclass"] == row.subclass
        assert document["group_id"] == f"source:{row.doc_id}"
        assert document["vendor_id"] == f"vendor:{matrix.VENDORS[row.vendor].key}"
        expected_base = f"{row.doc_id}-base" if row.render_mode == "image" else None
        assert document["base_id"] == expected_base
        assert document["source_family"] == row.source_family


def test_isolation_has_no_overlap_and_the_set_is_complete(reference_splits: Dict[str, Any]) -> None:
    report = split.check_isolation(reference_splits["documents"])
    assert report["overlap_total"] == 0
    assert report["group_id"] == []
    assert report["vendor_id"] == []
    assert report["base_id"] == []
    assert report["derivation_family"] == []
    assert report["dev_documents"] == 10
    assert report["test_documents"] == 20


def test_source_family_is_a_category_and_crosses_the_split_on_purpose(
    reference_splits: Dict[str, Any],
) -> None:
    documents = reference_splits["documents"]
    families: Dict[str, set] = {}
    for document in documents:
        families.setdefault(document["source_family"], set()).add(document["split"])
    crossing = sorted(name for name, splits in families.items() if len(splits) > 1)
    assert crossing, "the category label is expected on both sides of the split"
    # The isolation key is the source group, not the category.
    assert reference_splits["isolation_key"] == "group_id"
    assert "source group" in split.check_rule()


def test_every_split_document_has_all_of_its_references(reference_dir: Path, reference_splits: Dict[str, Any]) -> None:
    documents = reference_splits["documents"]
    declared = {document["doc_id"] for document in documents}
    present = {path.stem for path in (reference_dir / "documents").iterdir()}
    assert split.check_references(documents, present) == {"missing": [], "extra": []}
    summary = generate.verify_references(reference_dir)
    assert summary["missing_references"] == 0
    assert summary["documents"] == DOCUMENT_COUNT
    assert declared == set(matrix.BY_ID)


def test_splits_and_manifest_agree(reference_dir: Path, reference_splits: Dict[str, Any]) -> None:
    manifest_doc = {
        entry["doc_id"]: entry
        for entry in manifest(reference_dir)["files"]
        if entry["path"].startswith("documents/")
    }
    for document in reference_splits["documents"]:
        entry = manifest_doc[document["doc_id"]]
        for key in ("split", "group_id", "vendor_id", "base_id"):
            assert entry[key] == document[key], (document["doc_id"], key)


def test_the_gold_split_matches_the_split_table(reference_dir: Path, reference_splits: Dict[str, Any]) -> None:
    by_id = {document["doc_id"]: document for document in reference_splits["documents"]}
    for doc_id, document in by_id.items():
        record = json.loads((reference_dir / "gold" / f"{doc_id}.json").read_text(encoding="utf-8"))
        assert record["split"] == document["split"], doc_id


def test_a_gold_split_drift_is_a_missing_reference(reference_dir: Path, tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone_tree(reference_dir, clone)
    path = clone / "gold" / "da-a1-01.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["split"] = "test"
    replace_bytes(path, generate.canonical_bytes(payload))
    report = generate.verify_references(clone)
    assert report["missing_references"] >= 1
    assert any("gold.split" in detail for detail in report["details"])


def test_matrix_snapshot_records_the_frozen_a3_parameters(reference_dir: Path) -> None:
    snapshot = json.loads((reference_dir / "matrix.snapshot.json").read_text(encoding="utf-8"))
    assert snapshot["seed"] == matrix.SEED
    assert snapshot["a3"]["rows_per_page"] == matrix.A3_ROWS_PER_PAGE
    assert snapshot["a3"]["line_count"] == matrix.A3_LINE_COUNT
    assert snapshot["a3"]["description_form"] == "wrapped"
    candidates = build.a3_candidates()
    assert snapshot["a3"]["candidates_total"] == len(candidates)
    assert snapshot["a3"]["passed_count"] == sum(1 for c in candidates if c["passed"])
    first = next(index for index, c in enumerate(candidates) if c["passed"])
    assert snapshot["a3"]["first_pass_index"] == first + 1
    assert [d["doc_id"] for d in snapshot["docs"]] == [row.doc_id for row in matrix.ROWS]
    fingerprints = build.fingerprints()
    for document in snapshot["docs"]:
        assert document["fingerprint"] == fingerprints[document["doc_id"]]


# ---------------------------------------------------------------------------
# previews (D6)
# ---------------------------------------------------------------------------


def test_every_page_has_a_preview(reference_dir: Path) -> None:
    pages = sorted(p.name for p in (reference_dir / "previews" / "pages").iterdir())
    assert len(pages) == PREVIEW_COUNT
    for row in matrix.ROWS:
        record = json.loads((reference_dir / "gold" / f"{row.doc_id}.json").read_text(encoding="utf-8"))
        for page in range(1, record["file"]["page_count"] + 1):
            assert (reference_dir / "previews" / "pages" / f"{row.doc_id}.p{page}.png").is_file()


@pytest.mark.parametrize("doc_id,page", [("da-a3-01", 2), ("da-e-04", 3), ("da-e-06", 2)])
def test_the_later_pages_of_a_multi_page_document_are_previewed(
    reference_dir: Path, doc_id: str, page: int
) -> None:
    from PIL import Image

    path = reference_dir / "previews" / "pages" / f"{doc_id}.p{page}.png"
    assert path.is_file()
    with Image.open(path) as image:
        assert image.mode == "RGB"
        assert image.width > 100 and image.height > 100


def test_the_contact_sheet_covers_every_page_and_labels_each_cell(reference_dir: Path) -> None:
    from PIL import Image

    layout = generate.contact_sheet_layout()
    assert len(layout["cells"]) == PREVIEW_COUNT
    assert layout["columns"] * layout["rows"] >= PREVIEW_COUNT
    keys = {(cell["doc_id"], cell["page"]) for cell in layout["cells"]}
    assert ("da-a3-01", 2) in keys
    assert ("da-e-04", 3) in keys
    assert ("da-e-06", 2) in keys
    for cell in layout["cells"]:
        assert cell["doc_id"] in cell["labels"][0]
        assert str(cell["page"]) in cell["labels"][0]
        assert cell["subclass"] in cell["labels"][1]
        assert cell["legibility"] in cell["labels"][1]
    image_path = reference_dir / "previews" / "contact_sheet.png"
    with Image.open(image_path) as image:
        assert image.mode == "RGB"
        assert image.size == tuple(layout["size"])
        assert image.size[0] >= 600 and image.size[1] >= 600
    # The legibility status is the matrix value, not a re-judgement.
    assert {cell["legibility"] for cell in layout["cells"]} == {
        "clean_text", "image_readable", "image_degraded_unverified"
    }


# ---------------------------------------------------------------------------
# --check and the comparison function (D8)
# ---------------------------------------------------------------------------


def test_check_accepts_an_identical_copy(reference_dir: Path, tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone_tree(reference_dir, clone)
    result = generate.compare_datasets(clone, reference_dir)
    assert result["ok"], result["failures"]
    assert result["counts"]["missing"] == 0
    assert result["counts"]["extra"] == 0
    assert result["counts"]["generated_compared"] == result["counts"]["generated_total"]


def _damage_missing_document(tree: Path) -> None:
    (tree / "documents" / "da-a1-01.pdf").unlink()


def _damage_missing_gold(tree: Path) -> None:
    (tree / "gold" / "da-a2-01.json").unlink()


def _damage_changed_text(tree: Path) -> None:
    path = tree / "text" / "da-a3-01.p2.txt"
    replace_bytes(path, path.read_bytes() + b"\nTOTAL 0.00")


def _damage_changed_splits(tree: Path) -> None:
    path = tree / "splits.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["documents"][0]["split"] = "test"
    replace_bytes(path, generate.canonical_bytes(payload))


def _damage_changed_manifest_field(tree: Path) -> None:
    path = tree / "manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["upstream_commit"] = "0" * 40
    replace_bytes(path, generate.canonical_bytes(payload))


def _damage_extra_file(tree: Path) -> None:
    (tree / "documents" / "da-zz-99.pdf").write_bytes(b"%PDF-1.4\n% extra\n")


def _damage_changed_matrix(tree: Path) -> None:
    path = tree / "matrix.snapshot.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["docs"][0]["fingerprint"] = "0" * 64
    replace_bytes(path, generate.canonical_bytes(payload))


def _damage_changed_preview(tree: Path) -> None:
    from PIL import Image

    path = tree / "previews" / "pages" / "da-a1-01.p1.png"
    image = Image.new("RGB", (64, 64), (255, 0, 0))
    buffer = path.with_suffix(".new")
    image.save(buffer, "PNG")
    os.replace(buffer, path)


@pytest.mark.parametrize(
    "damage",
    [
        _damage_missing_document,
        _damage_missing_gold,
        _damage_changed_text,
        _damage_changed_splits,
        _damage_changed_manifest_field,
        _damage_extra_file,
        _damage_changed_matrix,
        _damage_changed_preview,
    ],
    ids=[
        "missing-document",
        "missing-gold",
        "changed-text",
        "changed-splits",
        "changed-manifest-field",
        "extra-file",
        "changed-matrix",
        "changed-preview",
    ],
)
def test_check_rejects_drift(reference_dir: Path, tmp_path: Path, damage) -> None:
    clone = tmp_path / "clone"
    clone_tree(reference_dir, clone)
    clean = generate.compare_datasets(clone, reference_dir)
    assert clean["ok"]
    damage(clone)
    result = generate.compare_datasets(clone, reference_dir)
    assert not result["ok"]
    assert result["failures"]


def test_check_compares_the_runtime_report_by_existence_only(reference_dir: Path, tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone_tree(reference_dir, clone)
    path = clone / "reports" / "check_summary.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["generated_at"] = "2000-01-01T00:00:00+00:00"
    replace_bytes(path, generate.canonical_bytes(payload))
    result = generate.compare_datasets(clone, reference_dir)
    assert result["ok"], result["failures"]


def test_check_reports_an_unreadable_runtime_report(reference_dir: Path, tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone_tree(reference_dir, clone)
    replace_bytes(clone / "reports" / "check_summary.json", b"{not json")
    result = generate.compare_datasets(clone, reference_dir)
    assert not result["ok"]


@pytest.mark.slow
def test_check_end_to_end_regenerates_and_compares(reference_dir: Path, tmp_path: Path) -> None:
    before = tree_hashes(reference_dir)
    fresh = tmp_path / "fresh"
    code = generate.main(
        ["--seed", str(matrix.SEED), "--out", str(fresh), "--check",
         "--reference", str(reference_dir / "manifest.json")]
    )
    assert code == 0
    assert tree_hashes(reference_dir) == before, "the reference tree must stay read-only"
    # The same run is the second generation: every generated file hashed the same.
    fresh_manifest = manifest(fresh)
    reference_manifest = manifest(reference_dir)
    generated = {
        entry["path"]: entry["sha256"]
        for entry in reference_manifest["files"]
        if entry["artifact_role"] == "generated"
    }
    produced = {
        entry["path"]: entry["sha256"]
        for entry in fresh_manifest["files"]
        if entry["artifact_role"] == "generated"
    }
    assert produced == generated


@pytest.mark.slow
def test_a_damaged_reference_makes_the_cli_check_fail(reference_dir: Path, tmp_path: Path) -> None:
    clone = tmp_path / "damaged"
    clone_tree(reference_dir, clone)
    (clone / "gold" / "da-c1-01.json").unlink()
    code = generate.main(
        ["--seed", str(matrix.SEED), "--out", str(tmp_path / "fresh"), "--check",
         "--reference", str(clone / "manifest.json")]
    )
    assert code != 0


@pytest.mark.slow
def test_a_permuted_generation_order_writes_the_same_files(reference_dir: Path, tmp_path: Path) -> None:
    order = [row.doc_id for row in matrix.ROWS]
    permutation = order[1::2] + order[0::2]
    assert sorted(permutation) == sorted(order)
    out = tmp_path / "permuted"
    summary = generate.generate_dataset(out, seed=matrix.SEED, order=permutation)
    assert summary["documents"] == DOCUMENT_COUNT
    reference_manifest = manifest(reference_dir)
    fresh_manifest = manifest(out)
    generated = {
        entry["path"]: entry["sha256"]
        for entry in reference_manifest["files"]
        if entry["artifact_role"] == "generated"
    }
    produced = {
        entry["path"]: entry["sha256"]
        for entry in fresh_manifest["files"]
        if entry["artifact_role"] == "generated"
    }
    assert produced == generated


# ---------------------------------------------------------------------------
# atomic publication and directory safety (D2)
# ---------------------------------------------------------------------------


def test_an_existing_output_is_refused(reference_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "frozen"
    out.mkdir()
    (out / "keep.txt").write_bytes(b"frozen\n")
    code = generate.main(["--seed", str(matrix.SEED), "--out", str(out)])
    assert code != 0
    assert sorted(p.name for p in out.iterdir()) == ["keep.txt"]
    assert (out / "keep.txt").read_bytes() == b"frozen\n"
    # The refusal happens before any work: no temporary directory is created.
    assert [p for p in tmp_path.iterdir() if p.name.startswith(".tmp-docuagents-v1-")] == []


def test_check_refuses_the_reference_directory_as_output(reference_dir: Path) -> None:
    before = tree_hashes(reference_dir)
    code = generate.main(
        ["--seed", str(matrix.SEED), "--out", str(reference_dir), "--check",
         "--reference", str(reference_dir / "manifest.json")]
    )
    assert code != 0
    assert tree_hashes(reference_dir) == before


def test_a_failure_keeps_only_a_marked_temporary_directory(
    reference_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "never_published"
    calls = {"count": 0}
    real_render = generate.render_document

    def failing_render(spec, seed):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("injected render failure")
        return real_render(spec, seed)

    monkeypatch.setattr(generate, "render_document", failing_render)
    with pytest.raises(generate.GenerateError) as excinfo:
        generate.generate_dataset(out, seed=matrix.SEED)
    assert not out.exists()
    tmp_dir = excinfo.value.tmp_dir
    assert tmp_dir is not None and Path(tmp_dir).is_dir()
    assert (Path(tmp_dir) / ".incomplete").is_file()
    assert "injected render failure" in (Path(tmp_dir) / ".incomplete").read_text(encoding="utf-8")
    assert Path(tmp_dir).name.startswith(".tmp-docuagents-v1-")


def test_a_failure_does_not_touch_the_reference(reference_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before = tree_hashes(reference_dir)

    def failing_write(*args, **kwargs):
        raise RuntimeError("injected write failure")

    monkeypatch.setattr(generate, "_write_bytes", failing_write)
    with pytest.raises(generate.GenerateError):
        generate.generate_dataset(tmp_path / "never_published", seed=matrix.SEED)
    assert tree_hashes(reference_dir) == before
    assert not (tmp_path / "never_published").exists()


@pytest.mark.slow
def test_the_output_directory_is_published_in_one_move(tmp_path: Path) -> None:
    parent = tmp_path / "nested" / "parent"
    out = parent / "dataset"
    generate.generate_dataset(out, seed=matrix.SEED, order=None)  # full run through os.replace
    assert (out / "manifest.json").is_file()
    leftovers = [p for p in parent.iterdir() if p.name.startswith(".tmp-docuagents-v1-")]
    assert leftovers == []


# ---------------------------------------------------------------------------
# offline guard (D11)
# ---------------------------------------------------------------------------


def test_the_guard_blocks_a_connection_and_restores_the_previous_one() -> None:
    import socket as socket_module

    offline.install_network_guard()
    try:
        assert offline.guard_installed()
        with pytest.raises(offline.NetworkBlocked):
            socket_module.create_connection(("example.com", 443), timeout=1)
        assert "create_connection" in offline.blocked_attempts()[-1]
    finally:
        offline.uninstall_network_guard()
    assert not offline.guard_installed()


def test_the_guard_is_idempotent() -> None:
    offline.install_network_guard()
    try:
        first = offline.blocked_attempts()
        offline.install_network_guard()
        assert not first or offline.blocked_attempts() == first
    finally:
        offline.uninstall_network_guard()


def test_a_subprocess_importing_the_guard_is_blocked(tmp_path: Path) -> None:
    script = tmp_path / "child.py"
    script.write_text(
        "import socket, sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from docuagents_gen import offline\n"
        "offline.install_network_guard()\n"
        "try:\n"
        "    socket.create_connection(('example.com', 443), timeout=1)\n"
        "except offline.NetworkBlocked:\n"
        "    print('blocked')\n"
        "    raise SystemExit(0)\n"
        "print('REACHED')\n"
        "raise SystemExit(3)\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, "-I", str(script)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    assert "blocked" in result.stdout
    assert "REACHED" not in result.stdout


def test_the_cli_entry_point_installs_the_guard(tmp_path: Path) -> None:
    assert not offline.guard_installed()
    try:
        code = generate.main(
            ["--seed", str(matrix.SEED), "--out", str(tmp_path / "out"), "--check",
             "--reference", str(tmp_path / "missing" / "manifest.json")]
        )
        assert code != 0
        assert offline.guard_installed()
    finally:
        offline.uninstall_network_guard()
    assert not offline.guard_installed()


def test_check_without_reference_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    # ``--out`` has a default, so ``--check`` alone reaches the usage branch.
    assert not offline.guard_installed()
    try:
        code = generate.main(["--check"])
        assert code == 2
        assert offline.guard_installed()
    finally:
        offline.uninstall_network_guard()
    assert "--reference" in capsys.readouterr().err
    assert not offline.guard_installed()


def test_the_package_generator_uses_no_network_client() -> None:
    import ast

    # ``offline.py`` imports ``socket`` on purpose: it replaces the socket
    # constructors.  It imports no client and never calls a constructor.
    network = {"ssl", "http", "urllib", "urllib3", "requests", "httpx", "aiohttp"}
    for name in ("generate.py", "split.py", "offline.py"):
        tree = ast.parse((ROOT / "docuagents_gen" / name).read_text(encoding="utf-8"), filename=name)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert not (imported & network), (name, imported & network)
    source = (ROOT / "docuagents_gen" / "generate.py").read_text(encoding="utf-8")
    for token in ("urlopen", "requests.get", "requests.post", "httpx.", "socket.socket("):
        assert token not in source, f"generate.py calls {token}"


# ---------------------------------------------------------------------------
# the two-branch price policy (D9, T07-DEF-01)
# ---------------------------------------------------------------------------


def test_a_three_decimal_unit_price_takes_the_rounding_branch() -> None:
    # The source price is 4.125 and the page prints it with three decimals;
    # the printed amount 4.12 is one cent below the half-up product.
    result = gold.evaluate_line_amount("1", "4.125", Decimal("4.12"))
    assert result["branch"] == "rounding"
    assert result["unit_price_precision"] == 3
    assert result["status"] == "PASS"
    assert result["delta"] == "0.01"
    assert "4.125" in result["reason"]
    assert "rounding" in result["reason"]


def test_a_two_decimal_unit_price_is_compared_strictly() -> None:
    result = gold.evaluate_line_amount("1", "4.12", Decimal("4.13"))
    assert result["branch"] == "strict"
    assert result["status"] == "FAIL"
    # The same cent difference is a real conflict when the price is printed to
    # the cent: no unrounded source price can explain it.
    assert result["reason"] is None


def test_the_rounding_branch_still_rejects_a_large_difference() -> None:
    result = gold.evaluate_line_amount("1", "4.125", Decimal("4.20"))
    assert result["branch"] == "rounding"
    assert result["status"] == "FAIL"


def test_no_generated_document_needs_the_rounding_branch(reference_dir: Path) -> None:
    rounding = []
    for row in matrix.ROWS:
        record = json.loads((reference_dir / "gold" / f"{row.doc_id}.json").read_text(encoding="utf-8"))
        invoice = record["invoice"]
        if invoice is None:
            continue
        for item in invoice["line_items"]:
            if item["unit_price"] is None:
                continue
            if gold.printed_price_precision(item["unit_price"]) > 2:
                rounding.append((row.doc_id, item["index"]))
    assert rounding == [], "the 30 main samples must all take the strict branch"


def test_the_c_class_deltas_are_real_conflicts(reference_dir: Path) -> None:
    expected = {
        "da-c1-01": ("-36.00", "A_line_amount"),
        "da-c1-02": ("-63.00", "A_line_amount"),
    }
    for doc_id, (delta, rule) in expected.items():
        record = json.loads((reference_dir / "gold" / f"{doc_id}.json").read_text(encoding="utf-8"))
        assert record["arithmetic"]["lines"][0]["delta"] == delta
        assert record["acceptance_policy"]["rules"][rule] == "FAIL"
    c2 = json.loads((reference_dir / "gold" / "da-c2-01.json").read_text(encoding="utf-8"))
    assert c2["arithmetic"]["delta_subtotal"] == "-10.00"
    c3 = json.loads((reference_dir / "gold" / "da-c3-01.json").read_text(encoding="utf-8"))
    assert c3["arithmetic"]["delta_total"] == "-12.00"


# ---------------------------------------------------------------------------
# reproducibility knobs
# ---------------------------------------------------------------------------


def test_canonical_bytes_are_sorted_compact_and_utf8() -> None:
    payload = generate.canonical_bytes({"b": 1, "a": "中文"})
    assert payload == '{"a":"中文","b":1}'.encode("utf-8")
    assert generate.canonical_sha256({"a": 1}) == hashlib.sha256(b'{"a":1}').hexdigest()


def test_text_is_normalised_to_nfc_and_lf() -> None:
    decomposed = "é\r\nnext\rlast"
    assert generate.normalize_text(decomposed) == "é\nnext\nlast"
    assert generate.text_bytes(decomposed) == "é\nnext\nlast".encode("utf-8")


def test_generation_rejects_an_order_that_is_not_a_permutation(tmp_path: Path) -> None:
    with pytest.raises(generate.GenerateError):
        generate.generate_dataset(tmp_path / "bad", order=["da-a1-01"])


def test_the_manifest_paths_are_sorted(reference_manifest: Dict[str, Any]) -> None:
    paths = [entry["path"] for entry in reference_manifest["files"]]
    assert paths == sorted(paths)


def _iter_json_files(root: Path) -> Iterable[Path]:
    return (path for path in root.rglob("*.json") if path.is_file())


def test_the_report_records_the_run(reference_dir: Path) -> None:
    report = json.loads((reference_dir / "reports" / "check_summary.json").read_text(encoding="utf-8"))
    for field in (
        "generated_at",
        "duration_seconds",
        "seed",
        "counts",
        "manifest_digest",
        "matrix_digest",
        "environment_digest",
        "generator_commit",
        "upstream_commit",
    ):
        assert field in report, field
    assert report["seed"] == matrix.SEED
    assert report["upstream_commit"] == generate.UPSTREAM_COMMIT
    assert report["counts"]["total"] == DOCUMENT_COUNT
    assert len(report["manifest_digest"]) == 64
