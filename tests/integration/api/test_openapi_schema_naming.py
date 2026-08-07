"""The OpenAPI component namespace must be free of short-name collisions.

Two distinct classes sharing a short name make the generated spec depend on
registration order: Pydantic keeps one under ``ClassName`` and demotes the
other to ``dotted.module.path__ClassName``, and *which* one is demoted varies
between processes. That is not a cosmetic problem —

- ``app.openapi()`` stops being reproducible, so any CI gate that compares the
  live spec against a checked-in artifact fails on roughly half of its runs
  regardless of whether anything actually drifted (fm#880);
- the TypeScript clients generated from the spec inherit the coin flip. The
  copilot and dashboard clients were generated from the same source on
  different machines and disagreed about which class owns the short name
  ``VerificationStatus``.

So this is the precondition for a contract-drift gate, and the invariant that
keeps generated clients reproducible.

The schema is built in a **subprocess under a pinned environment** rather than
read from the shared ``faultmaven.main.app`` singleton. Several integration
modules (``tests/integration/modules/auth/test_oauth_*.py``) set
``OAUTH_ENABLED`` and drop ``faultmaven.main`` from ``sys.modules`` at *import*
time, and pytest imports every test module during collection — so the singleton
this file would otherwise inspect depends on which modules were collected. A
gate whose whole purpose is reproducibility must not itself be order-dependent:
CI would evaluate the OAuth-mounted app and a developer reproducing the failure
locally would evaluate a different one.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]

# The widest set of routers, so the component namespace under test is a
# superset of every deployment's. A collision reachable in any configuration is
# therefore reachable here.
PINNED_ENVIRONMENT = {
    "SKIP_SERVICE_CHECKS": "true",
    "OAUTH_ENABLED": "true",
    "AUTH_MODE": "oauth",
    "WORKOS_API_KEY": "placeholder-not-a-credential",
    "WORKOS_CLIENT_ID": "placeholder-not-a-credential",
    "WORKOS_REDIRECT_URI": "https://app.faultmaven.com/auth/sso/callback",
}

# Kept from the caller's environment; everything else is dropped rather than
# enumerated, so a setting nobody thought to pin cannot change which routers
# mount and therefore which components exist.
_SYSTEM_ENVIRONMENT_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "SYSTEMROOT",
    }
)

# Emits the two facts the assertions need: the component names, and the modules
# loaded while building them (a collision alias is a module path, so resolving
# one needs the child's module table, not the parent's).
_PROBE = """
import json, sys
from faultmaven.main import app
schemas = app.openapi()["components"]["schemas"]
print("---PROBE---")
print(json.dumps({
    "schemas": sorted(schemas),
    "modules": sorted(sys.modules),
    "verification_status": schemas.get("VerificationStatus"),
    "solution_verification": schemas.get("SolutionVerificationData"),
}))
"""


@pytest.fixture(scope="module")
def probe():
    """Component names and loaded modules from a freshly built app."""
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=PROJECT_ROOT,
        env={
            **{k: v for k, v in os.environ.items() if k in _SYSTEM_ENVIRONMENT_KEYS},
            **PINNED_ENVIRONMENT,
        },
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert (
        result.returncode == 0
    ), f"could not build the app to inspect its schema:\n{result.stderr[-3000:]}"
    # The app logs to stdout while importing; the payload is the last line.
    payload = result.stdout.split("---PROBE---")[-1].strip()
    return json.loads(payload)


def module_qualified_aliases(names, loaded_modules):
    """Return the component names that exist only because a short name collided.

    Pydantic's fallback name is the class's dotted module path with ``.``
    replaced by ``__``, followed by the class name. That shape is recognisable
    without hardcoding a list of classes: split the name at each ``__``
    boundary and ask whether the prefix, read back as a dotted path, is a
    module that was loaded while the schema was built. It necessarily is — the
    class was registered from that module.

    FastAPI mints ``__`` in names of its own accord, for request bodies
    (``Body_submit_turn_api_v1_cases__case_id__turns_post``), but those come
    from path parameters rather than module paths, so no prefix of them
    resolves to a loaded module. Testing the property rather than excluding
    known-good names by pattern means a collision between any two classes is
    caught, including ones in third-party packages.
    """
    loaded = set(loaded_modules)
    aliases = {}
    for name in names:
        segments = name.split("__")
        # Take the longest prefix that resolves, not the first: ``faultmaven``
        # is itself a loaded module, so stopping early would report the package
        # root instead of the module that actually holds the colliding class.
        for split in range(len(segments) - 1, 0, -1):
            module = ".".join(segments[:split])
            if module in loaded:
                aliases[name] = module
                break
    return aliases


@pytest.mark.integration
@pytest.mark.slow
def test_no_component_name_collisions(probe):
    """No component may be demoted to a module-qualified name.

    An empty result means every schema is registered under its short name,
    which is what makes the spec reproducible across processes.
    """
    aliases = module_qualified_aliases(probe["schemas"], probe["modules"])

    assert not aliases, (
        "OpenAPI component names collided; Pydantic demoted these to "
        "module-qualified names, which makes app.openapi() depend on "
        "registration order:\n"
        + "\n".join(
            f"  {name}  (from {module})" for name, module in sorted(aliases.items())
        )
        + "\n\nTwo classes share a short name. Rename one of them — do not "
        "suppress this test: a demoted name is chosen non-deterministically, "
        "so the checked-in spec and every generated client become unstable."
    )


def verification_status_problems(
    schema_names, loaded_modules, verification_status, solution_verification
):
    """Return the reasons the fm#880 collision would be back; empty if it is not.

    Split out from the test so the rule can be exercised against synthetic
    namespaces as well as the real one — the subprocess probe can only show the
    namespace as it happens to be today, which cannot demonstrate that the rule
    still fires when a collision does occur.

    The condition is stated as "these two never collide", not "both are
    published". Since #982 retired the replay API, no route carries
    ``response_model=Case``, so the engine's ``VerificationStatus`` enum is no
    longer reachable from the published contract and does not appear in it. A
    name that is absent cannot collide — but it can come back, either directly
    or as a demoted alias the moment something re-exposes the Case graph, so
    both shapes are still checked.
    """
    problems = []

    aliases = {
        name: module
        for name, module in module_qualified_aliases(
            schema_names, loaded_modules
        ).items()
        if name.split("__")[-1] == "VerificationStatus"
    }
    if aliases:
        problems.append(
            "VerificationStatus was demoted to a module-qualified alias — the "
            "collision is back: "
            + ", ".join(
                f"{name} (from {module})" for name, module in sorted(aliases.items())
            )
        )

    if solution_verification is None:
        problems.append(
            "SolutionVerificationData is missing from the component namespace; "
            "it is published by the RESOLVED-phase response and should be there"
        )
    elif solution_verification.get("type") != "object":
        problems.append(
            "SolutionVerificationData should be an object; it is "
            f"{solution_verification.get('type')}"
        )

    if verification_status is not None and "enum" not in verification_status:
        problems.append(
            "VerificationStatus is published again but is not the domain enum "
            f"(type={verification_status.get('type')}) — something else has "
            "taken the short name"
        )

    return problems


@pytest.mark.integration
@pytest.mark.slow
def test_verification_status_classes_have_distinct_names(probe):
    """Pin the specific collision that motivated fm#880.

    ``modules.case.domain.models.VerificationStatus`` (the engine's assessment
    enum) and the RESOLVED-phase response model formerly also called
    ``VerificationStatus`` are unrelated types that happened to share a name.
    Checking *shape* — enum vs object — rather than mere presence is what
    distinguishes them being genuinely separate components from one of them
    having been demoted and the assertion following the alias.
    """
    problems = verification_status_problems(
        probe["schemas"],
        probe["modules"],
        probe["verification_status"],
        probe["solution_verification"],
    )

    assert not problems, "\n".join(problems)


def test_verification_status_rule_detects_a_returning_collision():
    """The rule above must still fail when the collision actually comes back.

    Pure and instant, like ``test_alias_detection_ignores_fastapi_generated_names``:
    it exercises the rule rather than the app, so the rule cannot quietly stop
    being able to fail just because today's namespace happens to satisfy it.
    """
    loaded = {"faultmaven", "faultmaven.models.case_ui"}
    enum = {"enum": ["verified", "refuted"]}
    obj = {"type": "object"}

    # Today's shape: the enum is unpublished, the object is present. Clean.
    assert (
        verification_status_problems(["SolutionVerificationData"], loaded, None, obj)
        == []
    )

    # Both published under their own short names is equally clean.
    assert (
        verification_status_problems(
            ["VerificationStatus", "SolutionVerificationData"], loaded, enum, obj
        )
        == []
    )

    # A demoted alias is the collision returning.
    assert verification_status_problems(
        ["faultmaven__models__case_ui__VerificationStatus", "SolutionVerificationData"],
        loaded,
        None,
        obj,
    )

    # The short name taken by something that is not the enum.
    assert verification_status_problems(
        ["VerificationStatus", "SolutionVerificationData"], loaded, obj, obj
    )

    # The published half going missing.
    assert verification_status_problems(["VerificationStatus"], loaded, enum, None)


def test_alias_detection_ignores_fastapi_generated_names():
    """The detector must not mistake FastAPI's own ``__`` names for collisions.

    Pure and instant — it exercises the rule itself rather than the app, so a
    change to the heuristic is caught without waiting on a subprocess.
    """
    loaded = {"faultmaven", "faultmaven.models.case_ui", "app.routers"}

    assert (
        module_qualified_aliases(
            ["Body_submit_turn_api_v1_cases__case_id__turns_post"], loaded
        )
        == {}
    )

    assert module_qualified_aliases(
        ["faultmaven__models__case_ui__VerificationStatus"], loaded
    ) == {
        "faultmaven__models__case_ui__VerificationStatus": "faultmaven.models.case_ui"
    }
