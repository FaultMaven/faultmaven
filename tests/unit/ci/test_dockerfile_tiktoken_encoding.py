"""The image carries every tiktoken encoding the code loads.

tiktoken downloads an encoding's BPE file on first use. The Dockerfile bakes the
file into the image, so a pod with no network still counts real tokens. That
holds only while the baked set covers what the code asks for: code that starts
loading a second encoding needs a second prefetch, and nothing else would
notice.

The CI image build proves the prefetch works, because its second load runs
through an unreachable proxy. These tests pin what that build cannot see:

- every ``get_encoding`` call in ``faultmaven/`` names its encoding with a
  literal, directly or through a local name assigned only literals, so the set
  of encodings is knowable before the code runs;
- each of those encodings is in the Dockerfile's offline check;
- no module loads an encoding at import, where a download without a timeout
  would hold startup on the network;
- the cache directory is set before the prefetch writes to it.

Encodings loaded inside dependencies are not scanned: none of the ones the app
imports loads one.
"""

from __future__ import annotations

import ast
import functools
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO_ROOT / "Dockerfile"
SOURCE = REPO_ROOT / "faultmaven"

#: tiktoken calls that pick an encoding from a model name at runtime.
_BY_MODEL = {"encoding_for_model", "encoding_name_for_model"}
#: Calls that load an encoding: tiktoken's own, and the shared loader in
#: ``utils.token_estimation`` every other module goes through.
_LOADERS = {"get_encoding", "_get_tiktoken_encoder"}


@functools.lru_cache(maxsize=1)
def _trees() -> tuple[tuple[Path, ast.AST], ...]:
    """Every module under ``faultmaven/`` that names one of the calls these
    tests look for, parsed once for all of them. A call has to spell its name,
    so a module that never mentions one cannot make it."""
    names = _BY_MODEL | _LOADERS
    trees = []
    for path in sorted(SOURCE.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if any(name in text for name in names):
            trees.append((path, ast.parse(text)))
    return tuple(trees)


def _called(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    return getattr(node.func, "attr", None) or getattr(node.func, "id", None)


def _function_scopes(tree: ast.AST):
    """Each function, with every call made in its own body."""
    for scope in ast.walk(tree):
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield scope


def _literals_assigned(scope: ast.AST, name: str) -> list[ast.expr]:
    return [
        node.value
        for node in ast.walk(scope)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and node.value is not None
        and any(
            isinstance(target, ast.Name) and target.id == name
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
        )
    ]


@functools.lru_cache(maxsize=1)
def _loaded_encodings() -> tuple[frozenset[str], tuple[str, ...]]:
    """The encodings ``get_encoding`` is called with, and the calls whose
    encoding cannot be read off the source."""
    loaded: set[str] = set()
    unresolved: list[str] = []
    for path, tree in _trees():
        for scope in _function_scopes(tree):
            for call in ast.walk(scope):
                if _called(call) != "get_encoding":
                    continue
                where = f"{path.relative_to(REPO_ROOT)}:{call.lineno}"
                arg = call.args[0] if call.args else None
                values = (
                    _literals_assigned(scope, arg.id)
                    if isinstance(arg, ast.Name)
                    else [arg]
                )
                if values and all(
                    isinstance(v, ast.Constant) and isinstance(v.value, str)
                    for v in values
                ):
                    loaded.update(v.value for v in values)
                else:
                    unresolved.append(where)
    return frozenset(loaded), tuple(unresolved)


def _instructions() -> list[str]:
    """The Dockerfile's instructions, continuation lines joined, comments out."""
    text = re.sub(r"\\\n", " ", DOCKERFILE.read_text(encoding="utf-8"))
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _offline_check() -> tuple[int, str]:
    """The prefetch RUN, and the part of it that loads through the dead proxy."""
    runs = [
        (i, step)
        for i, step in enumerate(_instructions())
        if step.startswith("RUN ") and "tiktoken" in step and "127.0.0.1:9" in step
    ]
    assert len(runs) == 1, runs
    index, step = runs[0]
    return index, step.split("127.0.0.1:9", 1)[1]


def test_the_code_names_its_encodings():
    """An encoding picked from a model name, a setting or any other runtime
    value is one no build step can know in advance."""
    by_model = [
        f"{path.relative_to(REPO_ROOT)}:{node.lineno}"
        for path, tree in _trees()
        for node in ast.walk(tree)
        if _called(node) in _BY_MODEL
    ]
    _, unresolved = _loaded_encodings()
    assert by_model == []
    assert unresolved == (), "pass get_encoding a literal: " + ", ".join(unresolved)


def test_every_encoding_the_code_loads_is_checked_offline_in_the_image():
    loaded, _ = _loaded_encodings()
    # The scan has to see the loader that exists today, or the check below
    # passes on an empty set.
    assert "cl100k_base" in loaded
    _, offline = _offline_check()
    checked = set(re.findall(r"get_encoding\('([a-z0-9_]+)'\)", offline))
    assert loaded <= checked, f"not checked offline: {sorted(loaded - checked)}"


def test_no_module_loads_an_encoding_at_import():
    """tiktoken's download has no timeout, so a load at import can hold
    startup on the network. Loads belong in the function that needs them."""
    at_import = []
    for path, tree in _trees():
        calls = [node for node in ast.walk(tree) if _called(node) in _LOADERS]
        if not calls:
            continue
        in_functions = {
            id(node) for scope in _function_scopes(tree) for node in ast.walk(scope)
        }
        at_import += [
            f"{path.relative_to(REPO_ROOT)}:{call.lineno}"
            for call in calls
            if id(call) not in in_functions
        ]
    assert at_import == []


def test_the_cache_directory_is_set_before_the_prefetch():
    """Set after it, the prefetch and its offline check would both use the
    system temp directory while runtime looked in the configured one."""
    instructions = _instructions()
    settings = [
        i for i, step in enumerate(instructions) if "TIKTOKEN_CACHE_DIR" in step
    ]
    prefetch, _ = _offline_check()
    env = [i for i in settings if instructions[i].startswith("ENV TIKTOKEN_CACHE_DIR=")]
    assert len(env) == 1, [instructions[i] for i in settings]
    assert env[0] < prefetch
    assert settings == env, "nothing else may set the cache directory"
