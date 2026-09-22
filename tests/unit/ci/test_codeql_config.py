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
from functools import lru_cache
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

#: Both names the CodeQL CLI accepts for a pack manifest. A second pack added
#: under the other spelling is still auto-detected, so both are looked for.
MANIFEST_NAMES = ("codeql-pack.yml", "qlpack.yml")

#: Every pack this file knows about. `test_no_unreviewed_pack_appears` fails on
#: anything else under `EXTENSIONS_DIR`, because code scanning detects packs by
#: POSITION: a second directory dropped in there ships barrier rows that
#: silence alerts, with nothing in this repository reading them.
KNOWN_PACKS: frozenset[str] = frozenset({"faultmaven-path-sanitizers"})

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

#: A models-as-data `type` naming a module: a dotted Python path with exactly
#: one trailing `!`. Anchored and whole-matched on purpose — see
#: `test_module_types_carry_exactly_one_bang_suffix`.
_MODULE_TYPE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*!")


def _pack_dirs() -> list[Path]:
    """Every directory under `EXTENSIONS_DIR` that code scanning reads as a pack."""
    if not EXTENSIONS_DIR.is_dir():
        return []
    return sorted(
        {
            manifest.parent
            for name in MANIFEST_NAMES
            for manifest in EXTENSIONS_DIR.rglob(name)
        }
    )


def _manifest_of(pack_dir: Path) -> Path:
    for name in MANIFEST_NAMES:
        if (pack_dir / name).is_file():
            return pack_dir / name
    raise AssertionError(f"no pack manifest in {pack_dir}")


@lru_cache(maxsize=1)
def _load_model_rows() -> tuple[tuple[Path, dict, list], ...]:
    """Every `(file, addsTo, row)` triple ANYWHERE under `EXTENSIONS_DIR`.

    Walked from the extensions directory rather than from `PACK_DIR`, because
    that is how code scanning finds them: a row's reach comes from where the
    file sits, not from which pack this file happens to know about.
    """
    triples: list[tuple[Path, dict, list]] = []
    for model_file in sorted(EXTENSIONS_DIR.rglob("*.model.yml")):
        document = yaml.safe_load(model_file.read_text(encoding="utf-8"))
        assert isinstance(document, dict) and isinstance(
            document.get("extensions"), list
        ), f"{model_file.relative_to(REPO_ROOT)} is not a data-extension document"
        for extension in document["extensions"]:
            for row in extension["data"]:
                triples.append((model_file, extension["addsTo"], row))
    return tuple(triples)


# ---------------------------------------------------------------------------
# The pack is where code scanning will look, and says what it must say
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_unreviewed_pack_appears_under_extensions() -> None:
    """A second pack here ships barrier rows nobody in this repository reads.

    Detection is by POSITION, so anything dropped under `.github/codeql/
    extensions/` is loaded whether or not this file knows about it. Enumerated
    rather than assumed: `_load_model_rows` walks the whole directory, and this
    is what makes a new pack arrive as a failure instead of as silence.
    """
    found = {d.name for d in _pack_dirs()}
    assert found == set(KNOWN_PACKS), (
        "unreviewed CodeQL pack(s) under .github/codeql/extensions/: "
        f"{sorted(found - set(KNOWN_PACKS))}; missing: "
        f"{sorted(set(KNOWN_PACKS) - found)}. Every pack here must be paid for "
        "by a behavioural check in this file."
    )


@pytest.mark.unit
@pytest.mark.parametrize("pack_dir", _pack_dirs(), ids=lambda d: d.name)
def test_pack_sits_where_code_scanning_detects_it(pack_dir: Path) -> None:
    manifest_path = _manifest_of(pack_dir)
    assert manifest_path.is_file(), (
        f"{manifest_path.relative_to(REPO_ROOT)} is missing. Code scanning "
        "detects a model pack by its position under .github/codeql/extensions/; "
        "a pack moved elsewhere is never read and the analysis reports no error."
    )
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    where = manifest_path.relative_to(REPO_ROOT)

    # `name` and `version` are what the pack is RESOLVED by. Without either the
    # CLI cannot resolve it and simply carries on with the model absent — the
    # inert-and-looks-fine failure this whole file exists for. Both were
    # unasserted until the #1394 review deleted them and all twenty tests
    # passed.
    assert isinstance(manifest.get("name"), str) and "/" in manifest["name"], (
        f"{where}: `name` must be a `scope/pack` string; without it the pack "
        "cannot be resolved and the model is silently absent."
    )
    assert (
        isinstance(manifest.get("version"), str) and manifest["version"]
    ), f"{where}: `version` is missing; the pack cannot be resolved without it."
    assert manifest["library"] is True, where

    # A MAPPING, checked as one. `"codeql/python-all" in manifest[...]` passes
    # vacuously when the value is a string, because `in` degrades to a
    # substring test — so `extensionTargets: "codeql/python-all-ish"` would
    # have satisfied the old assertion while being a malformed manifest.
    targets = manifest.get("extensionTargets")
    assert isinstance(targets, dict), (
        f"{where}: `extensionTargets` must be a mapping of pack -> version "
        f"range, got {type(targets).__name__}."
    )
    assert "codeql/python-all" in targets.keys(), where


@pytest.mark.unit
@pytest.mark.parametrize("pack_dir", _pack_dirs(), ids=lambda d: d.name)
def test_every_model_file_is_covered_by_the_manifest_glob(pack_dir: Path) -> None:
    """A model file the manifest does not glob is inert, and looks fine.

    This asserts pathlib's reading of the patterns, not CodeQL's; the two agree
    on the shapes used here, and the measurement that the rows actually reach
    the evaluator is in the pack's own header.
    """
    manifest = yaml.safe_load(_manifest_of(pack_dir).read_text(encoding="utf-8"))
    patterns = manifest["dataExtensions"]
    assert patterns, f"{pack_dir.name}: dataExtensions is empty"

    globbed = {p.resolve() for pattern in patterns for p in pack_dir.glob(pattern)}
    on_disk = {p.resolve() for p in pack_dir.rglob("*.model.yml")}
    assert on_disk, f"{pack_dir.name} contains no *.model.yml files"
    assert on_disk <= globbed, (
        "model files not matched by any dataExtensions pattern (they will be "
        f"ignored): {sorted(str(p.relative_to(pack_dir)) for p in on_disk - globbed)}"
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
def test_module_types_carry_exactly_one_bang_suffix() -> None:
    """The one character between a live barrier and an inert file.

    Without `!`, `getExtraNodeFromType` takes `.getAnInstance()` of the module
    node and matches nothing. The pack still loads and the rows still count.

    Matched whole, not by `endswith`. `"faultmaven.utils.runbook_id!!"` ends
    with `!` and — with the `rstrip("!")` this file used to strip it — yielded
    the right module name, so the name-set check below passed too, while the
    type resolved to nothing and the alerts came back. That is the same bug
    this suffix exists to prevent, reintroduced by the guard for it.
    """
    for model_file, _adds_to, row in _load_model_rows():
        type_name = row[0]
        assert _MODULE_TYPE_RE.fullmatch(type_name), (
            f"{model_file.name}: type {type_name!r} must be a dotted module "
            "path with EXACTLY one trailing '!', or the row resolves to "
            "nothing. See this file's docstring."
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
        # `removesuffix`, never `rstrip`: `rstrip("!")` eats a whole run of
        # them and would launder `module!!` into a name that matches.
        declared.add((type_name.removesuffix("!"), match.group("name")))

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
    ("value", "kwargs"),
    [
        # Separators, traversal, NUL, drive letters, look-alikes, emptiness.
        ("../../../../escaped", {}),
        ("..", {}),
        (".", {}),
        ("a/b", {}),
        ("a\\b", {}),
        ("a\x00b", {}),
        ("C:\\Windows", {}),
        ("  ", {}),
        ("", {}),
        (None, {}),
        ("\u2044slash-lookalike", {}),
        # The SECOND argument is an input too (#1394 review). Before the fix
        # these three were returned verbatim, and the barrier row silenced the
        # flow that carried them to the filesystem.
        ("???", {"fallback": "../../../../escaped"}),
        (None, {"fallback": "/etc/passwd"}),
        ("???", {"fallback": "???"}),
        # Past the 60-character bound, where `[:60]` can land on a hyphen and
        # put back the character `_slug` had just stripped. Every case in the
        # first block is under 12 characters, so none of them reached this.
        ("a" * 59 + " " + "b" * 10, {}),
        ("-".join(["ab"] * 30), {}),
        ("x" * 200, {}),
        ("word " * 40, {}),
        ("???", {"fallback": "-".join(["cd"] * 30)}),
    ],
)
def test_safe_path_component_yields_one_harmless_segment(
    value: str | None, kwargs: dict[str, str]
) -> None:
    """The claim: the return value is a single segment that cannot traverse."""
    from faultmaven.utils.runbook_id import safe_path_component

    component = safe_path_component(value, **kwargs)

    assert component, "an empty component would silently drop the discriminator"
    # One `fullmatch` covers non-emptiness, the absence of every separator and
    # of `.`/`..`, and the hyphen boundary; the two assertions below are the
    # property at the CALL SITE, which the regex does not state.
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", component), component
    assert Path(f"scope_{component}").name == f"scope_{component}"
    assert (Path("/tree") / f"scope_{component}").resolve().is_relative_to("/tree")


@pytest.mark.unit
@pytest.mark.security
def test_safe_path_component_bound_is_still_enforced() -> None:
    """Stripping after the slice must not be an excuse to stop bounding it.

    The bound is what keeps the component inside NAME_MAX and inside
    `uploaded_files.filename`; a fix for the trailing hyphen that dropped the
    slice would pass every case above.
    """
    from faultmaven.utils.runbook_id import _MAX_SLUG_CHARS, safe_path_component

    assert len(safe_path_component("x" * 500)) == _MAX_SLUG_CHARS
    assert len(safe_path_component("???", fallback="y" * 500)) == _MAX_SLUG_CHARS


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
