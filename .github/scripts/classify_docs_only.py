#!/usr/bin/env python3
"""Decide whether a pull request's diff touches anything a test or the image reads.

This is the CI gate that decides **whether the test suites run at all**
(`.github/workflows/ci-cd.yml`, job `changes`). A wrong `true` skips every
suite and the image build on a diff that breaks `main`, and a skipped required
check reads as passing -- so the failure mode is a silent merge with no tests,
not a red build.

It lives here, as an importable module, rather than as a heredoc inside the
workflow, because a gate that decides whether tests run must itself be
testable: see ``tests/unit/ci/test_classify_docs_only.py`` (#1539).

Stdlib only, on purpose: the workflow runs it with the runner's ``python3``
and no ``setup-python`` step, so there is no dependency to pin.

Contract with the workflow, unchanged by the extraction:

* reads ``changed.txt`` from the working directory -- one path per line, the
  pull request's file list as produced by
  ``git diff --name-only --no-renames <merge-base> HEAD``;
* reads ``RESOLVED`` from the environment and refuses to classify unless it is
  exactly ``"true"`` (the fail-closed arm -- an unresolved file list must never
  read as "nothing to test");
* appends ``docs_only=<true|false>`` to ``$GITHUB_OUTPUT``;
* exits ``1``, having written nothing, when the pin probe itself is broken.
"""

from __future__ import annotations

import ast
import os
import pathlib
from typing import Iterable, List, NamedTuple, Optional, Sequence

# Two literals that exist under tests/ today. The pin probe reports by finding
# nothing, and a probe pointed at the wrong tree reports the same way -- every
# document inert, every suite skipped. If these stop being found, the probe is
# what changed, and it refuses to answer rather than sweep.
POSITIVE_CONTROLS = ("CLAUDE.md", "docs/architecture")

# The roots ``classify`` calls inert. A path is only worth refusing over if
# this gate could wave it through, and these are the directories where that
# happens -- so they are also what makes a reference under tests/ a
# DOCUMENTATION reference rather than just an expression with a string in it.
DOC_ROOTS = ("docs", ".claude")

# Identifiers that name a documentation path without spelling one. Evidence of
# the last resort: it is consulted only for an operand of a path build that
# resolves to no constant at all, which is exactly the ``DOCS_DIR / name``
# shape the refusal exists for.
DOCISH_NAME_TOKENS = ("doc", "claude", "readme")

# pytest's temporary directories. A path rooted at one cannot name a tracked
# file, so ``tmp_path / "docs"`` is a scratch directory that happens to be
# spelled like the documentation root, not a reference to it. Matched on
# underscore-separated parts rather than as substrings, so ``template_dir``
# is not read as a temporary.
TEMP_NAME_PARTS = ("tmp", "temp", "tmpdir", "tempdir")


class ProbeBroken(Exception):
    """The pin probe cannot answer, so the classifier must not answer either."""


class UnreadableReference(NamedTuple):
    """A documentation path a test builds that the probe cannot show is pinned.

    ``seen`` is the most the probe could recover of the path -- the empty
    string when it recovered nothing -- and is what the remediation hangs on:
    it names the directory whose contents are now unprotected.
    """

    source: str
    lineno: int
    expression: str
    seen: str

    def __str__(self) -> str:
        what = f"{self.seen!r}" if self.seen else "a path it could not recover"
        return (
            f"{self.source}:{self.lineno} builds {what} from "
            f"`{self.expression}` -- the probe cannot tell which documents "
            "that reads"
        )


class ProbeResult(NamedTuple):
    """What the pin probe recovered, and what it refused to guess at.

    ``examined`` counts the documentation references the refusal LOOKED at and
    discharged. A guard that never looks anywhere is green for the same reason
    a guard with nothing to find is, so the count is published rather than
    inferred: ``tests/unit/ci/test_classify_docs_only.py`` asserts it is
    non-zero on the real tree.
    """

    literals: set
    unreadable: List[UnreadableReference]
    examined: int


def classify(path: str) -> Optional[str]:
    """A ROOTED allowlist, not a suffix match.

    `*.md` anywhere would sweep in resources/knowledge/pack/**.md -- the
    shipped KB pack, which the Dockerfile COPYs and the ingestion tests read.
    Only these roots hold files that reach neither pytest nor the image: the
    Dockerfile COPYs requirements, pyproject, faultmaven/, alembic/ and
    resources/, and none of them appear below.

    Inertness is therefore decided by LOCATION, not by extension --
    `docs/foo.py` classifies inert, because nothing collects, imports or
    packages it from there. That is deliberate rather than an oversight, and it
    is the same surface the `--no-renames` note in the workflow is about: what
    makes a path safe is where it sits, so a path that MOVES has to be judged
    at both ends.

    Returns the reason the path is NOT inert, or ``None`` when it is.
    """
    if path.startswith(".github/"):
        return "a workflow or action definition"
    if path.startswith("docs/reference/api/"):
        return "a generated API artifact, not prose"
    if path.startswith("docs/") or path.startswith(".claude/"):
        return None
    if "/" not in path and path.endswith(".md"):
        return None
    return "not documentation"


def _keep(text: str) -> bool:
    return (
        4 <= len(text) <= 200 and "*" not in text and not text.startswith(("/", "http"))
    )


def _div_operands(node: ast.AST) -> list:
    """Flatten ``a / b / c`` left-to-right into ``[a, b, c]``."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _div_operands(node.left) + [node.right]
    return [node]


def _path_parts(value: str) -> list:
    """The parts of one spelled component that pathlib would keep."""
    return [part for part in value.split("/") if part not in ("", ".")]


def _join_constants(values: Iterable[str]) -> str:
    """Join spelled components the way ``pathlib``'s ``/`` joins them.

    Not ``"/".join``, which agrees with pathlib only on components that carry
    no separator of their own and no leading slash:

    * an ABSOLUTE component restarts the path -- ``Path("docs") / "/etc"`` is
      ``/etc``, not ``docs//etc``. The result then starts with ``/`` and
      ``_keep`` drops it, which is the right answer: an absolute path is not
      a repository-relative one.
    * a TRAILING SLASH is not a component boundary -- ``Path("docs/") /
      "guides"`` is ``docs/guides``. ``"/".join`` made it ``docs//guides``,
      a literal matching nothing, so the directory silently stopped pinning.
    * WHITESPACE is part of the name -- ``Path("docs") / " guides"`` is
      ``docs/ guides``. Components used to be ``.strip()``ed, which recovered
      ``docs/guides``: a literal for a directory that does not exist, while
      the one that does went unpinned. Both of those were silent, and both
      failed in the unsafe direction (#1549).
    """
    absolute = False
    parts: list = []
    for value in values:
        if value.startswith("/"):
            absolute = True
            parts = []
        parts.extend(_path_parts(value))
    text = "/".join(parts)
    return "/" + text if absolute else text


def _joined_paths(tree: ast.AST) -> set:
    """Paths a test spells with ``pathlib``'s ``/``, recovered as one literal.

    ``ROOT / "docs" / "architecture"`` names the directory
    ``docs/architecture``, but the literal walk sees only ``"docs"`` and
    ``"architecture"``, and ``pinned_by`` matches neither against
    ``docs/architecture/x.md``: the endswith arm wants a tail, and the
    directory arm is conditional on the literal containing a ``/``. So a
    directory built by division pins nothing beneath it.

    That gap is closed HERE, in what the walk recovers, rather than by
    loosening ``pinned_by`` to let any bare literal pin beneath itself. The
    two are not equivalent on this tree: ``"docs"`` is already a literal
    (twice, both times a chain component), so a bare-directory arm would pin
    every path under ``docs/`` and the classifier would answer ``false`` for
    every docs-only diff -- the feature dead, and dead silently, because a
    conservative ``false`` looks exactly like a correct one. Joining is also
    the truer reading: the components are how the path was *spelled*, not
    separate names, and recovering the author's path is the same class of fix
    as ``--no-renames`` -- get the detector's input right rather than teach
    the detector to guess.

    Of each chain, only its MAXIMAL all-constant tail is emitted, and only
    when that tail is at least two components long. Emitting every prefix of
    it would add ``docs/reference`` from ``ROOT / "docs" / "reference" /
    "api" / "openapi.json"`` and pin all of ``docs/reference/**`` on the
    strength of a test that reads one generated artifact; a one-component
    tail is already collected verbatim by the literal walk, and emitting it
    again would say nothing new.

    ‼ THIS FUNCTION RECOVERS ONE SPELLING, and recovers it only where the
    whole tail is constant. It is not a reader of paths in general, and
    nothing here should be read as "every chain". ``DOCS_DIR / name`` --
    the commonest real shape -- a chain split across statements,
    ``os.path.join``, ``"/".join``, an f-string and a multi-argument
    ``Path(...)`` all recover NOTHING from this walk, and a chain whose tail
    is a glob loses the directory along with the glob because ``_keep``
    drops the text.

    What stops each of those from silently disarming a document is not this
    function; it is ``unreadable_references``, which refuses to classify a
    diff at all when a test builds a documentation path this walk cannot
    read (#1549). Widening the grammar here is therefore optional, and
    narrowing it is safe: a shape this stops recovering becomes a refusal,
    which costs a pipeline run rather than a silent skip.
    """
    nested = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            if isinstance(node.left, ast.BinOp) and isinstance(node.left.op, ast.Div):
                nested.add(id(node.left))

    joined = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
            continue
        if id(node) in nested:
            continue
        tail = []
        for operand in reversed(_div_operands(node)):
            if isinstance(operand, ast.Constant) and isinstance(operand.value, str):
                tail.insert(0, operand.value)
            else:
                break
        if len(tail) >= 2:
            text = _join_constants(tail)
            if _keep(text):
                joined.add(text)
    return joined


def _pins_beneath(prefix: str, literals: set) -> bool:
    """True when EVERY path under ``prefix`` is already pinned.

    ``pinned_by``'s directory arm fires when a literal contains a ``/`` and
    the path starts with it, so a literal naming ``prefix`` or any ancestor
    of it that is itself a multi-component literal covers the whole subtree
    -- whatever the test goes on to read there.

    This is what discharges a reference the walk could not read whole. A
    test that spells ``docs/architecture`` and then joins an unknown
    filename onto it loses nothing: the directory pins, so every document
    in it is already executable. A test that spells ``docs`` and joins an
    unknown name onto THAT loses everything, because a one-component
    literal pins nothing beneath itself -- the decision taken in #1539,
    which cannot be revisited without answering ``false`` for every
    docs-only diff.
    """
    parts = prefix.split("/")
    for end in range(1, len(parts) + 1):
        candidate = "/".join(parts[:end])
        if "/" in candidate and candidate in literals:
            return True
    return False


def _names_in(node: ast.AST):
    """Every identifier ``node`` mentions, as a name or an attribute."""
    for inner in ast.walk(node):
        if isinstance(inner, ast.Name):
            yield inner.id
        elif isinstance(inner, ast.Attribute):
            yield inner.attr


def _spelled_temporary(identifier: str) -> bool:
    return any(part in TEMP_NAME_PARTS for part in identifier.lower().split("_"))


def _is_temporary(bases: Iterable[ast.AST], temporaries: frozenset) -> bool:
    """True when what a path is built ON is a pytest temporary directory.

    Only what comes BEFORE the documentation component is consulted, because
    that is what decides which tree the path lands in: ``tmp_path / "docs"``
    is a scratch directory, while ``ROOT / "docs" / tmp_name`` reads the
    repository's own and must still refuse.
    """
    for base in bases:
        for identifier in _names_in(base):
            if _spelled_temporary(identifier) or identifier in temporaries:
                return True
    return False


def _is_docish_name(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        ident = node.id
    elif isinstance(node, ast.Attribute):
        ident = node.attr
    else:
        return False
    lowered = ident.lower()
    return any(token in lowered for token in DOCISH_NAME_TOKENS)


class _ConstantPaths:
    """Resolves module names that are bound ONCE to a constant path.

    Not a step towards resolving names in ``_joined_paths``: nothing here is
    emitted as a literal, and a name resolved here can only ever DISCHARGE a
    refusal, never create a pin. That asymmetry is what makes it safe to be
    approximate -- the failure mode of resolving too little is a refusal,
    which costs a pipeline run.

    Assign-once is the whole rule. A name assigned twice, rebound by a loop,
    a ``with``, a comprehension or a function parameter resolves to nothing,
    because the probe would otherwise discharge a reference on the strength
    of a value that no longer holds at the point of use.
    """

    PATH_CTORS = ("Path", "PurePath", "PosixPath", "PurePosixPath")

    def __init__(self, nodes: Sequence[ast.AST]) -> None:
        bindings: dict = {}
        counts: dict = {}
        every: dict = {}

        def bind(target: ast.AST) -> None:
            for inner in ast.walk(target):
                if isinstance(inner, ast.Name):
                    counts[inner.id] = counts.get(inner.id, 0) + 1

        # ONE pass over a node list the caller already materialised. This
        # runs over every tests/**/*.py on every pull request, so each
        # extra `ast.walk` of the module is paid 800-odd times.
        for node in nodes:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    bind(target)
                    if isinstance(target, ast.Name):
                        every.setdefault(target.id, []).append(node.value)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                bind(node.target)
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                bind(node.target)
            elif isinstance(node, ast.withitem) and node.optional_vars is not None:
                bind(node.optional_vars)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args
                for arg in (
                    *args.posonlyargs,
                    *args.args,
                    *args.kwonlyargs,
                    args.vararg,
                    args.kwarg,
                ):
                    if arg is not None:
                        counts[arg.arg] = counts.get(arg.arg, 0) + 2

        for name, values in every.items():
            if counts.get(name) == 1:
                bindings[name] = values[0]
        self._bindings = bindings

        # Names that are bound to a documentation path SOMEWHERE, even when
        # they are rebound and so resolve to nothing. Evidence only -- it can
        # make the probe refuse, never discharge. Without it a directory
        # reached through a rebound name spelled unlike documentation
        # (`WALKED = ROOT / "docs"`, reassigned) is invisible.
        self.documentary = frozenset(
            name
            for name, values in every.items()
            if any(
                _documentation_prefixes([self.resolve(value) or ""]) for value in values
            )
        )

        # A name bound once to something rooted at a temporary directory is
        # itself a temporary directory, however it is spelled -- `base =
        # tmp_path_factory.mktemp("kb")` is the shape that needs this.
        # Iterated to a fixed point so a chain of them propagates, over
        # identifier sets gathered ONCE: re-walking each bound expression per
        # iteration made this quadratic in the module's binding count.
        mentions = {
            name: frozenset(_names_in(value)) for name, value in bindings.items()
        }
        temporary = {
            name
            for name, identifiers in mentions.items()
            if any(_spelled_temporary(identifier) for identifier in identifiers)
        }
        while True:
            grown = {
                name
                for name, identifiers in mentions.items()
                if name not in temporary and identifiers & temporary
            }
            if not grown:
                break
            temporary |= grown
        self.temporary = frozenset(temporary)

    def resolve(self, node: Optional[ast.AST], depth: int = 0) -> Optional[str]:
        """The constant path ``node`` spells, or ``None``."""
        if node is None or depth > 5:
            return None
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else None
        if isinstance(node, ast.Name):
            return self.resolve(self._bindings.get(node.id), depth + 1)
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else None
            name = name or getattr(func, "id", None)
            if name in self.PATH_CTORS and node.args and not node.keywords:
                values = [self.resolve(arg, depth + 1) for arg in node.args]
                if all(value is not None for value in values):
                    return _join_constants(values)
            return None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            operands = _div_operands(node)
            values = [self.resolve(operand, depth + 1) for operand in operands]
            if values and values[0] is None:
                # An unknown ROOT is fine: what pins is repository-relative,
                # so the chain is resolved from its first constant onwards.
                values = values[1:]
            if values and all(value is not None for value in values):
                return _join_constants(values)
            return None
        return None


def _segment_runs(segments: Sequence[Optional[str]], separator: str) -> list:
    """Each maximal run of adjacent resolved segments, joined as spelled.

    Returned with the index the run STARTS at, because a documentation root
    can be spelled across several segments -- ``"do" + "cs"`` resolves to
    ``docs`` in the run and in no single segment of it -- and the caller
    needs to know which operands come before it.
    """
    runs = []
    start: Optional[int] = None
    current: list = []
    for index, segment in enumerate(segments):
        if segment is None:
            if current:
                runs.append((start, separator.join(current)))
                current, start = [], None
        else:
            if start is None:
                start = index
            current.append(segment)
    if current:
        runs.append((start, separator.join(current)))
    return runs


def _documentation_prefixes(runs: Iterable[str]) -> list:
    """The part of each run from its first documentation root onwards.

    Stops at the first component carrying a placeholder or a glob, because
    nothing after one is known. ``"docs/%s" % name`` would otherwise be
    discharged by the literal ``docs/%s`` -- which the literal walk collects
    from the template itself, and which pins a directory that does not
    exist. A guard discharged by its own input is no guard.
    """
    prefixes = []
    for run in runs:
        parts = _path_parts(run)
        for index, part in enumerate(parts):
            if part not in DOC_ROOTS:
                continue
            known = []
            for component in parts[index:]:
                if any(marker in component for marker in "%{}*"):
                    break
                known.append(component)
            if known:
                prefixes.append("/".join(known))
            break
    return prefixes


def _flat_operands(node: ast.AST, optype) -> list:
    """Flatten a left-associative chain of one operator into its operands."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, optype):
        return _flat_operands(node.left, optype) + [node.right]
    return [node]


class _Composition(NamedTuple):
    """One expression that builds a string out of pieces.

    ``segments`` and ``operands`` are positionally aligned: a segment is the
    constant that operand spells, or ``None`` when the probe cannot tell.
    ``separator`` is what the idiom puts between adjacent pieces -- ``"/"``
    for the path builders, the joining string for ``str.join``, and ``""``
    for the text idioms, whose pieces abut.
    """

    node: ast.AST
    segments: list
    operands: list
    separator: str


def _compositions(nodes: Sequence[ast.AST], paths: _ConstantPaths):
    """Every expression under ``tree`` that builds a string out of pieces.

    Idiom recognition decides only whether the refusal LOOKS at an
    expression, never whether it fires -- which is why an idiom missing from
    this list is a gap in COVERAGE (one more unreadable shape that stays
    silent) and never a false refusal. Division, ``/=``, concatenation,
    ``%``, f-strings, ``.format()``, ``str.join``, ``os.path.join`` and a
    multi-argument ``Path(...)`` are the ones #1549 enumerated.
    """
    nested = set()
    for node in nodes:
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.Add)):
            left = node.left
            if isinstance(left, ast.BinOp) and isinstance(left.op, type(node.op)):
                nested.add(id(left))

    def built(operands, separator):
        return _Composition(
            node=node,
            segments=[paths.resolve(operand) for operand in operands],
            operands=list(operands),
            separator=separator,
        )

    for node in nodes:
        if isinstance(node, ast.AugAssign):
            if isinstance(node.op, ast.Div):
                yield built([node.target, node.value], "/")
            elif isinstance(node.op, ast.Add):
                yield built([node.target, node.value], "")
        elif isinstance(node, ast.BinOp) and id(node) not in nested:
            if isinstance(node.op, ast.Div):
                yield built(_div_operands(node), "/")
            elif isinstance(node.op, ast.Add):
                yield built(_flat_operands(node, ast.Add), "")
            elif isinstance(node.op, ast.Mod):
                yield built([node.left], "")
        elif isinstance(node, ast.JoinedStr):
            yield built(node.values, "")
        elif isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            if name in _ConstantPaths.PATH_CTORS and node.args:
                # One argument too: `Path("docs")` is a handle on the
                # documentation root, and nothing built from it downstream
                # need be a composition this walk can see. That is not the
                # #1539 bare-directory decision being revisited -- a bare
                # literal still PINS nothing, and this makes it pin nothing
                # either. It makes the gate refuse rather than sweep.
                yield built(node.args, "/")
            elif name == "joinpath" and isinstance(func, ast.Attribute):
                yield built([func.value, *node.args], "/")
            elif name == "format" and isinstance(func, ast.Attribute):
                yield built([func.value], "")
            elif name == "join" and isinstance(func, ast.Attribute):
                owner = func.value
                if getattr(owner, "attr", "") == "path" or getattr(owner, "id", "") in (
                    "path",
                    "posixpath",
                    "ntpath",
                    "os",
                ):
                    yield built(node.args, "/")
                else:
                    separator = paths.resolve(owner)
                    if separator is not None:
                        items: list = []
                        for arg in node.args:
                            if isinstance(arg, (ast.List, ast.Tuple, ast.Set)):
                                items.extend(arg.elts)
                            else:
                                items.append(arg)
                        yield built(items, separator)


def unreadable_references(tree: ast.AST, literals: set, source: str = "<tree>"):
    """Documentation paths a module builds that the probe cannot show are pinned.

    The fail-closed arm for the pin probe's GRAMMAR, matching the one it
    already has for its positive controls (#1549). ``_joined_paths`` reads one
    spelling of one idiom; every other way of building a path recovers
    nothing, and a directory that recovers nothing pins nothing beneath it --
    so a document a test reads is classified inert, the suites skip, and a
    skipped required check reads as passing. Refusing is the only answer that
    does not need a grammar covering the shapes someone thought of.

    ‼ THE SCOPE IS THE WHOLE COST OF THIS. The trigger is *a test builds a
    DOCUMENTATION path the probe cannot read*, not *the probe cannot read an
    expression*: the test tree is full of f-strings, ``.format()`` and joins
    that have nothing to do with ``docs/``, and refusing on those would force
    the full suite on every docs-only diff -- deleting the feature rather than
    hardening it. Three things keep it narrow, and each one is a control in
    the test module:

    * a path build only counts when it MENTIONS documentation -- a component
      under ``DOC_ROOTS``, or, where a path build's operand resolves to no
      constant at all, an identifier named like one;
    * it is DISCHARGED when the probe can show the path is pinned anyway,
      which covers the real shape on this tree today: a directory spelled
      whole and an unknown filename joined onto it;
    * a path rooted at a pytest temporary directory is not a repository path.

    Measured on the tree this shipped against: zero refusals over 10
    documentation references examined, so a correctly scoped refusal costs
    nothing until someone writes one of these shapes.

    ‼ WHAT IT STILL DOES NOT SEE, stated rather than left to be discovered.
    Each was planted against this implementation and missed, and each had
    ZERO live sites under ``tests/`` when it shipped:

    * a documentation path IMPORTED from another module -- resolution is
      per-module, so a constant defined elsewhere is just a name here;
    * a path read out of a non-Python fixture -- the probe parses ``.py``,
      which is also why the literal walk has never seen one;
    * a document reached through a helper in the application package, and
      ``Path(*parts)``, ``os.path.abspath("docs")``, ``str.replace`` and
      slicing, none of which are idioms ``_compositions`` looks at;
    * a ROOT-LEVEL ``*.md`` built dynamically (``ROOT / f"{name}.md"``, or
      ``ROOT.rglob("*.md")``). It names no documentation root, so it is
      indistinguishable from any other dynamic filename -- and refusing on
      every constant ending in ``.md`` would fire on the temporary markdown
      files the knowledge tests write by the dozen.

    Returns ``(refusals, examined)``, where ``examined`` counts the
    documentation references that were checked and discharged.
    """
    nodes = list(ast.walk(tree))
    paths = _ConstantPaths(nodes)
    refusals = []
    examined = 0
    reported = set()
    for built in _compositions(nodes, paths):
        segments, operands = built.segments, built.operands

        skeleton = "".join(segment for segment in segments if segment)
        if "://" in skeleton or skeleton.startswith(("http", "mailto:")):
            continue  # a URL, not a path into this repository

        found = [
            (start, prefix)
            for start, text in _segment_runs(segments, built.separator)
            for prefix in _documentation_prefixes([text])
        ]
        prefixes = [prefix for _, prefix in found]
        if found:
            # Everything the path is built ON, up to the run that names
            # documentation.
            marker = found[0][0]
        else:
            # No piece SPELLS a document, so the evidence has to be a piece
            # that resolves to nothing but is known to be one. Two strengths,
            # and they are trusted differently:
            #
            # * BOUND to a documentation path somewhere in this module. That
            #   is a fact about the code, so it counts whatever the idiom.
            # * merely NAMED like one -- `DOCS_DIR / name`, the commonest
            #   real shape, and a `docs_dir` fixture with it. A guess, so it
            #   counts only for a path BUILDER: an identifier says nothing
            #   about what an f-string or a `.format()` is assembling, and
            #   the test tree is full of `document_lines` and
            #   `MAX_BULK_DOCUMENT_IDS`.
            named = [
                index
                for index, (segment, operand) in enumerate(zip(segments, operands))
                if segment is None
                and (
                    (isinstance(operand, ast.Name) and operand.id in paths.documentary)
                    or (built.separator == "/" and _is_docish_name(operand))
                )
            ]
            if not named:
                continue
            marker = named[0]

        if _is_temporary(operands[:marker], paths.temporary):
            continue
        if prefixes and all(_pins_beneath(prefix, literals) for prefix in prefixes):
            examined += 1
            continue

        node = built.node
        key = (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))
        if key in reported:
            continue
        reported.add(key)
        refusals.append(
            UnreadableReference(
                source=source,
                lineno=getattr(node, "lineno", 0),
                expression=" ".join(ast.unparse(node).split())[:160],
                seen=prefixes[0] if prefixes else "",
            )
        )
    return refusals, examined


def probe(tests_dir: pathlib.Path) -> ProbeResult:
    """Collect the pin probe's literals, then its refusals.

    Two passes, because a refusal is decided AGAINST the literal set: whether
    a reference the walk could not read matters depends on whether some other
    test already pinned the directory it names. One pass could only refuse on
    everything it had not seen yet.
    """
    sources = sorted(tests_dir.rglob("*.py"))
    literals = set()
    for source in sources:
        try:
            tree = ast.parse(source.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            raise ProbeBroken(f"cannot parse {source} -- refusing to classify")
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value.strip()
                if _keep(text):
                    literals.add(text)
        literals |= _joined_paths(tree)

    for control in POSITIVE_CONTROLS:
        if control not in literals:
            raise ProbeBroken(
                f"the pin probe found no literal {control!r} under "
                "tests/ -- the probe is broken, refusing to classify"
            )

    unreadable: List[UnreadableReference] = []
    examined = 0
    for source in sources:
        tree = ast.parse(source.read_text(encoding="utf-8", errors="replace"))
        found, checked = unreadable_references(tree, literals, str(source))
        unreadable.extend(found)
        examined += checked
    return ProbeResult(literals=literals, unreadable=unreadable, examined=examined)


def collect_literals(tests_dir: pathlib.Path) -> set:
    """Every string literal under ``tests_dir`` that could name a document.

    Documentation a test READS is not inert: tests pin CLAUDE.md's migration
    head and its reasoning-intent call-site table, the user guide's Groq
    models, and the key files under docs/architecture/. Ask the test tree which
    paths those are instead of keeping a second list here that drifts silently
    -- a new pinning test re-arms the suites for its document simply by
    existing.

    String literals only. A plain grep over tests/ also matches the dozens of
    docstrings that merely CITE a document, which would call nearly every docs
    change executable and give back none of the time.

    Raises ``ProbeBroken`` when a file will not parse or a positive control has
    gone missing.
    """
    return probe(tests_dir).literals


def pinned_by(path: str, literals: Iterable[str]) -> Optional[str]:
    """Loose on purpose: a false match costs one full pipeline run, a missed
    one merges a red main. Tests name a document by full path
    (`docs/getting-started/user-guide.md`), by a tail they join onto a root
    (`core-architecture/architectural-design-principles.md`), and by the
    directory alone (`docs/architecture`)."""
    for literal in literals:
        if literal == path:
            return literal
        if path.endswith("/" + literal):
            return literal
        if "/" in literal and path.startswith(literal + "/"):
            return literal
    return None


def decide(
    changed: Iterable[str],
    literals: Iterable[str],
    log=print,
    unreadable: Sequence[UnreadableReference] = (),
) -> str:
    """``"true"`` when every changed path is inert, ``"false"`` otherwise.

    An ``unreadable`` reference forces ``"false"`` for the WHOLE diff rather
    than for some subset of it. Nothing narrower is honest: the probe does
    not know which documents the reference reads, so it does not know which
    paths in the diff it covers (#1549).
    """
    verdict = "true"
    for reference in unreadable:
        log(f"  {reference}")
        verdict = "false"
    for path in changed:
        reason = classify(path)
        if reason is not None:
            log(f"  {path}: {reason}")
            verdict = "false"
            continue
        literal = pinned_by(path, literals)
        if literal is not None:
            log(f"  {path}: a test reads it (tests/ names {literal!r})")
            verdict = "false"
            continue
        log(f"  {path}: inert")
    return verdict


def main() -> int:
    output = os.environ.get("GITHUB_OUTPUT", "/dev/null")

    def emit(value, why):
        print(f"{why} -> docs_only={value}")
        with open(output, "a", encoding="utf-8") as fh:
            fh.write(f"docs_only={value}\n")

    if os.environ.get("RESOLVED") != "true":
        emit("false", "the pull request's file list could not be resolved")
        return 0

    changed = [
        line.strip()
        for line in pathlib.Path("changed.txt").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not changed:
        emit("false", "the diff is empty")
        return 0

    tests = pathlib.Path("tests")
    if not tests.is_dir():
        print("::error::tests/ is missing -- the pin probe cannot run")
        return 1

    try:
        result = probe(tests)
    except ProbeBroken as exc:
        print(f"::error::{exc}")
        return 1

    if result.unreadable:
        print(
            f"{len(result.unreadable)} documentation reference(s) under tests/ "
            "cannot be read by the pin probe. Spell the path whole "
            '(ROOT / "docs" / "section" / "file.md") or name the directory in '
            "one literal, so the probe can tell which documents it reads."
        )
    emit(
        decide(changed, result.literals, unreadable=result.unreadable),
        f"{len(changed)} changed file(s), {result.examined} documentation "
        "reference(s) checked",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
