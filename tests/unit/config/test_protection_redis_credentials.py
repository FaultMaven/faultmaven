"""Protection config has no Redis credential source of its own (fm#897).

``config/protection.py`` used to hand-assemble ``redis://{REDIS_HOST}:{REDIS_PORT}``
(and, in the production loader, a hardcoded fictional host). Because an explicit
``redis_url`` short-circuits ``RedisClientFactory._build_config``, that
self-assembled URL bypassed the password lookup entirely: under cloud's discrete
credential config the rate limiter authenticated anonymously and rate limiting
silently stopped working.

The property pinned here is not "one loader was fixed" but "no producer
assembles a URL": every ``ProtectionSettings`` producer leaves ``redis_url`` as
``None`` under discrete config, so resolution falls to the central factory —
which does read ``REDIS_PASSWORD``.
"""

import logging
import os
from unittest.mock import patch

import pytest

from faultmaven.api.middleware.rate_limiting import RateLimitMiddleware
from faultmaven.config.protection import (
    get_development_protection_settings,
    get_production_protection_settings,
    validate_protection_settings,
)
from faultmaven.infrastructure.redis_client import RedisClientFactory

# Late-bound: the production code under test re-imports the settings module by
# name on every call, so this must reset whichever module object *it* reads,
# not whichever one existed when this file was imported. See
# `tests/utils.reset_settings_singleton`.
from tests.utils import reset_settings_singleton as reset_settings

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


def test_a_blank_redis_url_reads_as_unset_at_the_factory():
    """A present-but-empty ``REDIS_URL`` means "not configured", not ``''``.

    An unset ConfigMap key yields ``''``, not absence. The behaviour is not the
    presets' — both hardcode ``redis_url=None``, so asserting it of them checks
    a constant. It lives in ``RedisClientFactory._build_config``, which decides
    URL-vs-discrete on truthiness, and the near-miss is what matters: ``''`` must
    fall through to the discrete credentials rather than reach
    ``redis.from_url('')``, which would drop the password lookup entirely — the
    fm#897 failure mode, with a blank string standing in for a self-assembled
    URL.
    """
    with patch.dict(os.environ, {}, clear=False):
        _apply_env(
            {
                "REDIS_URL": "",
                "REDIS_HOST": _HOST,
                "REDIS_PORT": _PORT,
                "REDIS_DB": _DB,
                "REDIS_PASSWORD": _PASSWORD,
            }
        )
        try:
            # The blank really did survive as far as settings — otherwise this
            # test passes against a value that was never empty to begin with.
            from faultmaven.config.settings import get_settings

            assert get_settings().database.redis_url == ""

            config = RedisClientFactory._build_config(None, None, None, None)

            assert not config["url"], (
                f"a blank REDIS_URL was carried forward as {config['url']!r}; "
                "an explicit URL short-circuits the password lookup"
            )
            assert config["host"] == _HOST
            assert config["port"] == int(_PORT)
            assert config["db"] == int(_DB)
            assert config["password"] == _PASSWORD
        finally:
            reset_settings()


def test_a_blank_redis_url_is_never_handed_to_from_url():
    """And the consequence, at the construction surface that would break.

    ``_build_config`` returning a falsy URL is only half the property: the
    branch that acts on it must take the discrete path. Pinned by watching the
    two constructors — ``redis.from_url('')`` raises on a real client, so a
    regression here is an outage, not a silent weakening.
    """
    from unittest.mock import MagicMock

    import faultmaven.infrastructure.redis_client as redis_client_module

    with patch.dict(os.environ, {}, clear=False):
        _apply_env(
            {
                "REDIS_URL": "",
                "REDIS_HOST": _HOST,
                "REDIS_PORT": _PORT,
                "REDIS_DB": _DB,
                "REDIS_PASSWORD": _PASSWORD,
            }
        )
        try:
            fake_redis_module = MagicMock()
            with (
                patch.object(redis_client_module, "redis", fake_redis_module),
                patch.object(redis_client_module, "REDIS_AVAILABLE", True),
            ):
                RedisClientFactory.create_client()

            fake_redis_module.from_url.assert_not_called()
            fake_redis_module.Redis.assert_called_once()
            kwargs = fake_redis_module.Redis.call_args.kwargs
            assert kwargs["host"] == _HOST
            assert kwargs["password"] == _PASSWORD
        finally:
            reset_settings()


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
        get_production_protection_settings().redis_url, None, None, None
    )

    assert config["password"] == _PASSWORD
    for special in "@:/#?%":
        assert special in config["password"]


@pytest.mark.parametrize(
    "loader",
    [get_development_protection_settings],
    ids=lambda f: f.__name__,
)
@pytest.mark.parametrize(
    "env_value,expected", [("false", False), ("true", True), (None, True)]
)
def test_the_general_loaders_honour_the_fail_open_key_in_both_directions(
    cloud_discrete_credentials, monkeypatch, loader, env_value, expected
):
    """The development preset must not hardcode the degrade policy.

    Both directions, so a preset that pinned the *default* fails too. Production
    is excluded on purpose and has its own test below — it is the one preset
    that pins the policy, and it pins it closed.
    """
    if env_value is None:
        monkeypatch.delenv("PROTECTION_RATE_LIMIT_FAIL_OPEN", raising=False)
    else:
        monkeypatch.setenv("PROTECTION_RATE_LIMIT_FAIL_OPEN", env_value)
    reset_settings()

    assert loader().fail_open_on_redis_error is expected


@pytest.mark.parametrize("env_value", ["true", "false", None])
def test_production_fails_closed_whatever_the_key_says(
    cloud_discrete_credentials, monkeypatch, env_value
):
    """Production pins fail-closed, and no environment value opens it.

    The argument for defaulting production open was that rungs 1 and 2 of the
    degrade ladder enforce limits first, making the fail-open rung nearly
    unreachable. That is false while the sliding window counts seconds rather
    than requests (``ZADD key current_time current_time`` — score and member are
    the same integer second, so ``ZCARD`` cannot exceed the window in seconds and
    every ``global`` limit is unreachable). ``global`` is the only limit that
    applies to unauthenticated traffic, so under that defect fail-open is the
    whole ladder rather than its floor.

    Swept over both explicit values and absence: a loader that quietly started
    reading the key again would fail on ``"true"``.
    """
    if env_value is None:
        monkeypatch.delenv("PROTECTION_RATE_LIMIT_FAIL_OPEN", raising=False)
    else:
        monkeypatch.setenv("PROTECTION_RATE_LIMIT_FAIL_OPEN", env_value)
    reset_settings()

    assert get_production_protection_settings().fail_open_on_redis_error is False, (
        "production honoured PROTECTION_RATE_LIMIT_FAIL_OPEN; the fail-open rung "
        "is not justifiable while the sliding window counts seconds"
    )


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

    # ... and the rate-limiting policy is untouched.
    assert get_development_protection_settings().fail_open_on_redis_error is True


# --------------------------------------------------------------------------- #
# What gates the protection MIDDLEWARE
#
# It is ``ProtectionSettings.enabled``, and never the PII/Presidio gate:
# `redaction.py` branches on `settings.protection.protection_enabled`, and the
# admin API reports that field as `pii_redaction_enabled`. Both presets pin
# ``enabled=True``, so an operator turning PII redaction off cannot take rate
# limiting and deduplication with it (fm#1023 removed the settings-driven loader
# that used to make this configurable, and silently off by default).
# --------------------------------------------------------------------------- #


def _install_middleware(loader_settings):
    from fastapi import FastAPI

    from faultmaven.api.protection import setup_protection_middleware

    return setup_protection_middleware(FastAPI(), settings=loader_settings)


@pytest.mark.parametrize("loader", _LOADERS, ids=lambda f: f.__name__)
def test_turning_pii_redaction_off_leaves_the_middleware_installed(monkeypatch, loader):
    """Turning PII redaction off must not remove rate limiting and dedup."""
    monkeypatch.setenv("PROTECTION_ENABLED", "false")  # PII/Presidio off
    reset_settings()

    # The key really is live on the redaction side — otherwise this asserts
    # nothing about a coupling that could exist.
    from faultmaven.config.settings import get_settings

    assert get_settings().protection.protection_enabled is False

    settings = loader()
    assert settings.enabled is True

    setup_info = _install_middleware(settings)
    assert setup_info["protection_enabled"] is True
    assert "rate_limiting" in setup_info["middleware_added"]
    assert "deduplication" in setup_info["middleware_added"]


def test_an_explicitly_disabled_settings_object_installs_nothing():
    """The ``enabled`` gate is still live for a caller that passes its own.

    No preset produces this any more, but ``setup_protection_middleware``
    branches on it, and a caller handing it a disabled settings object must get
    a bare app rather than a half-installed stack.
    """
    settings = get_production_protection_settings()
    settings.enabled = False

    setup_info = _install_middleware(settings)

    assert setup_info["protection_enabled"] is False
    assert setup_info["middleware_added"] == []


def test_disabled_protection_is_announced_not_silent(caplog):
    """ "No protection middleware anywhere" must not be a silent state.

    fm#1023 was exactly this state — an empty middleware stack on every staging
    box — and what made it survive was that nothing said so at a level anyone
    reads. The loader that used to carry the announcement was removed with the
    fix; the announcement belongs to the branch that installs nothing, which is
    here. Whether a deployment may disable protection is the owner's call; being
    legible about it is not.
    """
    settings = get_production_protection_settings()
    settings.enabled = False

    with caplog.at_level(logging.WARNING, logger="faultmaven.api.protection"):
        setup_info = _install_middleware(settings)

    assert setup_info["middleware_added"] == []

    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "faultmaven.api.protection"
    ]
    assert any(
        "rate limiting" in message.lower() and "deduplication" in message.lower()
        for message in warnings
    ), f"nothing warned that both protections were skipped; got {warnings}"


def test_installed_protection_does_not_warn(caplog):
    """The converse, so the warning stays a signal rather than constant noise."""
    settings = get_production_protection_settings()
    assert settings.enabled is True

    with caplog.at_level(logging.WARNING, logger="faultmaven.api.protection"):
        setup_info = _install_middleware(settings)

    assert setup_info["protection_enabled"] is True
    assert [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "faultmaven.api.protection"
    ] == []


def test_an_explicitly_configured_redis_url_is_still_honoured():
    """``None`` is not a ceiling: an operator's REDIS_URL still wins.

    The presets hand the factory ``None``, which means "resolve centrally" and
    not "ignore what the operator configured" — the factory falls through to
    ``settings.database.redis_url``.
    """
    explicit = "redis://:urlpw@explicit-host:6390/4"
    with patch.dict(os.environ, {}, clear=False):
        _apply_env({"REDIS_URL": explicit, "REDIS_HOST": _HOST})
        try:
            assert get_production_protection_settings().redis_url is None
            config = RedisClientFactory._build_config(None, None, None, None)
            assert config["url"] == explicit
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


def test_a_configured_redis_url_is_masked_before_it_leaves_the_process():
    """The password never survives into anything emitted.

    This used to be asserted through ``get_protection_health_endpoints``, an
    endpoint that was never registered on any router (#974). Deleting it would
    have taken the only coverage of a *live* property with it: the masking is
    ``RedisClientFactory._mask_url``, and its real callers are the Redis
    construction log line and ``_scrub_redis_secrets``, whose reason string is
    both logged and embedded in the boot-refusal message. Pinned at the helper
    that does the work rather than at a caller that did not run.
    """
    secret = "sup3rs3cr3t"
    url = f"redis://:{secret}@redis.internal:6379/0"

    masked = RedisClientFactory._mask_url(url)

    assert secret not in masked
    assert masked == "redis://:***@redis.internal:6379/0"


@pytest.mark.parametrize(
    "url",
    [
        "redis://redis.internal:6379/0",  # no credentials at all
        "rediss://user@redis.internal:6379/0",  # user, no password
    ],
)
def test_masking_leaves_a_passwordless_url_intact(url):
    """Masking must not corrupt the common case.

    A mangled URL here would be reported back to an operator as their
    configuration, sending them to debug a host that does not exist.
    """
    assert RedisClientFactory._mask_url(url) == url


def test_an_unparseable_url_is_still_scrubbed():
    """The fallback fails safe.

    ``_mask_url`` swallows parse errors, so this pins that the swallow does not
    hand back the raw string: a URL malformed enough to defeat ``urlparse``
    still must not carry its credentials onward.
    """
    mangled = "redis://:pw@@[not-a-host:6379"

    masked = RedisClientFactory._mask_url(mangled)

    assert "pw" not in masked or masked.startswith("redis://***@")
