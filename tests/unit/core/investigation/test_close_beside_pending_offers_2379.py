"""#2379: the status menu's Close beside a pending revision and beside a
``needs_info`` resolve offer.

The rule (2026-10-28): Close is the user's choice at any time, resolve is
offered when the case has earned it, and the two are not offered together.

State 1, a problem-statement revision awaiting the user on a resolvable case
whose resolution the user declined. The population is backstop-leg cases (the
cause on the ``rcc`` leg), not yet ``solution_verified``: a verified fix bars a
revision. The ``rcc`` leg needs a verified symptom, so under the revision the
decline's signature has no cause leg and the decline does not cover. The user's
close cancels the revision first, so ``derive_disposition_eligibility`` reads
the case as that close finds it: Close reads ``ready``, and the menu close
cancels the revision and closes with ``resolution_declined``.

State 2, a ``needs_info`` resolve offer: the model's ``resolved`` on a
NEEDS_INFO case stores a pending RESOLVED marked ``needs_info``. Close reads
``suggests_alternative`` beside it and ``ready`` once the offer is answered.

Real ``process_turn`` turns with a stubbed generator: no live LLM.
"""

import pytest

from faultmaven.core.investigation.milestone_engine.transition_consent import (
    revision_offer_key,
)
from faultmaven.core.investigation.problem_status import (
    propose_revision,
    revision_refusal,
)
from faultmaven.core.investigation.terminal_transitions import (
    ClosureReadiness,
    ResolutionReadiness,
    assess_resolution_readiness,
    cause_identification_leg,
    closure_verdict,
    declined_resolve_entry,
    derive_disposition_eligibility,
)
from faultmaven.modules.case.contracts import CaseState, ProblemStatus
from faultmaven.modules.case.domain.models.evidence import (
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
)
from tests.unit.core.investigation.test_close_after_declined_resolve_2360 import (
    _REASON,
    _declined_case,
    _menu_close,
)
from tests.unit.core.investigation.test_declined_resolution_binds_the_model_1895 import (
    _FILE_ID,
    _engine,
    _turn,
)
from tests.unit.core.investigation.test_resolution_backstop_turn import (
    _case,
    _confirmed_case,
)

pytestmark = pytest.mark.unit

_REVISED = (
    "AssumeRoleWithWebIdentity fails for the nightly batch pods in the "
    "data-processor namespace; the API pods assume the role normally"
)


def _add_symptom_row(case) -> str:
    """A user symptom row from turn 0, before the fix's turn-1 rows, so it does
    not read as the problem persisting after the fix. Returns its id."""
    row = Evidence(
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        primary_purpose="observe the reported symptom",
        summary="The batch pods' AssumeRole calls fail; the API pods' succeed.",
        source_type=EvidenceSourceType.LOGS,
        source_file_id=_FILE_ID,
        collected_by="user",
        collected_at_turn=0,
    )
    case.evidence.append(row)
    return row.evidence_id


def _propose_a_revision(case, row_id: str) -> None:
    kw = {
        "text": _REVISED,
        "evidence_ids": [row_id],
        "basis": "the failures are confined to the batch pods",
        "offer_key": revision_offer_key(_REVISED),
    }
    assert revision_refusal(case, **kw) is None, "premise: the revision is admitted"
    propose_revision(case, **kw)


def _declined_under_a_revision():
    case = _declined_case()
    row_id = _add_symptom_row(case)
    assert declined_resolve_entry(case) is not None, "premise: the decline stands"
    _propose_a_revision(case, row_id)
    assert case.progress.problem_status == ProblemStatus.REVISION_PENDING
    assert closure_verdict(case) == ClosureReadiness.SUGGEST_RESOLVE, "premise"
    assert cause_identification_leg(case) is None, "premise: no leg under it"
    assert declined_resolve_entry(case) is None, "premise: no cover under it"
    return case


class TestARevisionAfterTheDecline:
    def test_the_menu_shows_close(self):
        case = _declined_under_a_revision()
        assert derive_disposition_eligibility(case) == {
            "resolved": "ready",
            "closed": "ready",
        }

    def test_the_read_leaves_the_case_as_it_was(self):
        case = _declined_under_a_revision()
        before = case.model_dump()
        derive_disposition_eligibility(case)
        assert case.model_dump() == before
        assert case.progress.problem_status == ProblemStatus.REVISION_PENDING

    async def test_the_menu_close_cancels_it_and_closes(self):
        case = _declined_under_a_revision()
        engine = _engine()
        await _menu_close(engine, case)
        assert case.problem_verification.pending_revision is None
        assert case.progress.problem_status == ProblemStatus.VERIFIED
        assert case.pending_transition["to_state"] == "closed"
        assert case.pending_transition["closure_reason"] == _REASON

        await _turn(engine, case, "yes")
        assert case.state == CaseState.CLOSED
        assert case.closure_reason == _REASON


class TestARevisionWithNoDecline:
    async def test_close_stays_hidden_and_the_menu_close_pivots(self):
        case = _confirmed_case()
        _propose_a_revision(case, _add_symptom_row(case))
        assert derive_disposition_eligibility(case)["closed"] == (
            "suggests_alternative"
        )

        await _menu_close(_engine(), case)
        assert case.problem_verification.pending_revision is None
        assert case.pending_transition["to_state"] == "resolved"


async def _needs_info_offer():
    """A stabilized case (the problem gone, the cause not shown removed), and
    the model's ``resolved`` on it."""
    case = _case(absence=EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE)
    assert (
        assess_resolution_readiness(case).verdict == ResolutionReadiness.NEEDS_INFO
    ), "premise"
    engine = _engine()
    await _turn(engine, case, "the failover is holding", propose="resolved")
    assert case.pending_transition["to_state"] == "resolved"
    assert case.pending_transition["needs_info"] is True
    return engine, case


class TestANeedsInfoResolveOffer:
    async def test_the_menu_hides_close_beside_it(self):
        _, case = await _needs_info_offer()
        assert derive_disposition_eligibility(case) == {
            "resolved": "needs_info",
            "closed": "suggests_alternative",
        }

    async def test_close_returns_once_it_is_answered(self):
        engine, case = await _needs_info_offer()
        await _turn(engine, case, "that is all I have")
        assert case.pending_transition["to_state"] == "closed"
        assert derive_disposition_eligibility(case)["closed"] == "ready"

    async def test_a_menu_close_beside_it_proposes_closed(self):
        """The menu close cancels the offer and proposes CLOSED: the case is
        not resolvable, so the close does not pivot to a resolve."""
        engine, case = await _needs_info_offer()
        await _menu_close(engine, case)
        assert case.pending_transition["to_state"] == "closed"
        assert "needs_info" not in case.pending_transition
        assert derive_disposition_eligibility(case)["closed"] == "ready"
