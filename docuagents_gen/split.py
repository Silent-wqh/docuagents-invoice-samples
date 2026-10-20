"""Split table and isolation checks for the main sample set (sample plan §6.3).

``splits.py`` answers one question per document: which split holds it, which
source group it belongs to, which supplier entity printed it, and whether it
derives from a generated image base.  ``build_splits`` writes the record; the
``iso*`` functions prove the dev/test boundary holds.

Isolation rule (sample plan §6.3):

* the **source group** is the connected group of a document's derivation.  In
  this set every document is constructed on its own, so the group of ``X`` is
  ``source:X`` and no two documents share it;
* the **supplier entity** is ``vendor:<key>`` from ``matrix.VENDORS``;
* the **image base** is ``<doc_id>-base`` for an image document;
* the **derivation family** is the set of documents that share a base or carry
  an explicit derivation mark.

The four sets of ``dev`` and ``test`` must not intersect.

``matrix.Row.source_family`` is a *category* label (``clean_pdf``,
``degraded_scan``, ...).  The same category appears in both splits on purpose,
so it is recorded in ``splits.json`` and never used as an isolation key.  The
isolation test therefore looks at ``group_id``, not at ``source_family``.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Sequence

from . import matrix


class SplitError(RuntimeError):
    """Raised when a split record or the dev/test isolation is not valid."""


def base_id(row: matrix.Row) -> str | None:
    """The image base of a document, or ``None`` for a PDF document."""
    if row.render_mode != "image":
        return None
    return f"{row.doc_id}-base"


def group_id(row: matrix.Row) -> str:
    """The source group: one document, one construction, one group."""
    return f"source:{row.doc_id}"


def vendor_id(row: matrix.Row) -> str:
    """The supplier entity, shared by several documents of one split."""
    return f"vendor:{matrix.VENDORS[row.vendor].key}"


def record(row: matrix.Row) -> Dict[str, Any]:
    """The ``splits.json`` entry of one row."""
    return {
        "doc_id": row.doc_id,
        "split": row.split,
        "class": row.klass,
        "subclass": row.subclass,
        "group_id": group_id(row),
        "vendor_id": vendor_id(row),
        "base_id": base_id(row),
        "source_family": row.source_family,
    }


def build_splits(rows: Sequence[matrix.Row] | None = None) -> Dict[str, Any]:
    """The complete ``splits.json`` document, in matrix order."""
    selected = list(matrix.ROWS if rows is None else rows)
    return {
        "schema_version": 1,
        "contract_version": "1.1",
        "seed": matrix.SEED,
        "isolation_key": "group_id",
        "documents": [record(row) for row in selected],
    }


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


def _by_split(documents: Iterable[Mapping[str, Any]]) -> Dict[str, List[Mapping[str, Any]]]:
    out: Dict[str, List[Mapping[str, Any]]] = {"dev": [], "test": []}
    for document in documents:
        split = document.get("split")
        if split not in out:
            raise SplitError(f"{document.get('doc_id')}: unknown split {split!r}")
        out[split].append(document)
    return out


def derivation_families(documents: Iterable[Mapping[str, Any]]) -> Dict[str, List[str]]:
    """``family key -> doc ids`` for the derivation families of the set.

    A document without a base is its own family (a PDF is generated directly,
    not derived from an image base).  Documents that share a base are one
    family, which is the multi-page image case the plan protects.
    """
    families: Dict[str, List[str]] = {}
    for document in documents:
        key = document.get("base_id") or f"source:{document['doc_id']}"
        families.setdefault(key, []).append(document["doc_id"])
    return {key: sorted(ids) for key, ids in sorted(families.items())}


def _sets(documents: Sequence[Mapping[str, Any]]) -> Dict[str, set]:
    return {
        "group_id": {document["group_id"] for document in documents},
        "vendor_id": {document["vendor_id"] for document in documents},
        "base_id": {document["base_id"] for document in documents if document.get("base_id")},
        "derivation_family": set(derivation_families(documents)),
    }


def isolation_report(documents: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Overlap counts and crossing values between the dev and test splits."""
    split = _by_split(documents)
    dev, test = _sets(split["dev"]), _sets(split["test"])
    report: Dict[str, Any] = {"counts": {}}
    for name in ("group_id", "vendor_id", "base_id", "derivation_family"):
        crossing = sorted(dev[name] & test[name])
        report[name] = crossing
        report["counts"][name] = len(crossing)
    report["dev_documents"] = len(split["dev"])
    report["test_documents"] = len(split["test"])
    report["overlap_total"] = sum(report["counts"].values())
    return report


def check_isolation(documents: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Raise :class:`SplitError` unless the dev/test isolation holds.

    Returns the report, so a caller can print the counts it proved.
    """
    report = isolation_report(documents)
    if report["overlap_total"]:
        raise SplitError(
            "the dev/test isolation does not hold: "
            + ", ".join(f"{name}={report[name]}" for name in
                        ("group_id", "vendor_id", "base_id", "derivation_family")
                        if report[name])
        )
    if report["dev_documents"] != 10 or report["test_documents"] != 20:
        raise SplitError(
            f"split sizes must be 10 dev / 20 test, got "
            f"{report['dev_documents']} / {report['test_documents']}"
        )
    return report


def check_rule(isolation_key: str = "group_id") -> str:
    """A one-line statement of the isolation key, for a report or a comment."""
    return (
        f"isolation is checked on {isolation_key} (source group) and on "
        "vendor_id, base_id and derivation family; matrix.source_family is a "
        "category label and is expected to cross the split"
    )


def check_references(documents: Sequence[Mapping[str, Any]],
                     available: Iterable[str]) -> Dict[str, Any]:
    """Every split document has an artifact, and every artifact a document.

    Args:
        documents: the ``splits.json`` entries.
        available: the doc ids that have artifacts on disk.

    Returns:
        ``{"missing": [...], "extra": [...]}``; both empty means the split
        table and the artifact set agree.
    """
    declared = [document["doc_id"] for document in documents]
    if len(declared) != len(set(declared)):
        raise SplitError("the split table declares a document twice")
    present = set(available)
    return {
        "missing": sorted(set(declared) - present),
        "extra": sorted(present - set(declared)),
    }


__all__ = [
    "SplitError",
    "base_id",
    "build_splits",
    "check_isolation",
    "check_references",
    "check_rule",
    "derivation_families",
    "group_id",
    "isolation_report",
    "record",
    "vendor_id",
]
