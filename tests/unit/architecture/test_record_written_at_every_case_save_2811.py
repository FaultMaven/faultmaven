"""Every write of a case either folds its outcome record or changes no field
the record reads (#2811, the save census).

The incident: #2658, step 0's menu close, saved a case it had changed with
no record write, so the stored record disagreed with the saved case; #2811's
pull found the other paths the same way, one at a time (the deterministic
step-0 turns, ``PATCH /cases/{id}``, the metadata edit, case creation, the
out-of-band reclassification). The record is now folded at the two save
helpers (``commit_turn_plan``, ``update_case_with_retry``) after the save's
lapse, at creation and the reclassification, and in the metadata edit's own
scoped write. This census lists every reference to a case repository's write in
``faultmaven/`` outside the repositories themselves, keyed by
``(path, qualname)``:

* ``WRITES``: the function folds the record before the write; it must
  reference one of ``RECORD_WRITERS`` (a revert of the fold fails here);
* any other entry names the field the write changes and why the record does
  not read it.

A new write, a moved one or a changed count fails until it is declared.
Writes of other aggregates that share a method name (a user, a session, the
redaction context) are excluded by receiver.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

from tests.source_tree import parse_file

pytestmark = [pytest.mark.unit, pytest.mark.architecture]

_ROOT = Path(__file__).resolve().parents[3]
_CASE = "faultmaven/modules/case/"

#: A case repository's writes.
WRITE_METHODS = frozenset(
    {
        "save",
        "save_progress",
        "update_metadata_fields",
        "reassign_driver",
        "release_driver",
        "release_drivers_for_team_leave",
        "release_drivers_for_deactivation",
        "delete_evidence",
        "delete_uploaded_file",
        "add_message",
        "update_evidence_vectorized",
    }
)

#: Receivers of a same-named method on another aggregate.
OTHER_RECEIVERS = frozenset(
    {"redaction_ctx", "user_repo", "user_repository", "session_store"}
)

#: The repositories themselves (they implement the writes), and the user store.
EXCLUDED = (
    _CASE + "infrastructure/",
    "faultmaven/infrastructure/persistence/user_repository.py",
)

#: What a ``WRITES`` site, or a function nested in it, must reference.
RECORD_WRITERS = frozenset(
    {"record_before_save", "record_case_outcome", "compute_case_outcome"}
)

WRITES = "writes the record"

#: (path, qualname) -> ({method: references}, why).
SAVE_SITES: dict[tuple[str, str], tuple[dict[str, int], str]] = {
    # The two save helpers: lapse, fold, save (``record_before_save``).
    (
        "faultmaven/core/investigation/milestone_engine/turn_commit.py",
        "commit_turn_plan",
    ): (
        {"save": 1},
        WRITES,
    ),
    (_CASE + "utils/retry.py", "update_case_with_retry"): ({"save": 1}, WRITES),
    # The metadata edit: a description outside an investigation is the
    # statement in force; its record rides the same scoped write (the other
    # call writes a title, or a description that did not change).
    (_CASE + "domain/services/case_service.py", "CaseService._write_metadata"): (
        {"update_metadata_fields": 2},
        WRITES,
    ),
    # A new case's first record, and the reclassification, which re-dates its
    # file's rows (their coverage, read by the mitigation's relief and span).
    (_CASE + "domain/services/case_service.py", "CaseService.create_case"): (
        {"save": 1},
        WRITES,
    ),
    (
        "faultmaven/modules/agent/domain/services/investigation_service/reclassification.py",
        "EvidenceReclassifier.reclassify_out_of_band",
    ): ({"save": 1}, WRITES),
    ("faultmaven/cli/backfill_case_outcome.py", "backfill_case_outcome"): (
        {"save_progress": 1},
        WRITES,
    ),
    # No record input.
    (
        "faultmaven/core/investigation/milestone_engine/runbook_creation.py",
        "RunbookCreator._run_runbook_conversion",
    ): ({"save": 1}, "the runbook conversion's notice and state, read by no fold"),
    (
        "faultmaven/core/investigation/milestone_engine/vectorization.py",
        "EvidenceVectorizer._vectorize_evidence",
    ): ({"update_evidence_vectorized": 1}, "an evidence row's vectorized flag"),
    (_CASE + "domain/services/case_driver.py", "CaseDriverMixin.reassign_driver"): (
        {"reassign_driver": 1},
        "driver_id",
    ),
    (_CASE + "domain/services/case_driver.py", "CaseDriverMixin._release"): (
        {"release_driver": 1},
        "driver_id",
    ),
    (_CASE + "api/routes/driver.py", "reassign_case_driver"): (
        {"reassign_driver": 1},
        "driver_id, through the service",
    ),
    ("faultmaven/cli/personal_tenant.py", "reanchor._release_driven"): (
        {"release_driver": 1},
        "driver_id",
    ),
    (
        "faultmaven/modules/auth/domain/services/team_service.py",
        "TeamService.leave_team",
    ): (
        {"release_drivers_for_team_leave": 2},
        "driver_id",
    ),
    (
        "faultmaven/modules/auth/domain/services/user_service.py",
        "UserService._release_driven_cases",
    ): ({"release_drivers_for_deactivation": 1}, "driver_id"),
}


def _receiver(node: ast.Attribute) -> str:
    value = node.value
    if isinstance(value, ast.Attribute):
        return value.attr
    return value.id if isinstance(value, ast.Name) else ""


def _scan() -> tuple[dict, dict]:
    """The writes found, and the record writers each function references."""
    found: dict[tuple[str, str], Counter] = {}
    writers: dict[tuple[str, str], set] = {}
    for path in sorted((_ROOT / "faultmaven").rglob("*.py")):
        rel = path.relative_to(_ROOT).as_posix()
        if rel.startswith(EXCLUDED):
            continue

        def visit(node, quals, rel=rel):
            for child in ast.iter_child_nodes(node):
                inner = quals
                if isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    inner = quals + [child.name]
                key = (rel, ".".join(inner) or "<module>")
                if (
                    isinstance(child, ast.Attribute)
                    and isinstance(child.ctx, ast.Load)
                    and child.attr in WRITE_METHODS
                    and _receiver(child) not in OTHER_RECEIVERS
                ):
                    found.setdefault(key, Counter())[child.attr] += 1
                if isinstance(child, ast.Name):
                    name = child.id
                elif isinstance(child, ast.Attribute):
                    name = child.attr
                else:
                    name = None
                if name in RECORD_WRITERS:
                    writers.setdefault(key, set()).add(name)
                visit(child, inner)

        visit(parse_file(path), [])
    return found, writers


def test_every_case_write_is_declared():
    found, _ = _scan()
    problems = []
    for key in sorted(set(found) | set(SAVE_SITES)):
        declared = SAVE_SITES.get(key, ({}, ""))[0]
        if dict(found.get(key, {})) != declared:
            problems.append(
                f"{key[0]}::{key[1]}: declared {declared}, found {dict(found.get(key, {}))}"
            )
    assert not problems, (
        "a case is written where the save census does not say:\n    "
        + "\n    ".join(problems)
        + "\n  Fold the record before the write (record_before_save), or "
        "declare the field it changes that no fold reads (#2811)."
    )


def test_every_write_that_says_so_folds_the_record():
    """Fails when a helper's fold is reverted: #2658 again."""
    _, writers = _scan()
    missing = [
        f"{path}::{qualname}"
        for (path, qualname), (_methods, why) in SAVE_SITES.items()
        if why == WRITES and not _references_writer(writers, path, qualname)
    ]
    assert not missing, f"declared as writing the record, but fold none: {missing}"


def _references_writer(writers: dict, path: str, qualname: str) -> bool:
    """The function, or a function nested in it, references a record writer."""
    return any(
        p == path and (q == qualname or q.startswith(qualname + "."))
        for p, q in writers
    )
