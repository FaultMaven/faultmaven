"""OAuth limits are per client, not per ingress (fm#948).

``OAuthRateLimiter.check_rate_limit`` keyed on ``request.client.host``. Behind
an ingress that address is the ingress pod's, identical for every caller, so
``/token``'s 5-per-minute budget was shared by the entire deployment: the first
user to authenticate refused everyone else for the rest of the minute.

Every test here drives the enforcement surface — ``check_rate_limit`` raising
``HTTPException`` 429 — rather than a key helper, because a correct key that no
limit consults fixes nothing.

The fix adopts the fm#927 resolver, so the tests below pin *both* directions at
once: the shared bucket must open up behind a configured proxy, and the header
rotation fm#927 closed must stay closed everywhere else.
"""

import time
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers, State

from faultmaven.api.middleware.client_ip import resolve_client_ip
from faultmaven.modules.auth.api import rate_limiting as oauth_rate_limiting
from faultmaven.modules.auth.api.rate_limiting import (
    OAuthRateLimiter,
    require_oauth_rate_limit_token,
    reset_rate_limiter,
    trusted_proxy_networks,
)
from faultmaven.modules.auth.api.sso import sso_callback

pytestmark = [pytest.mark.unit, pytest.mark.security]

INGRESS = "10.42.0.7"
INGRESS_RANGE = "10.42.0.0/16"
CLIENT_A = "203.0.113.1"
CLIENT_B = "203.0.113.2"
TOKEN_LIMIT = 5  # /token, requests per minute


class _StubRequest:
    """A request stub whose headers are a REAL ``starlette.Headers``.

    Not a dict: a dict cannot express two ``X-Forwarded-For`` field lines, and
    that is precisely the shape that defeated the resolver once already. A
    fixture that cannot represent the input can never fail on it.
    """

    def __init__(self, peer, headers=None, rate_limit_results=None):
        self.client = _Peer(peer) if peer is not None else None
        items = (
            list(headers.items()) if isinstance(headers, dict) else list(headers or [])
        )
        self.headers = Headers(
            raw=[(name.lower().encode(), value.encode()) for name, value in items]
        )
        # ``sso_callback`` reads the browser-binding state cookie off the same
        # request object the limiter and the audit resolve their address from.
        self.cookies: dict[str, str] = {}
        # A real ``State``, not a namespace: the limiter offers its result to
        # ``rate_limit_results`` through the same attribute lookup Starlette
        # provides, and a stand-in with different lookup semantics would let a
        # regression there pass. Seeded only when a test asks for it — an
        # absent key is the reduced-stack case, where the middleware that owns
        # the list is not installed.
        self.state = State()
        if rate_limit_results is not None:
            self.state.rate_limit_results = rate_limit_results


class _Peer:
    def __init__(self, host):
        self.host = host


def _forwarded(client_ip, peer=INGRESS):
    """A request that arrived at ``peer`` carrying ``client_ip`` forwarded."""
    return _StubRequest(peer, {"X-Forwarded-For": client_ip})


async def _spend(limiter, request_factory, count, endpoint="/token"):
    """Consume ``count`` of the budget, asserting none of them is refused."""
    for n in range(count):
        await limiter.check_rate_limit(request_factory(), endpoint)


async def _refused(limiter, request, endpoint="/token") -> bool:
    try:
        await limiter.check_rate_limit(request, endpoint)
    except HTTPException as exc:
        assert exc.status_code == 429
        return True
    return False


class TestClientsBehindAnIngressDoNotShareOneBucket:
    """The fm#948 defect itself, at the enforcement boundary."""

    async def test_one_clients_exhausted_budget_does_not_refuse_another(self):
        limiter = OAuthRateLimiter(trusted_proxies=[INGRESS_RANGE])

        await _spend(limiter, lambda: _forwarded(CLIENT_A), TOKEN_LIMIT)

        # Client A has spent its whole minute...
        assert await _refused(limiter, _forwarded(CLIENT_A))
        # ...and client B, arriving through the very same ingress pod, has not.
        assert not await _refused(limiter, _forwarded(CLIENT_B))

    async def test_every_endpoint_is_keyed_the_same_way(self):
        """The defect was in the shared method, so it applied to all six."""
        for endpoint, limit in (
            ("/authorize", 10),
            ("/token", 5),
            ("/revoke", 20),
            ("/sso/login", 10),
            ("/sso/callback", 10),
            ("/sso/exchange", 5),
        ):
            limiter = OAuthRateLimiter(trusted_proxies=[INGRESS_RANGE])
            await _spend(limiter, lambda: _forwarded(CLIENT_A), limit, endpoint)

            assert await _refused(limiter, _forwarded(CLIENT_A), endpoint), endpoint
            assert not await _refused(limiter, _forwarded(CLIENT_B), endpoint), endpoint


class TestTheEvasionDirectionStaysClosed:
    """Adopting the resolver must not re-open fm#927.

    A caller that is not behind a configured proxy chooses the content of its
    own ``X-Forwarded-For``. If that value reached the key, the limit would be
    keyed on a value the limited party picks — which is no limit at all.
    """

    async def test_rotating_a_forged_header_cannot_draw_a_fresh_budget(self):
        limiter = OAuthRateLimiter()  # no trusted proxies
        attacker = "198.51.100.77"

        for n in range(TOKEN_LIMIT):
            await limiter.check_rate_limit(
                _StubRequest(attacker, {"X-Forwarded-For": f"1.2.3.{n}"}), "/token"
            )

        assert await _refused(
            limiter, _StubRequest(attacker, {"X-Forwarded-For": "1.2.3.99"})
        )

    async def test_a_second_field_line_cannot_draw_one_either(self):
        """``Headers.get()`` reads only the first line; the resolver reads all."""
        limiter = OAuthRateLimiter()
        attacker = "198.51.100.78"

        for n in range(TOKEN_LIMIT):
            await limiter.check_rate_limit(
                _StubRequest(
                    attacker,
                    [
                        ("X-Forwarded-For", f"9.9.9.{n}"),
                        ("X-Forwarded-For", f"8.8.8.{n}"),
                    ],
                ),
                "/token",
            )

        assert await _refused(
            limiter,
            _StubRequest(
                attacker,
                [("X-Forwarded-For", "9.9.9.99"), ("X-Forwarded-For", "8.8.8.99")],
            ),
        )


class TestUnconfiguredIsExactlyTodaysBehaviour:
    """The fix is inert until the infrastructure setting lands.

    With no trusted proxies the resolver returns the socket peer and
    ``UNKNOWN_CLIENT_IP`` when there is none — byte-identical to the code this
    replaced. A deployment that upgrades without setting anything must see no
    change at all.
    """

    async def test_distinct_peers_get_distinct_budgets(self):
        limiter = OAuthRateLimiter()

        await _spend(limiter, lambda: _StubRequest("198.51.100.1"), TOKEN_LIMIT)

        assert await _refused(limiter, _StubRequest("198.51.100.1"))
        assert not await _refused(limiter, _StubRequest("198.51.100.2"))

    async def test_a_missing_peer_is_still_limited_and_does_not_crash(self):
        """No transport peer must not mean no limit — nor an exception."""
        limiter = OAuthRateLimiter()

        await _spend(limiter, lambda: _StubRequest(None), TOKEN_LIMIT)

        assert await _refused(limiter, _StubRequest(None))

    async def test_a_missing_peer_does_not_share_a_real_clients_budget(self):
        limiter = OAuthRateLimiter()

        await _spend(limiter, lambda: _StubRequest(None), TOKEN_LIMIT)

        assert not await _refused(limiter, _StubRequest("198.51.100.3"))


class TestAMistypedTrustEntryNarrowsTrust:
    """``10.42.0.7/16`` is the classic operator paste: a live pod address plus
    the cluster mask. ``ipaddress.ip_network`` defaults to ``strict=False`` and
    would round it *outward* to ``10.42.0.0/16``, trusting 65,536 addresses.

    ``parse_trusted_proxies`` is strict, so the entry is dropped and the ingress
    is simply not trusted — the limit degrades to the shared bucket rather than
    widening trust. Asserted here, at the surface that renders the consequence,
    and not only in the parser's own tests: a malformed entry must narrow trust,
    never widen it.
    """

    async def test_the_ingress_is_not_trusted_so_headers_are_ignored(self):
        limiter = OAuthRateLimiter(trusted_proxies=["10.42.0.7/16"])

        # Alternating forwarded addresses; if the mistyped entry had been
        # rounded up, these would be two independent budgets.
        for n in range(TOKEN_LIMIT):
            await limiter.check_rate_limit(
                _forwarded(CLIENT_A if n % 2 else CLIENT_B), "/token"
            )

        assert await _refused(limiter, _forwarded("203.0.113.3"))

    async def test_a_correctly_written_range_still_works(self):
        """Strictness must not cost the correct configuration."""
        limiter = OAuthRateLimiter(trusted_proxies=[INGRESS_RANGE])

        await _spend(limiter, lambda: _forwarded(CLIENT_A), TOKEN_LIMIT)

        assert not await _refused(limiter, _forwarded(CLIENT_B))


class TestTheTrustListComesFromTheEnvironmentAtConstruction:
    """``_oauth_rate_limiter = OAuthRateLimiter()`` runs at *import* time, and
    that is late enough: ``main`` calls ``load_dotenv()`` at its own import,
    before any router — and therefore this module — is imported, so `.env` has
    already reached ``os.environ``. Post-boot the value does not change, so
    resolving once at construction is equivalent to resolving on first use and
    carries no sentinel to get wrong.
    """

    async def test_the_configured_range_reaches_the_limiter(self, monkeypatch):
        monkeypatch.setenv("PROTECTION_TRUSTED_PROXIES", INGRESS_RANGE)
        limiter = OAuthRateLimiter()

        # Two clients arriving through the same trusted ingress hold separate
        # budgets — which they only do if the environment was actually read.
        await _spend(limiter, lambda: _forwarded(CLIENT_A), TOKEN_LIMIT)

        assert await _refused(limiter, _forwarded(CLIENT_A))
        assert not await _refused(limiter, _forwarded(CLIENT_B))

    async def test_an_unset_key_trusts_nothing(self, monkeypatch):
        """The safe default: no entry means no header influences the key."""
        monkeypatch.delenv("PROTECTION_TRUSTED_PROXIES", raising=False)
        limiter = OAuthRateLimiter()

        # Untrusted peer, so the forwarded addresses are ignored and all five
        # requests land on the peer's single budget.
        for n in range(TOKEN_LIMIT):
            await limiter.check_rate_limit(_forwarded(f"203.0.113.{n}"), "/token")

        assert await _refused(limiter, _forwarded("203.0.113.99"))

    async def test_it_is_resolved_once_and_then_pinned(self, monkeypatch):
        """Re-reading per request would make the key depend on a mutable global."""
        monkeypatch.setenv("PROTECTION_TRUSTED_PROXIES", INGRESS_RANGE)
        limiter = OAuthRateLimiter()

        await _spend(limiter, lambda: _forwarded(CLIENT_A), TOKEN_LIMIT)
        monkeypatch.delenv("PROTECTION_TRUSTED_PROXIES", raising=False)

        # Still keyed on the forwarded address, so client A is still refused. A
        # re-read would key on the ingress peer, whose budget is untouched.
        assert await _refused(limiter, _forwarded(CLIENT_A))

    async def test_an_explicit_list_ignores_the_environment_entirely(self, monkeypatch):
        monkeypatch.setenv("PROTECTION_TRUSTED_PROXIES", INGRESS_RANGE)
        limiter = OAuthRateLimiter(trusted_proxies=[])

        for n in range(TOKEN_LIMIT):
            await limiter.check_rate_limit(_forwarded(f"203.0.113.{n}"), "/token")

        assert await _refused(limiter, _forwarded("203.0.113.99"))

    async def test_reset_makes_the_singleton_re_read(self, monkeypatch):
        """Otherwise the first test to touch the singleton pins it for the run."""
        monkeypatch.setenv("PROTECTION_TRUSTED_PROXIES", INGRESS_RANGE)
        reset_rate_limiter()
        singleton = oauth_rate_limiting._oauth_rate_limiter

        await _spend(singleton, lambda: _forwarded(CLIENT_A), TOKEN_LIMIT)
        assert await _refused(singleton, _forwarded(CLIENT_A))

        monkeypatch.delenv("PROTECTION_TRUSTED_PROXIES", raising=False)
        reset_rate_limiter()

        # Trust dropped: five *differently* forwarded requests now share the
        # ingress peer's single budget, so the sixth is refused.
        for n in range(TOKEN_LIMIT):
            await singleton.check_rate_limit(_forwarded(f"203.0.113.{n}"), "/token")

        assert await _refused(singleton, _forwarded("203.0.113.99"))
        reset_rate_limiter()


class TestTheAdvertisedWaitIsTheRealOne:
    """``Retry-After: 60`` was hardcoded under a *sliding* window.

    The wait is until the oldest surviving timestamp ages out, which for a
    client that filled its budget nearly a minute ago is seconds. Telling it to
    wait a full minute is the difference between a client that backs off
    correctly and one that gives up.
    """

    async def _refusal(self, limiter, request, endpoint="/token"):
        with pytest.raises(HTTPException) as refusal:
            await limiter.check_rate_limit(request, endpoint)
        assert refusal.value.status_code == 429
        return refusal.value

    async def test_a_nearly_aged_out_window_advertises_seconds(self, monkeypatch):
        limiter = OAuthRateLimiter()
        peer = "198.51.100.11"
        # A budget spent ~55 seconds ago: five seconds from the oldest ageing out.
        now = time.time()
        limiter._requests[(peer, "/token")] = [now - 55 + n * 0.1 for n in range(5)]

        refusal = await self._refusal(limiter, _StubRequest(peer))

        assert 1 <= int(refusal.headers["Retry-After"]) <= 6, refusal.headers

    async def test_a_freshly_spent_window_still_advertises_a_full_minute(self):
        """Not "always small": the honest answer is large when the wait is."""
        limiter = OAuthRateLimiter()
        peer = "198.51.100.12"

        await _spend(limiter, lambda: _StubRequest(peer), TOKEN_LIMIT)
        refusal = await self._refusal(limiter, _StubRequest(peer))

        assert int(refusal.headers["Retry-After"]) == 60, refusal.headers

    async def test_the_wait_never_rounds_down_to_zero(self, monkeypatch):
        """A sub-second answer reads as "retry immediately", which is no wait."""
        limiter = OAuthRateLimiter()
        peer = "198.51.100.13"
        now = time.time()
        limiter._requests[(peer, "/token")] = [now - 59.9] * TOKEN_LIMIT

        refusal = await self._refusal(limiter, _StubRequest(peer))

        assert int(refusal.headers["Retry-After"]) >= 1


class TestTheDependencyConsultsTheFixedLimiter:
    """The wiring, not just the method.

    ``require_oauth_rate_limit_token`` is the callable FastAPI actually
    ``Depends`` on. A perfect ``check_rate_limit`` that no dependency reaches
    would leave ``/token`` exactly as broken as before.
    """

    async def test_it_refuses_once_the_budget_is_spent(self, monkeypatch):
        monkeypatch.delenv("PROTECTION_TRUSTED_PROXIES", raising=False)
        reset_rate_limiter()
        try:
            for _ in range(TOKEN_LIMIT):
                await require_oauth_rate_limit_token(_StubRequest("198.51.100.9"))

            with pytest.raises(HTTPException) as refusal:
                await require_oauth_rate_limit_token(_StubRequest("198.51.100.9"))

            assert refusal.value.status_code == 429
        finally:
            reset_rate_limiter()

    async def test_it_separates_clients_behind_a_configured_ingress(self, monkeypatch):
        monkeypatch.setenv("PROTECTION_TRUSTED_PROXIES", INGRESS_RANGE)
        reset_rate_limiter()
        try:
            for _ in range(TOKEN_LIMIT):
                await require_oauth_rate_limit_token(_forwarded(CLIENT_A))

            with pytest.raises(HTTPException):
                await require_oauth_rate_limit_token(_forwarded(CLIENT_A))

            # The neighbour is unaffected — the whole point of fm#948.
            await require_oauth_rate_limit_token(_forwarded(CLIENT_B))
        finally:
            monkeypatch.delenv("PROTECTION_TRUSTED_PROXIES", raising=False)
            reset_rate_limiter()


class TestTheSSOAuditRecordsTheSameAddress:
    """The JIT-provisioning audit trail and the SSO limit must name one client.

    ``sso_callback`` recorded ``request.client.host``, which behind an ingress
    is the ingress pod for every login on the deployment — an audit column that
    says "10.42.0.7" for all of them answers no question anyone would ask of it.
    It resolves through ``trusted_proxy_networks()``, the limiter's own list, so
    the address the audit names is by construction the address the limit was
    applied to.
    """

    @staticmethod
    def _service():
        service = Mock()
        service.complete_callback = AsyncMock(
            return_value="https://app.example.com/sso/callback?code=abc"
        )
        return service

    async def _call(self, request, service):
        return await sso_callback(
            request=request,
            code="idp-code",
            state="idp-state",
            error=None,
            service=service,
        )

    async def test_it_records_the_forwarded_client_behind_a_trusted_proxy(
        self, monkeypatch
    ):
        monkeypatch.setenv("PROTECTION_TRUSTED_PROXIES", INGRESS_RANGE)
        reset_rate_limiter()
        service = self._service()
        try:
            await self._call(_forwarded(CLIENT_A), service)
        finally:
            monkeypatch.delenv("PROTECTION_TRUSTED_PROXIES", raising=False)
            reset_rate_limiter()

        assert service.complete_callback.call_args.kwargs["client_ip"] == CLIENT_A

    async def test_a_forged_header_cannot_choose_what_the_audit_records(
        self, monkeypatch
    ):
        """An attacker-selectable audit trail is not an audit trail."""
        monkeypatch.delenv("PROTECTION_TRUSTED_PROXIES", raising=False)
        reset_rate_limiter()
        attacker = "198.51.100.77"
        service = self._service()
        try:
            await self._call(_forwarded("1.2.3.4", peer=attacker), service)
        finally:
            reset_rate_limiter()

        assert service.complete_callback.call_args.kwargs["client_ip"] == attacker

    async def test_no_transport_peer_records_null_not_the_sentinel(self, monkeypatch):
        """``UNKNOWN_CLIENT_IP`` is a limiter key, never an audited address.

        The service parameter and the audit column are both nullable; writing
        the string "unknown" into an IP column would be a value that looks like
        data and is not.
        """
        monkeypatch.delenv("PROTECTION_TRUSTED_PROXIES", raising=False)
        reset_rate_limiter()
        service = self._service()
        try:
            await self._call(_StubRequest(None), service)
        finally:
            reset_rate_limiter()

        assert service.complete_callback.call_args.kwargs["client_ip"] is None

    async def test_the_audit_and_the_limit_agree_on_one_request(self, monkeypatch):
        """One trust source, so the two answers cannot drift apart."""
        monkeypatch.setenv("PROTECTION_TRUSTED_PROXIES", INGRESS_RANGE)
        reset_rate_limiter()
        request = _forwarded(CLIENT_B)
        service = self._service()
        try:
            await self._call(request, service)
            resolved_for_limits = resolve_client_ip(request, trusted_proxy_networks())
        finally:
            monkeypatch.delenv("PROTECTION_TRUSTED_PROXIES", raising=False)
            reset_rate_limiter()

        assert (
            service.complete_callback.call_args.kwargs["client_ip"]
            == resolved_for_limits
            == CLIENT_B
        )
