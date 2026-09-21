"""The CodeQL data-extension pack, held to what it claims (#1394).

`.github/codeql/extensions/faultmaven-path-sanitizers/` tells CodeQL that two
functions sanitise a path. A `barrierModel` row is not a hint — the
`py/path-injection` query consumes it as a dataflow barrier via
`SanitizerFromModel` — so a row that names a function which no longer checks
anything silences a real vulnerability, silently and for good.

There are two distinct failure directions, and this file guards both.

**The model stops being true.** Someone renames `resolve_within_root`, or
rewrites it to return an uncontained path, and the row goes on asserting
containment. Guarded by importing what each row names and driving it with the
hostile inputs it exists to refuse, rather than by reading the source.

**The model stops being LOADED**, which is worse, because nothing about it
looks wrong. Measured while writing it: the pack resolved, the two rows were
ingested (`codeql resolve extensions` reported `barrierModel ... rowCount 1`
twice), and the analysis produced byte-identical results to the run without
it — 19 alerts either way, the three `py/path-injection` ones included. The
cause was one character. A Python models-as-data `type` that names a MODULE
must carry a `!` suffix: without it `getExtraNodeFromType` resolves the dotted
path and then takes `.getAnInstance()` of the module, which matches nothing.
`API::moduleImport("faultmaven.utils.path_containment")` is also empty on its
own — only `moduleImport("faultmaven").getMember("utils").getMember(...)`
resolves, which is the walk the `!` form performs. So the suffix is asserted
here by name: it is the difference between a live barrier and an inert file,
and no other signal distinguishes them.

The same reasoning covers the pack's location and manifest. Code scanning
detects a model pack because it sits under `.github/codeql/extensions/`; move
it, mistype `extensionTargets`, or narrow `dataExtensions` so the glob misses
a model file, and the analysis carries on happily with the model absent.
"""

from __future__ import annotations

import importlib
import os
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The directory code scanning looks in. Not configurable, and not a detail:
#: a pack anywhere else is simply never read.
EXTENSIONS_DIR = REPO_ROOT / ".github" / "codeql" / "extensions"
PACK_DIR = EXTENSIONS_DIR / "faultmaven-path-sanitizers"
MANIFEST = PACK_DIR / "codeql-pack.yml"
CONFIG_FILE = REPO_ROOT / ".github" / "codeql" / "codeql-config.yml"

#: Every function this repository asserts to be a `path-injection` barrier,
#: as `(module, attribute)`. Declared here as well as in the model so that
#: adding a row without adding a behavioural check below fails: the model is
#: a claim, and this file is where the claim is paid for.
MODELLED: frozenset[tuple[str, str]] = frozenset(
    {
        ("faultmaven.utils.path_containment", "resolve_within_root"),
        ("faultmaven.utils.runbook_id", "safe_path_component"),
    }
)

#: `barrierModel(type, path, kind, madId)` — `madId` is supplied by the
#: evaluator, so a data row carries exactly three columns. A fourth is
#: accepted by YAML and rejected by nothing else.
BARRIER_ROW_ARITY = 3

_MEMBER_RETURN_RE = re.compile(
    r"^Member\[(?P<name>[A-Za-z_][A-Za-z0-9_]*)\]\.ReturnValue$"
)


def _load_model_rows() -> list[tuple[Path, dict, list]]:
    """Every `(file, addsTo, row)` triple declared by the pack."""
    triples: list[tuple[Path, dict, list]] = []
    for model_file in sorted(PACK_DIR.rglob("*.model.yml")):
        document = yaml.safe_load(model_file.read_text(encoding="utf-8"))
        for extension in document["extensions"]:
            for row in extension["data"]:
                triples.append((model_file, extension["addsTo"], row))
    return triples


# ---------------------------------------------------------------------------
# The pack is where code scanning will look, and says what it must say
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pack_sits_where_code_scanning_detects_it() -> None:
    assert MANIFEST.is_file(), (
        f"{MANIFEST.relative_to(REPO_ROOT)} is missing. Code scanning detects a "
        "model pack by its position under .github/codeql/extensions/; a pack "
        "moved elsewhere is never read and the analysis reports no error."
    )
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["library"] is True
    assert "codeql/python-all" in manifest["extensionTargets"]


@pytest.mark.unit
def test_every_model_file_is_covered_by_the_manifest_glob() -> None:
    """A model file the manifest does not glob is inert, and looks fine."""
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    patterns = manifest["dataExtensions"]
    assert patterns, "dataExtensions is empty: the pack declares no models"

    globbed = {p.resolve() for pattern in patterns for p in PACK_DIR.glob(pattern)}
    on_disk = {p.resolve() for p in PACK_DIR.rglob("*.model.yml")}
    assert on_disk, "the pack contains no *.model.yml files"
    assert on_disk <= globbed, (
        "model files not matched by any dataExtensions pattern (they will be "
        f"ignored): {sorted(str(p.relative_to(PACK_DIR)) for p in on_disk - globbed)}"
    )


@pytest.mark.unit
def test_rows_target_the_python_barrier_predicate_with_the_right_arity() -> None:
    triples = _load_model_rows()
    assert triples, "the pack declares no rows"
    for model_file, adds_to, row in triples:
        where = f"{model_file.name}: {row}"
        assert adds_to["pack"] == "codeql/python-all", where
        assert adds_to["extensible"] == "barrierModel", where
        assert (
            len(row) == BARRIER_ROW_ARITY
        ), f"{where} (expected {BARRIER_ROW_ARITY} columns)"
        assert row[2] == "path-injection", where


@pytest.mark.unit
def test_module_types_carry_the_bang_suffix() -> None:
    """The one character between a live barrier and an inert file.

    Without `!`, `getExtraNodeFromType` takes `.getAnInstance()` of the module
    node and matches nothing. The pack still loads and the rows still count.
    """
    for model_file, _adds_to, row in _load_model_rows():
        type_name = row[0]
        assert type_name.endswith("!"), (
            f"{model_file.name}: type {type_name!r} names a module and must end "
            "with '!' or the row resolves to nothing. See this file's docstring."
        )


@pytest.mark.unit
def test_rows_name_exactly_the_functions_this_file_pays_for() -> None:
    declared: set[tuple[str, str]] = set()
    for model_file, _adds_to, row in _load_model_rows():
        type_name, access_path, _kind = row
        match = _MEMBER_RETURN_RE.match(access_path)
        assert match, (
            f"{model_file.name}: access path {access_path!r} is not "
            "`Member[<name>].ReturnValue`; this file only knows how to verify "
            "that shape, so extend it rather than widening the model silently."
        )
        declared.add((type_name.rstrip("!"), match.group("name")))

    assert declared == set(MODELLED), (
        "the model and this file disagree about what is claimed to sanitise. "
        f"only in the model: {sorted(declared - set(MODELLED))}; "
        f"only here: {sorted(set(MODELLED) - declared)}"
    )


@pytest.mark.unit
@pytest.mark.parametrize(("module_name", "attribute"), sorted(MODELLED))
def test_every_modelled_function_still_exists(module_name: str, attribute: str) -> None:
    module = importlib.import_module(module_name)
    assert callable(getattr(module, attribute, None)), (
        f"{module_name}.{attribute} is modelled as a path-injection barrier but "
        "is not a callable on that module any more. A row that names nothing "
        "resolves to nothing, so the alerts it suppressed come back — but a row "
        "that names the WRONG thing suppresses a real flow. Fix the row."
    )


# ---------------------------------------------------------------------------
# ...and each one still does what the row claims
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
def test_resolve_within_root_refuses_every_escape(tmp_path: Path) -> None:
    """The claim: the return value is always strictly inside `root`.

    Driven rather than read. A reimplementation that returns the path
    unchanged, or that anchors on the containing directory instead of the
    root, passes a source inspection and fails here.
    """
    from faultmaven.utils.path_containment import PathEscape, resolve_within_root

    root = tmp_path / "tree"
    (root / "inner").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "link"
    link.symlink_to(outside, target_is_directory=True)

    hostile = {
        "relative traversal": root / ".." / "outside" / "x.md",
        "deep traversal": root / ".." / ".." / ".." / "x.md",
        "absolute path": Path("/etc/passwd"),
        "the root itself": root,
        "embedded NUL": str(root / "a\x00b"),
        "symlink out of tree": link / "x.md",
    }
    for label, candidate in hostile.items():
        with pytest.raises(PathEscape):
            resolve_within_root(
                candidate, root=root, source=label, subject="path", tree="test"
            )

    # Positive control: the guard is a guard, not a refusal of everything.
    # Without this, deleting the function body would pass every case above.
    allowed = resolve_within_root(
        root / "inner" / "ok.md", root=root, source="t", subject="path", tree="test"
    )
    assert allowed.is_relative_to(root.resolve()) and allowed != root.resolve()


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.parametrize(
    "hostile",
    [
        "../../../../escaped",
        "..",
        ".",
        "a/b",
        "a\\b",
        "a\x00b",
        "C:\\Windows",
        "  ",
        "",
        None,
        "\u2044slash-lookalike",
    ],
)
def test_safe_path_component_yields_one_harmless_segment(hostile: str | None) -> None:
    """The claim: the return value is a single segment that cannot traverse."""
    from faultmaven.utils.runbook_id import safe_path_component

    component = safe_path_component(hostile)

    assert component, "an empty component would silently drop the discriminator"
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", component), component
    assert component not in {".", ".."}
    assert os.sep not in component and "/" not in component and "\\" not in component
    assert "\x00" not in component
    # The property that matters at the call site: interpolating it into a
    # directory name cannot move the write anywhere.
    assert Path(f"scope_{component}").name == f"scope_{component}"


# ---------------------------------------------------------------------------
# The configuration file is inert, and says so
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_codeql_config_stays_empty_until_something_reads_it() -> None:
    """Default setup only loads this file once a repository property names it.

    Until then a key added here takes effect nowhere, which is a quiet way to
    believe a path is excluded or a query disabled when it is not. The file's
    header carries the one command that wires it; this fails so that nobody
    reaches the belief without reading that.
    """
    config = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    assert set(config) == {"name"}, (
        "codeql-config.yml has grown a functional key while nothing reads it. "
        "Set the `github-codeql-config-file` repository property in the same "
        "change (the command is in the file's header), then update this test."
    )
