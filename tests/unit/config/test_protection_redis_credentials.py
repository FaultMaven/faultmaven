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

import logging
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
        # The middleware gate, not the PII gate — see the gate tests below.
        os.environ["BASIC_PROTECTION_ENABLED"] = "true"
        os.environ.pop("PROTECTION_ENABLED", None)
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
    """``PROTECTION_RATE_LIMIT_FAIL_OPEN`` governs both loaders.

    The settings path used to hardcode ``True`` and ignore the operator.
    """
    if env_value is None:
        monkeypatch.delenv("PROTECTION_RATE_LIMIT_FAIL_OPEN", raising=False)
    else:
        monkeypatch.setenv("PROTECTION_RATE_LIMIT_FAIL_OPEN", env_value)
    reset_settings()

    # Settings path.
    assert load_protection_settings().fail_open_on_redis_error is expected

    # Environment path (settings construction failed).
    monkeypatch.setattr(
        "faultmaven.config.settings.get_settings",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert load_protection_settings().fail_open_on_redis_error is expected


@pytest.mark.parametrize("loader", _LOADERS, ids=lambda f: f.__name__)
@pytest.mark.parametrize(
    "env_value,expected", [("false", False), ("true", True), (None, True)]
)
def test_every_loader_honours_the_fail_open_key_in_both_directions(
    cloud_discrete_credentials, monkeypatch, loader, env_value, expected
):
    """No loader may hardcode the degrade policy over the operator's key.

    ``get_production_protection_settings`` hardcoded ``False``, making
    ``PROTECTION_RATE_LIMIT_FAIL_OPEN`` a no-op in the one environment where the
    503-on-every-request cliff actually bites. Swept over every producer and
    both directions, so a loader that pinned the *new* default would fail too.
    """
    if env_value is None:
        monkeypatch.delenv("PROTECTION_RATE_LIMIT_FAIL_OPEN", raising=False)
    else:
        monkeypatch.setenv("PROTECTION_RATE_LIMIT_FAIL_OPEN", env_value)
    reset_settings()

    assert loader().fail_open_on_redis_error is expected


def test_hardening_pii_redaction_does_not_make_rate_limiting_fail_closed(
    cloud_discrete_credentials, monkeypatch
):
    """The operator footgun: two policies, two keys, no coupling.

    ``PROTECTION_FAIL_OPEN`` binds to ``settings.protection.fail_open`` and
    governs PII-redaction fail-open (#654). Someone hardening redaction to fail
    closed must not thereby turn a Redis blip into a 503 on every request.
    """
    monkeypatch.setenv("PROTECTION_FAIL_OPEN", "false")
    monkeypatch.delenv("PROTECTION_RATE_LIMIT_FAIL_OPEN", raising=False)
    reset_settings()

    # The key really is live on the redaction side — otherwise this test would
    # pass just as well against a key nothing reads.
    from faultmaven.config.settings import get_settings

    assert get_settings().protection.fail_open is False

    # ... and the rate-limiting policy is untouched, on both load paths.
    assert load_protection_settings().fail_open_on_redis_error is True

    monkeypatch.setattr(
        "faultmaven.config.settings.get_settings",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert load_protection_settings().fail_open_on_redis_error is True


# --------------------------------------------------------------------------- #
# Which field gates the protection MIDDLEWARE
#
# `_load_from_settings` read `settings.security.protection_enabled`, a field on
# no settings section at all, so it raised AttributeError on every call and this
# path was dead. The replacement must be the basic-protection gate, not the
# PII/Presidio gate: `redaction.py` branches on `protection_enabled`, and the
# admin API reports it as `pii_redaction_enabled`.
# --------------------------------------------------------------------------- #


def _install_middleware(loader_settings):
    from fastapi import FastAPI

    from faultmaven.api.protection import setup_protection_middleware

    return setup_protection_middleware(FastAPI(), settings=loader_settings)


def test_middleware_gate_is_basic_protection_not_pii_redaction(monkeypatch):
    """Turning PII redaction off must not remove rate limiting and dedup."""
    monkeypatch.setenv("BASIC_PROTECTION_ENABLED", "true")
    monkeypatch.setenv("PROTECTION_ENABLED", "false")  # PII/Presidio off
    reset_settings()

    settings = load_protection_settings()
    assert settings.enabled is True

    setup_info = _install_middleware(settings)
    assert setup_info["protection_enabled"] is True
    assert "rate_limiting" in setup_info["middleware_added"]
    assert "deduplication" in setup_info["middleware_added"]


def test_middleware_gate_honours_basic_protection_disabled(monkeypatch):
    """And the converse: PII on does not force the middleware on."""
    monkeypatch.setenv("BASIC_PROTECTION_ENABLED", "false")
    monkeypatch.setenv("PROTECTION_ENABLED", "true")  # PII/Presidio on
    reset_settings()

    settings = load_protection_settings()
    assert settings.enabled is False

    setup_info = _install_middleware(settings)
    assert setup_info["protection_enabled"] is False
    assert setup_info["middleware_added"] == []


def test_disabled_protection_is_announced_not_silent(monkeypatch, caplog):
    """ "No protection middleware anywhere" must not be a silent state.

    ``basic_protection_enabled`` defaults to False and nothing sets it, so the
    settings path installs no rate limiting, no deduplication and no timeouts —
    and said nothing about it. Whether the default should flip is the owner's
    call; being legible in the logs is not.
    """
    monkeypatch.setenv("BASIC_PROTECTION_ENABLED", "false")
    reset_settings()

    with caplog.at_level(logging.WARNING, logger="faultmaven.config.protection"):
        settings = load_protection_settings()

    assert settings.enabled is False

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "basic_protection_enabled" in message and "BASIC_PROTECTION_ENABLED" in message
        for message in warnings
    ), f"no warning naming the field and the env var; got {warnings}"


def test_enabled_protection_does_not_warn(monkeypatch, caplog):
    """The converse, so the warning stays a signal rather than constant noise."""
    monkeypatch.setenv("BASIC_PROTECTION_ENABLED", "true")
    reset_settings()

    with caplog.at_level(logging.WARNING, logger="faultmaven.config.protection"):
        assert load_protection_settings().enabled is True

    assert [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "basic_protection_enabled" in r.getMessage()
    ] == []


def test_environment_fallback_uses_the_same_middleware_gate(monkeypatch):
    """The emergency path must not gate middleware on the PII key either."""
    monkeypatch.setattr(
        "faultmaven.config.settings.get_settings",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    monkeypatch.setenv("PROTECTION_ENABLED", "false")
    monkeypatch.delenv("BASIC_PROTECTION_ENABLED", raising=False)
    assert load_protection_settings().enabled is True

    monkeypatch.setenv("BASIC_PROTECTION_ENABLED", "false")
    assert load_protection_settings().enabled is False


def test_the_dead_settings_path_is_alive(monkeypatch):
    """Regression: `_load_from_settings` raised AttributeError on every call.

    Anything that reintroduces a nonexistent settings attribute here silently
    demotes every caller to its error branch, which is how this went unnoticed.
    """
    monkeypatch.setenv("BASIC_PROTECTION_ENABLED", "true")
    reset_settings()

    settings = load_protection_settings()  # must not raise

    assert settings.enabled is True
    assert settings.redis_key_prefix == "faultmaven"  # the settings-path value


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
