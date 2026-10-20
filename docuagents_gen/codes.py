"""Adapter for the DocuAgentS shared status-code contract (T07).

The single source of truth is ``contracts/codes.v1.1.json``, copied verbatim
from the DocuAgentS main project (baseline ``a35f41c``).  This module only
loads that file, verifies its canonical form and SHA-256 digest, and exposes
the values as read-only constants.  It defines no enumeration values of its
own, so the sample repository cannot drift from the contract.

The canonical form is the one used by ``docuagents.codes`` in the main
project: sorted keys, compact separators, no BOM, no trailing newline.

Loading rules (the same three steps as the main project):

1. the raw bytes must equal the canonical serialization of the parsed object;
2. the SHA-256 of the raw bytes must equal :data:`CONTRACT_SHA256`;
3. ``contract_version`` must equal :data:`CONTRACT_VERSION`.

A failure raises :class:`ContractError`; the caller must not continue with a
suspect contract.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Mapping

CONTRACT_VERSION: Final[str] = "1.1"

#: SHA-256 of the canonical UTF-8 bytes of ``contracts/codes.v1.1.json``.
#: The value is pinned by the DocuAgentS main project (``docs/CONTRACTS.md``).
CONTRACT_SHA256: Final[str] = (
    "7fb40fa01668dadcbfa24b4cdad8b45859b8177e5a1c3c12688f76d456bbcb56"
)

CONTRACT_PATH: Final[Path] = (
    Path(__file__).resolve().parents[1] / "contracts" / "codes.v1.1.json"
)


class ContractError(RuntimeError):
    """Raised when the contract file is missing, non-canonical or stale."""


def canonical_bytes(obj: Any) -> bytes:
    """Return the canonical bytes used for the contract digest."""
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return text.encode("utf-8")


def digest(data: bytes) -> str:
    """Return the lowercase hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def load_contract(
    path: Path | None = None,
    expected_sha256: str | None = None,
) -> Mapping[str, Any]:
    """Load, verify and return the contract.

    Raises:
        ContractError: if any verification step fails.
    """
    source = CONTRACT_PATH if path is None else Path(path)
    expected = CONTRACT_SHA256 if expected_sha256 is None else expected_sha256

    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise ContractError(f"cannot read contract file: {source}") from exc

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"contract is not valid UTF-8 JSON: {source}") from exc

    if not isinstance(parsed, dict):
        raise ContractError("contract root must be a JSON object")

    if canonical_bytes(parsed) != raw:
        raise ContractError("contract file is not in canonical byte form")

    actual = digest(raw)
    if actual != expected:
        raise ContractError(
            f"contract digest mismatch: expected {expected}, got {actual}"
        )

    if parsed.get("contract_version") != CONTRACT_VERSION:
        raise ContractError(
            "contract_version mismatch: expected "
            f"{CONTRACT_VERSION!r}, got {parsed.get('contract_version')!r}"
        )

    return MappingProxyType(parsed)


_CONTRACT = load_contract()


def _values(key: str) -> tuple[str, ...]:
    values = _CONTRACT[key]
    if not isinstance(values, list):
        raise ContractError(f"contract namespace {key!r} must be a list")
    return tuple(values)


CONTRACT: Final[Mapping[str, Any]] = _CONTRACT

# Namespaces used by the sample generator and by the gold annotation. ----
ISSUE: Final[tuple[str, ...]] = _values("issue")
SCOPE_REASON: Final[tuple[str, ...]] = _values("scope_reason")
SCOPE_STATUS: Final[tuple[str, ...]] = _values("scope_status")
EVIDENCE_STATUS: Final[tuple[str, ...]] = _values("evidence_status")
BBOX_PRECISION: Final[tuple[str, ...]] = _values("bbox_precision")
VALIDATION_DECISION: Final[tuple[str, ...]] = _values("validation_decision")
CHECKS_RESULT: Final[tuple[str, ...]] = tuple(_CONTRACT["checks"]["result"])

# Gold-only annotation provenance. Never used for runtime evidence. -----
GOLD_ANNOTATION_SOURCE: Final[tuple[str, ...]] = _values("gold_annotation_source")

__all__ = [
    "BBOX_PRECISION",
    "CHECKS_RESULT",
    "CONTRACT",
    "CONTRACT_PATH",
    "CONTRACT_SHA256",
    "CONTRACT_VERSION",
    "ContractError",
    "EVIDENCE_STATUS",
    "GOLD_ANNOTATION_SOURCE",
    "ISSUE",
    "SCOPE_REASON",
    "SCOPE_STATUS",
    "VALIDATION_DECISION",
    "canonical_bytes",
    "digest",
    "load_contract",
]
