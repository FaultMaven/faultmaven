"""Every operator command refuses a non-persistent database before it acts (#1659).

The API lifespan and the jobs runner refuse an empty or in-memory
``DATABASE_URL`` at boot (fm#1647). The ``fm-*`` commands did not.
``fm-provision-service-account`` printed "✅ Created service account" and a
refresh token for an account held in an in-memory store that vanished with the
process. ``faultmaven.cli._database_gate.require_persistent_database_or_exit``
now applies the same rule to them, and this file pins two things:

1. **The census.** ``pyproject.toml`` ``[project.scripts]`` is the authority on
   which commands exist, so it is read here, not copied. Each declared
   command's ``main()`` must call the shared gate as a top-level statement,
   and must call the shared function rather than a local one of the same name.
   Everything ``main()`` calls before the gate must be on
   ``_ARGUMENT_ONLY_CALLS``, the calls that only parse and validate arguments.
   That makes the region before the gate fail closed. A branch that runs a
   helper, ``asyncio.run`` or ``run_until_complete`` before the gate fails
   here, whichever mode it hides behind. A behavioural run can only see the
   modes it drives. The dev scripts that reach the database are found by
   scanning ``scripts/`` for the calls and imports that reach it, and are held
   to the gate too.
2. **The helper's exit contract.** Exit 1 with the boot gate's message on
   stderr and nothing on stdout. Stdout stays empty so
   ``--token-only > token.txt`` captures nothing.

Each command is also driven for real, in a child process, by
``tests/integration/test_operator_commands_refuse_nonpersistent_database.py``,
because a structural check says nothing about what the process does.
"""

from __future__ import annotations

import ast
import importlib
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from faultmaven.cli._database_gate import require_persistent_database_or_exit
from faultmaven.config.persistent_database import DEFAULT_DATABASE_URL

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
GATE = "require_persistent_database_or_exit"

with (REPO_ROOT / "pyproject.toml").open("rb") as _handle:
    DECLARED: dict[str, str] = tomllib.load(_handle)["project"]["scripts"]

#: Commands that act on no database and so may skip the gate, each with its
#: reason. Empty: every command shipped today reads or writes the deployment's
#: database. An entry here is a decision someone must write down, never a way
#: to quiet this test.
NEEDS_NO_DATABASE: dict[str, str] = {}

#: What a command's ``main()`` may call BEFORE the gate: parsing and validating
#: its own arguments, nothing that reads settings or the database. Measured on
#: the ten commands shipped with #1659. A name added here must be one that
#: cannot reach the database, and adding it is what a reviewer reads.
_ARGUMENT_ONLY_CALLS = frozenset(
    {
        # argparse
        "ArgumentParser",
        "add_argument",
        "add_argument_group",
        "add_mutually_exclusive_group",
        "add_parser",
        "add_subparsers",
        "error",
        "parse_args",
        "set_defaults",
        # the shared --dry-run/--yes check (faultmaven/cli/_confirmation.py)
        "require_confirmation",
        # command-local argument helpers
        "_add_apply_flags",  # fm-personal-tenant: adds --dry-run/--apply
        "normalize_domain",  # fm-provision-sso-org: folds --domain
        # builtins and str methods used on argument values, and the usage exit
        "any",
        "exit",
        "isspace",
        "print",
        "sorted",
        "strip",
    }
)

#: The calls that reach the database, directly or through the container. A dev
#: script that makes any of them is held to the gate.
_DATABASE_REACHING_CALLS = {
    "initialize",
    "get_db_session",
    "get_engine",
    "get_session_factory",
    "create_async_engine",
}

#: Modules whose import puts the database in reach. The container composes
#: stores lazily on first access (``_container_impl`` runs ``asyncio.run`` from
#: its getters), so importing it is enough, however the instance is then
#: spelled. Models alone are not: ``generate_er_diagram.py`` reads metadata.
_DATABASE_REACHING_IMPORTS = (
    "faultmaven.container",
    "faultmaven._container_impl",
    "faultmaven.infrastructure.persistence.database",
    "faultmaven.infrastructure.persistence.sessionless_",
    "faultmaven.infrastructure.persistence.user_repository",
    "faultmaven.infrastructure.auth.database_user_store",
    # A command's async core (``provision``, ``reset_kb``, ...) carries no gate
    # of its own: the gate is in ``main()``. A script that imports one reaches
    # the database without it.
    "faultmaven.cli",
)


def _reaches_the_database(module: str) -> bool:
    """A module whose import puts the database in reach, including any module's
    repositories (``faultmaven.modules.<m>.infrastructure...``)."""
    return module.startswith(_DATABASE_REACHING_IMPORTS) or (
        module.startswith("faultmaven.modules.") and ".infrastructure" in module
    )


#: Scripts the scan finds that cannot run at all, each with the fact that makes
#: it dead. The test re-checks the fact, so the exemption lapses when it does.
_DEAD_SCRIPTS = {
    "scripts/migration_backfill_scopes.py": "faultmaven.container.app_container",
}

#: The dev scripts that reach the database on ``main`` today. The scan below
#: must find at least these. If it found none, it would pass without looking.
_KNOWN_DATABASE_SCRIPTS = {
    "scripts/auth/create_user.py",
    "scripts/auth/list_users.py",
    "scripts/cleanup_corrupt_cases.py",
}


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _starts_an_event_loop(call: ast.Call) -> bool:
    """``asyncio.run(...)`` or any ``....run_until_complete(...)``."""
    func = call.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "run_until_complete":
        return True
    return (
        func.attr == "run"
        and isinstance(func.value, ast.Name)
        and func.value.id == "asyncio"
    )


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no top-level function {name!r}")


def _top_level_gate_lines(body: list[ast.stmt]) -> list[int]:
    return [
        stmt.lineno
        for stmt in body
        if isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Call)
        and _call_name(stmt.value) == GATE
    ]


def _event_loop_lines(node: ast.AST) -> list[int]:
    return [
        n.lineno
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and _starts_an_event_loop(n)
    ]


def _module_source(module_path: str) -> tuple[Path, ast.Module]:
    path = REPO_ROOT / (module_path.replace(".", "/") + ".py")
    return path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# ---------------------------------------------------------------------------
# 1. The census over [project.scripts]
# ---------------------------------------------------------------------------


def test_the_census_reads_a_non_empty_declaration():
    assert DECLARED, "no [project.scripts] declared; the census would be vacuous"
    stale = set(NEEDS_NO_DATABASE) - set(DECLARED)
    assert not stale, f"exemptions for commands that no longer exist: {stale}"


@pytest.mark.parametrize(
    "command", sorted(set(DECLARED) - set(NEEDS_NO_DATABASE)), ids=str
)
def test_every_declared_command_gates_before_it_runs_anything(command):
    module_path, _, attr = DECLARED[command].partition(":")
    path, tree = _module_source(module_path)
    entry = _function(tree, attr)

    gate_lines = _top_level_gate_lines(entry.body)
    assert gate_lines, (
        f"{command} ({path.relative_to(REPO_ROOT)}:{attr}) never calls {GATE}() "
        "as a top-level statement of its entrypoint. Call it once, after the "
        "arguments are validated and before the first asyncio.run, so that on "
        "an empty or in-memory DATABASE_URL the command refuses rather than "
        "acting on a store that dies with the process (#1659)."
    )
    first_gate = min(gate_lines)
    early_loops = [line for line in _event_loop_lines(entry) if line < first_gate]
    assert not early_loops, (
        f"{command}: an event loop starts at line(s) {early_loops}, before "
        f"{GATE}() at line {first_gate}"
    )
    before_gate = sorted(
        {
            (node.lineno, _call_name(node) or type(node.func).__name__)
            for node in ast.walk(entry)
            if isinstance(node, ast.Call) and node.lineno < first_gate
        }
    )
    unknown = [
        (line, name) for line, name in before_gate if name not in _ARGUMENT_ONLY_CALLS
    ]
    assert not unknown, (
        f"{command}: {attr}() calls {unknown} before {GATE}() at line "
        f"{first_gate}. Only argument parsing and validation may run first. "
        "Move the gate above the call, or, if it truly cannot reach settings or "
        "the database, add it to _ARGUMENT_ONLY_CALLS with a reason."
    )

    # The name must be the shared gate, not a local function of the same name.
    module = importlib.import_module(module_path)
    assert getattr(module, GATE) is require_persistent_database_or_exit, (
        f"{command}: {GATE} in {module_path} is not the shared gate from "
        "faultmaven.cli._database_gate"
    )


# ---------------------------------------------------------------------------
# 2. The dev scripts that reach the database
# ---------------------------------------------------------------------------


def _database_scripts() -> dict[str, ast.Module]:
    found: dict[str, ast.Module] = {}
    for path in sorted((REPO_ROOT / "scripts").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:  # pragma: no cover - a broken script is not ours
            continue
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            if any(_reaches_the_database(m) for m in modules):
                found[str(path.relative_to(REPO_ROOT))] = tree
                break
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name == "initialize":
                # Only the container's initialize(): ``container.initialize()``.
                func = node.func
                if not (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "container"
                ):
                    continue
            if name in _DATABASE_REACHING_CALLS:
                found[str(path.relative_to(REPO_ROOT))] = tree
                break
    return found


DATABASE_SCRIPTS = _database_scripts()
LIVE_DATABASE_SCRIPTS = {
    script: tree
    for script, tree in DATABASE_SCRIPTS.items()
    if script not in _DEAD_SCRIPTS
}


@pytest.mark.parametrize("script", sorted(_DEAD_SCRIPTS), ids=str)
def test_a_dead_script_exemption_holds_only_while_the_script_is_dead(script):
    """The exempted script imports a module that does not exist, so it dies
    on import before it could reach anything. Once that module exists, the
    exemption no longer holds and the script is held to the gate."""
    import importlib.util

    missing_module = _DEAD_SCRIPTS[script]
    assert script in DATABASE_SCRIPTS, f"{script} is no longer found by the scan"
    assert importlib.util.find_spec(missing_module) is None, (
        f"{missing_module} exists now, so {script} can run: drop it from "
        "_DEAD_SCRIPTS and gate it"
    )


def test_the_script_scan_finds_the_known_database_scripts():
    missing = _KNOWN_DATABASE_SCRIPTS - set(DATABASE_SCRIPTS)
    assert not missing, (
        f"the scan no longer finds {sorted(missing)}. Either they stopped "
        "reaching the database (update _KNOWN_DATABASE_SCRIPTS) or the scan "
        "went blind"
    )


def _is_main_guard(node: ast.stmt) -> bool:
    """``if __name__ == "__main__":``"""
    test = getattr(node, "test", None)
    return (
        isinstance(node, ast.If)
        and isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


def _unconditional_gate_lines(tree: ast.Module) -> list[int]:
    """Gate calls that run whenever their block runs: statements directly in the
    module body, in a top-level function's body, or in the ``__main__`` guard.
    A gate inside any other ``if``, loop or ``try`` covers only some paths."""
    blocks: list[list[ast.stmt]] = [tree.body]
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            blocks.append(node.body)
        elif _is_main_guard(node):
            blocks.append(node.body)
    return [line for body in blocks for line in _top_level_gate_lines(body)]


@pytest.mark.parametrize("script", sorted(LIVE_DATABASE_SCRIPTS), ids=str)
def test_every_database_script_gates_before_it_runs_anything(script):
    tree = LIVE_DATABASE_SCRIPTS[script]
    gate_calls = _unconditional_gate_lines(tree)
    assert gate_calls, (
        f"{script} reaches the database and never calls {GATE}() unconditionally "
        "(in the module body, a top-level function's body, or the __main__ guard)"
    )
    runs = _event_loop_lines(tree)
    early = [line for line in runs if line < min(gate_calls)]
    assert not early, (
        f"{script}: an event loop starts at line(s) {early}, before {GATE}() at "
        f"line {min(gate_calls)}"
    )
    imports = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom)
        and n.module == "faultmaven.cli._database_gate"
        and any(alias.name == GATE for alias in n.names)
    ]
    assert imports, f"{script}: {GATE} is not imported from the shared gate"


# ---------------------------------------------------------------------------
# 3. The helper's exit contract
# ---------------------------------------------------------------------------


def _settings(url):
    return SimpleNamespace(database=SimpleNamespace(database_url=url))


@pytest.mark.parametrize(
    "url",
    ["", ":memory:", "sqlite+aiosqlite:///?timeout=30", "file::memory:?cache=shared"],
)
def test_refusal_exits_1_with_the_boot_gates_message_on_stderr_only(url, capsys):
    with patch("faultmaven.config.settings.get_settings", return_value=_settings(url)):
        with pytest.raises(SystemExit) as raised:
            require_persistent_database_or_exit()
    assert raised.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == "", "a refusal must leave stdout empty (--token-only)"
    assert "configures no persistent database" in captured.err
    assert DEFAULT_DATABASE_URL in captured.err
    assert "✅" not in captured.err


@pytest.mark.parametrize(
    "url", [DEFAULT_DATABASE_URL, "postgresql+asyncpg://fm@db:5432/faultmaven"]
)
def test_a_persistent_database_passes_silently(url, capsys):
    with patch("faultmaven.config.settings.get_settings", return_value=_settings(url)):
        assert require_persistent_database_or_exit() is None
    assert capsys.readouterr() == ("", "")


def test_the_gate_judges_the_settings_the_command_reads():
    """The rule is the boot gate's, applied to the ``get_settings()`` singleton
    every command reads afterwards — not a second reading of the environment
    that could judge a different URL."""
    sentinel = _settings(DEFAULT_DATABASE_URL)
    with (
        patch("faultmaven.config.settings.get_settings", return_value=sentinel),
        patch(
            "faultmaven.config.persistent_database.require_persistent_database"
        ) as boot_gate,
    ):
        require_persistent_database_or_exit()
    boot_gate.assert_called_once_with(sentinel)
