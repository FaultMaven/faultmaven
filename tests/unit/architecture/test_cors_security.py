"""CORS is decided by ONE question: is this a deployed environment? (fm#985 item 17)

Both CORS branches in ``main.setup_middleware`` used to test
``== Environment.PRODUCTION`` — the "only production is special" pattern
fm#1023 removed from protection routing and left standing here. ``Environment``
has a third member, so a box on ``ENVIRONMENT=staging`` ran production's strict
fail-closed rate limits **and** development's CORS at the same time: a wildcard
``CORS_ALLOW_ORIGINS`` accepted, ``localhost`` origins appended, and the RFC1918
``allow_origin_regex`` installed with ``allow_credentials`` on — so any host on
any private network could make credentialed cross-origin calls to a deployed
box. Staging is now classified as deployed, the same as production.

**These tests drive ``main.setup_middleware`` and read the CORS middleware off
the resulting stack.** The file they replace re-implemented the wildcard check
as a local helper and asserted against *that*, so it would have stayed green
through any change to ``main.py`` — including the one this file exists to pin.
A guard that never touches the code it guards is not a guard.

Three properties:

1. **Coverage.** Every ``Environment`` member is swept, not enumerated, so a
   fourth member added later is checked the day it appears — and it is
   *deployed* until someone says otherwise, because the predicate is written
   as "not development" rather than "in (staging, production)".
2. **The refusal.** A wildcard origin on a deployed environment refuses the
   boot and names every offending origin. The shipped default
   ``CORS_ALLOW_ORIGINS`` contains two, so this is the ordinary path for a
   deployed box that configures nothing.
3. **The policy actually installed.** Deployed gets strict origins with no
   ``allow_origin_regex`` and no appended localhost; development gets both.
"""

import re
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

import faultmaven.main as main
from faultmaven.config.settings import Environment, get_settings

pytestmark = [pytest.mark.unit, pytest.mark.security, pytest.mark.architecture]

DEPLOYED = [e for e in Environment if e is not Environment.DEVELOPMENT]
ALL_ENVIRONMENTS = list(Environment)


def _settings_with(environment, origins):
    """A real settings object, so nothing here can disagree with the app's shape."""
    settings = get_settings().model_copy(deep=True)
    settings.server.environment = environment
    settings.security.cors_allow_origins = list(origins)
    return settings


def _install(environment, origins):
    """Run the real ``setup_middleware`` against a throwaway app."""
    app = FastAPI()
    settings = _settings_with(environment, origins)
    with (
        patch.object(main, "app", app),
        patch("faultmaven.config.settings.get_settings", return_value=settings),
    ):
        main.setup_middleware()
    return app


def _cors(app):
    for middleware in app.user_middleware:
        if middleware.cls is CORSMiddleware:
            return middleware.kwargs
    raise AssertionError("CORSMiddleware was never installed")


@pytest.mark.parametrize("environment", DEPLOYED, ids=[e.value for e in DEPLOYED])
def test_a_wildcard_origin_refuses_the_boot_on_every_deployed_environment(environment):
    """``staging`` is the member this used to let through."""
    with pytest.raises(RuntimeError) as excinfo:
        _install(environment, ["http://localhost:3333", "chrome-extension://*"])

    message = str(excinfo.value)
    assert "SECURITY ERROR" in message
    assert "chrome-extension://*" in message
    # The environment is named, and named by its VALUE — a bare str() of the
    # enum member would put "Environment.STAGING" in front of an operator
    # (#827), in the one message that tells them what to fix.
    assert f"ENVIRONMENT={environment.value}" in message
    assert "Environment." not in message


@pytest.mark.parametrize("environment", DEPLOYED, ids=[e.value for e in DEPLOYED])
def test_every_offending_origin_is_named(environment):
    with pytest.raises(RuntimeError) as excinfo:
        _install(
            environment,
            ["chrome-extension://*", "moz-extension://*", "https://faultmaven.ai"],
        )

    message = str(excinfo.value)
    assert "chrome-extension://*" in message
    assert "moz-extension://*" in message


@pytest.mark.parametrize("environment", DEPLOYED, ids=[e.value for e in DEPLOYED])
def test_the_shipped_default_origins_refuse_a_deployed_boot(environment):
    """Not a hypothetical input: this is what a deployed box that sets nothing has.

    ``SecuritySettings.cors_allow_origins`` defaults to a list carrying
    ``chrome-extension://*`` and ``moz-extension://*``, so the refusal is the
    ordinary path rather than an edge case, and a deployed overlay must supply
    concrete origins. The staging overlay in ``faultmaven-enterprise-infra``
    already does (``https://app.staging.faultmaven.ai``).
    """
    shipped_default = get_settings().__class__().security.cors_allow_origins
    assert any("://*" in origin for origin in shipped_default), (
        "this test is only meaningful while the shipped default carries a "
        f"wildcard; it now reads {shipped_default}"
    )

    with pytest.raises(RuntimeError, match="SECURITY ERROR"):
        _install(environment, shipped_default)


@pytest.mark.parametrize("environment", DEPLOYED, ids=[e.value for e in DEPLOYED])
def test_a_deployed_environment_installs_no_private_network_regex(environment):
    """The other half of item 17, and the half a wildcard check cannot catch.

    ``allow_origin_regex`` admitted every RFC1918 address with
    ``allow_credentials`` on, so a deployed box was reachable cross-origin from
    anything on its private network whatever ``CORS_ALLOW_ORIGINS`` said. No
    origin list configures that away; only the branch does.
    """
    app = _install(environment, ["https://app.staging.faultmaven.ai"])
    kwargs = _cors(app)

    assert kwargs.get("allow_origin_regex") is None
    assert kwargs["allow_origins"] == [
        "https://app.staging.faultmaven.ai",
        "https://faultmaven.ai",
    ], "a deployed environment appended development origins"
    assert not any("localhost" in origin for origin in kwargs["allow_origins"])


def test_development_keeps_the_local_network_affordance():
    """The behaviour this must NOT take away from a contributor's checkout."""
    app = _install(Environment.DEVELOPMENT, ["chrome-extension://*"])
    kwargs = _cors(app)

    regex = kwargs.get("allow_origin_regex")
    assert regex, "development lost its private-network CORS regex"
    for reachable in (
        "http://192.168.1.50:3333",
        "http://10.0.0.7",
        "http://172.16.4.1:5173",
        "http://localhost:3333",
    ):
        assert re.match(regex, reachable), f"{reachable} no longer matches"

    assert "http://localhost:3333" in kwargs["allow_origins"]
    assert "http://localhost:5173" in kwargs["allow_origins"]
    # And the wildcard that refuses a deployed boot is accepted here.
    assert "chrome-extension://*" in kwargs["allow_origins"]


@pytest.mark.parametrize(
    "environment", ALL_ENVIRONMENTS, ids=[e.value for e in ALL_ENVIRONMENTS]
)
def test_exactly_one_environment_gets_the_permissive_branch(environment):
    """Swept rather than enumerated, so a fourth member is covered on arrival.

    Phrased against ``Environment.DEVELOPMENT`` by identity rather than against
    a list of the deployed members, so adding a member cannot silently widen
    the permissive side: a new member has no regex until someone changes this
    assertion on purpose.
    """
    app = _install(environment, ["https://example.invalid"])
    permissive = _cors(app).get("allow_origin_regex") is not None

    assert permissive is (
        environment is Environment.DEVELOPMENT
    ), f"{environment.value} is on the wrong side of the CORS split"
