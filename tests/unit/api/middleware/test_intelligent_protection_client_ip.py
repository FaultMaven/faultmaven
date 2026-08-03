"""Intelligent protection identifies clients by resolved address (fm#948).

``_get_client_identifier`` and ``_prepare_request_data`` read
``request.client.host``. Behind an ingress that is one address for the whole
deployment, so every anonymous caller sharing a common User-Agent collapsed
onto a single identifier — one behavioural profile, one reputation, one
circuit breaker for everybody.

Both halves are pinned: the identifier must separate real clients behind a
*configured* proxy, and must not be selectable by a caller who sends its own
forwarding header from anywhere else.
"""

from unittest.mock import Mock, patch

import pytest
from starlette.datastructures import Headers

from faultmaven.api.middleware.intelligent_protection import (
    IntelligentProtectionMiddleware,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

INGRESS = "10.42.0.7"
INGRESS_RANGE = "10.42.0.0/16"
ATTACKER = "198.51.100.77"
COMMON_UA = "Mozilla/5.0 (FaultMaven Copilot)"


def _request(peer, headers=None):
    """Headers are a real ``starlette.Headers``, not a dict.

    A dict cannot express repeated ``X-Forwarded-For`` field lines — the shape
    that broke this resolver once, so the fixture has to be able to carry it.
    """
    request = Mock()
    request.client = Mock(host=peer) if peer is not None else None
    items = list(headers.items()) if isinstance(headers, dict) else list(headers or [])
    request.headers = Headers(
        raw=[(name.lower().encode(), value.encode()) for name, value in items]
    )
    request.url = Mock(path="/api/v1/cases")
    request.method = "POST"
    request.query_params = {}
    request.path_params = {}
    return request


def _forwarded(client_ip, peer=INGRESS, user_agent=COMMON_UA):
    return _request(peer, {"X-Forwarded-For": client_ip, "User-Agent": user_agent})


def _middleware(trusted_proxies=None):
    kwargs = {} if trusted_proxies is None else {"trusted_proxies": trusted_proxies}
    return IntelligentProtectionMiddleware(
        app=Mock(), coordinator=Mock(), enabled=True, **kwargs
    )


class TestTheIdentifier:
    def test_a_forged_header_cannot_split_one_caller_into_many(self):
        """Rotating the header from an untrusted peer buys nothing.

        If it did, one caller would hold as many reputations as it cared to
        mint, and behavioural analysis would never accumulate evidence against
        any of them.
        """
        middleware = _middleware([])

        identifiers = {
            middleware._get_client_identifier(_forwarded(f"1.2.3.{n}", peer=ATTACKER))
            for n in range(1, 100)
        }

        assert len(identifiers) == 1

    def test_real_clients_behind_a_configured_proxy_stay_distinct(self):
        """The fm#948 direction: one ingress must not mean one client."""
        middleware = _middleware([INGRESS_RANGE])

        identifiers = {
            middleware._get_client_identifier(_forwarded(f"203.0.113.{n}"))
            for n in range(1, 50)
        }

        assert len(identifiers) == 49

    def test_the_identifier_still_shape_matches(self):
        """The hashing is untouched; only the address feeding it changed."""
        middleware = _middleware([INGRESS_RANGE])

        identifier = middleware._get_client_identifier(_forwarded("203.0.113.50"))

        assert identifier.startswith("client_")
        assert len(identifier) == len("client_") + 16

    def test_the_user_agent_still_participates(self):
        """Same client, two agents, two identifiers — as before the change."""
        middleware = _middleware([INGRESS_RANGE])

        identifiers = {
            middleware._get_client_identifier(
                _forwarded("203.0.113.50", user_agent=agent)
            )
            for agent in ("curl/8.4.0", COMMON_UA)
        }

        assert len(identifiers) == 2


class TestTheRequestDataFedToTheCoordinator:
    """``client_ip`` is what reputation and anomaly detection are keyed on, so
    it must agree with the identifier rather than being resolved separately."""

    async def test_it_reports_the_forwarded_address_behind_a_trusted_proxy(self):
        middleware = _middleware([INGRESS_RANGE])

        data = await middleware._prepare_request_data(_forwarded("203.0.113.50"))

        assert data["client_ip"] == "203.0.113.50"

    async def test_it_ignores_a_forged_header_from_an_untrusted_peer(self):
        middleware = _middleware([])

        data = await middleware._prepare_request_data(
            _forwarded("1.2.3.4", peer=ATTACKER)
        )

        assert data["client_ip"] == ATTACKER

    async def test_a_missing_peer_is_not_an_ip_shaped_value(self):
        middleware = _middleware([])

        data = await middleware._prepare_request_data(_request(None))

        assert data["client_ip"] == "unknown"


class TestTheDefaultTrustsNothing:
    """Omitting the argument must mean today's behaviour, not a widened trust.

    Same empty default as ``RateLimitMiddleware`` and
    ``PerformanceTrackingMiddleware``, so the three cannot diverge on what "the
    client" means for one request.
    """

    def test_headers_do_not_influence_the_identifier(self):
        middleware = _middleware()

        assert middleware._get_client_identifier(
            _forwarded("1.2.3.4", peer=ATTACKER)
        ) == middleware._get_client_identifier(_forwarded("5.6.7.8", peer=ATTACKER))

    async def test_headers_do_not_influence_the_reported_address(self):
        middleware = _middleware()

        data = await middleware._prepare_request_data(
            _forwarded("1.2.3.4", peer=ATTACKER)
        )

        assert data["client_ip"] == ATTACKER

    def test_a_disabled_instance_still_carries_the_attribute(self):
        """Parsed before the disabled early-return, or a later enable would
        reach an instance with no ``trusted_proxies`` at all."""
        middleware = IntelligentProtectionMiddleware(
            app=Mock(), coordinator=Mock(), enabled=False
        )

        assert middleware.trusted_proxies == ()


class TestTheConstructionSitePassesThePolicy:
    """A middleware that supports the argument but is never given it is not
    fixed. ``ProtectionSystem`` is where the deployment's policy meets the
    middleware, so that is where the wiring is asserted."""

    @staticmethod
    def _system(trusted_proxies):
        from faultmaven.api.protection import ProtectionSystem

        system = ProtectionSystem.__new__(ProtectionSystem)
        system.app = Mock()
        system.session_store = Mock()
        system.logger = Mock()
        system.intelligent_config = Mock()
        system.basic_config = Mock(trusted_proxies=list(trusted_proxies))
        system.intelligent_middleware = None
        return system

    async def test_the_settings_value_reaches_the_middleware(self):
        system = self._system([INGRESS_RANGE])

        with patch(
            "faultmaven.api.protection.IntelligentProtectionMiddleware"
        ) as middleware_cls:
            await system._setup_intelligent_protection()

        assert middleware_cls.call_args.kwargs["trusted_proxies"] == [INGRESS_RANGE]

    async def test_the_middleware_actually_installed_gets_it_too(self):
        """``add_middleware`` builds the instance that serves requests; the
        directly constructed one is retained for status queries and shutdown."""
        system = self._system([INGRESS_RANGE])

        with patch("faultmaven.api.protection.IntelligentProtectionMiddleware"):
            await system._setup_intelligent_protection()

        assert system.app.add_middleware.call_args.kwargs["trusted_proxies"] == [
            INGRESS_RANGE
        ]

    async def test_an_unconfigured_deployment_passes_the_empty_list(self):
        system = self._system([])

        with patch(
            "faultmaven.api.protection.IntelligentProtectionMiddleware"
        ) as middleware_cls:
            await system._setup_intelligent_protection()

        assert middleware_cls.call_args.kwargs["trusted_proxies"] == []
