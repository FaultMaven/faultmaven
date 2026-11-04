"""Close each ChromaDB client a test opens when that test ends.

chromadb keeps a local client's System in the process-wide
``SharedSystemClient._identifier_to_system``, keyed by path. The System holds
the SQLite pool, the HNSW segment files and a Rust runtime. It stays there
until the last client over that path calls ``close()``. Deleting the client
and collecting garbage releases nothing. So a test that opens a client over
its own ``tmp_path`` and never closes it leaves about ten file descriptors and
fifty threads open for the rest of the process. A serial run left ~130 Systems
open, 1,130 descriptors in all. It failed with EMFILE in every later test that
opened a file, in any shell whose soft ``ulimit -n`` is the common 1024.

Where a test, a fixture or a helper opens an on-disk client, it wraps it:

    client = closed_at_teardown(chromadb.PersistentClient(path=...))

and the autouse ``_close_the_chroma_clients_the_test_opened`` in
``tests/conftest.py`` closes it once the test and its own fixtures are torn
down. ``close()`` is refcounted per path, so a closed client never stops a
System another holder still uses. It releases the segment files at once;
chromadb's SQLite handles go when the closed client is garbage-collected.
An ``EphemeralClient`` needs none of this: every one in the process shares a
single in-memory System that holds no files.

``Client.close()`` first shipped in chromadb 1.5.2. The suite runs on the
lockfile's version.

Function scope only. A client held by a module- or session-scoped fixture must
not be registered: it would be closed under every test after the first.

A test that leaves a client open under its own ``tmp_path`` fails at teardown
(:func:`systems_open_under`), whether it opened it or a container it composed.
"""

from __future__ import annotations

import os
import sys
from typing import Protocol, TypeVar


class _Closable(Protocol):
    def close(self) -> None: ...


ClientT = TypeVar("ClientT", bound=_Closable)

_OPENED: list[_Closable] = []


def closed_at_teardown(client: ClientT) -> ClientT:
    """Register ``client`` to be closed when the running test ends; return it."""
    _OPENED.append(client)
    return client


def close_the_clients_opened() -> None:
    """Close every registered client, newest first, then raise what failed.

    Every client is closed even when an earlier close raises. Otherwise the
    rest would stay open and be closed in the next test's teardown, which
    would then be blamed for the error. One failure is raised as itself;
    several are raised together in an ``ExceptionGroup``.
    """
    clients = _OPENED[::-1]
    _OPENED.clear()
    errors: list[Exception] = []
    for client in clients:
        try:
            client.close()
        except Exception as exc:
            errors.append(exc)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise ExceptionGroup("closing the test's ChromaDB clients failed", errors)


def systems_open_under(root: os.PathLike[str] | str) -> list[str]:
    """The persist paths under ``root`` that chromadb still holds a local System for.

    Reads ``SharedSystemClient._identifier_to_system``, chromadb's private
    process-wide cache: a local client's persist path, as it was passed, is its
    key until the last client over it closes, and is popped then (verified on
    1.5.8). Only persistent Systems are read: an HTTP client's key is a UUID and
    an ``EphemeralClient``'s is ``"ephemeral"``. A relative key is resolved
    against ``root``, as opened by a test that ``chdir``s there (the shipped
    ``./data`` defaults). Residual: a relative open made from another working
    directory is read as if made at ``root``, and one that climbs out of a
    subdirectory of ``root`` with ``..`` is missed. Nothing is open when
    chromadb was never imported.
    """
    cache = sys.modules.get("chromadb.api.shared_system_client")
    if cache is None:
        return []
    prefix = os.path.realpath(root) + os.sep
    systems = cache.SharedSystemClient._identifier_to_system
    return sorted(
        key
        for key, system in list(systems.items())
        if system.settings.is_persistent
        and (os.path.realpath(os.path.join(prefix, key)) + os.sep).startswith(prefix)
    )
