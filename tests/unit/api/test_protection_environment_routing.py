"""Every environment installs the protection middleware (fm#1023).

``setup_protection_middleware`` used to name ``production`` and ``development``
and send everything else to a settings-driven loader gated on
``basic_protection_enabled`` — a field defaulting to ``False`` that nothing set.
``Environment`` has a third member, ``staging``, so ``ENVIRONMENT=staging``
installed no rate limiting and no deduplication at all: ``app.user_middleware``
came back empty. The on-prem box and the flip-rehearsal overlay both run
staging.

Three properties are pinned:

1. **Coverage.** Every ``Environment`` member — iterated, not enumerated, so a
   fourth member is covered the day it is added — plus a string that is not an
   ``Environment`` at all, installs both middlewares.
2. **Semantics.** Only ``development`` gets the permissive preset. Staging and
   unknown names get production's, which is fail-*closed* on a Redis error —
   the discriminator that separates this fix from one that merely routed
   staging somewhere that happened to install middleware.
3. **Fail closed on setup failure.** A preset that raises — or settings that do
   not validate — propagates rather than leaving a bare app behind, which is the
   same unprotected state arrived at from a different direction, and it says so
   at ERROR on the way out.
4. **Honest reporting.** ``protection_enabled`` describes what was installed, not
   what was intended: an install that fails reports ``False``. Both swallowing
   callers (the Redis fail-open policy, and the composition root's development
   carve-out) read this dict, so a hopeful flag would be believed.
5. **Staging owns its Redis namespace.** Production's semantics, but not
   production's keys — otherwise the two collide on a shared Redis.
"""

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.middleware import DeduplicationMiddleware, RateLimitMiddleware
from faultmaven.api.protection import setup_protection_middleware
from faultmaven.config.protection import get_production_protection_settings
from faultmaven.config.settings import Environment
from faultmaven.models.protection import ProtectionSettings, RateLimitConfig

pytestmark = [pytest.mark.unit, pytest.mark.security]

# Not an ``Environment`` member, and not a near-miss of one either: the branch
# must not be reachable by any string other than ``development``.
UNKNOWN_ENVIRONMENT = "weird-env"

# The enum *members*, not their ``.value`` strings: ``main.py`` passes
# ``settings.server.environment``, which is an ``Environment``. ``Environment``
# subclasses ``str`` so the two compare alike today, and pinning the shape the
# real caller uses keeps that an observation rather than an assumption.
ALL_ENVIRONMENTS = list(Environment) + [UNKNOWN_ENVIRONMENT]


def _install(environment=None):
    """Install on a *fresh* app, never the ``main.py`` singleton."""
    app = FastAPI()
    if environment is None:
        setup_info = setup_protection_middleware(app)
    else:
        setup_info = setup_protection_middleware(app, environment=environment)
    return app, setup_info


def _installed(app):
    return {middleware.cls for middleware in app.user_middleware}


def _resolved_settings(app):
    """The settings the limiter actually runs with, not a second call to a preset."""
    for middleware in app.user_middleware:
        if middleware.cls is RateLimitMiddleware:
            return middleware.kwargs["settings"]
    raise AssertionError("RateLimitMiddleware was never installed")


@pytest.mark.parametrize("environment", ALL_ENVIRONMENTS)
def test_every_environment_installs_both_middlewares(environment):
    """The defect, at the surface that showed it: an empty middleware stack."""
    app, setup_info = _install(environment)

    assert setup_info["protection_enabled"] is True, (
        f"{environment!r} installed no protection at all; " f"setup_info={setup_info}"
    )
    assert _installed(app) == {RateLimitMiddleware, DeduplicationMiddleware}, (
        f"{environment!r} left the middleware stack as "
        f"{[m.cls.__name__ for m in app.user_middleware]}"
    )
    assert set(setup_info["middleware_added"]) == {"rate_limiting", "deduplication"}


@pytest.mark.parametrize(
    "environment",
    [Environment.DEVELOPMENT, "development"],
    ids=["enum_member", "plain_string"],
)
def test_development_still_gets_the_development_preset(environment):
    """The one value that may loosen protection, asserted explicitly.

    Otherwise a fix that routed *everything* to production would pass the sweep
    above while quietly removing the development bypass headers and the roomier
    limits that make local iteration workable.

    Both spellings, because the discriminator is the ``Environment`` member:
    ``Environment`` subclasses ``str``, so a caller holding a plain string must
    still land here rather than be quietly hardened into production's preset.
    """
    app, setup_info = _install(environment)

    assert setup_info["settings_source"] == "development_defaults"
    settings = _resolved_settings(app)
    assert settings.protection_bypass_headers == ["X-Dev-Bypass", "X-Test-Bypass"]
    assert settings.rate_limits["global"].requests == 5000
    assert _installed(app) == {RateLimitMiddleware, DeduplicationMiddleware}


@pytest.mark.parametrize("environment", [Environment.STAGING, Environment.PRODUCTION])
def test_staging_gets_production_semantics(environment):
    """Staging is not "development with a different name".

    This is the mutation-observable discriminator: routing staging to the
    development preset still installs both middlewares and still passes the
    sweep, but hands it a fail-*open* degrade policy and the bypass headers.
    """
    app, setup_info = _install(environment)

    assert setup_info["settings_source"] == "production_defaults"

    settings = _resolved_settings(app)
    assert settings.fail_open_on_redis_error is False
    assert settings.protection_bypass_headers == []
    assert settings.rate_limits["global"].requests == 500


def test_an_unknown_environment_gets_production_not_a_permissive_branch():
    """The near-miss: a name nobody anticipated must fail *safe*.

    An unrecognised ``ENVIRONMENT`` is a misconfiguration, and the safe reading
    of a misconfiguration is "this might be production".
    """
    app, setup_info = _install(UNKNOWN_ENVIRONMENT)

    assert setup_info["settings_source"] == "production_defaults"
    settings = _resolved_settings(app)
    assert settings.fail_open_on_redis_error is False
    assert settings.protection_bypass_headers == []
    assert _installed(app) == {RateLimitMiddleware, DeduplicationMiddleware}


def test_the_default_environment_argument_is_production():
    """A caller that omits ``environment`` gets the strict preset.

    The default used to be ``development``, so an omission silently loosened
    every limit and enabled the bypass headers.
    """
    app, setup_info = _install()

    assert setup_info["settings_source"] == "production_defaults"
    assert _resolved_settings(app).fail_open_on_redis_error is False
    assert _installed(app) == {RateLimitMiddleware, DeduplicationMiddleware}


def test_a_failing_preset_refuses_to_boot_rather_than_serve_unprotected(monkeypatch):
    """A preset that raises must propagate, not leave the app unprotected.

    The handler asked ``settings.fail_open_on_redis_error`` — but when the
    *preset call* is what raised, ``settings`` is still ``None`` and the guard
    read as "swallow". The app then booted with an empty middleware stack and a
    single ERROR line: fm#1023's failure mode reached through a second door.
    Nothing ever stated a degrade policy, and an unknown policy is not
    permission to fail open.

    The dependency is monkeypatched, not the function under test: patching
    ``setup_protection_middleware`` itself would prove nothing about it.
    """
    monkeypatch.setattr(
        "faultmaven.api.protection.get_production_protection_settings",
        lambda: (_ for _ in ()).throw(RuntimeError("preset exploded")),
    )

    app = FastAPI()
    with pytest.raises(RuntimeError, match="preset exploded"):
        setup_protection_middleware(app, environment=Environment.STAGING)

    assert _installed(app) == set(), "an unprotected app survived a failed preset"


def test_settings_that_do_not_validate_refuse_to_boot():
    """Invalid settings must raise, not return an app with nothing installed.

    The validation-failure branch logged an ERROR and ``return``ed ``setup_info``
    — so a caller handing in settings the validator rejects got a bare app and
    one log line: the same unprotected state as the preset-raises door next to
    it, which does fail closed. The two are now symmetric.

    The settings here are otherwise ordinary, and in particular carry the
    ``fail_open_on_redis_error=True`` default. That is the point: the degrade
    policy for a Redis *outage* must have no say over settings that never
    validated, or the generic handler swallows the raise and returns the app
    anyway.
    """
    invalid = ProtectionSettings(
        rate_limits={"global": RateLimitConfig(requests=0, window=60)}
    )
    assert invalid.fail_open_on_redis_error is True, (
        "this test is only meaningful while the fail-open default is True — "
        "otherwise the generic handler would re-raise regardless"
    )

    app = FastAPI()
    with pytest.raises(ValueError, match="validation failed"):
        setup_protection_middleware(app, settings=invalid)

    assert _installed(app) == set(), "an unprotected app survived invalid settings"


def test_a_validation_failure_is_logged_before_it_propagates(caplog):
    """The refusal must be legible, not just fatal.

    The ``ValueError`` handler used to re-raise silently. That is survivable
    where the raise reaches an operator — but the composition root's development
    carve-out swallows it, so on a development box this log line is the only
    statement of *why* the app is running unprotected.
    """
    invalid = ProtectionSettings(
        rate_limits={"global": RateLimitConfig(requests=0, window=60)}
    )

    with caplog.at_level(logging.ERROR, logger="faultmaven.api.protection"):
        with pytest.raises(ValueError, match="validation failed"):
            setup_protection_middleware(FastAPI(), settings=invalid)

    errors = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.ERROR and r.name == "faultmaven.api.protection"
    ]
    assert any(
        "protection" in message.lower() for message in errors
    ), f"the refusal named no reason at ERROR; got {errors}"


def test_protection_enabled_is_not_reported_until_the_installs_land():
    """``protection_enabled`` is a report, not an intention.

    The flag used to be set before the two ``add_middleware`` calls, so any
    failure in them left ``protection_enabled: True`` on an app carrying no
    protection middleware. That matters precisely because a caller can swallow
    the failure: ``fail_open_on_redis_error`` is ``True`` by default, and the
    composition root's development carve-out swallows too — both then read back a
    dict claiming protection is on.

    The failure is provoked at the real surface rather than by patching:
    Starlette refuses ``add_middleware`` once an application has started, and
    entering a ``TestClient`` context starts it.
    """
    app = FastAPI()
    fail_open = get_production_protection_settings()
    fail_open.fail_open_on_redis_error = True

    with TestClient(app):
        setup_info = setup_protection_middleware(app, settings=fail_open)

    assert setup_info["protection_enabled"] is False, (
        "an app that installed nothing reported protection as enabled; "
        f"setup_info={setup_info}"
    )
    assert setup_info["error"], "the swallowed failure left no trace in setup_info"
    assert _installed(app) == set()


class TestStagingOwnsItsRedisNamespace:
    """Production semantics, but never production's keys.

    fm#1023 put staging on the production preset, which pins
    ``redis_key_prefix="faultmaven_prod"``. Pointed at one Redis instance the two
    environments would then share every rate-limit counter and every dedup key —
    a staging load test spending production's quota, and an identical request
    issued in both answered ``409`` in the second.
    """

    def test_staging_gets_its_own_prefix(self):
        app, _ = _install(Environment.STAGING)

        assert _resolved_settings(app).redis_key_prefix == "faultmaven_staging"

    def test_production_keeps_its_own(self):
        """The override must be scoped to staging, not applied to the preset."""
        app, _ = _install(Environment.PRODUCTION)

        assert _resolved_settings(app).redis_key_prefix == "faultmaven_prod"

    def test_an_unknown_environment_stays_on_the_production_prefix(self):
        """Only ``staging`` is carved out; a name nobody anticipated is not.

        An unrecognised value already lands on production's *settings*; giving it
        a third namespace would mean a typo silently detached a deployment from
        the counters its peers share.
        """
        app, _ = _install(UNKNOWN_ENVIRONMENT)

        assert _resolved_settings(app).redis_key_prefix == "faultmaven_prod"

    def test_the_two_environments_do_not_collide(self):
        """The property, not the two constants."""
        staging, _ = _install(Environment.STAGING)
        production, _ = _install(Environment.PRODUCTION)

        assert (
            _resolved_settings(staging).redis_key_prefix
            != _resolved_settings(production).redis_key_prefix
        )
