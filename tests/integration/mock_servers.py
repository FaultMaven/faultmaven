"""A free port for a test that serves an app on loopback.

The mock LLM and web-search servers that lived here went with the conftest
fixture that started them, which no test requested (#2240).
"""

import socket


def get_free_port() -> int:
    """Get a free port for server binding."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        s.listen(1)
        port = s.getsockname()[1]
    return port
