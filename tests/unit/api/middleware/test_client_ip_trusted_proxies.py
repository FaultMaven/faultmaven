"""The rate-limit key cannot be chosen by the party being limited (fm#927).

``RateLimitMiddleware._get_client_ip`` used to read ``X-Forwarded-For`` and
``X-Real-IP`` with no trusted-proxy check. The ``global`` limit — the only one
that applies to unauthenticated traffic — is keyed on that value, so a caller
that rotated the header drew a fresh quota on every request and was never
limited.

The property pinned here is not "one header is now checked" but the invariant
that makes the key sound: **a request's key is influenced by a forwarding
header only when the socket peer is a configured trusted proxy.** The header
sweep below exercises that against the whole space of header shapes an attacker
controls, rather than against one representative example, because the defect was
not that a particular header was mishandled — it was that *any* header could
choose the key.

The second half pins the other direction, which is a real defect and not a
hypothetical: keying on the socket peer when a proxy *is* configured collapses
every client onto the ingress address and lets one caller exhaust everyone's
quota.
"""

import ipaddress
import logging
from unittest.mock import Mock

import pytest

from faultmaven.api.middleware.client_ip import (
    UNKNOWN_CLIENT_IP,
    parse_trusted_proxies,
    resolve_client_ip,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]


def _request(peer, headers=None):
    """A request stub exposing only what the resolver reads."""
    request = Mock()
    request.client = Mock(host=peer) if peer is not None else None
    request.headers = headers or {}
    return request


# Every shape an unauthenticated caller can put on the wire. The point is
# coverage of the input space, not of one example: a fix that special-cased a
# single header or a single format would leave the limit evadable through the
# others.
ATTACKER_CONTROLLED_HEADERS = [
    {"X-Forwarded-For": "1.2.3.4"},
    {"X-Forwarded-For": "203.0.113.9, 198.51.100.4"},
    {"X-Forwarded-For": "  10.0.0.1  "},
    {"X-Forwarded-For": "::1"},
    {"X-Forwarded-For": "[2001:db8::1]:443"},
    {"X-Forwarded-For": "1.2.3.4:5678"},
    {"X-Forwarded-For": "not-an-ip"},
    {"X-Forwarded-For": ""},
    {"X-Forwarded-For": ","},
    {"X-Real-IP": "1.2.3.4"},
    {"X-Real-IP": "2001:db8::dead"},
    {"X-Real-IP": "garbage"},
    {"X-Forwarded-For": "1.2.3.4", "X-Real-IP": "5.6.7.8"},
]

CALLER_PEER = "198.51.100.77"


@pytest.mark.parametrize("headers", ATTACKER_CONTROLLED_HEADERS)
def test_no_header_influences_the_key_without_a_trusted_proxy(headers):
    """With no trusted proxies configured, the key is the socket peer. Always.

    This is the whole security property. If any entry in the sweep returns
    something other than the peer, that value is one an attacker chose, and the
    quota it keys is one they can rotate away from.
    """
    resolved = resolve_client_ip(
        _request(CALLER_PEER, headers), parse_trusted_proxies(None)
    )

    assert resolved == CALLER_PEER


@pytest.mark.parametrize("headers", ATTACKER_CONTROLLED_HEADERS)
def test_no_header_influences_the_key_when_the_peer_is_not_a_trusted_proxy(headers):
    """Configuring *a* proxy does not make *every* peer trusted.

    A deployment behind an ingress still has an attack surface if a pod is
    reachable directly. Trust is a property of the address the request arrived
    from, so a caller that is not that address gets no say either way.
    """
    trusted = parse_trusted_proxies(["10.42.0.0/16"])

    resolved = resolve_client_ip(_request(CALLER_PEER, headers), trusted)

    assert resolved == CALLER_PEER


def test_forged_prefix_is_inert_behind_a_real_proxy():
    """The evasion attempt itself, run end to end.

    A caller sends its own ``X-Forwarded-For``; the ingress appends the address
    it actually saw. Walking the chain from the right stops at the first hop the
    caller could have written, so the forged prefix never becomes the key.
    """
    trusted = parse_trusted_proxies(["10.42.0.0/16"])
    request = _request(
        "10.42.0.7",
        {"X-Forwarded-For": "1.2.3.4, 9.9.9.9, 203.0.113.50"},
    )

    assert resolve_client_ip(request, trusted) == "203.0.113.50"


def test_trusted_proxy_forwards_the_real_client():
    """The configured case works, or the fix is just a denial of service."""
    trusted = parse_trusted_proxies(["10.42.0.0/16"])
    request = _request("10.42.0.7", {"X-Forwarded-For": "203.0.113.50"})

    assert resolve_client_ip(request, trusted) == "203.0.113.50"


def test_clients_behind_a_configured_proxy_do_not_share_one_bucket():
    """The inverse defect: distinct clients must resolve to distinct keys.

    Keying on the socket peer behind an ingress is not a safe fallback — it is
    a shared bucket, and one caller can exhaust it for everybody.
    """
    trusted = parse_trusted_proxies(["10.42.0.0/16"])
    peers = {
        resolve_client_ip(
            _request("10.42.0.7", {"X-Forwarded-For": f"203.0.113.{n}"}), trusted
        )
        for n in range(1, 20)
    }

    assert len(peers) == 19


def test_chain_of_only_trusted_hops_falls_back_to_real_ip():
    trusted = parse_trusted_proxies(["10.42.0.0/16", "10.43.0.0/16"])
    request = _request(
        "10.42.0.7",
        {"X-Forwarded-For": "10.43.0.1, 10.42.0.9", "X-Real-IP": "203.0.113.50"},
    )

    assert resolve_client_ip(request, trusted) == "203.0.113.50"


def test_unparseable_hop_never_becomes_the_key():
    """Garbage in a forwarded chain is attacker-controlled text.

    Returning it would put arbitrary strings into a Redis key, which is how a
    limiter acquires an unbounded keyspace on top of an evadable limit.
    """
    trusted = parse_trusted_proxies(["10.42.0.0/16"])
    request = _request(
        "10.42.0.7",
        {"X-Forwarded-For": "203.0.113.50, ' OR 1=1 --, \x00\x00"},
    )

    resolved = resolve_client_ip(request, trusted)

    assert resolved == "203.0.113.50"
    ipaddress.ip_address(resolved)  # parses, so it is an address and not text


def test_missing_peer_is_not_an_ip_shaped_key():
    """An absent transport peer must not collide with a real client's bucket."""
    assert (
        resolve_client_ip(_request(None), parse_trusted_proxies(None))
        == UNKNOWN_CLIENT_IP
    )


class TestTrustedProxyParsing:
    def test_addresses_and_cidrs_both_parse(self):
        trusted = parse_trusted_proxies(
            ["10.42.0.7", "192.168.0.0/16", "2001:db8::/32"]
        )

        assert len(trusted) == 3
        assert (
            resolve_client_ip(
                _request("192.168.4.4", {"X-Forwarded-For": "203.0.113.50"}), trusted
            )
            == "203.0.113.50"
        )

    def test_comma_separated_string_is_accepted(self):
        assert len(parse_trusted_proxies("10.0.0.0/8, 192.168.0.0/16")) == 2

    def test_unparseable_entry_is_dropped_loudly_and_the_rest_survive(self, caplog):
        """A typo must narrow trust, never widen it, and never boot-fail.

        Raising here would surface inside ``setup_protection_middleware``'s
        ``except``, which logs and continues — so one typo would silently
        disable rate limiting, deduplication and timeouts deployment-wide.
        Dropping the entry is the strictly safer failure.
        """
        with caplog.at_level(logging.ERROR):
            trusted = parse_trusted_proxies(["10.42.0.0/16", "not-a-cidr", "999.1.1.1"])

        assert len(trusted) == 1
        assert "not-a-cidr" in caplog.text
        assert "999.1.1.1" in caplog.text

    def test_a_dropped_entry_does_not_leave_its_headers_trusted(self):
        """The consequence of the drop, not just the drop."""
        trusted = parse_trusted_proxies(["not-a-cidr"])

        assert (
            resolve_client_ip(
                _request(CALLER_PEER, {"X-Forwarded-For": "1.2.3.4"}), trusted
            )
            == CALLER_PEER
        )

    @pytest.mark.parametrize("empty", [None, [], "", "  ", ",,", [""], ["  "]])
    def test_every_spelling_of_unset_trusts_nothing(self, empty):
        assert parse_trusted_proxies(empty) == ()
