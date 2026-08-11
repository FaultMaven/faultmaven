"""Client IP resolution for request-path protections.

Anything that keys a limit, a quota or a per-client counter on "the client"
must agree on what the client *is*. This module is that single answer.

The rule, in one sentence: **forwarding headers are honoured only when the
socket peer is a configured trusted proxy, and never otherwise.**

Why it has to be a rule rather than a convenience
-------------------------------------------------
``X-Forwarded-For`` and ``X-Real-IP`` are request headers. Any client can send
any value in them. A limiter that reads them unconditionally is keyed on a
value the limited party chooses, so a caller that rotates the header gets a
fresh bucket on every request and is never limited. That is not a degraded
limit, it is no limit — and the ``global`` limit is the only one that applies
to unauthenticated traffic.

The inverse mistake is just as real: keying on the socket peer alone behind an
ingress collapses every client in the world onto the ingress address, so one
caller exhausts everybody's quota. Neither "always trust" nor "never trust" is
correct; trust has to be a property of *where the request arrived from*.

The algorithm
-------------
1. If the socket peer is not a configured trusted proxy, return the socket
   peer and ignore every forwarding header. This is the security property:
   with no trusted proxies configured, headers cannot influence the key at all.
2. If the socket peer *is* trusted, walk the ``X-Forwarded-For`` chain from the
   right and return the first address that is not itself a trusted proxy. The
   right-hand end is the portion appended by infrastructure we trust; the
   left-hand end is whatever the caller sent. Stopping at the first untrusted
   hop is what makes a forged prefix inert — a caller who sends
   ``X-Forwarded-For: 1.2.3.4`` still gets keyed on the address the ingress
   appended, not on the one they chose.
3. If there is no chain, or every hop in it is trusted, use the socket peer.

The chain is assembled from **every** ``X-Forwarded-For`` field line, not just
the first. RFC 7230 §3.2.2 makes repeated lines equivalent to one comma list,
and a proxy that appends its own line rather than extending the caller's is
both legal and common — reading one line would walk only the caller's chain.

``X-Real-IP`` is never used to pick the address, only as a hint that a proxy
may be present. It carries one value and no chain, so nothing distinguishes
what a trusted proxy wrote from what a caller sent — believing it would re-open
the rotation this module closes. The ``X-Forwarded-For`` walk is sound
*because* the proxy appends where we can see it, which is what makes the first
untrusted hop identifiable.

Malformed entries are skipped rather than returned: an unparseable value is
attacker-controlled text, and putting it in a Redis key is how a limiter grows
an unbounded keyspace.
"""

from __future__ import annotations

import ipaddress
import logging
import time
from typing import Iterable, Optional, Tuple

from fastapi import Request

logger = logging.getLogger(__name__)

TrustedProxies = Tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]

# Returned when a request has no socket peer at all (ASGI transports that do
# not populate ``scope["client"]`` — notably in-process test transports).
# Deliberately not an IP-shaped string, so it cannot collide with a real key.
UNKNOWN_CLIENT_IP = "unknown"

# A deployment that sits behind a proxy but has not configured one is a real,
# silent misconfiguration: every client collapses onto the proxy's address and
# shares a single bucket. It is worth saying so, but it is triggered by request
# content, so it must not be able to flood the log.
_UNCONFIGURED_PROXY_WARNING_INTERVAL_SECONDS = 300.0
_last_unconfigured_proxy_warning = 0.0


def parse_trusted_proxies(values: Optional[Iterable[str] | str]) -> TrustedProxies:
    """Parse operator-supplied trusted proxy entries into networks.

    Accepts bare addresses (``10.0.1.7``) and CIDRs (``10.0.0.0/8``), either as
    an iterable or as one comma-separated string.

    Unparseable entries are logged at ERROR and **skipped**, not raised. Two
    reasons, and they point the same way:

    * Skipping fails closed on the axis that matters. A dropped entry means one
      fewer address is trusted, so the worst case is a limit keyed on the proxy
      rather than a limit a caller can evade.
    * Raising here would not produce a clean boot refusal, and could not be
      reported as one. Every caller is a middleware ``__init__`` —
      ``RateLimitMiddleware``, ``PerformanceTrackingMiddleware``,
      ``LoggingMiddleware``, the OAuth/SSO limiter — and none of them runs
      inside ``setup_protection_middleware``: ``add_middleware`` only records a
      ``Middleware(cls, ...)`` entry, and Starlette instantiates the classes
      later, in ``build_middleware_stack()`` at app start. The ``ValueError``
      would surface there, outside every handler the composition root wraps
      around protection setup — an uncatchable crash during ASGI stack
      construction rather than a refusal anything can log or degrade. A typo in
      one proxy entry does not warrant that, when dropping the entry already
      leaves every limit intact and merely coarser. The skip is the only
      proportionate refusal, and it narrows trust rather than widening it.

    Parsing is **strict** about host bits, and that is the whole point rather
    than pedantry. ``ipaddress.ip_network`` defaults to ``strict=False``, which
    silently rounds a mistyped entry *outward*: ``10.244.226.134/16`` becomes
    ``10.244.0.0/16`` and trusts 65,536 addresses, ``192.168.1.50/8`` becomes
    ``192.0.0.0/8`` and trusts 16.7 million. That is precisely the shape of
    mistake an operator makes — pasting a live pod address and appending the
    cluster's mask — and lenient parsing would turn it into a silently widened
    trust boundary, breaking the one property this function is supposed to
    hold: **a malformed entry must narrow trust, never widen it.** A bare
    address is still accepted and means that host alone (``/32``).
    """
    if values is None:
        return ()
    if isinstance(values, str):
        values = values.split(",")

    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw in values:
        entry = raw.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=True))
        except ValueError as exc:
            logger.error(
                "Ignoring trusted-proxy entry %r (%s). It is not an address or a "
                "network address with a matching prefix — write the network "
                "address itself (10.244.0.0/16), not a host inside it. Until it "
                "is corrected, requests arriving via that proxy are keyed on the "
                "proxy's own address rather than the originating client.",
                entry,
                exc,
            )
    return tuple(networks)


def _parse_address(
    value: str,
) -> Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Parse one hop into an address, or ``None`` if it is not one.

    Tolerates the shapes proxies actually emit: a bracketed IPv6 literal, and
    an ``address:port`` pair. A bare IPv6 address contains colons too, so the
    port strip only applies when there is exactly one colon and no brackets.

    IPv4-mapped IPv6 (``::ffff:10.0.0.1``) is folded to its IPv4 form. A server
    bound to a dual-stack socket reports IPv4 peers in that notation, and
    ``IPv6Address('::ffff:10.0.0.1') in IPv4Network('10.0.0.0/8')`` is *False* —
    so without the fold a correctly configured trusted proxy silently stops
    matching and the deployment degrades to one shared bucket. It also keeps a
    forwarded client from occupying two keys under two spellings of the same
    address. (That second benefit is confined to the forwarded path: an
    untrusted peer is returned verbatim, because its spelling is chosen by our
    own socket rather than by the caller.)
    """
    candidate = value.strip()
    if not candidate:
        return None

    if candidate.startswith("["):
        # [::1] or [::1]:8080
        closing = candidate.find("]")
        if closing == -1:
            return None
        candidate = candidate[1:closing]
    elif candidate.count(":") == 1:
        candidate = candidate.split(":", 1)[0]

    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None

    mapped = getattr(address, "ipv4_mapped", None)
    return mapped if mapped is not None else address


def _is_trusted(
    address: Optional[ipaddress.IPv4Address | ipaddress.IPv6Address],
    trusted_proxies: TrustedProxies,
) -> bool:
    if address is None:
        return False
    return any(address in network for network in trusted_proxies)


def _warn_unconfigured_proxy(peer: str) -> None:
    global _last_unconfigured_proxy_warning

    now = time.monotonic()
    if (
        now - _last_unconfigured_proxy_warning
        < _UNCONFIGURED_PROXY_WARNING_INTERVAL_SECONDS
    ):
        return
    _last_unconfigured_proxy_warning = now

    logger.warning(
        "Request from %s carried forwarding headers but that address is not in "
        "PROTECTION_TRUSTED_PROXIES, so the headers were ignored and the limit "
        "was keyed on the socket peer. If FaultMaven is behind a proxy or "
        "ingress, set PROTECTION_TRUSTED_PROXIES to its address range — "
        "otherwise every client shares one bucket. If it is not behind a "
        "proxy, this is a client sending headers it has no business sending, "
        "and ignoring them is correct.",
        peer,
    )


def resolve_client_ip(request: Request, trusted_proxies: TrustedProxies) -> str:
    """Return the address to key per-client protections on.

    Args:
        request: the incoming request.
        trusted_proxies: networks whose forwarding headers may be believed, as
            returned by :func:`parse_trusted_proxies`. Empty means no header is
            ever believed.

    Returns:
        An IP address string, or :data:`UNKNOWN_CLIENT_IP` when the transport
        exposes no peer.
    """
    peer = request.client.host if request.client else None

    if peer is None:
        return UNKNOWN_CLIENT_IP

    peer_address = _parse_address(peer)
    if not _is_trusted(peer_address, trusted_proxies):
        # The socket peer is the client. Headers are noise at best and an
        # evasion attempt at worst; either way they do not enter the key.
        if request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP"):
            _warn_unconfigured_proxy(peer)
        return peer

    # EVERY ``X-Forwarded-For`` field line, joined in arrival order — not
    # ``headers.get()``, which returns only the FIRST line.
    #
    # RFC 7230 §3.2.2 makes repeated field lines semantically identical to one
    # comma-joined list, so an intermediary may legitimately append its value as
    # a separate line rather than extending the caller's (HAProxy's
    # ``option forwardfor`` is the standard example — its ``if-none`` argument
    # exists precisely because it otherwise adds a line). Reading only the first
    # line then walks the caller's chain and ignores the proxy's, which hands
    # the key straight back to the caller: measured before this fix, one client
    # rotating its own field line produced 199 distinct keys behind a configured
    # trusted proxy, i.e. #927 in full.
    #
    # The right-to-left walk is sound only if it sees what the proxy appended,
    # so it has to consume the whole set.
    hops = [
        hop
        for line in request.headers.getlist("x-forwarded-for")
        for hop in (h.strip() for h in line.split(","))
        if hop
    ]

    # Right to left: the trailing hops were appended by infrastructure we
    # trust, the leading ones are whatever the caller supplied. The first hop
    # that is not itself a trusted proxy is the earliest address we have any
    # reason to believe.
    for hop in reversed(hops):
        address = _parse_address(hop)
        if address is None:
            # Attacker-controlled text. Skipping keeps it out of the key.
            continue
        if not _is_trusted(address, trusted_proxies):
            return str(address)

    # No chain, or a chain consisting entirely of trusted proxies: the peer is
    # the most specific address we can actually justify.
    #
    # ``X-Real-IP`` is deliberately NOT consulted here. It looks like a safe
    # fallback and is not one: it carries a single value with no chain, so
    # there is no way to tell the part a trusted proxy wrote from the part a
    # caller sent. Believing it re-opens exactly the key rotation this module
    # exists to close — a caller behind a proxy that does not overwrite the
    # header gets a fresh bucket per request by varying it. The ``X-Forwarded-For``
    # walk is safe *because* the proxy appends, which is what lets us find the
    # first hop the caller could not have written; `X-Real-IP` has no such
    # structure, so it is unverifiable by construction.
    #
    # Cost of leaving it out: a deployment whose proxy sets only `X-Real-IP` and
    # no `X-Forwarded-For` keys on the proxy address. That is the safe
    # degradation, and the fix is to have the proxy set `X-Forwarded-For` —
    # which every proxy in front of this service does, NGINX ingress included.
    return peer
