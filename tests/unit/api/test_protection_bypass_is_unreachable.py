"""A bypass header cannot switch off a standalone deployment's limiter (fm#985 item 15).

``RateLimitMiddleware._should_bypass`` keys on header **presence**: any request
carrying a name from ``ProtectionSettings.protection_bypass_headers`` skips rate
limiting entirely, no value required and no authentication involved. The
development preset arms two of them, ``X-Dev-Bypass`` and ``X-Test-Bypass``.

Until this change the preset was chosen by ``ENVIRONMENT``, which is **unset**
on the standalone quickstart — the path every self-hosted operator follows — and
falls to the settings default ``development``. So the shipped self-hosted
posture was an opt-out rate limiter.

The correction is that ``development`` and ``standalone`` are different
questions. ``development`` describes who is editing the code; ``standalone``
describes how the product is deployed. One value cannot answer both, so the
preset is now chosen by its own axis, ``PROTECTION_PROFILE``
(``config/protection.resolve_protection_profile``), which defaults to
``hardened``.

This module pins the property that matters — *un-preset is not the same as
unreachable* — at four layers, each with its own observable so that no one of
them can make another inert:

1. **Selection.** The profile, not the environment, picks the preset, and the
   default is the hardened one.
2. **The veto is monotone.** ``ENVIRONMENT`` can refuse a development profile;
   it can never select one. No pair of values is looser than the profile alone.
3. **The choke point.** Bypass headers are stripped from *whatever* settings are
   installed — a caller's own object included — unless the profile is
   ``development``.
4. **Behaviour, both columns.** A request carrying ``X-Dev-Bypass`` is refused
   over the limit on the default posture, and is *not* refused under an explicit
   development profile. The second column is the positive control: without it a
   test asserting "the header did not help" would also pass against a header
   name that never meant anything.

And one scan, because layers 3 and 4 are only worth as much as the claim that
every installation goes through them: ``api/protection.py`` is the single place
in the application that installs ``RateLimitMiddleware``, and
``protection_bypass_headers`` has a single consumer.
"""

import ast
import contextlib
import itertools
import pathlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.middleware import RateLimitMiddleware
from faultmaven.api.protection import setup_protection_middleware
from faultmaven.config.protection import (
    PROTECTION_PROFILE_ENV_VAR,
    ProtectionProfile,
    get_development_protection_settings,
    resolve_protection_profile,
)
from faultmaven.config.settings import Environment
from faultmaven.models.protection import ProtectionSettings, RateLimitConfig

pytestmark = [pytest.mark.unit, pytest.mark.security]

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
PACKAGE_ROOT = REPO_ROOT / "faultmaven"

# The headers the permissive preset arms. Read from the preset rather than
# spelled here: a test that hard-codes a header name keeps passing after the
# preset renames one, while asserting nothing about the name that is now live.
BYPASS_HEADERS = get_development_protection_settings().protection_bypass_headers

# Distinct client addresses hand each test its own ``global`` window, as in
# ``tests/integration/api/test_rate_limit_wire_refusal.py``.
_ADDRESS = (f"198.51.100.{n}" for n in itertools.count(120))


# --------------------------------------------------------------------------- #
# 1 + 2. Selection and the veto
# --------------------------------------------------------------------------- #


def test_the_default_profile_is_hardened(monkeypatch):
    """Nothing set is the quickstart, and the quickstart must be the safe one."""
    monkeypatch.delenv(PROTECTION_PROFILE_ENV_VAR, raising=False)

    assert (
        resolve_protection_profile(Environment.DEVELOPMENT)
        is ProtectionProfile.HARDENED
    )


@pytest.mark.parametrize(
    "environment",
    [Environment.DEVELOPMENT, "development", Environment.STAGING, "weird-env", None],
    ids=["dev_member", "dev_string", "staging", "unknown", "unnamed"],
)
def test_no_environment_selects_the_permissive_preset_on_its_own(
    monkeypatch, environment
):
    """The defect, stated as a property: ``ENVIRONMENT`` cannot loosen anything.

    ``development`` is in the list for the obvious reason, and so are the rest:
    the fix must not have moved which single value arms the headers.
    """
    monkeypatch.delenv(PROTECTION_PROFILE_ENV_VAR, raising=False)

    assert resolve_protection_profile(environment) is ProtectionProfile.HARDENED


@pytest.mark.parametrize(
    "environment",
    [Environment.DEVELOPMENT, "development", "DEVELOPMENT ", None],
    ids=["member", "string", "untidy_string", "unnamed"],
)
def test_the_development_profile_is_honoured_only_on_a_development_environment(
    monkeypatch, environment
):
    """A contributor's checkout keeps the headers; an unnamed caller does not.

    ``None`` is in here as the fail-safe leg: ``setup_protection_middleware``
    defaults its own ``environment`` parameter to production, and a caller that
    named no environment at all is treated the same way — as a deployment.
    """
    monkeypatch.setenv(PROTECTION_PROFILE_ENV_VAR, "development")

    expected = (
        ProtectionProfile.DEVELOPMENT
        if str(getattr(environment, "value", environment) or "").strip().lower()
        == "development"
        else ProtectionProfile.HARDENED
    )
    assert resolve_protection_profile(environment) is expected


@pytest.mark.parametrize("environment", [Environment.STAGING, Environment.PRODUCTION])
def test_a_deployed_environment_vetoes_the_development_profile(
    monkeypatch, caplog, environment
):
    """The relation is monotone: both keys can harden, neither can loosen.

    Without this a deployment could arm the bypass headers on a box explicitly
    named ``production`` — which is not possible today and must not become
    possible in exchange for fixing the standalone default.
    """
    monkeypatch.setenv(PROTECTION_PROFILE_ENV_VAR, "development")

    with caplog.at_level("ERROR", logger="faultmaven.config.protection"):
        profile = resolve_protection_profile(environment)

    assert profile is ProtectionProfile.HARDENED
    assert "PROTECTION_PROFILE" in caplog.text, (
        "the refusal was silent; an operator whose bypass headers stopped "
        "working has nothing to read"
    )


@pytest.mark.parametrize(
    "value", ["", "   ", "develpoment", "hardened ", "HARDENED", "true", "1"]
)
def test_an_unrecognised_profile_fails_safe(monkeypatch, value):
    """A typo is a misconfiguration, and a misconfiguration might be a deployment.

    ``hardened`` with surrounding space or in capitals is *recognised* — the
    value is normalised — so those two legs assert the normalisation rather
    than the fallback. Either way the answer is the same, which is the point.
    """
    monkeypatch.setenv(PROTECTION_PROFILE_ENV_VAR, value)

    assert (
        resolve_protection_profile(Environment.DEVELOPMENT)
        is ProtectionProfile.HARDENED
    )


# --------------------------------------------------------------------------- #
# 3. The choke point
# --------------------------------------------------------------------------- #


def _installed_settings(app) -> ProtectionSettings:
    for middleware in app.user_middleware:
        if middleware.cls is RateLimitMiddleware:
            return middleware.kwargs["settings"]
    raise AssertionError("RateLimitMiddleware was never installed")


def test_caller_supplied_bypass_headers_are_disarmed(monkeypatch):
    """Selection alone would only have moved the default.

    ``setup_protection_middleware`` accepts a ``ProtectionSettings`` of the
    caller's own, and that object bypasses preset selection entirely. If the
    fix lived only in the preset choice, anything handing in settings would
    still arm the headers — un-preset rather than unreachable.
    """
    monkeypatch.delenv(PROTECTION_PROFILE_ENV_VAR, raising=False)
    app = FastAPI()

    setup_info = setup_protection_middleware(
        app,
        settings=ProtectionSettings(protection_bypass_headers=["X-Sneaky-Bypass"]),
        environment=Environment.DEVELOPMENT,
    )

    assert _installed_settings(app).protection_bypass_headers == []
    assert setup_info["bypass_headers_disarmed"] == ["X-Sneaky-Bypass"], (
        "the disarm left no trace in setup_info, so an operator reading the "
        "boot record cannot tell it happened"
    )
    assert setup_info["bypass_headers"] == []


def test_the_disarm_does_not_mutate_the_callers_settings(monkeypatch):
    """The caller's object is theirs; disarming it in place is a side effect."""
    monkeypatch.delenv(PROTECTION_PROFILE_ENV_VAR, raising=False)
    supplied = ProtectionSettings(protection_bypass_headers=["X-Sneaky-Bypass"])

    setup_protection_middleware(
        FastAPI(), settings=supplied, environment=Environment.DEVELOPMENT
    )

    assert supplied.protection_bypass_headers == ["X-Sneaky-Bypass"]


def test_a_declared_development_checkout_keeps_its_headers(monkeypatch):
    """The carve-out the ruling allows, asserted so it is not lost by accident.

    Without this the whole change could be satisfied by deleting the bypass
    headers outright, which is a different decision from the one taken.
    """
    monkeypatch.setenv(PROTECTION_PROFILE_ENV_VAR, "development")
    app = FastAPI()

    setup_info = setup_protection_middleware(app, environment=Environment.DEVELOPMENT)

    assert setup_info["settings_source"] == "development_defaults"
    assert _installed_settings(app).protection_bypass_headers == BYPASS_HEADERS
    assert "bypass_headers_disarmed" not in setup_info


# --------------------------------------------------------------------------- #
# 4. Behaviour, both columns
# --------------------------------------------------------------------------- #


def _live_middleware(app) -> RateLimitMiddleware:
    """The built ``RateLimitMiddleware`` object, not the registration record."""
    node = app.middleware_stack
    for _ in range(64):
        if node is None:
            break
        if isinstance(node, RateLimitMiddleware):
            return node
        node = getattr(node, "app", None)
    raise AssertionError("no RateLimitMiddleware in the built middleware stack")


@contextlib.contextmanager
def _serving(environment=Environment.DEVELOPMENT, limit: int = 3):
    """A scratch app carrying the protection stack, with a tight global bucket.

    Built through ``setup_protection_middleware`` rather than by constructing
    the middleware directly: the question here is what a *deployment* installs,
    and a hand-built middleware would answer a different one.

    A FakeRedis is put on ``app.state`` and adopted by the middleware's own
    initialisation, for the reason
    ``tests/integration/api/test_rate_limit_wire_refusal.py`` gives at length —
    the process-wide stand-in binds its queue to the first event loop that
    touches it, and ``TestClient`` runs a new loop per context.
    """
    import fakeredis.aioredis as fakeredis_aio

    app = FastAPI()

    @app.get("/probe")
    async def probe():  # pragma: no cover - body is irrelevant to the assertion
        return {"ok": True}

    setup_protection_middleware(app, environment=environment)

    with TestClient(app, client=(next(_ADDRESS), 51000)) as client:
        middleware = _live_middleware(app)
        app.state.redis_client = fakeredis_aio.FakeRedis(decode_responses=True)
        middleware._initialized = False
        middleware._degraded = False
        middleware._last_attempt_at = None

        limiter = middleware.rate_limiter
        original = dict(limiter._configs)
        tightened = dict(original)
        tightened["global"] = RateLimitConfig(enabled=True, requests=limit, window=60)
        limiter.configure_limits(tightened)
        try:
            yield client, middleware
        finally:
            limiter.configure_limits(original)


@pytest.mark.parametrize("header", BYPASS_HEADERS)
def test_a_bypass_header_is_still_rate_limited_on_the_default_posture(
    monkeypatch, header
):
    """The behaviour assertion the whole item is about.

    Deliberately not "the preset is production" — that is a name, and a name
    can be right while the header still works (a second install site, a caller
    with its own settings, a consumer nobody enumerated). This drives the
    middleware stack a deployment serves and asks whether the header helped.
    """
    monkeypatch.delenv(PROTECTION_PROFILE_ENV_VAR, raising=False)
    limit = 3

    with _serving(limit=limit) as (client, middleware):
        errors_before = middleware.metrics["errors"]
        responses = [
            client.get("/probe", headers={header: "1"}) for _ in range(limit + 2)
        ]

    codes = [r.status_code for r in responses]
    assert (
        codes[:limit] == [200] * limit
    ), f"a request inside the limit was refused: {codes}"
    assert codes[limit:] == [429, 429], (
        f"{header} skipped the limiter on a deployment that never asked for "
        f"it: {codes}"
    )
    assert middleware.metrics["errors"] == errors_before, (
        "the limiter swallowed an exception during the run, so the refusals "
        "above cannot be attributed to the limit"
    )
    assert middleware.rate_limiter.is_degraded is False


@pytest.mark.parametrize("header", BYPASS_HEADERS)
def test_the_same_header_does_bypass_a_declared_development_checkout(
    monkeypatch, header
):
    """The positive control, and the mutation column for the test above.

    Without it, the assertion that ``X-Dev-Bypass`` "did not help" would pass
    just as well against a header name that means nothing anywhere — the shape
    where a failed probe reads as a pass. Here the same name, the same route
    and the same limit are served past the limit, so the header demonstrably
    does something, and the previous test shows the default posture does not
    let it.
    """
    monkeypatch.setenv(PROTECTION_PROFILE_ENV_VAR, "development")
    limit = 3

    with _serving(limit=limit) as (client, _middleware):
        codes = [
            client.get("/probe", headers={header: "1"}).status_code
            for _ in range(limit + 2)
        ]

    assert codes == [200] * (limit + 2), (
        f"{header} did not bypass the limiter even where it is armed, so the "
        f"test above proves nothing about the header: {codes}"
    )


# --------------------------------------------------------------------------- #
# The scan the layers above rest on
# --------------------------------------------------------------------------- #


def _production_modules():
    """Every ``.py`` under ``faultmaven/`` — the whole application, not a subtree.

    Named as a generator with a positive-control assertion at each call site,
    because a guard that walks the wrong directory is green for the same reason
    a correct one is.
    """
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def _relative(path: pathlib.Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def test_one_module_installs_the_rate_limiter():
    """The choke point is only a choke point if nothing else installs one.

    Finds every module that passes ``RateLimitMiddleware`` to ``add_middleware``
    or ``Middleware(...)``, or constructs it directly. ``api/protection.py`` is
    the expected answer and is asserted as a positive control: a scan that finds
    nothing at all has usually stopped working rather than proved a property.
    """
    modules = _production_modules()
    assert len(modules) > 100, f"the scan walked {len(modules)} files; wrong root?"

    installers = set()
    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = node.func
            names = []
            if isinstance(callee, ast.Attribute) and callee.attr == "add_middleware":
                names = node.args[:1]
            elif isinstance(callee, ast.Name) and callee.id == "Middleware":
                names = node.args[:1]
            elif isinstance(callee, ast.Name) and callee.id == "RateLimitMiddleware":
                installers.add(_relative(path))
            for arg in names:
                if isinstance(arg, ast.Name) and arg.id == "RateLimitMiddleware":
                    installers.add(_relative(path))
                if isinstance(arg, ast.Attribute) and arg.attr == "RateLimitMiddleware":
                    installers.add(_relative(path))

    assert installers == {"faultmaven/api/protection.py"}, (
        "the rate limiter is installed somewhere that does not pass through "
        "the bypass-header choke point in setup_protection_middleware: "
        f"{sorted(installers)}"
    )


def test_the_bypass_header_list_has_one_consumer():
    """Who *reads* the list decides what a bypass can switch off.

    The disarm covers rate limiting because rate limiting is the only thing
    that consults the list. A second consumer — deduplication growing a
    header skip, say — would be a second bypass surface reached by the same
    header names, and this fails the day one appears.
    """
    modules = _production_modules()
    assert len(modules) > 100, f"the scan walked {len(modules)} files; wrong root?"

    seen = set()
    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "protection_bypass_headers"
            ) or (
                isinstance(node, ast.keyword)
                and node.arg == "protection_bypass_headers"
            ):
                seen.add(_relative(path))
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id == "protection_bypass_headers":
                    seen.add(_relative(path))

    assert seen == {
        # Declares the field.
        "faultmaven/models/protection.py",
        # Produces it: the two presets.
        "faultmaven/config/protection.py",
        # Disarms it: the install choke point.
        "faultmaven/api/protection.py",
        # Consumes it: ``_should_bypass``, the one place presence is checked.
        "faultmaven/api/middleware/rate_limiting.py",
    }, f"the bypass header list grew a producer or a consumer: {sorted(seen)}"
