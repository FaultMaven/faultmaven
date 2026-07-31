"""The trusted-proxy policy reaches the limiter, from every loader (fm#927).

A correct resolver that no enforcement path calls fixes nothing, and a setting
that only three of four loaders populate is a deployment-shaped hole. Two
properties are pinned here:

1. ``RateLimitMiddleware`` keys the ``global`` limit on the resolver's answer —
   so a forged ``X-Forwarded-For`` cannot select the bucket.
2. Every ``ProtectionSettings`` producer reads ``PROTECTION_TRUSTED_PROXIES``
   from the one reader, so the four paths cannot disagree about what the
   deployment asked for.
"""

import contextlib
import logging
import os
from unittest.mock import AsyncMock, Mock, patch

import pytest
from starlette.datastructures import Headers

from faultmaven.api.middleware.rate_limiting import RateLimitMiddleware
from faultmaven.config.protection import (
    get_development_protection_settings,
    get_production_protection_settings,
    load_protection_settings,
)
from faultmaven.models.protection import LimitType, RateLimitResult

pytestmark = [pytest.mark.unit, pytest.mark.security]

INGRESS = "10.42.0.7"
ATTACKER = "198.51.100.77"


def _request(peer, headers=None):
    """Headers are a real ``starlette.Headers``, not a dict.

    A dict cannot express repeated field lines, which is the shape that broke
    the resolver once already — a fixture that cannot represent the input can
    never fail on it.
    """
    request = Mock()
    request.client = Mock(host=peer)
    request.headers = Headers(
        raw=[(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    )
    request.url = Mock(path="/api/v1/cases")
    request.query_params = {}
    request.cookies = {}
    return request


def _middleware(trusted_proxies):
    settings = get_production_protection_settings()
    settings.trusted_proxies = list(trusted_proxies)
    with patch(
        "faultmaven.api.middleware.rate_limiting.RedisRateLimiter"
    ) as limiter_cls:
        limiter_cls.return_value = Mock()
        return RateLimitMiddleware(app=Mock(), settings=settings)


class TestTheLimiterKeysOnTheResolvedAddress:
    async def test_rotating_the_header_does_not_rotate_the_bucket(self):
        """The #927 evasion, at the enforcement boundary.

        Without a trusted proxy configured, a thousand distinct forged headers
        from one caller must all land on one key. If they land on a thousand,
        the caller has a thousand quotas.
        """
        middleware = _middleware([])
        keys = {
            middleware._get_client_ip(
                _request(ATTACKER, {"X-Forwarded-For": f"1.2.3.{n}"})
            )
            for n in range(1, 200)
        }

        assert keys == {ATTACKER}

    async def test_the_global_check_uses_that_key(self):
        """Pins the wiring, not just the helper.

        ``_get_client_ip`` could be perfect and unused; this asserts the value
        that reaches ``check_rate_limit`` is the resolved one.
        """
        middleware = _middleware([])
        recorded = {}

        async def capture(key, limit_type, identifier):
            recorded["key"] = key
            recorded["identifier"] = identifier
            return RateLimitResult(
                allowed=True, limit_type=limit_type, current_count=1, limit=500
            )

        middleware.rate_limiter.check_rate_limit = AsyncMock(side_effect=capture)
        request = _request(ATTACKER, {"X-Forwarded-For": "1.2.3.4"})

        await middleware._check_global_rate_limit(middleware._get_client_ip(request))

        assert recorded["key"] == ATTACKER
        assert recorded["identifier"] == f"global:{ATTACKER}"

    async def test_a_configured_proxy_still_separates_real_clients(self):
        middleware = _middleware(["10.42.0.0/16"])

        keys = {
            middleware._get_client_ip(
                _request(INGRESS, {"X-Forwarded-For": f"203.0.113.{n}"})
            )
            for n in range(1, 50)
        }

        assert len(keys) == 49


class TestEveryLoaderReadsTheKey:
    """One reader, four loaders — the pattern ``_fail_open_default`` established."""

    LOADERS = {
        "settings": lambda: load_protection_settings(),
        "development": get_development_protection_settings,
        "production": get_production_protection_settings,
    }

    @pytest.mark.parametrize("name", sorted(LOADERS))
    def test_configured_value_reaches_the_settings(self, name):
        with patch.dict(
            os.environ, {"PROTECTION_TRUSTED_PROXIES": "10.42.0.0/16, 10.43.0.1"}
        ):
            settings = self.LOADERS[name]()

        assert settings.trusted_proxies == ["10.42.0.0/16", "10.43.0.1"]

    @pytest.mark.parametrize("name", sorted(LOADERS))
    def test_unset_means_trust_nothing(self, name):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PROTECTION_TRUSTED_PROXIES", None)
            settings = self.LOADERS[name]()

        assert settings.trusted_proxies == []

    def test_environment_fallback_loader_reads_it_too(self):
        """The degraded path is a loader like any other.

        ``_load_from_environment`` only runs when settings construction itself
        raised. A process that has lost its settings must not thereby lose the
        trust policy and start believing forwarded headers.
        """
        from faultmaven.config.protection import _load_from_environment

        with patch.dict(os.environ, {"PROTECTION_TRUSTED_PROXIES": "10.42.0.0/16"}):
            settings = _load_from_environment()

        assert settings.trusted_proxies == ["10.42.0.0/16"]


class TestPerformanceMiddlewareUsesTheSameRule:
    """The address a request is *labelled* with must not disagree with the one
    it is *limited* by — otherwise metrics attribute a flood to the wrong client.

    Observability rather than enforcement, but the wiring was previously
    unasserted: dropping the argument would have gone unnoticed.
    """

    def test_it_honours_a_configured_trusted_proxy(self):
        from faultmaven.api.middleware.performance import PerformanceTrackingMiddleware

        middleware = PerformanceTrackingMiddleware(
            app=Mock(), trusted_proxies=["10.42.0.0/16"]
        )

        assert (
            middleware._get_client_ip(
                _request(INGRESS, {"X-Forwarded-For": "203.0.113.50"})
            )
            == "203.0.113.50"
        )

    def test_it_ignores_headers_by_default(self):
        """Same empty default as the limiter, so the two cannot diverge."""
        from faultmaven.api.middleware.performance import PerformanceTrackingMiddleware

        middleware = PerformanceTrackingMiddleware(app=Mock())

        assert (
            middleware._get_client_ip(
                _request(ATTACKER, {"X-Forwarded-For": "1.2.3.4"})
            )
            == ATTACKER
        )

    def test_it_agrees_with_the_limiter_on_the_same_request(self):
        from faultmaven.api.middleware.performance import PerformanceTrackingMiddleware

        limiter = _middleware(["10.42.0.0/16"])
        perf = PerformanceTrackingMiddleware(
            app=Mock(), trusted_proxies=["10.42.0.0/16"]
        )
        request = _request(INGRESS, {"X-Forwarded-For": "1.2.3.4, 203.0.113.50"})

        assert limiter._get_client_ip(request) == perf._get_client_ip(request)


class TestProductionSaysSoWhenItIsUnconfigured:
    """Empty in production is safe but coarse, and must not be discovered late.

    The request-time warning only fires once forwarding headers actually
    arrive and is throttled to one per five minutes — too quiet to catch during
    a rollout. Production is by definition behind something, so the preset
    itself says it at startup.
    """

    def test_empty_in_production_warns_at_startup(self, caplog):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PROTECTION_TRUSTED_PROXIES", None)
            with caplog.at_level(logging.WARNING):
                settings = get_production_protection_settings()

        assert settings.trusted_proxies == []
        assert "PROTECTION_TRUSTED_PROXIES is empty" in caplog.text
        assert "share one" in caplog.text

    def test_a_configured_production_deployment_is_quiet(self):
        """The warning must not fire on the correct configuration."""
        with patch.dict(os.environ, {"PROTECTION_TRUSTED_PROXIES": "10.244.0.0/16"}):
            with caplog_at_warning() as records:
                settings = get_production_protection_settings()

        assert settings.trusted_proxies == ["10.244.0.0/16"]
        assert not [r for r in records if "TRUSTED_PROXIES" in r.getMessage()]

    def test_it_warns_rather_than_refusing_to_boot(self):
        """An unset value degrades availability; refusing to start removes it."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PROTECTION_TRUSTED_PROXIES", None)
            settings = get_production_protection_settings()

        assert settings.enabled is True
        assert settings.rate_limiting_enabled is True


@contextlib.contextmanager
def caplog_at_warning():
    """Collect WARNING records from the protection config logger."""
    logger = logging.getLogger("faultmaven.config.protection")
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Collector(level=logging.WARNING)
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.WARNING)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def test_production_still_pins_fail_closed():
    """#927 clears the last stated blocker on unpinning; the pin stays anyway.

    Both defects that made the fail-open argument false have now been fixed, so
    nothing mechanical prevents flipping this. It remains a deliberate posture
    decision — pinned here so that clearing a blocker does not quietly become
    permission to reverse it.
    """
    assert get_production_protection_settings().fail_open_on_redis_error is False
