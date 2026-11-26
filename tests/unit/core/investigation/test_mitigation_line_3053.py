"""The mitigation line and the readers it would otherwise contradict (#3053
S3d-4, rulings Q7-Q10 of 2026-11-25 and D1-D4 of round 118).

The outcome statement states the mitigation's facts from the turn-start and
turn-final records' ``mitigation``: what the problem did after it (seen gone,
seen gone then seen again, still present), the user's effective declaration
and the stall's proceed. The milestone status reads the record's
``milestones_shown`` whole and its disposition line only while a declaration
is effective, and the statement guards' bar reads the record's own source
derivations, so after a bound relapse, or a declaration F5 voids, a revision
or a false-alarm finding is admitted again. The copy is pinned verbatim here.
No live LLM.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from faultmaven.api.contract_version import API_CONTRACT_VERSION
from faultmaven.core.investigation import cause_assurance
from faultmaven.core.investigation import outcome as outcome_module
from faultmaven.core.investigation.cause_assurance import (
    _mitigation_declared_stable,
    _mitigation_verified,
    mitigation_observation,
)
from faultmaven.core.investigation.milestone_engine.transition_consent import (
    revision_offer_key,
)
from faultmaven.core.investigation.narration.codes import OfferCode
from faultmaven.core.investigation.narration.fix_verification import (
    CaseIdentity,
    FixVerificationFresh,
    render_fix_verification,
)
from faultmaven.core.investigation.narration.offers import (
    MITIGATION_DECLARED,
    MITIGATION_PROCEEDED,
)
from faultmaven.core.investigation.narration.prompt_state import (
    MILESTONES_OMITTED,
    milestone_status,
)
from faultmaven.core.investigation.narration.statement import (
    Change,
    OutcomeBaseline,
    OutcomeChange,
    offer_stated,
    outcome_changes,
    outcome_statement,
)
from faultmaven.core.investigation.outcome import (
    compute_case_outcome,
    narration_outcome,
    record_case_outcome,
)
from faultmaven.core.investigation.problem_status import (
    _acted_on_bar,
    invalidation_refusal,
    revision_refusal,
)
from faultmaven.core.investigation.prompts.context_builder import (
    narration_fold as fold_module,
)
from faultmaven.core.investigation.prompts.context_builder.assembly import (
    _milestone_status_section,
)
from faultmaven.core.investigation.prompts.context_builder.narration_fold import (
    NarrationFold,
)
from faultmaven.core.investigation.schemas import ProblemVerificationUpdate
from faultmaven.core.investigation.terminal_transitions import (
    mitigation_closure_line,
)
from faultmaven.modules.case.contracts import (
    EvidenceCategory,
    InvestigationStage,
    MitigationOutcome,
    ProblemStatus,
    WritePoint,
)
from tests.unit.core.investigation import settled_cases_3053 as settled
from tests.unit.core.investigation.bound_rows import bound_row, bound_to_add
from tests.unit.core.investigation.test_outcome_statement_2811_s2c3a import (
    HEAD,
    SEP,
    _process,
    _response,
)
from tests.unit.core.investigation.test_problem_statement_revision_and_false_alarm import (
    _engine as _revision_engine,
)

pytestmark = pytest.mark.unit

_PRESENT = EvidenceCategory.SYMPTOM_EVIDENCE
_GONE = EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE
_NONE = (False, False)
_VERIFIED_BAR = "a mitigation was verified against the current statement"
_DECLARED_BAR = "a mitigation was declared stable against the current statement"
_AFTER = "after the mitigation carried out in turn 6"


def _record(case):
    return compute_case_outcome(None, case, at=WritePoint.CYCLE)


def _base(record) -> OutcomeBaseline:
    return OutcomeBaseline(record, None, True, frozenset())


def _statement(prior, now) -> str | None:
    return outcome_statement(outcome_changes(_base(prior), now), frozenset())


def _shown(case) -> str:
    return case.problem_verification.verifying_evidence_ids[0]


def _accepted():
    """Accepted at 6, nothing re-checked."""
    return settled._build((True, False, None), _NONE)


def _verified():
    """Accepted at 6, a bound relief at 7, the verify registered."""
    return settled._build((True, True, None), _NONE)


def _relapse(case, turn: int = 8) -> None:
    bound_row(case, _PRESENT, turn=turn, rechecks=_shown(case))


def _relief(case, turn: int) -> None:
    bound_row(case, _GONE, turn=turn, rechecks=_shown(case))


def _declared(case, turn: int) -> None:
    record = case.progress.mitigation
    record.disposition, record.disposition_at_turn = "declared_stable", turn


# ---------------------------------------------------------------------------
# The clauses: the ruled copy, and each entered from any other state
# ---------------------------------------------------------------------------


def _m(**fields) -> MitigationOutcome:
    return MitigationOutcome(attempt_turn=6, **fields)


@pytest.mark.parametrize(
    "kind, mitigation, clause",
    [
        (
            OutcomeChange.MITIGATION_RELIEF,
            _m(relief_turn=7, verified=True),
            f"{_AFTER}, the problem was seen gone",
        ),
        (
            OutcomeChange.MITIGATION_RELAPSE,
            _m(relief_turn=7, present_turn=8, holding=False),
            f"{_AFTER}, the problem was seen gone, then seen again (turn 8)",
        ),
        (
            OutcomeChange.MITIGATION_NO_RELIEF,
            _m(present_turn=7),
            f"{_AFTER}, the problem was still present (turn 7)",
        ),
        (
            OutcomeChange.MITIGATION_DECLARED,
            _m(disposition="declared_stable", disposition_turn=7),
            "you reported the mitigation stable (turn 7)",
        ),
        (
            OutcomeChange.MITIGATION_PROCEEDED,
            _m(disposition="proceed_unmitigated", disposition_turn=7),
            "the investigation went on before the mitigation was verified (turn 7)",
        ),
    ],
)
def test_each_mitigation_clause_is_the_ruled_copy(kind, mitigation, clause):
    change = Change(kind, mitigation=mitigation)
    assert change.key == (kind, None)
    assert outcome_statement([change], frozenset()) == f"{HEAD}{clause}."


@pytest.mark.parametrize(
    "kind",
    [OutcomeChange.MITIGATION_DECLARED, OutcomeChange.MITIGATION_PROCEEDED],
)
def test_a_disposition_with_no_turn_prints_none(kind):
    """D3: the closure line's ``at_turn``, never "(turn None)"."""
    value = (
        "declared_stable"
        if kind == OutcomeChange.MITIGATION_DECLARED
        else ("proceed_unmitigated")
    )
    change = Change(kind, mitigation=_m(disposition=value))
    statement = outcome_statement([change], frozenset())
    assert "turn" not in statement


def test_a_registered_relief_states_seen_gone_once():
    case = _accepted()
    before = _record(case)
    _relief(case, 7)
    case.progress.mitigation.verified = True
    now = _record(case)
    assert _statement(before, now) == f"{HEAD}{_AFTER}, the problem was seen gone."
    assert _statement(now, now) is None
    assert _statement(None, now) is None, "a record minted this turn states nothing"


def test_a_stored_shape_one_record_with_a_holding_mitigation_states_nothing():
    """Critique point 4: entering from absence is sound, and a record stored
    before ``shape`` with the same holding mitigation is no absence."""
    now = _record(_verified())
    assert now.mitigation.verified and now.mitigation.relief_turn == 7, "premise"
    old = now.model_copy(update={"shape": 1})
    assert _statement(old, now) is None


def test_an_unregistered_relief_and_a_stored_verdict_with_no_row_state_nothing():
    """D2: "seen gone" needs the record's holding ``verified`` and a bound
    relief. A relief the model has not registered is asked for in
    ``<fix_verification>``; a stored verdict with no bound row observed
    nothing."""
    case = _accepted()
    before = _record(case)
    _relief(case, 7)
    assert _statement(before, _record(case)) is None
    bare = _accepted()
    base = _record(bare)
    bare.progress.mitigation.verified = True
    now = _record(bare)
    assert now.mitigation.verified and now.mitigation.relief_turn is None, "premise"
    assert _statement(base, now) is None


def test_a_relapse_states_once_and_a_new_relief_and_relapse_again():
    """Amendment 1: the relapse is keyed on its relief turn. A later
    persistence of the same relapse states nothing; a new relief and relapse
    (r7/p8 becoming r9/p10) states again; a relapse may be the first line."""
    case = _verified()
    held = _record(case)
    _relapse(case, 8)
    first = _record(case)
    relapsed = f"{_AFTER}, the problem was seen gone, then seen again"
    assert _statement(held, first) == f"{HEAD}{relapsed} (turn 8)."
    assert _statement(None, first) is None
    _relapse(case, 9)
    assert _statement(first, _record(case)) is None
    _relief(case, 10)
    _relapse(case, 11)
    assert _statement(first, _record(case)) == f"{HEAD}{relapsed} (turn 11)."


def test_a_relief_and_a_persistence_in_one_turn_state_the_relapse():
    """r == p: ``mitigation_holds`` is false, so the clause is the relapse's.
    The record's order of the two rows is not read."""
    case = _accepted()
    before = _record(case)
    _relief(case, 7)
    _relapse(case, 7)
    now = _record(case)
    assert (now.mitigation.relief_turn, now.mitigation.present_turn) == (7, 7)
    assert _statement(before, now) == (
        f"{HEAD}{_AFTER}, the problem was seen gone, then seen again (turn 7)."
    )


def test_a_persistence_with_no_relief_states_still_present():
    case = _accepted()
    before = _record(case)
    _relapse(case, 7)
    assert _statement(before, _record(case)) == (
        f"{HEAD}{_AFTER}, the problem was still present (turn 7)."
    )


def test_an_undated_attempt_states_no_observation():
    """D3: an ``{a}`` clause is omitted with no attempt turn."""
    case = _accepted()
    case.proposed_actions[-1].accepted_in_turn = None
    before = _record(case)
    _relief(case, 7)
    _relapse(case, 8)
    case.progress.mitigation.verified = True
    now = _record(case)
    assert now.mitigation.attempt_turn is None, "premise"
    assert _statement(before, now) is None


def test_a_declaration_states_while_effective_and_a_proceed_states():
    case = _accepted()
    before = _record(case)
    _declared(case, 7)
    declared = _record(case)
    assert _statement(before, declared) == (
        f"{HEAD}you reported the mitigation stable (turn 7)."
    )
    _relapse(case, 8)
    assert _statement(before, _record(case)) == (
        f"{HEAD}{_AFTER}, the problem was still present (turn 8)."
    ), "F5 voids it: no declaration clause"
    proceeded = settled._build((True, False, "proceed_unmitigated"), _NONE)
    assert _statement(_record(_accepted()), _record(proceeded)) == (
        f"{HEAD}the investigation went on before the mitigation was verified "
        "(turn 7)."
    )


# ---------------------------------------------------------------------------
# Pins: {a}, the closure line's constants, the rca-infeasible close
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("relief", [None, 7])
def test_a_is_the_attempt_turn_the_block_and_the_closure_line_print(relief):
    """D3: ``{a}`` is ``MitigationOutcome.attempt_turn``, the turn
    ``<fix_verification>`` prints as executed and the closure line's."""
    case = _verified() if relief else _accepted()
    record = narration_outcome(case)
    a = record.mitigation.attempt_turn
    assert a == mitigation_observation(case).attempt_turn == 6
    identity = CaseIdentity(
        case.case_id, case.state, InvestigationStage.MITIGATION, case.current_turn
    )
    block = render_fix_verification(
        record,
        identity,
        lambda _id: None,
        FixVerificationFresh(mitigation_verify_refused=True),
        lambda: None,
    )
    assert f"Mitigation: executed turn {a}:" in "\n".join(block)
    if relief is None:
        assert f"a mitigation was applied (turn {a})" in mitigation_closure_line(case)
    change = Change(OutcomeChange.MITIGATION_NO_RELIEF, mitigation=record.mitigation)
    clause = outcome_statement([change], frozenset())
    assert f"after the mitigation carried out in turn {a}," in clause


@pytest.mark.parametrize(
    "value, constant",
    [
        ("declared_stable", MITIGATION_DECLARED),
        ("proceed_unmitigated", MITIGATION_PROCEEDED),
    ],
)
def test_the_last_two_clauses_are_the_closure_lines(value, constant):
    """Q8: the declaration and the proceed reuse the #2100 closure line."""
    case = settled._build((True, False, value), _NONE)
    line = mitigation_closure_line(case)
    kind = (
        OutcomeChange.MITIGATION_DECLARED
        if value == "declared_stable"
        else OutcomeChange.MITIGATION_PROCEEDED
    )
    clause = outcome_statement(
        [Change(kind, mitigation=narration_outcome(case).mitigation)], frozenset()
    )
    assert line == f"{constant} (turn 7)"
    assert clause == f"{HEAD}{line}."


def _rca_stated(case) -> frozenset:
    params = {"closure_line": mitigation_closure_line(case)}
    return offer_stated(OfferCode.RCA_INFEASIBLE_CLOSE, params)


def _relapsed():
    case = _verified()
    _relapse(case)
    return case


def _still_present():
    case = _accepted()
    _relapse(case, 7)
    return case


@pytest.mark.parametrize(
    "build, kind",
    [
        (_verified, OutcomeChange.MITIGATION_RELIEF),
        (_relapsed, OutcomeChange.MITIGATION_RELAPSE),
        (_still_present, OutcomeChange.MITIGATION_NO_RELIEF),
        (
            lambda: settled._build((True, False, "declared_stable"), _NONE),
            OutcomeChange.MITIGATION_DECLARED,
        ),
        (
            lambda: settled._build((True, False, "proceed_unmitigated"), _NONE),
            OutcomeChange.MITIGATION_PROCEEDED,
        ),
    ],
)
def test_the_rca_infeasible_close_claims_each_fact_its_line_states(build, kind):
    """Amendment 6: a replayed close does not state the fact twice."""
    case = build()
    stated = _rca_stated(case)
    assert (kind, None) in stated
    changes = outcome_changes(_base(_record(_accepted())), _record(case))
    assert kind in {c.kind for c in changes}, "premise: the turn entered it"
    assert outcome_statement([c for c in changes if c.kind == kind], stated) is None


def test_a_holding_relief_after_a_persistence_claims_no_relapse():
    """The closure line's "seen again …, before that relief" is a holding
    relief, not a relapse."""
    case = _accepted()
    _relapse(case, 7)
    _relief(case, 8)
    case.progress.mitigation.verified = True
    stated = _rca_stated(case)
    assert (OutcomeChange.MITIGATION_RELIEF, None) in stated
    assert (OutcomeChange.MITIGATION_RELAPSE, None) not in stated
    assert offer_stated(OfferCode.RCA_INFEASIBLE_CLOSE, {"closure_line": None}) == (
        frozenset()
    )


# ---------------------------------------------------------------------------
# The bar (Q8, D4): the record's source derivations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(settled.CASES))
def test_the_bars_inputs_are_the_cycle_records(name):
    """D4: the bar's two inputs equal the cycle-written record's
    ``mitigation.verified`` and ``declared_stable_effective``."""
    case = settled.CASES[name]()
    record_case_outcome(case, at=WritePoint.CYCLE)
    mitigation = case.progress.outcome.mitigation
    assert _mitigation_verified(case) is bool(mitigation and mitigation.verified)
    assert _mitigation_declared_stable(case) is bool(
        mitigation and mitigation.declared_stable_effective
    )


def test_a_bound_relapse_lifts_the_bar_and_a_holding_verify_keeps_it():
    """Behaviour difference: after a bound relapse a statement revision or a
    false-alarm finding is admitted again (``revision_refusal`` and
    ``invalidation_refusal`` share the bar). A stored verdict with no bound
    row still bars."""
    assert _acted_on_bar(_verified()) == _VERIFIED_BAR
    assert _acted_on_bar(_relapsed()) is None
    bare = _accepted()
    bare.progress.mitigation.verified = True
    assert _acted_on_bar(bare) == _VERIFIED_BAR


@pytest.mark.parametrize(
    "build, refusal",
    [(_verified, _VERIFIED_BAR), (_relapsed, None)],
    ids=["holding", "relapsed"],
)
def test_a_false_alarm_finding_is_admitted_after_a_bound_relapse(build, refusal):
    """Behaviour difference: the finding's guard reads the same bar."""
    case = build()
    gone = [e.evidence_id for e in case.evidence if e.category == _GONE]
    basis = "no 401 in the gateway log for the reported window"
    assert invalidation_refusal(case, gone, basis) == refusal


def _declared_then_present(verified: bool):
    """Declared at 7, then seen present at 8: F5 voids the declaration."""
    case = settled._build((True, verified, "declared_stable"), _NONE)
    _relapse(case, 8)
    return case


def _present_then_declared(verified: bool):
    """Seen present at 8, then declared at 9: the user's call stands."""
    case = settled._build((True, verified, "declared_stable"), _NONE)
    _relapse(case, 8)
    case.progress.mitigation.disposition_at_turn = 9
    return case


@pytest.mark.parametrize("verified", [False, True], ids=["declared", "verified"])
def test_an_f5_voided_declaration_lifts_the_bar_in_its_order_only(verified):
    """``relapse_declared_stable`` in both orders (critique point 7)."""
    voided = _declared_then_present(verified)
    stands = _present_then_declared(verified)
    assert _acted_on_bar(voided) is None
    assert _acted_on_bar(stands) == _DECLARED_BAR


@pytest.mark.parametrize("verified", [False, True], ids=["declared", "verified"])
def test_the_disposition_line_prints_only_an_effective_declaration(verified):
    """Behaviour difference (D2's sentence): a declaration F5 voids prints
    no ``mitigation_disposition`` line."""
    voided = milestone_status(narration_outcome(_declared_then_present(verified)))
    stands = milestone_status(narration_outcome(_present_then_declared(verified)))
    assert voided.mitigation_disposition is None
    assert stands.mitigation_disposition == "declared_stable"
    section = _milestone_status_section(NarrationFold(_present_then_declared(verified)))
    assert "- mitigation_disposition: declared_stable" in section
    voided_section = _milestone_status_section(
        NarrationFold(_declared_then_present(verified))
    )
    assert "mitigation_disposition" not in voided_section
    proceeded = settled._build((True, False, "proceed_unmitigated"), _NONE)
    assert milestone_status(narration_outcome(proceeded)).mitigation_disposition == (
        "proceed_unmitigated"
    )


def test_a_raising_relief_reaches_the_bar_only_over_a_stored_verify_or_declaration():
    """Amendment 7: the bar reads ``mitigation_relief`` through the record's
    derivations; a raise there propagates, as the record's own fold's does,
    and only where a verify or a declaration is stored."""

    def _raise(*args, **kwargs):
        raise RuntimeError("relief unreadable")

    with patch.object(cause_assurance, "mitigation_relief", _raise):
        assert _acted_on_bar(_accepted()) is None
        with pytest.raises(RuntimeError, match="relief unreadable"):
            _acted_on_bar(_verified())
        with pytest.raises(RuntimeError, match="relief unreadable"):
            _acted_on_bar(settled._build((True, False, "declared_stable"), _NONE))


# ---------------------------------------------------------------------------
# The relapse, through process_turn
# ---------------------------------------------------------------------------


def _relapse_turn_response(case):
    row = bound_to_add(case, _PRESENT, "logins fail with 401 again", turn=8)
    return _response(
        evidence_to_add=[row.model_copy(update={"rechecks": _shown(case)})]
    )


_REVISED = (
    "token refresh calls to the identity provider are rejected after the "
    "session cache expires"
)
_BASIS = "every failing request in the log is a token refresh"


def _revision(case):
    return _response(
        verification_updates=ProblemVerificationUpdate(
            revised_problem_statement=_REVISED,
            revision_basis=_BASIS,
            revision_evidence_ids=[_shown(case)],
        )
    )


def _refusal(case) -> str | None:
    """What the apply step's guard answers the revision ``_revision`` sends."""
    return revision_refusal(
        case, _REVISED, [_shown(case)], _BASIS, revision_offer_key(_REVISED)
    )


async def test_a_bound_relapse_after_a_verified_relief_through_process_turn():
    """The regression: the turn states the relapse, its next prompt omits
    the gate, a revision is then admitted, the stage stays DIAGNOSIS (Q7),
    and the API's ``completed_milestones`` still lists the gate (the S4
    residual, #2609)."""
    engine, case = _revision_engine(), _verified()
    case.current_turn = 7
    reply, _ = await _process(engine, case, "it's back", _relapse_turn_response(case))
    assert reply == (
        "Noted." + SEP + HEAD + "after the mitigation carried out in turn 6, the "
        "problem was seen gone, then seen again (turn 8)."
    )
    mitigation = case.progress.outcome.mitigation
    assert mitigation.verified is _mitigation_verified(case) is False
    assert case.progress.mitigation.verified is True, "the stored flag: registered"
    section = _milestone_status_section(NarrationFold(case))
    assert "- mitigation_accepted" in section
    assert "- mitigation_verified" not in section
    assert case.progress.current_stage is InvestigationStage.DIAGNOSIS
    assert "mitigation_verified" in case.progress.completed_milestones

    assert _refusal(case) is None
    await _process(engine, case, "they're all token refreshes", _revision(case))
    assert case.progress.problem_status == ProblemStatus.REVISION_PENDING


async def test_a_holding_verify_still_refuses_the_revision_through_process_turn():
    """The control."""
    engine, case = _revision_engine(), _verified()
    case.current_turn = 7
    reply, _ = await _process(engine, case, "thanks", _response())
    assert reply == "Noted.", "nothing changed"
    assert _refusal(case) == _VERIFIED_BAR
    await _process(engine, case, "they're all token refreshes", _revision(case))
    assert case.progress.problem_status == ProblemStatus.VERIFIED


# ---------------------------------------------------------------------------
# Q1, Q9 and the notes
# ---------------------------------------------------------------------------


def test_a_failed_fold_omits_the_milestone_status_counted_once():
    """Q1: no fresh fallback is left; with ``compute_case_outcome`` raising,
    the milestone status is its declared line and the narration failure
    counter rises once."""
    case = _relapsed()
    counted: list = []
    counter = SimpleNamespace(
        labels=lambda at: SimpleNamespace(inc=lambda: counted.append(at))
    )

    def _raise(*args, **kwargs):
        raise RuntimeError("fold failed")

    with (
        patch.object(outcome_module, "compute_case_outcome", _raise),
        patch.object(fold_module, "case_outcome_write_failures_total", counter),
    ):
        fold = NarrationFold(case)
        assert _milestone_status_section(fold) == MILESTONES_OMITTED
        assert _milestone_status_section(fold) == MILESTONES_OMITTED
    assert counted == [WritePoint.NARRATION]


def test_the_api_contract_version_is_unchanged():
    """Q9: no S3d PR changes it (16.3.0 at ``c117a2450``, this stage's base);
    ``milestones_completed`` moves with S4."""
    assert API_CONTRACT_VERSION == "16.3.0"


_PACKAGE = Path(__file__).resolve().parents[4] / "faultmaven"
#: A note that promises the mitigation's work to a later S3d stage.
_PROMISE = re.compile(r"(until|at|to|is|are) (stage )?S3d\b|S3d's|S3d-4 \(#3053\)")


def _stale_notes(text: str) -> list[str]:
    """Each line promising work to S3d, and each ``Q1 (a)`` mitigation note
    (the ruling that kept the stored verify fresh), with the line before."""
    lines = text.splitlines()
    return [
        line
        for i, line in enumerate(lines)
        if _PROMISE.search(line)
        or ("Q1 (a)" in line and "mitigation" in (lines[i - 1] if i else "") + line)
    ]


def test_no_stale_s3d_or_q1a_mitigation_note_remains():
    found = {
        path.relative_to(_PACKAGE).as_posix(): notes
        for path in sorted(_PACKAGE.rglob("*.py"))
        if (notes := _stale_notes(path.read_text()))
    }
    assert found == {}


@pytest.mark.parametrize(
    "text",
    [
        "``mitigation.verified`` stays a fresh input until S3d-4 (#3053).",
        "the mitigation lines are S3d's (#3053)",
        '"""``mitigation.verified`` as stored, read fresh (ruling Q1 (a))."""',
        "# the stored\n# ``mitigation.verified`` (ruling Q1 (a)) and the note",
    ],
)
def test_the_note_scan_bites(text):
    """A positive control per shape the scan names."""
    assert _stale_notes(text)
