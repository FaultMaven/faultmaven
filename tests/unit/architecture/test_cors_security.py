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
from faultmaven.config.settings import FaultMavenSettings as SettingsClass

pytestmark = [pytest.mark.unit, pytest.mark.security, pytest.mark.architecture]

DEPLOYED = [e for e in Environment if e is not Environment.DEVELOPMENT]
ALL_ENVIRONMENTS = list(Environment)


def _settings_with(environment, origins, *, is_cloud=False):
    """A real settings object, so nothing here can disagree with the app's shape."""
    settings = get_settings().model_copy(deep=True)
    settings.server.environment = environment
    settings.security.cors_allow_origins = list(origins)
    # ``is_cloud`` is a read-only property on the real class, so the deployment
    # mode is set through the field it is derived from rather than stubbed —
    # otherwise the test would be asserting against its own stub instead of
    # against ADR-004's predicate.
    settings.deployment_mode = "cloud" if is_cloud else "standalone"
    assert settings.is_cloud is is_cloud
    return settings


def _install(environment, origins, *, is_cloud=False):
    """Run the real ``setup_middleware`` against a throwaway app."""
    app = FastAPI()
    settings = _settings_with(environment, origins, is_cloud=is_cloud)
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
    # ``_env_file=None`` — the idiom ``test_deployment_coherence`` uses — so
    # this reads the SHIPPED default and not a developer's configuration.
    #
    # Measured, because it was raised as a live defect and is not one today:
    # with a ``.env`` pinning concrete origins, and again with
    # ``CORS_ALLOW_ORIGINS`` exported in the shell, all 12 tests here pass with
    # or without this argument. Two things have to both hold for that, and
    # neither belongs to this file: ``SecuritySettings.model_config`` carries
    # no ``env_file`` (so the nested model never reads the file, only
    # ``os.environ``), and ``tests/conftest.py``'s autouse isolation fixture
    # pops ``CORS_ALLOW_ORIGINS`` out of ``os.environ`` before every test —
    # which is also what neutralises ``main.py``'s import-time
    # ``load_dotenv()``. Written this way anyway: an assertion about the
    # shipped default should not be a hostage to a conftest three directories
    # up continuing to list this one key.
    shipped_default = SettingsClass(_env_file=None).security.cors_allow_origins
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
def test_a_cloud_deployment_is_deployed_for_CORS_whatever_it_names_its_environment(
    environment,
):
    """The shape the environment name alone leaves on the development branch.

    ``DEPLOYMENT_MODE=cloud`` with ``ENVIRONMENT=development`` is reachable —
    ``config.deployment_coherence`` relates the deployment mode to auth,
    storage and tenancy and never to the environment name, and
    ``test_protection_environment_routing`` has a sibling test that exists
    precisely because a fleet can name any environment it likes. On that
    fleet the first version of this fix skipped the wildcard fail-fast,
    accepted the shipped ``chrome-extension://*`` defaults, appended localhost
    and installed the RFC1918 ``allow_origin_regex`` with
    ``allow_credentials`` on: any host on any private network could then make
    credentialed cross-origin calls to a MULTI-TENANT fleet.

    So the predicate is *not development, **or** cloud*, and it is swept over
    every environment here rather than asserted for the one interesting value.
    """
    app = _install(environment, ["https://app.example.test"], is_cloud=True)
    kwargs = _cors(app)

    assert kwargs.get("allow_origin_regex") is None, (
        f"a cloud deployment naming ENVIRONMENT={environment.value} installed "
        f"the private-network CORS regex with credentials on"
    )
    assert not any("localhost" in origin for origin in kwargs["allow_origins"])


def test_a_cloud_deployment_naming_development_refuses_a_wildcard_origin():
    """The other half: the fail-fast must reach that fleet too.

    Without it the shipped default ``CORS_ALLOW_ORIGINS`` — which carries two
    wildcards — is accepted on a multi-tenant deployment.
    """
    with pytest.raises(RuntimeError, match="SECURITY ERROR"):
        _install(
            Environment.DEVELOPMENT,
            SettingsClass(_env_file=None).security.cors_allow_origins,
            is_cloud=True,
        )


def test_the_two_callers_of_the_deployed_predicate_cannot_disagree():
    """One question, one spelling — measured across both consumers.

    ``main.setup_middleware`` (CORS, and the protection carve-out) and
    ``api.protection.setup_protection_middleware`` (the setup-failure refusal,
    and the empty-trusted-proxies warning) both turn on "is this a deployed
    box". They ask it of the same function; this pins that they get the same
    answer for every shape, so a future edit that inlines the expression at
    one of them re-creates fm#985 item 17 loudly rather than quietly.

    The observable at each caller is the thing that caller decides, not the
    predicate re-read: the CORS regex on one side, the setup-failure refusal
    on the other.
    """
    from faultmaven.config.protection import is_deployed_environment

    shapes = [(env, cloud) for env in ALL_ENVIRONMENTS for cloud in (False, True)]

    for environment, is_cloud in shapes:
        expected = is_deployed_environment(environment, is_cloud_deployment=is_cloud)

        cors_says_deployed = (
            _cors(
                _install(environment, ["https://app.example.test"], is_cloud=is_cloud)
            ).get("allow_origin_regex")
            is None
        )
        assert cors_says_deployed is expected, (
            f"CORS disagrees for ENVIRONMENT={environment.value}, "
            f"is_cloud={is_cloud}"
        )

        protection_says_deployed = _protection_refuses_a_setup_failure(
            environment, is_cloud
        )
        assert protection_says_deployed is expected, (
            f"the protection setup-failure refusal disagrees for "
            f"ENVIRONMENT={environment.value}, is_cloud={is_cloud}"
        )


def _protection_refuses_a_setup_failure(environment, is_cloud) -> bool:
    """Does ``setup_protection_middleware`` raise, or boot unprotected?

    Provoked at the real surface: Starlette refuses ``add_middleware`` once an
    application has started.
    """
    from fastapi.testclient import TestClient

    from faultmaven.api.protection import setup_protection_middleware
    from faultmaven.config.protection import get_production_protection_settings

    app = FastAPI()
    settings = get_production_protection_settings(fail_open_on_redis_error=True)
    with TestClient(app):
        try:
            setup_protection_middleware(
                app,
                settings=settings,
                environment=environment,
                is_cloud_deployment=is_cloud,
            )
        except RuntimeError:
            return True
    return False


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
