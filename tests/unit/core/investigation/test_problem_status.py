"""``problem_status`` has one writer, and ``symptom_verified`` is its view.

The boolean used to be assigned in four places (the milestone loop, the
KB-resolution collapse, the evidence-citation revert, the retraction). It is now
derived, and every transition goes through ``core/investigation/problem_status``.
The scan below fails on any other assignment in the package.
"""

import ast
from pathlib import Path

import pytest

import faultmaven
from faultmaven.core.investigation import problem_status
from faultmaven.core.investigation.problem_status import (
    cause_work_accepted,
    unverify_problem,
    verify_problem,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    InquiryData,
    InvestigationProgress,
    ProblemStatus,
)

pytestmark = pytest.mark.unit

_PACKAGE = Path(faultmaven.__file__).parent
_WRITER = Path(problem_status.__file__).resolve()


def _case() -> Case:
    return Case(
        case_id="case_00000000000a",
        user_id="u",
        enterprise_id="e",
        title="t",
        description="checkout orders failing",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="checkout orders failing",
            problem_statement_confirmed=True,
        ),
    )


def test_a_new_progress_is_unverified():
    progress = InvestigationProgress()
    assert progress.problem_status == ProblemStatus.UNVERIFIED
    assert progress.symptom_verified is False


def test_symptom_verified_is_derived_and_read_only():
    progress = InvestigationProgress(problem_status=ProblemStatus.VERIFIED)
    assert progress.symptom_verified is True
    with pytest.raises(AttributeError):
        progress.symptom_verified = False  # type: ignore[misc]


def test_symptom_verified_is_not_serialized():
    """One stored fact: the derived view never reaches the persisted blob."""
    dumped = InvestigationProgress(problem_status=ProblemStatus.VERIFIED).model_dump(
        mode="json"
    )
    assert dumped["problem_status"] == "verified"
    assert "symptom_verified" not in dumped


def test_verify_and_unverify_report_the_edge_only():
    case = _case()
    assert cause_work_accepted(case) is False

    assert verify_problem(case, via="test") is True
    assert case.progress.problem_status == ProblemStatus.VERIFIED
    assert cause_work_accepted(case) is True
    assert verify_problem(case, via="test") is False

    assert unverify_problem(case, via="test") is True
    assert case.progress.problem_status == ProblemStatus.UNVERIFIED
    assert unverify_problem(case, via="test") is False


def _status_writes(tree: ast.AST) -> list[int]:
    """Lines that assign ``<x>.problem_status`` or ``setattr(x, "problem_status", ...)``."""
    lines = []
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Attribute) and target.attr == "problem_status":
                lines.append(node.lineno)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "setattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "problem_status"
        ):
            lines.append(node.lineno)
    return lines


def test_problem_status_has_one_writer():
    offenders = {}
    writer_lines: list[int] = []
    for path in _PACKAGE.rglob("*.py"):
        lines = _status_writes(ast.parse(path.read_text(), filename=str(path)))
        if path.resolve() == _WRITER:
            writer_lines = lines
        elif lines:
            offenders[str(path.relative_to(_PACKAGE))] = lines

    # Positive control: the scan does see the writer's own assignment, so an
    # empty offender list means "none", not "the scanner saw nothing".
    assert writer_lines, "the scan found no assignment in problem_status.py"
    assert offenders == {}, (
        "problem_status is assigned outside core/investigation/problem_status.py; "
        f"route these through verify_problem / unverify_problem: {offenders}"
    )
