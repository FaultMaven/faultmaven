#!/usr/bin/env python3
"""Prove the process talks to a real Redis through the hiredis parser (fm#1031).

The cloud lockfile pins ``hiredis``; the cloud deployment runs a real Redis
server. That pairing — and only that pairing — is what ``Test Cloud`` is for.
Neither half is self-evident at run time:

* Redis is resolved by :func:`get_async_redis_client`, which **degrades to the
  in-process FakeRedis** whenever the server is unreachable and
  ``DEPLOYMENT_MODE`` is not ``cloud``. A wrong host, an unstarted service
  container, a typo'd port — all of them produce a green job that tested the
  stand-in. That silence is the whole of fm#1031.
* Which parser redis-py uses is decided by whether ``hiredis`` imports, and
  ``hiredis`` is a transitive extra (``redis[hiredis]``). Losing it swaps in the
  pure-Python parser, which behaves differently enough to hide a class of bug
  (fm#990) — silently, because nothing else looks.

So this converts both silences into an exit code. It is deliberately blunt:
it refuses to pass on evidence it could not gather, because a check that cannot
see the thing it is checking must not report success.

Run it wherever the pairing is claimed — the ``Test Cloud`` job runs it before
the suite. Configuration comes from the same environment the app reads
(``REDIS_HOST`` / ``REDIS_PORT`` / ``REDIS_DB`` / ``REDIS_PASSWORD``, or
``REDIS_URL``).
"""

import asyncio
import sys


class PairingError(RuntimeError):
    """The environment is not the cloud pairing this script asserts."""


def _live_parser_class(client) -> type:
    """The parser class on a connection this client actually opened.

    Read off the pool rather than off ``DefaultParser`` so the answer is about
    the client in hand, not about what a hypothetical new connection would pick.
    Both are redis-py internals; if they move, this raises rather than shrugging
    — see the module docstring on gathering evidence.
    """
    pool = getattr(client, "connection_pool", None)
    connections = list(getattr(pool, "_available_connections", None) or [])
    connections += list(getattr(pool, "_in_use_connections", None) or [])
    if not connections:
        raise PairingError(
            "no pooled Redis connection to inspect after a successful command — "
            "redis-py's pool internals have changed; update "
            "scripts/check_redis_pairing.py rather than dropping the check"
        )
    parser = getattr(connections[0], "_parser", None)
    if parser is None:
        raise PairingError(
            "a pooled Redis connection exposes no parser — redis-py's connection "
            "internals have changed; update scripts/check_redis_pairing.py rather "
            "than dropping the check"
        )
    return type(parser)


async def check() -> str:
    from faultmaven.infrastructure.redis_client import (
        get_async_redis_client,
        is_fakeredis,
    )

    client = await get_async_redis_client()

    if is_fakeredis(client):
        raise PairingError(
            "the app resolved Redis to the in-process FakeRedis, not a server. "
            "get_async_redis_client() falls back silently, so this is what an "
            "unreachable service container looks like: check REDIS_HOST / "
            "REDIS_PORT / REDIS_DB and that the redis service is healthy"
        )

    # A real command, not just the ping inside the factory: the read path is
    # where the parser lives, and reading back what was written is the only
    # thing that proves the server is answering rather than merely accepting a
    # connection.
    key = "faultmaven:ci:redis-pairing"
    await client.set(key, "ok", ex=60)
    value = await client.get(key)
    if value != "ok":
        raise PairingError(
            f"Redis round-trip returned {value!r} rather than 'ok' — the server "
            "is reachable but not storing values"
        )

    parser = _live_parser_class(client)
    if "hiredis" not in parser.__module__:
        raise PairingError(
            f"redis-py is parsing with {parser.__module__}.{parser.__name__}, not "
            "the hiredis parser. The cloud lockfile pins hiredis and the cloud "
            "deployment runs it, so a run without it is not the cloud pairing: "
            "check that requirements/cloud.txt still pins hiredis and that this "
            "environment installed it"
        )

    await client.aclose()
    return f"{type(client).__module__} via {parser.__module__}.{parser.__name__}"


def main() -> int:
    try:
        detail = asyncio.run(check())
    except PairingError as exc:
        print(f"::error::Redis pairing check failed: {exc}", file=sys.stderr)
        return 1
    print(f"✓ real Redis + hiredis parser: {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
