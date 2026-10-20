"""Offline guard for the command-line entry points (T03 defect 1, extended by T14).

``tests/conftest.py`` installs an offline guard for the test session.  That
guard covers the test process only: a CLI run (``python -m
docuagents_gen.generate``) and any subprocess it starts are outside it.  This
module provides the same guard as normal package code, so the command line can
prove it never reaches the network.

The guard is a replacement, not a mock: ``socket.socket``,
``socket.create_connection``, ``socket.getaddrinfo`` and ``socket.socketpair``
become functions that raise :class:`NetworkBlocked`.  Nothing here opens a
socket or catches the error, so a caller that needs the network fails loudly.

``install_network_guard`` is idempotent: a second call while the guard is
installed does nothing.  ``uninstall_network_guard`` restores exactly the
objects that were in place before the first install, which lets a test install
the guard on top of the session guard and take it off again without leaving
the session unprotected.
"""

from __future__ import annotations

import socket
from typing import Callable, Dict, List

#: The socket attributes the guard replaces.  ``socket.socket`` is a class, so
#: it is replaced by a function: the name no longer constructs a socket.
TARGETS = ("socket", "create_connection", "getaddrinfo", "socketpair")

_saved: Dict[str, object] = {}
_attempts: List[str] = []


class NetworkBlocked(RuntimeError):
    """Raised when guarded code tries to open a network connection."""


def _blocked(name: str) -> Callable[..., object]:
    def blocked(*args: object, **kwargs: object) -> object:
        _attempts.append(name)
        raise NetworkBlocked(f"{name}() was called while the offline guard was installed")

    blocked.__name__ = "blocked"
    blocked.__doc__ = f"Offline replacement for socket.{name}."
    return blocked


def install_network_guard() -> None:
    """Replace the socket constructors with functions that raise."""
    if _saved:
        return
    for name in TARGETS:
        if hasattr(socket, name):
            _saved[name] = getattr(socket, name)
            setattr(socket, name, _blocked(name))


def uninstall_network_guard() -> None:
    """Restore the socket constructors that were in place before the install."""
    for name, original in list(_saved.items()):
        setattr(socket, name, original)
    _saved.clear()
    del _attempts[:]


def guard_installed() -> bool:
    """True while this module's guard is the active set of constructors."""
    return bool(_saved)


def blocked_attempts() -> List[str]:
    """The blocked calls since the last install, for a test or a run report."""
    return list(_attempts)


__all__ = [
    "NetworkBlocked",
    "TARGETS",
    "blocked_attempts",
    "guard_installed",
    "install_network_guard",
    "uninstall_network_guard",
]
