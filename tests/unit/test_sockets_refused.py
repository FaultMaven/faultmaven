"""``tests.utils.sockets_refused``: the network block LLM tests run under (#1998).

#2023's review found per-test blocks installed on the test's own
``monkeypatch``: a test that called ``monkeypatch.undo()`` lifted the block, and
a real provider reached a model server on the host. The block lives on a
``MonkeyPatch`` of its own, covers every connect path, and fails when anything
attempted a connection.
"""

from __future__ import annotations

import socket

import pytest

from tests.utils import sockets_refused

pytestmark = pytest.mark.unit

#: TEST-NET-1: never routable, so even an unblocked attempt reaches nothing.
_NOWHERE = ("192.0.2.1", 9)

_PATHS = {
    "connect": lambda: socket.socket().connect(_NOWHERE),
    "connect_ex": lambda: socket.socket().connect_ex(_NOWHERE),
    "create_connection": lambda: socket.create_connection(_NOWHERE, timeout=1),
    "getaddrinfo": lambda: socket.getaddrinfo("model.example.invalid", 443),
}


@pytest.mark.parametrize("path", sorted(_PATHS))
def test_every_connect_path_is_refused_and_fails_the_block(path):
    with pytest.raises(AssertionError, match="attempted network connections"):
        with sockets_refused():
            with pytest.raises(OSError, match="refused in tests"):
                _PATHS[path]()


@pytest.mark.parametrize("path", sorted(_PATHS))
def test_a_tests_own_undo_cannot_lift_the_block(path, monkeypatch):
    """The test's ``monkeypatch.undo()`` reverts only the test's own patches."""
    with pytest.raises(AssertionError, match="attempted network connections"):
        with sockets_refused():
            monkeypatch.setattr(socket, "_sockets_refused_probe", 1, raising=False)
            monkeypatch.undo()
            with pytest.raises(OSError, match="refused in tests"):
                _PATHS[path]()


def test_a_clean_block_passes_and_restores_the_socket_module():
    before = (socket.socket.connect, socket.getaddrinfo)
    with sockets_refused():
        assert socket.socket.connect is not before[0]
    assert (socket.socket.connect, socket.getaddrinfo) == before


@pytest.mark.skipif(
    getattr(socket.socket.connect, "__module__", "") == "socket_guard",
    reason="a process-level socket guard refuses loopback too",
)
def test_own_loopback_admits_only_a_port_this_process_bound():
    with sockets_refused(own_loopback=True):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        client.close()
        server.close()
    with pytest.raises(AssertionError, match="attempted network connections"):
        with sockets_refused(own_loopback=True):
            with pytest.raises(OSError, match="refused in tests"):
                socket.create_connection(("127.0.0.1", 9), timeout=1)
