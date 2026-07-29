"""Protection config has no Redis credential source of its own (fm#897).

``config/protection.py`` used to hand-assemble ``redis://{REDIS_HOST}:{REDIS_PORT}``
(and, in the production loader, a hardcoded fictional host). Because an explicit
``redis_url`` short-circuits ``RedisClientFactory._build_config``, that
self-assembled URL bypassed the password lookup entirely: under cloud's discrete
credential config the rate limiter authenticated anonymously and rate limiting
silently stopped working.

The property pinned here is not "one loader was fixed" but "no loader assembles
a URL": every ``ProtectionSettings`` producer leaves ``redis_url`` as ``None``
under discrete config, so resolution falls to the central factory — which does
read ``REDIS_PASSWORD``.
"""

import os
from unittest.mock import patch

import pytest

from faultmaven.api.middleware.rate_limiting import RateLimitMiddleware
from faultmaven.config.protection import (
    get_development_protection_settings,
    get_production_protection_settings,
    load_protection_settings,
    validate_protection_settings,
)
from faultmaven.config.settings import reset_settings
from faultmaven.infrastructure.redis_client import RedisClientFactory

# Credential leakage is what this guards, so it carries the security marker.
pytestmark = [pytest.mark.unit, pytest.mark.security]

# Deliberately unlike every default: the settings default host is
# "faultmaven-redis-master" and the default port 6379, so a loader that fell
# back to its defaults could not accidentally satisfy these assertions.
_HOST = "redis.cloud.internal"
_PORT = "6381"
_DB = "3"
# Non-empty, and full of URL-special characters — a fixture password of ""
# or None would let a dropped credential pass as "no credential configured".
_PASSWORD = "p@ss:w/rd#with?specials%25"

_REDIS_KEYS = ("REDIS_URL", "REDIS_HOST", "REDIS_PORT", "REDIS_DB", "REDIS_PASSWORD")

# Every producer of a ProtectionSettings — the property must hold for all of
# them, not for whichever one a single test happened to pick.
_LOADERS = (
    load_protection_settings,
    get_development_protection_settings,
    get_production_protection_settings,
)


def _apply_env(env: dict) -> None:
    """Set exactly these REDIS_* vars, clearing the rest, and reload settings."""
    for key in _REDIS_KEYS:
        if key in env:
            os.environ[key] = env[key]
        else:
            os.environ.pop(key, None)
    reset_settings()


@pytest.fixture
def cloud_discrete_credentials():
    """Cloud-shaped config: discrete host/port/db/password, no REDIS_URL."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ["PROTECTION_ENABLED"] = "true"
        _apply_env(
            {
                "REDIS_HOST": _HOST,
                "REDIS_PORT": _PORT,
                "REDIS_DB": _DB,
                "REDIS_PASSWORD": _PASSWORD,
            }
        )
        yield
    reset_settings()


@pytest.mark.parametrize("loader", _LOADERS, ids=lambda f: f.__name__)
def test_no_loader_assembles_a_redis_url(cloud_discrete_credentials, loader):
    """Under discrete credentials every loader defers to central resolution."""
    settings = loader()

    assert settings.redis_url is None, (
        f"{loader.__name__} assembled its own Redis URL "
        f"({settings.redis_url!r}); an explicit URL short-circuits the "
        "factory's password lookup"
    )


@pytest.mark.parametrize("loader", _LOADERS, ids=lambda f: f.__name__)
def test_password_reaches_the_client_config_through_every_loader(
    cloud_discrete_credentials, loader
):
    """The URL each loader yields resolves to a config carrying the password.

    This walks the real chain: ProtectionSettings.redis_url →
    RateLimitMiddleware → RedisRateLimiter.redis_url → the argument
    ``get_async_redis_client`` hands to ``_build_config``.
    """
    protection_settings = loader()
    middleware = RateLimitMiddleware(
        app=lambda scope, receive, send: None, settings=protection_settings
    )

    limiter_url = middleware.rate_limiter.redis_url
    config = RedisClientFactory._build_config(limiter_url, None, None, None)

    assert config["password"] == _PASSWORD
    assert config["host"] == _HOST
    assert config["port"] == int(_PORT)
    assert config["db"] == int(_DB)


def test_special_character_password_survives_intact(cloud_discrete_credentials):
    """No encoding, no escaping, no truncation at any URL-special character.

    The discrete path never interpolates the password into a URL, so it must
    arrive byte-identical (fm#898).
    """
    config = RedisClientFactory._build_config(
        load_protection_settings().redis_url, None, None, None
    )

    assert config["password"] == _PASSWORD
    for special in "@:/#?%":
        assert special in config["password"]


def test_environment_fallback_loader_assembles_nothing(
    cloud_discrete_credentials, monkeypatch
):
    """The emergency path is a parallel source too, and must stay empty.

    ``_load_from_environment`` runs when ``get_settings()`` itself raises, so it
    cannot consult settings — but "cannot consult settings" is not a licence to
    hand-assemble a URL from REDIS_HOST/REDIS_PORT and drop the password.
    """

    def _broken_settings():
        raise RuntimeError("settings construction failed")

    monkeypatch.setattr("faultmaven.config.settings.get_settings", _broken_settings)

    settings = load_protection_settings()
    assert settings.redis_url is None

    # Settings work again by the time the factory resolves the connection.
    monkeypatch.undo()
    config = RedisClientFactory._build_config(settings.redis_url, None, None, None)
    assert config["password"] == _PASSWORD
    assert config["host"] == _HOST


def test_environment_fallback_treats_an_empty_redis_url_as_unset(monkeypatch):
    """``or None``: an empty REDIS_URL must not read as "explicitly configured"."""

    def _broken_settings():
        raise RuntimeError("settings construction failed")

    monkeypatch.setattr("faultmaven.config.settings.get_settings", _broken_settings)

    with patch.dict(os.environ, {}, clear=False):
        _apply_env({"REDIS_URL": "", "REDIS_HOST": _HOST})
        try:
            assert load_protection_settings().redis_url is None
        finally:
            reset_settings()


@pytest.mark.parametrize(
    "env_value,expected", [("false", False), ("true", True), (None, True)]
)
def test_fail_open_policy_comes_from_one_key_on_both_paths(
    cloud_discrete_credentials, monkeypatch, env_value, expected
):
    """``PROTECTION_FAIL_OPEN`` governs both loaders; the settings path used to
    hardcode ``True`` and ignore the operator entirely."""
    if env_value is None:
        monkeypatch.delenv("PROTECTION_FAIL_OPEN", raising=False)
    else:
        monkeypatch.setenv("PROTECTION_FAIL_OPEN", env_value)
    reset_settings()

    # Settings path.
    assert load_protection_settings().fail_open_on_redis_error is expected

    # Environment path (settings construction failed).
    monkeypatch.setattr(
        "faultmaven.config.settings.get_settings",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert load_protection_settings().fail_open_on_redis_error is expected


def test_an_explicitly_configured_redis_url_is_still_honoured():
    """``None`` is the default, not a ceiling: an operator's REDIS_URL wins."""
    explicit = "redis://:urlpw@explicit-host:6390/4"
    with patch.dict(os.environ, {}, clear=False):
        _apply_env({"REDIS_URL": explicit, "REDIS_HOST": _HOST})
        try:
            assert load_protection_settings().redis_url == explicit
        finally:
            reset_settings()


@pytest.mark.parametrize("loader", _LOADERS, ids=lambda f: f.__name__)
def test_validation_accepts_a_none_redis_url_so_middleware_is_installed(
    cloud_discrete_credentials, loader
):
    """Guards the "no protection at all" hazard.

    ``setup_protection_middleware`` returns early when validation fails, so a
    validator that rejected the normal ``redis_url=None`` case would leave the
    app with neither rate limiting nor deduplication installed.
    """
    from fastapi import FastAPI

    from faultmaven.api.protection import setup_protection_middleware

    settings = loader()
    assert settings.redis_url is None

    validation = validate_protection_settings(settings)
    assert validation["valid"] is True, validation["errors"]
    assert not validation["errors"]

    setup_info = setup_protection_middleware(FastAPI(), settings=settings)

    assert setup_info["protection_enabled"] is True
    assert "rate_limiting" in setup_info["middleware_added"]
    assert "deduplication" in setup_info["middleware_added"]


async def test_health_endpoint_never_emits_a_redis_password():
    """A configured URL carries its password inline; the endpoint masks it."""
    from faultmaven.api.protection import get_protection_health_endpoints

    secret = "sup3rs3cr3t"
    with patch.dict(os.environ, {}, clear=False):
        _apply_env({"REDIS_URL": f"redis://:{secret}@redis.internal:6379/0"})
        try:
            payload = await get_protection_health_endpoints()["health"]()
        finally:
            reset_settings()

    assert secret not in repr(payload)
    assert payload["redis_url"] == "redis://:***@redis.internal:6379/0"
    assert payload["redis_source"] == "explicit-url"


async def test_health_endpoint_reports_central_resolution_when_unset(
    cloud_discrete_credentials,
):
    """With no configured URL the endpoint says so rather than inventing one."""
    from faultmaven.api.protection import get_protection_health_endpoints

    payload = await get_protection_health_endpoints()["health"]()

    assert payload["redis_url"] is None
    assert payload["redis_source"] == "central-factory"
    assert _PASSWORD not in repr(payload)
