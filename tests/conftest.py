"""Session-wide guard: the test suite must run without network access (T03).

The guard is installed before any test module imports the probe, and it
replaces the real socket constructors.  A test that tries to open a
connection fails loudly instead of silently reaching the network.

This is a guard, not a mock: ``socket.socket`` is replaced by a function that
raises.  ``tests/test_offline.py`` proves that the replacement is active and
that the probe still completes while it is active.
"""

from __future__ import annotations

import socket

import pytest


class NetworkBlocked(RuntimeError):
    """Raised when test-time code tries to open a network connection."""


class NetworkGuard:
    """Counts and refuses every connection attempt, like an offline machine."""

    error_type = NetworkBlocked

    def __init__(self) -> None:
        self.attempts: list[str] = []
        self._saved: dict[str, object] = {}

    def _blocked(self, name: str):  # noqa: ANN202
        def blocked(*args, **kwargs):  # noqa: ANN002, ANN003
            self.attempts.append(name)
            raise NetworkBlocked(
                f"{name}() was called during an offline test run"
            )

        return blocked

    def install(self) -> None:
        targets = ("socket", "create_connection", "getaddrinfo", "socketpair")
        for name in targets:
            if hasattr(socket, name):
                self._saved[name] = getattr(socket, name)
                setattr(socket, name, self._blocked(name))

    def uninstall(self) -> None:
        for name, original in self._saved.items():
            setattr(socket, name, original)
        self._saved.clear()

    @property
    def installed(self) -> bool:
        return bool(self._saved)


@pytest.fixture(scope="session")
def network_guard() -> NetworkGuard:
    guard = NetworkGuard()
    guard.install()
    try:
        yield guard
    finally:
        guard.uninstall()


@pytest.fixture(scope="session", autouse=True)
def _offline_for_the_whole_session(network_guard: NetworkGuard) -> None:
    """Every test in this repository runs with the network guard installed."""
    yield
