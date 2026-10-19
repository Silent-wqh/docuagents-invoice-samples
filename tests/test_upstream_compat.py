"""Offline tests for the DocuAgentS upstream-compatibility probe (T03).

Every test reads local files only: no network, no LLM, no clock-dependent
behaviour.  The heavy checks (rendering, degrading, PDF extraction) are cached
once per session by ``docuagents_gen.probe.run_probe``.

Run:

    pytest tests/test_upstream_compat.py -q
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest

from docuagents_gen import probe

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# one probe run for the whole session
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def report() -> dict:
    return probe.run_probe()


@pytest.fixture(scope="session")
def facts(report: dict) -> dict:
    return {c["fact"]: c for c in report["facts"]}


# ---------------------------------------------------------------------------
# repository state
# ---------------------------------------------------------------------------


def test_pinned_commit_is_an_ancestor_of_head() -> None:
    out = subprocess.run(
        ["git", "merge-base", "--is-ancestor", probe.PINNED_COMMIT, "HEAD"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr


def test_upstream_directories_are_unmodified() -> None:
    out = subprocess.run(
        ["git", "diff", "--stat", probe.PINNED_COMMIT, "--", "generator", "scorer", "corpus", "examples"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "", f"upstream files changed:\n{out.stdout}"


def test_origin_and_upstream_remotes_exist() -> None:
    out = subprocess.run(["git", "remote", "-v"], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    remotes = {line.split()[0] for line in out.stdout.splitlines() if line.strip()}
    assert {"origin", "upstream"} <= remotes
    assert "Silent-wqh/docuagents-invoice-samples" in out.stdout
    assert "DrewKraken/invoice-extraction-benchmark" in out.stdout


# ---------------------------------------------------------------------------
# F1 .. F17: each fact is present, has evidence and a conclusion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fact", [f"F{i}" for i in range(1, 18)])
def test_fact_present_with_evidence(facts: dict, fact: str) -> None:
    assert fact in facts, f"{fact} is missing from the report"
    c = facts[fact]
    assert c["status"] in ("pass", "fail", "blocked")
    assert c["commit"] == probe.PINNED_COMMIT
    assert c["location"], f"{fact} has no file location"
    assert c["inputs"], f"{fact} has no check inputs"
    assert c["observed"], f"{fact} has no observed output"
    assert c["assertions"], f"{fact} has no assertion results"


@pytest.mark.parametrize("fact", [f"F{i}" for i in range(1, 18)])
def test_fact_passes_on_this_environment(facts: dict, fact: str) -> None:
    c = facts[fact]
    assert c["status"] == "pass", f"{fact}: {c['failure']}\n" + "\n".join(c["assertions"])


def test_no_fact_is_blocked_or_failed(report: dict) -> None:
    assert report["blocked_or_failed"] == []
    assert report["summary"]["fail"] == 0
    assert report["summary"]["blocked"] == 0


def test_summary_counts_match_facts(report: dict) -> None:
    counts = {"pass": 0, "fail": 0, "blocked": 0}
    for c in report["facts"]:
        counts[c["status"]] += 1
    assert counts["pass"] == report["summary"]["pass"]
    assert counts["fail"] == report["summary"]["fail"]
    assert counts["blocked"] == report["summary"]["blocked"]
    assert len(report["facts"]) == 17


def test_differences_name_affected_construction_and_adaptation(report: dict) -> None:
    for d in report["differences"]:
        assert d["severity"] in ("high", "medium", "low")
        assert d["title"]
        assert d["detail"]
        assert d["affected_sample_constructions"]
        assert d["adaptation"]


# ---------------------------------------------------------------------------
# the specific facts, asserted on their own evidence
# ---------------------------------------------------------------------------


def test_f3_std_headers(facts: dict) -> None:
    assert facts["F3"]["observed"]["headers"] == ["Description", "Qty", "Unit Price", "Amount"]


def test_f4_no_tax_prints_no_tax_row(facts: dict) -> None:
    assert facts["F4"]["observed"]["no_tax_pdf_tax_rows"] == []


def test_f5_zero_rate_is_replaced_for_us(facts: dict) -> None:
    us = facts["F5"]["observed"]["us_tax_rate_0"]
    assert us and us[0][2] != 0, "F5 says tax_rate=0 is replaced for US"
    za = facts["F5"]["observed"]["za_tax_rate_0"]
    assert za and za[0][2] == 0


def test_f7_overrides_are_printed(facts: dict) -> None:
    obs = facts["F7"]["observed"]
    assert obs["subtotal"] == 100.0
    assert obs["total"] == 120.0
    assert obs["printed_totals"]


def test_f9_image_is_one_page(facts: dict) -> None:
    assert facts["F9"]["observed"]["single_page"]["pages"] == 1
    assert "must be one page" in facts["F9"]["observed"]["two_page_image_error"]


def test_f12_layout_and_file_agree(facts: dict) -> None:
    obs = facts["F12"]["observed"]
    assert obs["layout_and_file_agree"] is True
    assert obs["layout_text_equals_pdf_text"] is True


def test_f15_published_sample_reproduces(facts: dict) -> None:
    matches = facts["F15"]["observed"]["published_corpus_matches"]
    assert matches and all(matches.values()), matches


def test_f16_code_and_data_licences_are_separate(facts: dict) -> None:
    obs = facts["F16"]["observed"]
    assert obs["license_kind"].startswith("MIT License")
    assert "Creative Commons Attribution 4.0" in obs["data_license_kind"]


# ---------------------------------------------------------------------------
# the report file matches a fresh probe, and stays portable
# ---------------------------------------------------------------------------


def test_report_file_is_current(report: dict) -> None:
    path = probe.REPORT_PATH
    assert path.is_file(), "run `python -m docuagents_gen.probe` first"
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["upstream"]["commit"] == probe.PINNED_COMMIT
    assert [c["fact"] for c in stored["facts"]] == [f"F{i}" for i in range(1, 18)]
    # Facts are regenerated deterministically; compare without the timestamp-free
    # environment block, which can differ between a laptop and CI.
    stored_facts = {c["fact"]: c for c in stored["facts"]}
    fresh_facts = {c["fact"]: c for c in report["facts"]}
    for fact in fresh_facts:
        assert stored_facts[fact]["status"] == fresh_facts[fact]["status"], fact


def test_report_has_no_absolute_user_paths(report: dict) -> None:
    text = json.dumps(report, ensure_ascii=False)
    assert not re.search(r"[A-Za-z]:\\\\Users\\\\", text), "report leaks a local user path"
    assert not re.search(r"[A-Za-z]:/Users/", text), "report leaks a local user path"
    assert re.search(r"%WINDIR%", text), "the font path must use the %WINDIR% placeholder"


def test_report_has_no_credentials(report: dict) -> None:
    text = json.dumps(report, ensure_ascii=False).lower()
    for marker in ("gho_", "ghp_", "github_pat_", "api_key", "password", "secret"):
        assert marker not in text, f"report contains {marker!r}"


# ---------------------------------------------------------------------------
# environment lock
# ---------------------------------------------------------------------------


def test_environment_lock_matches_runtime() -> None:
    lock_path = ROOT / "environment.lock.json"
    assert lock_path.is_file(), "environment.lock.json is missing"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    assert lock["python"]["version"] == "3.11.17"
    assert lock["baseline_date"] == "2026-03-04"
    assert lock["os"]["platform"].startswith("Windows")
    assert lock["fonts"], "the lock must record the handwriting font hash"
    for font in lock["fonts"].values():
        assert font["sha256"]
        assert font["reference"].startswith("%WINDIR%") or "\\" not in font["reference"]
    assert lock["locale"]["preferred_encoding"]
    assert lock["timezone"]


def test_locked_packages_are_exactly_pinned() -> None:
    lock = json.loads((ROOT / "environment.lock.json").read_text(encoding="utf-8"))
    for name, info in lock["packages"].items():
        assert re.fullmatch(r"\d+(\.\d+)*([.\-+a-z0-9]*)", info["version"]), (name, info["version"])
        assert info["license"], f"{name} has no licence recorded"
    # The three upstream pins must not drift.
    assert lock["packages"]["reportlab"]["version"] == "5.0.1"
    assert lock["packages"]["pillow"]["version"] == "12.3.0"
    assert lock["packages"]["python-dateutil"]["version"].startswith("2.9.0")


def test_requirements_dev_pins_every_direct_dependency() -> None:
    lines = [ln.strip() for ln in (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines()]
    pinned = [ln for ln in lines if ln and not ln.startswith("#")]
    assert pinned, "requirements-dev.txt has no requirements"
    for line in pinned:
        assert re.fullmatch(r"[A-Za-z0-9_.\-]+==[0-9][^\s;]*", line), f"not pinned: {line}"


# ---------------------------------------------------------------------------
# licensing notice
# ---------------------------------------------------------------------------


def test_notice_separates_code_and_data() -> None:
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert "MIT" in notice
    assert "CC BY 4.0" in notice
    assert "generator/" in notice and "scorer/" in notice
    assert "corpus/" in notice and "examples/" in notice
    assert "Ink Free" in notice


def test_upstream_license_is_unchanged() -> None:
    out = subprocess.run(
        ["git", "diff", "--stat", probe.PINNED_COMMIT, "--", "LICENSE", "DATA_LICENSE", "README.md"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "", out.stdout


def test_fonts_are_not_committed() -> None:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    tracked = out.stdout.splitlines()
    assert not [p for p in tracked if p.lower().endswith((".ttf", ".otf", ".ttc"))]
