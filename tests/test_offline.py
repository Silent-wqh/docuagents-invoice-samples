"""Offline assertions for the T03 probe (defect 2 of Review R1).

These tests prove three things, and none of them can pass by accident:

1. the session guard really replaced ``socket.socket`` (the object is not the
   original, and calling it raises);
2. a real outbound attempt is refused while the guard is installed;
3. the probe's heavy path (rendering, PDF extraction) completes with the
   guard installed, so the 17 facts do not need the network.

The guard itself lives in ``tests/conftest.py``.
"""

from __future__ import annotations

import ast
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

NETWORK_MODULES = {"socket", "ssl", "http", "urllib", "urllib3", "requests", "httpx", "aiohttp",
                   "ftplib", "telnetlib", "smtplib"}


# ---------------------------------------------------------------------------
# the guard is real
# ---------------------------------------------------------------------------


def test_guard_is_installed(network_guard) -> None:
    assert network_guard.installed, "the offline guard is not installed"
    # The real socket type is gone: the name now holds the guard's function.
    assert not isinstance(socket.socket, type)
    assert getattr(socket.socket, "__name__", "") == "blocked"


def test_an_outbound_connection_is_refused(network_guard) -> None:
    before = len(network_guard.attempts)
    with pytest.raises(network_guard.error_type):
        socket.create_connection(("example.com", 443), timeout=1)
    assert len(network_guard.attempts) == before + 1
    assert "create_connection" in network_guard.attempts[-1]


def test_a_raw_socket_is_refused(network_guard) -> None:
    with pytest.raises(network_guard.error_type):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    assert "socket" in network_guard.attempts[-1]


def test_dns_resolution_is_refused(network_guard) -> None:
    with pytest.raises(network_guard.error_type):
        socket.getaddrinfo("example.com", 443)


# ---------------------------------------------------------------------------
# the probe does not need the network
# ---------------------------------------------------------------------------


def test_probe_runs_with_the_guard_installed(network_guard) -> None:
    from docuagents_gen import probe

    before = len(network_guard.attempts)
    report = probe.run_probe()
    assert report["summary"]["fail"] == 0
    assert report["summary"]["blocked"] == 0
    assert len(network_guard.attempts) == before, (
        f"the probe tried to use the network: {network_guard.attempts[before:]}"
    )


def test_probe_does_not_import_network_modules() -> None:
    tree = ast.parse((ROOT / "docuagents_gen" / "probe.py").read_text(encoding="utf-8"),
                     filename="docuagents_gen/probe.py")
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not (imported & NETWORK_MODULES), imported & NETWORK_MODULES


def test_probe_has_no_url_fetch_calls() -> None:
    source = (ROOT / "docuagents_gen" / "probe.py").read_text(encoding="utf-8")
    for token in ("urlopen", "requests.get", "requests.post", "httpx.", "socket."):
        assert token not in source, f"probe.py calls {token}"
    # URLs are allowed only as recorded identifiers in the report header.
    urls = [ln for ln in source.splitlines() if "https://" in ln]
    assert all(ln.strip().startswith(('"repository"', '"fork"')) for ln in urls), urls


def test_no_test_module_imports_a_network_client() -> None:
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        # `socket` itself is allowed in the guard tests; HTTP clients are not.
        assert not (imported & (NETWORK_MODULES - {"socket"})), (path.name, imported)
