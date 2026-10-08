"""Structural pin for #1882: the engine performs no case-scoped commit.

A turn commits once, at the service, with the rows its ``TurnCommitPlan``
carries. These scans keep a mid-turn commit from coming back under
``core/investigation/milestone_engine/``: no repository ``save``, no
``add_report`` / ``create_checkpoint`` / ``generate_reports`` (each a write of
its own), and no background task that is not behind the turn's commit gate.

The documented exceptions, and why each is not a turn write:

- ``turn_commit.commit_turn_plan`` IS the commit; only the service's settlement
  calls it (pinned below).
- ``runbook_creation.RunbookCreator._run_runbook_conversion``: its completion
  notice save runs in the background conversion, which awaits the turn's commit
  gate before doing anything, so it only ever writes after the turn committed.
- ``runbook_creation.RunbookCreator.handle_runbook_creation``: spawns that
  conversion, passing it ``committed=plan.gate()``.
- ``vectorization.py``: the evidence vectorization tasks write a derived index
  of evidence that is already committed (Chroma plus a scoped
  ``update_evidence_vectorized``), whose truth does not depend on the turn.

In the service, background work is the shielded settlement itself and the
post-commit task it spawns after the commit (the upload links, best effort).

Read by AST, not by substring, so a comment or a docstring naming a call is not
mistaken for one, and a call written across lines is not missed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
ENGINE_DIR = ROOT / "faultmaven" / "core" / "investigation" / "milestone_engine"
SERVICE_DIR = (
    ROOT
    / "faultmaven"
    / "modules"
    / "agent"
    / "domain"
    / "services"
    / "investigation_service"
)
ROUTE = ROOT / "faultmaven" / "modules" / "case" / "api" / "routes" / "conversation.py"

#: Writes that commit on their own.
OWN_COMMIT_CALLS = {"add_report", "create_checkpoint", "generate_reports"}
#: Spawners of background work.
SPAWNS = {"create_task", "ensure_future"}


def _calls(path: Path):
    """Every call in ``path`` with the name of the function it sits in."""
    tree = ast.parse(path.read_text())
    found = []

    def visit(node, func):
        for child in ast.iter_child_nodes(node):
            inner = (
                child.name
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                else func
            )
            if isinstance(child, ast.Call):
                found.append((child, func))
            visit(child, inner)

    visit(tree, None)
    return found


def _callee(call: ast.Call) -> str | None:
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


def _repository_save(call: ast.Call) -> bool:
    f = call.func
    return (
        isinstance(f, ast.Attribute)
        and f.attr == "save"
        and ast.unparse(f.value).split(".")[-1].lstrip("_")
        in {"repository", "repo", "case_repo", "case_repository"}
    )


def _engine_files() -> list[Path]:
    files = sorted(ENGINE_DIR.glob("*.py"))
    assert len(files) > 20, f"scanned the wrong directory: {ENGINE_DIR}"
    return files


class TestTheEngineCommitsNothing:
    def test_no_repository_save_outside_the_documented_exceptions(self):
        allowed = {
            ("turn_commit.py", "commit_turn_plan"),
            ("runbook_creation.py", "_run_runbook_conversion"),
        }
        found = {
            (path.name, func)
            for path in _engine_files()
            for call, func in _calls(path)
            if _repository_save(call)
        }
        assert found <= allowed, f"a mid-turn commit is back: {found - allowed}"
        # Positive control: the scan sees the two saves that are allowed.
        assert found == allowed

    def test_no_write_that_commits_on_its_own(self):
        found = [
            (path.name, func, _callee(call))
            for path in _engine_files()
            for call, func in _calls(path)
            if _callee(call) in OWN_COMMIT_CALLS
        ]
        assert found == [], f"a write that commits on its own: {found}"

    def test_every_background_task_is_gated_or_independent_of_the_turn(self):
        found = []
        for path in _engine_files():
            for call, func in _calls(path):
                if _callee(call) not in SPAWNS:
                    continue
                if path.name == "vectorization.py":
                    continue
                spawned = call.args[0] if call.args else None
                gated = isinstance(spawned, ast.Call) and any(
                    k.arg == "committed" and ast.unparse(k.value) == "plan.gate()"
                    for k in spawned.keywords
                )
                found.append((path.name, func, gated))
        assert found == [
            ("runbook_creation.py", "handle_runbook_creation", True)
        ], f"an ungated background task: {found}"

    def test_the_conversion_awaits_its_gate_before_anything_else(self):
        """The notice save is allowed only because the conversion waits for the
        turn's commit first: the gate is the first thing it awaits."""
        tree = ast.parse((ENGINE_DIR / "runbook_creation.py").read_text())
        func = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
            and n.name == "_run_runbook_conversion"
        )
        first_await = next(n for n in ast.walk(func) if isinstance(n, ast.Await))
        assert "committed" in ast.unparse(first_await), ast.unparse(first_await)

    def test_only_the_settlement_calls_the_commit(self):
        callers = {
            path.relative_to(ROOT).as_posix()
            for directory in (ENGINE_DIR, SERVICE_DIR)
            for path in directory.glob("*.py")
            for call, _ in _calls(path)
            if _callee(call) == "commit_turn_plan"
        }
        assert callers == {
            "faultmaven/modules/agent/domain/services/investigation_service/"
            "turn_settlement.py"
        }, callers


class TestTheServiceSpawnsOnlyAfterTheCommit:
    def test_background_work_in_the_service_is_the_settlement_and_post_commit(
        self,
    ):
        """The service spawns two things: the shielded settlement itself, and
        the post-commit work (the upload links) it hands off the response
        path. Nothing else runs in the background of a turn."""
        found = sorted(
            (path.name, func)
            for path in SERVICE_DIR.glob("*.py")
            for call, func in _calls(path)
            if _callee(call) in SPAWNS
        )
        assert found == [
            ("turn_settlement.py", "_spawn_post_commit"),
            ("turn_settlement.py", "run_settlement_shielded"),
        ], found

    def test_post_commit_work_is_spawned_only_after_the_commit(self):
        tree = ast.parse((SERVICE_DIR / "turn_settlement.py").read_text())
        settle = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "settle_turn"
        )
        lines = {
            _callee(c): c.lineno for c in ast.walk(settle) if isinstance(c, ast.Call)
        }
        assert lines["commit_turn_plan"] < lines["_spawn_post_commit"]
        spawners = {
            func
            for call, func in _calls(SERVICE_DIR / "turn_settlement.py")
            if _callee(call) == "_spawn_post_commit"
        }
        assert spawners == {"settle_turn"}, spawners


class TestTheRouteBoundsOnlyThePreparation:
    def test_the_wait_for_wraps_prepare_turn_and_nothing_else(self):
        """A ``wait_for`` around the commit could cancel it mid-transaction
        (outcome unknown) or after it (a 504 for a committed turn)."""
        wrapped = [
            ast.unparse(call.args[0])
            for call, func in _calls(ROUTE)
            if func == "submit_turn" and _callee(call) == "wait_for"
        ]
        assert len(wrapped) == 1, wrapped
        assert wrapped[0].startswith("investigation_service.prepare_turn("), wrapped
        commits = [
            call
            for call, func in _calls(ROUTE)
            if func == "submit_turn" and _callee(call) == "commit_turn"
        ]
        assert len(commits) == 1
