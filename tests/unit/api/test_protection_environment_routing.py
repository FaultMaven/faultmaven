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
2. **Semantics.** *No* environment gets the permissive preset any more —
   fm#985 item 15 moved that decision onto ``PROTECTION_PROFILE``, because
   ``ENVIRONMENT`` is unset on the standalone quickstart and fell to
   ``development``. Every environment, including ``development``, gets
   production's preset: production's limits and no bypass header. That is
   still the discriminator that separates fm#1023's fix from one that merely
   routed staging somewhere that happened to install middleware, and it is now
   also the discriminator for item 15 — see
   ``tests/unit/api/test_protection_bypass_is_unreachable.py`` for the axis
   that *can* loosen it.

   The degrade policy is no longer part of "production's semantics" as far as
   the environment is concerned: fm#1566 moved it onto the profile too, so a
   self-hosted box naming ``ENVIRONMENT=production`` keeps its per-replica
   FakeRedis stand-in rung and only a ``cloud`` profile pins fail-closed. The
   tests that pin THAT axis are below, and they sweep every environment in
   order to say "none of them decides it".
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


def _install(environment=None, *, is_cloud_deployment=None):
    """Install on a *fresh* app, never the ``main.py`` singleton."""
    app = FastAPI()
    kwargs = {}
    if environment is not None:
        kwargs["environment"] = environment
    if is_cloud_deployment is not None:
        kwargs["is_cloud_deployment"] = is_cloud_deployment
    setup_info = setup_protection_middleware(app, **kwargs)
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
def test_development_no_longer_selects_the_permissive_preset(environment):
    """``ENVIRONMENT`` cannot loosen protection at all (fm#985 item 15).

    This test used to assert the opposite — that ``development`` selects the
    permissive preset — and that assertion was the defect written down. The
    standalone quickstart leaves ``ENVIRONMENT`` unset, the settings default is
    ``development``, and so every self-hosted operator ran a limiter that any
    request could switch off by carrying ``X-Dev-Bypass``.

    Both spellings, because a caller may hold a plain string: ``Environment``
    subclasses ``str``, and neither form may reach the permissive branch.
    """
    app, setup_info = _install(environment)

    assert setup_info["settings_source"] == "production_defaults"
    settings = _resolved_settings(app)
    assert settings.protection_bypass_headers == []
    assert settings.rate_limits["global"].requests == 500
    assert _installed(app) == {RateLimitMiddleware, DeduplicationMiddleware}


@pytest.mark.parametrize("environment", [Environment.STAGING, Environment.PRODUCTION])
def test_staging_gets_production_semantics(environment):
    """Staging is not "development with a different name".

    This is the mutation-observable discriminator: routing staging to the
    development preset still installs both middlewares and still passes the
    sweep, but hands it the bypass headers and the roomy limits.

    The degrade policy is NOT asserted here any more. fm#1566 moved it onto
    the protection profile, so it is no longer a function of the environment
    at all and asserting it here would pin the axis that was removed — see
    ``test_the_degrade_policy_is_keyed_on_the_profile_not_the_environment``.
    """
    app, setup_info = _install(environment)

    assert setup_info["settings_source"] == "production_defaults"

    settings = _resolved_settings(app)
    assert settings.protection_bypass_headers == []
    assert settings.rate_limits["global"].requests == 500


@pytest.mark.parametrize(
    "environment",
    ALL_ENVIRONMENTS,
    ids=[str(getattr(e, "value", e)) for e in ALL_ENVIRONMENTS],
)
@pytest.mark.parametrize(
    "key,expected_fail_open", [(None, True), ("true", True), ("false", False)]
)
@pytest.mark.parametrize("profile", [None, "hardened"])
def test_the_degrade_policy_is_keyed_on_the_profile_not_the_environment(
    monkeypatch, environment, key, expected_fail_open, profile
):
    """fm#1566: ``ENVIRONMENT`` no longer decides whether the limiter fails open.

    This test used to assert the opposite, in as many words — it was named
    ``..._is_keyed_on_the_environment_not_the_profile`` and its docstring said
    "a future change that routes the degrade policy through the new axis fails
    rather than shipping". That was the correct guard for fm#985 item 15, which
    deliberately moved the limits and the bypass headers and NOT this. fm#1566
    is the owner ruling that moves this too, so the guard is inverted rather
    than deleted: the axis it pins is now the profile.

    **The row the issue was filed about is ``ENVIRONMENT=production`` with no
    profile named** — a self-hosted operator doing the natural thing on a
    production install of a self-hosted product. It used to resolve fail-CLOSED
    on a single replica, where there is no fleet to shed onto and where the
    same flag disables ``RedisRateLimiter.fallback_enabled``, so the limiter
    refuses instead of recovering onto FakeRedis. It is now fail-open like
    every other non-cloud deployment, and ``PROTECTION_RATE_LIMIT_FAIL_OPEN``
    is honoured in both directions on every one of them.

    Swept over every ``Environment`` member plus a string that is not one, so
    the claim is "no environment decides this" rather than "the three we
    thought of do not".
    """
    monkeypatch.delenv("PROTECTION_RATE_LIMIT_FAIL_OPEN", raising=False)
    monkeypatch.delenv("PROTECTION_PROFILE", raising=False)
    if key is not None:
        monkeypatch.setenv("PROTECTION_RATE_LIMIT_FAIL_OPEN", key)
    if profile is not None:
        monkeypatch.setenv("PROTECTION_PROFILE", profile)

    app, _ = _install(environment)

    assert _resolved_settings(app).fail_open_on_redis_error is expected_fail_open, (
        f"ENVIRONMENT={environment!r} decided the Redis degrade policy. Since "
        f"fm#1566 it decides nothing here: the profile does, and a self-hosted "
        f"box that names a deployed environment must keep its per-replica "
        f"FakeRedis stand-in rung."
    )


@pytest.mark.parametrize(
    "environment",
    ALL_ENVIRONMENTS,
    ids=[str(getattr(e, "value", e)) for e in ALL_ENVIRONMENTS],
)
@pytest.mark.parametrize("key", [None, "true", "false"])
def test_a_cloud_deployment_pins_fail_closed_whatever_the_environment_and_key_say(
    monkeypatch, environment, key
):
    """The posture fm#1566 left alone, pinned so that leaving it alone is checked.

    A multi-replica fleet keeps fail-closed: its degraded rung is per replica,
    so N replicas enforce N independent copies of a limit whose configured
    value only means anything when it is shared. That is a floor, not a
    substitute, and the trade a fleet wants is the 503.

    Two selectors reach it and both are swept: ``DEPLOYMENT_MODE=cloud``
    (``is_cloud_deployment=True``, ADR-004's single source of truth) and an
    explicit ``PROTECTION_PROFILE=cloud`` — the latter being how a self-hosted
    deployment that *does* run several replicas asks for the fleet posture
    without claiming cloud mode.
    """
    monkeypatch.delenv("PROTECTION_RATE_LIMIT_FAIL_OPEN", raising=False)
    monkeypatch.delenv("PROTECTION_PROFILE", raising=False)
    if key is not None:
        monkeypatch.setenv("PROTECTION_RATE_LIMIT_FAIL_OPEN", key)

    by_deployment_mode, info = _install(environment, is_cloud_deployment=True)
    assert info["protection_profile"] == "cloud"
    assert _resolved_settings(by_deployment_mode).fail_open_on_redis_error is False

    monkeypatch.setenv("PROTECTION_PROFILE", "cloud")
    by_profile_key, info = _install(environment, is_cloud_deployment=False)
    assert info["protection_profile"] == "cloud"
    assert _resolved_settings(by_profile_key).fail_open_on_redis_error is False


def test_a_cloud_deployment_cannot_arm_the_bypass_headers_by_naming_development(
    monkeypatch,
):
    """The veto reads ENVIRONMENT, and a cloud deployment may name any it likes.

    ``resolve_protection_profile``'s veto refuses ``PROTECTION_PROFILE=development``
    on a deployed ``ENVIRONMENT``. It says nothing about ``DEPLOYMENT_MODE``, so
    a cloud deployment that also set ``ENVIRONMENT=development`` satisfied the
    veto and armed ``X-Dev-Bypass`` / ``X-Test-Bypass`` — presence alone skipping
    all rate limiting on a multi-tenant fleet.

    fm#1566's deployment-shape floor closes it: the resolution is the strictest
    of what the key asks for and what the shape implies, so cloud wins.
    """
    monkeypatch.setenv("PROTECTION_PROFILE", "development")

    app, info = _install(Environment.DEVELOPMENT, is_cloud_deployment=True)

    assert info["protection_profile"] == "cloud"
    assert _resolved_settings(app).protection_bypass_headers == []
    assert _resolved_settings(app).fail_open_on_redis_error is False


def test_an_unknown_environment_gets_production_not_a_permissive_branch():
    """The near-miss: a name nobody anticipated must fail *safe*.

    An unrecognised ``ENVIRONMENT`` is a misconfiguration, and the safe reading
    of a misconfiguration is "this might be production".
    """
    app, setup_info = _install(UNKNOWN_ENVIRONMENT)

    assert setup_info["settings_source"] == "production_defaults"
    settings = _resolved_settings(app)
    assert settings.protection_bypass_headers == []
    assert settings.rate_limits["global"].requests == 500
    assert _installed(app) == {RateLimitMiddleware, DeduplicationMiddleware}


def test_the_default_environment_argument_is_production():
    """A caller that omits ``environment`` gets the strict preset.

    The default used to be ``development``, so an omission silently loosened
    every limit and enabled the bypass headers.
    """
    app, setup_info = _install()

    assert setup_info["settings_source"] == "production_defaults"
    assert _resolved_settings(app).protection_bypass_headers == []
    assert _resolved_settings(app).rate_limits["global"].requests == 500
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
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("preset exploded")),
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
