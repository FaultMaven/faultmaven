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
from typing import Iterable, Optional

# Two literals that exist under tests/ today. The pin probe reports by finding
# nothing, and a probe pointed at the wrong tree reports the same way -- every
# document inert, every suite skipped. If these stop being found, the probe is
# what changed, and it refuses to answer rather than sweep.
POSITIVE_CONTROLS = ("CLAUDE.md", "docs/architecture")


class ProbeBroken(Exception):
    """The pin probe cannot answer, so the classifier must not answer either."""


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

    Only the MAXIMAL chain is emitted. Emitting every prefix would add
    ``docs/reference`` from ``ROOT / "docs" / "reference" / "api" /
    "openapi.json"`` and pin all of ``docs/reference/**`` on the strength of a
    test that reads one generated artifact.
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
                tail.insert(0, operand.value.strip())
            else:
                break
        if len(tail) >= 2:
            text = "/".join(tail)
            if _keep(text):
                joined.add(text)
    return joined


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
    literals = set()
    for source in sorted(tests_dir.rglob("*.py")):
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
    return literals


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


def decide(changed: Iterable[str], literals: Iterable[str], log=print) -> str:
    """``"true"`` when every changed path is inert, ``"false"`` otherwise."""
    verdict = "true"
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
        literals = collect_literals(tests)
    except ProbeBroken as exc:
        print(f"::error::{exc}")
        return 1

    emit(decide(changed, literals), f"{len(changed)} changed file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
