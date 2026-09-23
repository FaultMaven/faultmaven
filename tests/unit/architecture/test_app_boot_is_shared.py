"""Every `with TestClient(...)` in the suite is accounted for (fm#1569).

``faultmaven.main.app`` is a module-level singleton whose lifespan composes the
DI container, runs migrations and bootstraps the KB pack. Opening a
``with TestClient(app)`` runs that lifespan, and the suite used to run it once
per *test* rather than once per distinct configuration.

**The census that produced the number, kept runnable.** The issue counted
``grep -rn "with TestClient(" tests/`` and read the total as the population of
payers. It is not: most of those sites drive a scratch ``FastAPI()`` or
``Starlette()`` built in the test itself, whose lifespan is empty and costs
microseconds. So this guard pins **two** numbers — the whole population of
``with TestClient(`` sites, and the subset that boots the real application —
and it fails on a new site of either kind, because the two are told apart by a
resolver that can be fooled and a silent reclassification is exactly the
regression worth catching.

**What a new site has to do.** Add a line to ``EXPECTED`` naming the enclosing
function and its class:

``"real"``
    The site boots ``faultmaven.main.app``. Only legitimate where the LIFESPAN
    is the subject — a boot under a patched environment, or one asserted to
    refuse. Anything that merely needs *a* started application takes the
    ``booted_app_client`` fixture instead, which boots once per module.
``"scratch"``
    The site drives an application the test built itself. Free; listed so that
    a later edit pointing it at the real app is visible here.

A ``"real"`` site inside a module that also uses ``booted_app_client`` must
additionally take the ``unshared_app_boot`` fixture, which stands the shared
boot down for the duration — see ``test_an_unshared_boot_is_declared``. Two
lifespans live on one app object otherwise: the second re-composes
``app.state`` onto its own event loop, and its shutdown disposes the database
engine the first client is still serving from.
"""

from __future__ import annotations

import ast
import functools
import pathlib
import textwrap
import warnings

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.architecture]

TESTS_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: The exact census command the issue used, kept here so the number below and
#: the number a reader can reproduce from a shell are the same measurement.
CENSUS_COMMAND = 'grep -rn "with TestClient(" tests/ --include=*.py'

#: Sites that command finds, including the one inside a string literal.
#: It was **56** before fm#1569 (25 of them entering the real app's lifespan,
#: in six files, for 29 lifespans per run once the helpers called more than
#: once are counted).
EXPECTED_TOTAL_SITES = 39

#: Of those, the ones that enter the real application's lifespan. Was 25.
EXPECTED_REAL_APP_SITES = 8

#: The single sanctioned shared boot: ``_RealAppBoot.client`` in
#: ``tests/conftest.py`` calls ``TestClient.__enter__`` by hand rather than
#: through a ``with``, because the started client outlives the function that
#: started it. That makes it invisible to a scan for ``with`` statements, so
#: ``test_only_the_shared_broker_enters_a_lifespan_by_hand`` watches the other
#: spelling — otherwise the whole census could be sidestepped by writing
#: ``TestClient(app).__enter__()``.
MANUAL_LIFESPAN_ENTRY = {"tests/conftest.py": 1}

#: Every site, keyed by file and by the function (or method) that holds it.
#: The value is ``(kind, count)``.
EXPECTED: dict[str, dict[str, tuple[str, int]]] = {
    # -- boots the real app; the lifespan is the subject ---------------------
    "tests/infrastructure/test_pseudonym_key.py": {
        # The boot must REFUSE: a patched resolver raises, and the assertion is
        # that the failure reaches the caller rather than being logged.
        "TestTheStartupGate.test_an_unresolvable_key_stops_the_boot": ("real", 1),
    },
    "tests/integration/api/test_rate_limit_wire_refusal.py": {
        # Each case needs its own source address (a ``TestClient`` constructor
        # argument) and a FakeRedis bound to that client's own event loop, which
        # is what the helper's docstring is about. A shared boot has one of
        # each, so the three cases cannot be served from it.
        "_serving_with_global_limit": ("real", 1),
    },
    "tests/integration/test_main_app.py": {
        # Boots under a patched environment (`patch.dict(..., clear=True)`), so
        # the lifespan it runs is not the shared one.
        "test_application_uses_configuration_defaults": ("real", 1),
        "test_application_startup_with_invalid_configuration": ("real", 1),
        "TestPhase3MainApplicationValidation.test_feature_flags_integration_clean": (
            "real",
            1,
        ),
        # Named in fm#1569 as startup-subject tests: what they assert is that a
        # FRESH startup carries no migration machinery, which a borrowed boot
        # would no longer be evidence of.
        "TestPhase3MainApplicationValidation.test_application_startup_without_migration_overhead": (
            "real",
            1,
        ),
        "TestPhase3MainApplicationValidation.test_no_migration_configuration_references": (
            "real",
            1,
        ),
        # A scratch probe app built to carry one route.
        "test_multipart_form_field_limit_matches_max_upload_size": ("scratch", 1),
    },
    "tests/integration/test_readiness_and_redis.py": {
        # The only booting site in its module, so module-scoped sharing would
        # save nothing here — and converting it would move the boot relative to
        # the eight un-booted ``TestClient(app)`` calls around it.
        "test_health_reports_no_component_figure_it_did_not_measure": ("real", 1),
    },
    # -- drives an app the test built itself --------------------------------
    "tests/integration/api/test_no_unauthenticated_operations.py": {
        "test_a_gate_declared_after_a_service_parameter_is_not_a_gate": ("scratch", 1),
    },
    "tests/unit/api/middleware/test_composed_route_policy.py": {
        "test_an_undeclared_composed_mint_is_collapsed_to_a_409": ("scratch", 1),
        "test_a_declared_credential_mint_is_never_collapsed": ("scratch", 1),
        "test_the_exemption_does_not_widen_to_other_routes": ("scratch", 1),
        "test_the_exemption_holds_when_the_request_carries_a_trailing_slash": (
            "scratch",
            1,
        ),
        "test_declaring_a_post_route_does_not_exempt_other_methods_on_that_path": (
            "scratch",
            1,
        ),
        "test_the_legacy_idempotency_attribute_still_yields_both": ("scratch", 1),
        "test_the_cores_own_exemptions_are_unchanged": ("scratch", 1),
    },
    "tests/unit/api/middleware/test_deduplication_duplicate_response.py": {
        "harness": ("scratch", 1),
    },
    "tests/unit/api/middleware/test_logging_middleware_metrics.py": {
        "_logged_context": ("scratch", 1),
    },
    "tests/unit/api/middleware/test_rate_limit_cors_visibility.py": {
        "test_a_429_carries_the_cors_header": ("scratch", 1),
        "test_a_429_exposes_the_headers_it_sets": ("scratch", 1),
        "test_a_tripped_limit_does_not_refuse_the_preflight": ("scratch", 1),
        "test_the_fail_closed_503_carries_the_cors_header": ("scratch", 1),
        "test_the_outermost_position_is_read_the_way_starlette_stacks": ("scratch", 1),
    },
    "tests/unit/api/test_case_auto_titling.py": {
        "TestTurnEndpointNamesTheCase.client": ("scratch", 1),
    },
    "tests/unit/api/test_protection_bypass_is_unreachable.py": {
        "_serving": ("scratch", 1),
    },
    "tests/unit/api/test_protection_environment_routing.py": {
        "test_protection_enabled_is_not_reported_until_the_installs_land": (
            "scratch",
            1,
        ),
        "test_a_deployed_box_refuses_a_setup_failure_rather_than_serving_unprotected": (
            "scratch",
            1,
        ),
        "test_a_development_checkout_still_boots_unprotected_on_a_setup_failure": (
            "scratch",
            1,
        ),
    },
    "tests/unit/architecture/test_cors_security.py": {
        "_protection_refuses_a_setup_failure": ("scratch", 1),
    },
    "tests/unit/modules/auth/test_oauth_rate_limit_signalling.py": {
        "TestThroughTheFullMiddlewareStack.test_the_oauth_refusal_survives_the_middleware_on_the_way_out": (
            "scratch",
            1,
        ),
        "TestThroughTheFullMiddlewareStack.test_a_tighter_general_limit_still_wins": (
            "scratch",
            1,
        ),
        "TestThroughTheFullMiddlewareStack.test_an_unlimited_route_is_unaffected": (
            "scratch",
            1,
        ),
        "TestThroughTheFullMiddlewareStack.test_an_inner_writers_headers_on_a_200_are_left_alone": (
            "scratch",
            1,
        ),
        "TestThroughTheFullMiddlewareStack.test_a_refusal_carrying_no_limit_headers_is_still_left_alone": (
            "scratch",
            1,
        ),
        "TestThroughTheFullMiddlewareStack.test_the_served_response_advertises_the_oauth_limit_when_it_is_tightest": (
            "scratch",
            1,
        ),
        "TestTheOAuthLimiterNamesItsOwnBucket.test_a_refusal_names_the_endpoint_not_just_oauth": (
            "scratch",
            1,
        ),
        "TestTheOAuthLimiterNamesItsOwnBucket.test_an_allowed_request_publishes_the_same_token": (
            "scratch",
            1,
        ),
    },
}

#: Sites the text census finds that no AST walk can classify, because they are
#: inside a string literal rather than in the module's own code. Listed with
#: what they are, so the two counts reconcile without a fudge factor.
EXPECTED_SITES_IN_STRING_LITERALS = {
    # The child-process boot probe: a full lifespan in a subprocess with a clean
    # environment, which is the whole subject of that test and is unreachable
    # from any in-process fixture.
    "tests/integration/test_fresh_install_boots.py": 1,
}


#: This file is exempt from the TEXT census and from it alone. It discusses the
#: statement in prose and holds synthetic sources containing it, so counting its
#: occurrences would make the guard's own documentation move the number it
#: guards. It stays in the AST census, and that is what closes the hole: a real
#: ``with TestClient(...)`` written here would raise the AST count while leaving
#: the text count unchanged, which fails ``test_the_two_censuses_agree_on_the_total``.
_TEXT_CENSUS_EXEMPT = pathlib.Path(__file__).resolve()


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------


def _parse(source: str) -> ast.Module:
    """``ast.parse`` with the target file's own warnings kept out of this run.

    Several test modules carry invalid escape sequences in docstrings. Compiling
    them here would attribute a ``DeprecationWarning`` to this guard on every
    run, which is noise that belongs to the file being read.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        warnings.simplefilter("ignore", SyntaxWarning)
        return ast.parse(source)


def _dotted(node: ast.AST) -> str | None:
    """``a.b.c`` for a Name/Attribute chain, else None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def real_app_aliases(tree: ast.Module) -> set[str]:
    """Every expression in this module that denotes ``faultmaven.main.app``.

    Covers the four shapes the suite actually writes, plus fixture injection:

    * ``from faultmaven.main import app`` (and ``as`` something)
    * ``from faultmaven import main`` → ``main.app``
    * ``import faultmaven.main`` (and ``as`` something) → ``<alias>.app``
    * a function whose body returns one of the above — its NAME is then an alias
      too, because pytest injects it by name (``real_app`` is the live case).
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "faultmaven.main":
                for a in node.names:
                    if a.name == "app":
                        aliases.add(a.asname or a.name)
            elif node.module == "faultmaven":
                for a in node.names:
                    if a.name == "main":
                        aliases.add(f"{a.asname or a.name}.app")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "faultmaven.main":
                    aliases.add((a.asname or "faultmaven.main") + ".app")

    # Fixture injection: iterate to a fixed point so a fixture returning another
    # fixture's value is followed too.
    for _ in range(4):
        grown = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name in aliases:
                continue
            for inner in ast.walk(node):
                returned = None
                if isinstance(inner, ast.Return) and inner.value is not None:
                    returned = _dotted(inner.value)
                elif isinstance(inner, ast.Expr) and isinstance(inner.value, ast.Yield):
                    if inner.value.value is not None:
                        returned = _dotted(inner.value.value)
                if returned is not None and returned in aliases:
                    aliases.add(node.name)
                    grown = True
                    break
        if not grown:
            break
    return aliases


def client_class_aliases(tree: ast.Module) -> set[str]:
    """Every local name bound to ``TestClient``.

    The suite imports it plainly 64 times and aliases it nowhere, so this is a
    shape nobody writes today — but the callee check below is a name comparison,
    and one ``as`` would have made every site in that file invisible.
    """
    aliases = {"TestClient"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
            "testclient"
        ):
            for a in node.names:
                if a.name == "TestClient":
                    aliases.add(a.asname or a.name)
    return aliases


def _qualname_map(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def scan_source(source: str) -> list[tuple[str, str, int]]:
    """``(qualname, "real"|"scratch", lineno)`` for every ``with TestClient(...)``."""
    tree = _parse(source)
    aliases = real_app_aliases(tree)
    clients = client_class_aliases(tree)
    parents = _qualname_map(tree)

    def qualname(node: ast.AST) -> str:
        parts: list[str] = []
        cur = parents.get(node)
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                parts.append(cur.name)
            cur = parents.get(cur)
        return ".".join(reversed(parts)) or "<module>"

    found: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        for item in node.items:
            call = item.context_expr
            if not isinstance(call, ast.Call):
                continue
            if (_dotted(call.func) or "").split(".")[-1] not in clients:
                continue
            arg = call.args[0] if call.args else None
            target = _dotted(arg) if arg is not None else None
            kind = "real" if target in aliases else "scratch"
            found.append((qualname(node), kind, node.lineno))
    return found


@functools.lru_cache(maxsize=1)
def _test_files() -> tuple[pathlib.Path, ...]:
    """Cached: four tests below walk the same tree, and it is ~1300 files."""
    return tuple(sorted(TESTS_ROOT.rglob("*.py")))


@functools.lru_cache(maxsize=1)
def census() -> dict[str, dict[str, tuple[str, int]]]:
    """The live census, in the shape of ``EXPECTED``."""
    result: dict[str, dict[str, tuple[str, int]]] = {}
    for path in _test_files():
        source = path.read_text(encoding="utf-8")
        # Deliberately NOT ``"with TestClient(" in source``: the callee may be a
        # local alias, and keying the filter on the literal would skip the file
        # before the resolver ever saw it.
        if "TestClient" not in source:
            continue
        rel = path.relative_to(TESTS_ROOT.parent).as_posix()
        per_file: dict[str, tuple[str, int]] = {}
        for qualname, kind, _lineno in scan_source(source):
            previous = per_file.get(qualname)
            if previous is None:
                per_file[qualname] = (kind, 1)
            else:
                assert previous[0] == kind, (rel, qualname)
                per_file[qualname] = (kind, previous[1] + 1)
        if per_file:
            result[rel] = per_file
    return result


@functools.lru_cache(maxsize=1)
def text_census() -> dict[str, int]:
    """What ``CENSUS_COMMAND`` counts, per file."""
    counts: dict[str, int] = {}
    for path in _test_files():
        if path.resolve() == _TEXT_CENSUS_EXEMPT:
            continue
        source = path.read_text(encoding="utf-8")
        n = source.count("with TestClient(")
        if n:
            counts[path.relative_to(TESTS_ROOT.parent).as_posix()] = n
    return counts


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


def test_every_test_client_context_is_accounted_for():
    """A new ``with TestClient(...)`` anywhere in ``tests/`` fails here."""
    live = census()

    unlisted = []
    for path, entries in sorted(live.items()):
        for qualname, (kind, count) in sorted(entries.items()):
            expected = EXPECTED.get(path, {}).get(qualname)
            if expected != (kind, count):
                unlisted.append(
                    f"  {path}::{qualname} -> {(kind, count)} (listed: {expected!r})"
                )

    gone = []
    for path, entries in sorted(EXPECTED.items()):
        for qualname, value in sorted(entries.items()):
            if live.get(path, {}).get(qualname) != value:
                gone.append(f"  {path}::{qualname} listed as {value!r}, not found")

    assert not unlisted and not gone, textwrap.dedent("""
        The `with TestClient(...)` census moved.

        A site that needs *a* started application should take the
        `booted_app_client` fixture (tests/conftest.py), which boots
        faultmaven.main.app once per module instead of once per test. A site
        whose subject IS the lifespan keeps its own `with TestClient(app)`,
        takes `unshared_app_boot`, and is listed in EXPECTED as "real" with the
        reason. A scratch application built inside the test is listed as
        "scratch".

        Appeared or changed:
        {unlisted}

        Listed but not found (rename or removal — update EXPECTED):
        {gone}
        """).format(
        unlisted="\n".join(unlisted) or "  (none)",
        gone="\n".join(gone) or "  (none)",
    )


def test_the_two_censuses_agree_on_the_total():
    """The AST scan and the grep the issue ran must reconcile exactly.

    Without this, a site the resolver silently fails to parse — a new file
    shape, a walrus, a factory call — would be invisible to the guard above
    while still being there.
    """
    from_text = sum(text_census().values())
    from_ast = sum(count for f in census().values() for _kind, count in f.values())
    in_strings = sum(EXPECTED_SITES_IN_STRING_LITERALS.values())

    assert (
        from_text == EXPECTED_TOTAL_SITES
    ), f"{CENSUS_COMMAND} now finds {from_text} sites, not {EXPECTED_TOTAL_SITES}"
    assert from_ast + in_strings == from_text, (
        f"the AST scan sees {from_ast} sites and {in_strings} are declared to live "
        f"inside string literals, which does not add up to the {from_text} the "
        "text census finds — a site is being parsed as something else"
    )


def test_the_real_app_is_booted_by_only_a_handful_of_sites():
    """The number fm#1569 is about, pinned.

    It was 25 in-process sites (of 56 total) before the shared fixture landed.
    """
    real = [
        f"{path}::{qualname}"
        for path, entries in census().items()
        for qualname, (kind, _n) in entries.items()
        if kind == "real"
    ]
    assert len(real) == EXPECTED_REAL_APP_SITES, (
        "the number of sites entering faultmaven.main.app's lifespan changed:\n  "
        + "\n  ".join(sorted(real))
    )


def test_an_unshared_boot_is_declared():
    """A real-app boot cannot sit unannounced beside a shared one.

    In a module that also uses ``booted_app_client``, a bare
    ``with TestClient(app)`` puts two lifespans on one app object. The
    ``unshared_app_boot`` fixture is how a test says it means to, and this is
    the check that it said so.
    """
    offenders = []
    for path in _test_files():
        source = path.read_text(encoding="utf-8")
        if "TestClient" not in source or "booted_app_client" not in source:
            continue
        tree = _parse(source)
        aliases = real_app_aliases(tree)
        rel = path.relative_to(TESTS_ROOT.parent).as_posix()
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            params = {a.arg for a in func.args.args} | {
                a.arg for a in func.args.kwonlyargs
            }
            for node in ast.walk(func):
                if not isinstance(node, (ast.With, ast.AsyncWith)):
                    continue
                for item in node.items:
                    call = item.context_expr
                    if not isinstance(call, ast.Call):
                        continue
                    if (_dotted(call.func) or "").split(".")[-1] != "TestClient":
                        continue
                    arg = call.args[0] if call.args else None
                    if (_dotted(arg) if arg is not None else None) not in aliases:
                        continue
                    if "unshared_app_boot" not in params:
                        offenders.append(f"  {rel}:{node.lineno} in {func.name}()")

    assert not offenders, (
        "these tests boot faultmaven.main.app themselves in a module that also "
        "shares one, without taking the `unshared_app_boot` fixture that stands "
        "the shared boot down:\n" + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# The resolver's own blind spots, measured rather than assumed
# ---------------------------------------------------------------------------

_ALIAS_SHAPES = {
    "plain import": (
        "from faultmaven.main import app\n" "with TestClient(app) as c:\n" "    pass\n"
    ),
    "aliased import": (
        "from faultmaven.main import app as real\n"
        "with TestClient(real) as c:\n"
        "    pass\n"
    ),
    "module import": (
        "from faultmaven import main\n" "with TestClient(main.app) as c:\n" "    pass\n"
    ),
    "dotted import": (
        "import faultmaven.main\n"
        "with TestClient(faultmaven.main.app) as c:\n"
        "    pass\n"
    ),
    "dotted aliased": (
        "import faultmaven.main as fm\n" "with TestClient(fm.app) as c:\n" "    pass\n"
    ),
    "import inside the test": (
        "def test_x():\n"
        "    from faultmaven.main import app\n"
        "    with TestClient(app) as c:\n"
        "        pass\n"
    ),
    "returned by a fixture": (
        "from faultmaven.main import app\n"
        "@pytest.fixture\n"
        "def real_app():\n"
        "    return app\n"
        "def helper(real_app):\n"
        "    with TestClient(real_app) as c:\n"
        "        pass\n"
    ),
    "aliased TestClient import": (
        "from fastapi.testclient import TestClient as TC\n"
        "from faultmaven.main import app\n"
        "with TC(app) as c:\n"
        "    pass\n"
    ),
    "yielded by a fixture": (
        "from faultmaven.main import app\n"
        "@pytest.fixture\n"
        "def real_app():\n"
        "    yield app\n"
        "def helper(real_app):\n"
        "    with TestClient(real_app) as c:\n"
        "        pass\n"
    ),
}


@pytest.mark.parametrize("shape", sorted(_ALIAS_SHAPES))
def test_the_resolver_recognises_every_alias_shape_in_the_tree(shape):
    """Re-introduce the payer in each spelling and check the scan still sees it.

    A resolver that only knows ``from faultmaven.main import app`` would report
    a clean census while four of these shapes booted the app unannounced.
    """
    found = scan_source(_ALIAS_SHAPES[shape])
    assert found, f"{shape}: no site found at all"
    assert all(kind == "real" for _q, kind, _l in found), f"{shape}: read as scratch"


def test_the_resolver_does_not_call_a_scratch_app_real():
    """The other direction: over-approximating would make the guard noise.

    A file that imports the real app for an unrelated assertion and ALSO drives
    a scratch app must not have the scratch site counted as a payer.
    """
    source = (
        "from fastapi import FastAPI\n"
        "from faultmaven.main import app\n"
        "def test_x():\n"
        "    assert app.title\n"
        "    probe = FastAPI()\n"
        "    with TestClient(probe) as c:\n"
        "        pass\n"
    )
    assert [kind for _q, kind, _l in scan_source(source)] == ["scratch"]


def test_only_the_shared_broker_enters_a_lifespan_by_hand():
    """The census watches ``with``; this watches the other way in.

    ``TestClient(app).__enter__()`` runs the same lifespan and no ``with``
    statement scan can see it, so every site that starts a client outside a
    ``with`` is named here. Exactly one is sanctioned: the module-scoped broker
    in ``tests/conftest.py``, whose whole point is that the started client
    outlives the call that started it.
    """
    found: dict[str, int] = {}
    for path in _test_files():
        source = path.read_text(encoding="utf-8")
        if "__enter__" not in source:
            continue
        rel = path.relative_to(TESTS_ROOT.parent).as_posix()
        for node in ast.walk(_parse(source)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "__enter__"
            ):
                found[rel] = found.get(rel, 0) + 1

    assert found == MANUAL_LIFESPAN_ENTRY, (
        "a test starts a TestClient outside a `with` statement, which runs the "
        "application lifespan where the census cannot see it. Take "
        "`booted_app_client` instead, or add the site to MANUAL_LIFESPAN_ENTRY "
        f"with its reason.\n  live: {found}\n  listed: {MANUAL_LIFESPAN_ENTRY}"
    )


#: The fixtures a directory with its own pytest.ini has to re-export, or they
#: are simply absent there.
SHARED_BOOT_FIXTURES = frozenset(
    {"_real_app_boot", "booted_app_client", "unshared_app_boot"}
)

#: Anything pytest will adopt as a rootdir, which is what cuts conftest lookup.
_ROOTDIR_MARKERS = ("pytest.ini", "setup.cfg", "tox.ini", "pyproject.toml")


def test_every_nested_rootdir_re_exports_the_shared_boot():
    """`pytest tests/integration/...` is a different rootdir from `pytest tests/`.

    A directory under ``tests/`` that carries its own ``pytest.ini`` becomes the
    rootdir when pytest is invoked with a path inside it, and ``confcutdir``
    follows — so ``tests/conftest.py`` is never loaded and every fixture defined
    there is missing. CI runs ``pytest tests/`` from the repository root and
    would not notice; a developer running one file would get
    ``fixture 'booted_app_client' not found``.

    ``tests/integration/conftest.py`` already re-exports ``restore_tenant_context``
    for this reason. This is the general statement of that rule.
    """
    missing = []
    for marker_dir in sorted(
        {
            marker.parent
            for name in _ROOTDIR_MARKERS
            for marker in TESTS_ROOT.rglob(name)
        }
    ):
        conftest = marker_dir / "conftest.py"
        rel = marker_dir.relative_to(TESTS_ROOT.parent).as_posix()
        if not conftest.exists():
            missing.append(f"  {rel}/ has no conftest.py to re-export into")
            continue
        re_exported: set[str] = set()
        for node in ast.walk(_parse(conftest.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module == "tests.conftest":
                re_exported |= {a.asname or a.name for a in node.names}
        absent = sorted(SHARED_BOOT_FIXTURES - re_exported)
        if absent:
            missing.append(f"  {rel}/conftest.py does not re-export {absent}")

    assert not missing, (
        "a directory under tests/ carries its own pytest config, so it becomes "
        "pytest's rootdir when invoked directly and tests/conftest.py is not "
        "loaded there. Re-export the shared app-boot fixtures into its "
        "conftest:\n" + "\n".join(missing)
    )
