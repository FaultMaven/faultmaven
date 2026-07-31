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
from starlette.datastructures import Headers

from faultmaven.api.middleware.client_ip import (
    UNKNOWN_CLIENT_IP,
    parse_trusted_proxies,
    resolve_client_ip,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]


def _request(peer, headers=None):
    """A request stub whose headers are a REAL ``starlette.Headers``.

    Not a dict. A dict cannot represent two ``X-Forwarded-For`` field lines,
    and that is exactly the shape that broke this resolver: ``Headers.get()``
    returns only the first line, so a caller whose line arrived first chose the
    key. A dict-based fixture cannot express the input, so it cannot fail on
    it — the stand-in would have kept the gate permanently green.
    """
    request = Mock()
    request.client = Mock(host=peer) if peer is not None else None
    request.headers = _headers(headers)
    return request


def _headers(headers=None):
    """Build real ASGI headers. Accepts a mapping, or a list of (name, value)
    pairs so a test can repeat a field name."""
    if headers is None:
        items = []
    elif isinstance(headers, dict):
        items = list(headers.items())
    else:
        items = list(headers)
    return Headers(
        raw=[(name.lower().encode(), value.encode()) for name, value in items]
    )


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


class TestDuplicateForwardedForFieldLines:
    """A proxy may append its value as a SECOND field line, not extend the first.

    RFC 7230 §3.2.2 makes repeated field lines equivalent to one comma-joined
    list, so this is legal and common (HAProxy's ``option forwardfor`` is the
    canonical emitter). ``Headers.get()`` returns only the *first* line — so
    reading it walks the caller's chain and never sees the proxy's, handing the
    key back to the party being limited.

    This is #927 in full, not a variant of it, and the earlier dict-based
    fixtures could not express the input that triggers it.
    """

    TRUSTED = ["10.42.0.0/16"]

    def test_the_proxys_own_line_wins_over_the_callers(self):
        trusted = parse_trusted_proxies(self.TRUSTED)
        request = _request(
            "10.42.0.7",
            [("X-Forwarded-For", "1.2.3.4"), ("X-Forwarded-For", "203.0.113.9")],
        )

        assert resolve_client_ip(request, trusted) == "203.0.113.9"

    def test_rotating_your_own_field_line_cannot_rotate_the_bucket(self):
        """The evasion, swept over the space rather than shown once."""
        trusted = parse_trusted_proxies(self.TRUSTED)
        keys = {
            resolve_client_ip(
                _request(
                    "10.42.0.7",
                    [
                        ("X-Forwarded-For", f"9.9.9.{n}"),
                        ("X-Forwarded-For", "203.0.113.9"),
                    ],
                ),
                trusted,
            )
            for n in range(1, 200)
        }

        assert keys == {"203.0.113.9"}

    def test_split_and_merged_spellings_agree(self):
        """Same chain, two encodings, one key — or the encoding is a quota knob."""
        trusted = parse_trusted_proxies(self.TRUSTED)

        merged = resolve_client_ip(
            _request("10.42.0.7", {"X-Forwarded-For": "1.2.3.4, 203.0.113.9"}), trusted
        )
        split = resolve_client_ip(
            _request(
                "10.42.0.7",
                [("X-Forwarded-For", "1.2.3.4"), ("X-Forwarded-For", "203.0.113.9")],
            ),
            trusted,
        )

        assert merged == split == "203.0.113.9"

    def test_many_lines_each_holding_several_hops(self):
        trusted = parse_trusted_proxies(["10.42.0.0/16", "10.43.0.0/16"])
        request = _request(
            "10.42.0.7",
            [
                ("X-Forwarded-For", "1.2.3.4, 5.6.7.8"),
                ("X-Forwarded-For", "203.0.113.9, 10.43.0.1"),
                ("X-Forwarded-For", "10.42.0.9"),
            ],
        )

        # Rightmost non-trusted hop across the whole assembled chain.
        assert resolve_client_ip(request, trusted) == "203.0.113.9"

    def test_duplicate_lines_still_ignored_when_the_peer_is_untrusted(self):
        assert (
            resolve_client_ip(
                _request(
                    CALLER_PEER,
                    [("X-Forwarded-For", "1.2.3.4"), ("X-Forwarded-For", "5.6.7.8")],
                ),
                parse_trusted_proxies(None),
            )
            == CALLER_PEER
        )


@pytest.mark.parametrize(
    "name", ["X-Forwarded-For", "x-forwarded-for", "X-FORWARDED-FOR"]
)
def test_header_name_casing_does_not_change_the_answer(name):
    """Pins that the lookup is case-insensitive against REAL headers.

    ASGI servers deliver names lowercased and Starlette folds case, so this
    holds — but it held only by accident under dict fixtures, which are
    case-sensitive.
    """
    trusted = parse_trusted_proxies(["10.42.0.0/16"])

    assert resolve_client_ip(
        _request("10.42.0.7", {name: "203.0.113.50"}), trusted
    ) == ("203.0.113.50")


def test_chain_of_only_trusted_hops_falls_back_to_the_peer():
    """With nothing in the chain we can attribute, the peer is the honest key."""
    trusted = parse_trusted_proxies(["10.42.0.0/16", "10.43.0.0/16"])
    request = _request("10.42.0.7", {"X-Forwarded-For": "10.43.0.1, 10.42.0.9"})

    assert resolve_client_ip(request, trusted) == "10.42.0.7"


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Real-IP": "1.2.3.4"},
        {"X-Forwarded-For": "", "X-Real-IP": "1.2.3.4"},
        {"X-Forwarded-For": "10.42.0.9", "X-Real-IP": "1.2.3.4"},
        {"X-Forwarded-For": "not-an-ip", "X-Real-IP": "1.2.3.4"},
    ],
)
def test_x_real_ip_never_selects_the_key_even_from_a_trusted_peer(headers):
    """``X-Real-IP`` is a hint, never the answer — including behind a real proxy.

    It carries one value and no chain, so nothing distinguishes what the proxy
    wrote from what the caller sent. Believing it would restore the #927
    rotation for any deployment whose proxy passes the header through instead
    of overwriting it: vary the header, get a fresh bucket.

    Each case here has an all-trusted or unusable ``X-Forwarded-For``, which is
    exactly when a fallback would have kicked in.
    """
    trusted = parse_trusted_proxies(["10.42.0.0/16"])

    assert resolve_client_ip(_request("10.42.0.7", headers), trusted) == "10.42.0.7"


def test_rotating_x_real_ip_behind_a_trusted_proxy_cannot_rotate_the_bucket():
    """The rotation attempt itself, over the whole header space."""
    trusted = parse_trusted_proxies(["10.42.0.0/16"])
    keys = {
        resolve_client_ip(_request("10.42.0.7", {"X-Real-IP": f"1.2.3.{n}"}), trusted)
        for n in range(1, 200)
    }

    assert keys == {"10.42.0.7"}


class TestIPv4MappedIPv6:
    """``::ffff:10.0.0.1`` is 10.0.0.1, and both must key and match identically.

    A server on a dual-stack socket reports IPv4 peers in mapped notation, and
    ``IPv6Address('::ffff:10.42.0.7') in IPv4Network('10.42.0.0/16')`` is False.
    Unfolded, a correctly configured trusted proxy silently stops matching and
    the deployment degrades to the shared bucket it was configured to avoid.
    """

    def test_a_mapped_peer_still_matches_an_ipv4_trusted_range(self):
        trusted = parse_trusted_proxies(["10.42.0.0/16"])
        request = _request("::ffff:10.42.0.7", {"X-Forwarded-For": "203.0.113.50"})

        assert resolve_client_ip(request, trusted) == "203.0.113.50"

    def test_a_mapped_hop_resolves_to_its_ipv4_form(self):
        trusted = parse_trusted_proxies(["10.42.0.0/16"])
        request = _request("10.42.0.7", {"X-Forwarded-For": "::ffff:203.0.113.50"})

        assert resolve_client_ip(request, trusted) == "203.0.113.50"

    def test_both_spellings_of_one_client_share_one_key(self):
        """Otherwise a client occupies two buckets by changing notation."""
        trusted = parse_trusted_proxies(["10.42.0.0/16"])
        keys = {
            resolve_client_ip(
                _request("10.42.0.7", {"X-Forwarded-For": spelling}), trusted
            )
            for spelling in (
                "203.0.113.50",
                "::ffff:203.0.113.50",
                "[::ffff:203.0.113.50]:9",
            )
        }

        assert keys == {"203.0.113.50"}

    def test_a_mapped_address_is_still_excluded_when_untrusted(self):
        trusted = parse_trusted_proxies(["10.42.0.0/16"])

        assert (
            resolve_client_ip(
                _request("::ffff:198.51.100.77", {"X-Forwarded-For": "1.2.3.4"}),
                trusted,
            )
            == "::ffff:198.51.100.77"
        )


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


class TestUnconfiguredProxyWarning:
    """The docs promise this condition "is reported twice over" — so pin it.

    A deployment behind an unconfigured proxy is safe but coarse. The warning is
    the only request-time signal that it is happening, and it is throttled, so
    both the firing and the throttle need a guard or the promise is untested.
    """

    @staticmethod
    def _reset():
        import faultmaven.api.middleware.client_ip as mod

        mod._last_unconfigured_proxy_warning = 0.0

    def test_forwarding_headers_from_an_unlisted_peer_warn(self, caplog):
        self._reset()
        with caplog.at_level(logging.WARNING):
            resolve_client_ip(
                _request(CALLER_PEER, {"X-Forwarded-For": "1.2.3.4"}),
                parse_trusted_proxies(None),
            )

        assert "PROTECTION_TRUSTED_PROXIES" in caplog.text
        assert CALLER_PEER in caplog.text

    def test_a_request_with_no_forwarding_headers_is_silent(self, caplog):
        """Ordinary standalone traffic must not log on every request."""
        self._reset()
        with caplog.at_level(logging.WARNING):
            resolve_client_ip(_request(CALLER_PEER), parse_trusted_proxies(None))

        assert "PROTECTION_TRUSTED_PROXIES" not in caplog.text

    def test_the_warning_is_throttled(self, caplog):
        """It is triggered by request content, so it must not flood the log.

        Counts log *records*, not substring hits: the message names the setting
        twice, so a text count reads 2 for a single emission.
        """
        self._reset()
        with caplog.at_level(logging.WARNING):
            for _ in range(50):
                resolve_client_ip(
                    _request(CALLER_PEER, {"X-Forwarded-For": "1.2.3.4"}),
                    parse_trusted_proxies(None),
                )

        emitted = [
            r for r in caplog.records if "PROTECTION_TRUSTED_PROXIES" in r.getMessage()
        ]
        assert len(emitted) == 1

    def test_a_configured_deployment_never_warns(self, caplog):
        self._reset()
        with caplog.at_level(logging.WARNING):
            resolve_client_ip(
                _request("10.42.0.7", {"X-Forwarded-For": "203.0.113.50"}),
                parse_trusted_proxies(["10.42.0.0/16"]),
            )

        assert "PROTECTION_TRUSTED_PROXIES" not in caplog.text


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

    # The realistic operator mistake: copy a live pod address out of `kubectl
    # get pods -o wide`, append the cluster mask. ``ipaddress.ip_network``
    # defaults to strict=False and rounds these OUTWARD, so lenient parsing
    # turns a typo into a silently widened trust boundary — the exact opposite
    # of the "a typo must narrow trust" property asserted above.
    @pytest.mark.parametrize(
        "entry, would_have_trusted",
        [
            ("10.244.226.134/16", 65536),
            ("192.168.1.50/8", 16777216),
            ("172.16.30.9/12", 1048576),
            ("2001:db8::dead:beef/32", 2**96),
        ],
    )
    def test_host_bits_never_silently_widen_the_trust_boundary(
        self, entry, would_have_trusted, caplog
    ):
        """A mistyped prefix is dropped, not rounded up to a bigger network.

        Under strict=False each of these would parse — and every address in the
        resulting network would be believed when it set ``X-Forwarded-For``.
        """
        with caplog.at_level(logging.ERROR):
            trusted = parse_trusted_proxies([entry])

        assert trusted == ()
        assert entry in caplog.text
        # The consequence, asserted against a real request rather than by
        # re-summing the empty tuple (which was vacuous: 0 < anything).
        assert (
            resolve_client_ip(
                _request(
                    str(ipaddress.ip_network(entry, strict=False)[1]),
                    {"X-Forwarded-For": "1.2.3.4"},
                ),
                trusted,
            )
            != "1.2.3.4"
        ), f"an address inside the would-be {would_have_trusted}-address network is trusted"

    def test_a_bare_address_still_means_that_host_alone(self):
        """Strictness must not cost the ordinary single-proxy spelling."""
        trusted = parse_trusted_proxies(["10.42.0.7", "2001:db8::1"])

        assert [net.num_addresses for net in trusted] == [1, 1]
        assert (
            resolve_client_ip(
                _request("10.42.0.7", {"X-Forwarded-For": "203.0.113.50"}), trusted
            )
            == "203.0.113.50"
        )

    def test_a_correctly_written_network_is_still_accepted(self):
        trusted = parse_trusted_proxies(["10.244.0.0/16"])

        assert len(trusted) == 1
        assert (
            resolve_client_ip(
                _request("10.244.226.134", {"X-Forwarded-For": "203.0.113.50"}), trusted
            )
            == "203.0.113.50"
        )
