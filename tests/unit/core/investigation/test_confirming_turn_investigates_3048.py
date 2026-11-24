"""#3048 (B2): the turn that commits Gate 1 runs as INVESTIGATING.

A click or a bare typed consent commits Gate 1 at section 0c, before the
model call. The case used to stay INQUIRY until the check after the response,
so the confirming turn was prompted and answered on ``InquiryResponse``,
which carries no ``evidence_to_add`` and no symptom claim: data pasted at T1
could be recorded, and the problem verified, only at T3. The engine now moves
the case to INVESTIGATING before the prompt, so that turn is the first
INVESTIGATING turn. A Gate 1 the model's own ``user_confirmed_investigation``
commits still transitions after the response.

Driven through ``MilestoneEngine.process_turn`` with only the LLM seam
(``generate_structured_output``) replaced: no provider, no network.
"""

import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.transition_consent import (
    gate1_offer_key,
)
from faultmaven.core.investigation.schemas import (
    InquiryResponse,
    InvestigationResponse_Diagnosis,
)
from faultmaven.modules.case.contracts import (
    CaseState,
    EvidenceCategory,
    ProblemStatus,
    UploadedFile,
)
from tests.case_builders import inquiry_case

pytestmark = pytest.mark.unit

STATEMENT = "billing-exporter.service fails to start on node-3 since 09:00 UTC"
PASTE_ID = "file_3048a0000001"
PASTE = (
    "$ systemctl status billing-exporter\n"
    "x billing-exporter.service - Billing metrics exporter\n"
    "   Active: failed (Result: exit-code) since 09:00:12 UTC\n"
    "  Process: 4121 ExecStart=/opt/billing/bin/exporter (code=exited, status=203/EXEC)\n"
)
SYMPTOM_ROW = "billing-exporter.service is failed with status=203/EXEC"

CLICK = {
    "user_message": "Yes, that's correct. Let's investigate.",
    "intent_type": "confirmation",
    "intent_data": {"value": True, "proposal_id": gate1_offer_key(STATEMENT)},
}
TYPED = {"user_message": "yes"}


def _repo():
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c, **_: c)
    repo.get = AsyncMock(side_effect=lambda cid: None)
    return repo


class _Seam:
    """The LLM seam: records the schema and the prompt of every call and
    answers with the scripted reply for that schema."""

    def __init__(self, inquiry: InquiryResponse, investigating=None):
        self.inquiry = inquiry
        self.investigating = investigating
        self.calls: list[tuple[type, str]] = []

    async def __call__(self, prompt, schema_model, **_):
        self.calls.append((schema_model, prompt))
        if schema_model is InquiryResponse:
            return self.inquiry
        return self.investigating


def _engine(seam: _Seam) -> MilestoneEngine:
    engine = MilestoneEngine(MagicMock(), _repo(), investigation_tools=MagicMock())
    engine.generator.generate_structured_output = AsyncMock(side_effect=seam.__call__)
    return engine


def _inquiry_reply(*, statement=None, confirmed=False) -> InquiryResponse:
    updates = {"user_confirmed_investigation": confirmed}
    if statement:
        updates["proposed_problem_statement"] = statement
    return InquiryResponse(
        agent_response="The paste shows the exporter unit failing.",
        state_updates=updates,
    )


def _verifying_reply() -> InvestigationResponse_Diagnosis:
    """Records the symptom row from the T1 paste and claims the symptom."""
    return InvestigationResponse_Diagnosis.model_validate(
        {
            "agent_response": "Your status output shows the unit failed at start.",
            "state_updates": {
                "evidence_to_add": [
                    {
                        "summary": SYMPTOM_ROW,
                        "extract": "Active: failed (Result: exit-code)",
                        "category": "symptom_evidence",
                        "source_type": "text",
                        "source_file_id": PASTE_ID,
                    }
                ],
                "milestones": {
                    "symptom_verified": True,
                    "symptom_verification_evidence_ids": ["new_index_0"],
                },
            },
            "evidence_trail": {
                "milestone_justifications": {
                    "symptom_verified": (
                        "The T1 systemctl status paste shows the unit failed "
                        "with status=203/EXEC, as the statement says."
                    )
                }
            },
        }
    )


async def _after_t1(seam: _Seam):
    """T1: an INQUIRY turn carrying the status paste; the model proposes the
    statement. Returns the engine and the case, Gate 1 pending."""
    case = inquiry_case(
        case_id="case_3048aaaaaaaa",
        title="billing exporter down",
        user_id="user_3048",
        enterprise_id="org_3048",
        description="",
    )
    case.uploaded_files.append(
        UploadedFile(
            file_id=PASTE_ID,
            filename="pasted-content-20261124T090500Z.txt",
            size_bytes=len(PASTE),
            uploaded_at_turn=1,
            structural_index=PASTE,
        )
    )
    case.current_turn = 1
    engine = _engine(seam)
    await engine.process_turn(case, "the exporter is down", redaction_ctx=None)
    assert case.state == CaseState.INQUIRY
    assert case.inquiry.problem_statement_confirmed is False
    case.current_turn = 2
    return engine, case


@pytest.mark.parametrize("consent", [TYPED, CLICK], ids=["typed", "click"])
async def test_the_confirming_turn_records_the_t1_paste_and_verifies(consent):
    seam = _Seam(_inquiry_reply(statement=STATEMENT), _verifying_reply())
    engine, case = await _after_t1(seam)

    result = await engine.process_turn(case, **consent, redaction_ctx=None)

    schema, prompt = seam.calls[-1]
    assert schema is InvestigationResponse_Diagnosis
    # The T1 paste is in the T2 prompt, citable by its file_id, named by the
    # turn it arrived on.
    block = re.search(rf'<uploaded_file file_id="{PASTE_ID}"[^>]*>', prompt)
    assert block is not None
    assert 'label="pasted text (turn 1)"' in block.group(0)
    row = next(e for e in case.evidence if e.summary == SYMPTOM_ROW)
    assert row.category == EvidenceCategory.SYMPTOM_EVIDENCE
    assert row.source_file_id == PASTE_ID
    assert row.collected_at_turn == 2
    assert case.progress.problem_status == ProblemStatus.VERIFIED
    assert case.problem_verification.verifying_evidence_ids == [row.evidence_id]
    assert result["metadata"]["status_transitioned"] is True


@pytest.mark.parametrize("consent", [TYPED, CLICK], ids=["typed", "click"])
async def test_the_transition_runs_once_before_the_call(consent):
    seam = _Seam(
        _inquiry_reply(statement=STATEMENT),
        InvestigationResponse_Diagnosis(agent_response="Looking.", state_updates={}),
    )
    engine, case = await _after_t1(seam)
    states_at_call = []
    real = engine.transitions._transition_to_investigating

    async def _spy(c):
        await real(c)

    engine.transitions._transition_to_investigating = AsyncMock(side_effect=_spy)
    seam_call = engine.generator.generate_structured_output.side_effect

    async def _seam(prompt, schema_model, **kw):
        states_at_call.append(case.state)
        return await seam_call(prompt, schema_model, **kw)

    engine.generator.generate_structured_output.side_effect = _seam
    engine.deps.knowledge_service = MagicMock()
    engine.deps.knowledge_service.search_knowledge = AsyncMock(return_value=[])
    engine.deps.team_service = None
    engine.deps.share_repository = None
    result = await engine.process_turn(case, **consent, redaction_ctx=None)

    assert states_at_call == [CaseState.INVESTIGATING]
    engine.transitions._transition_to_investigating.assert_awaited_once()
    # The statement is searched once, before the prompt: INQUIRY's search and
    # the transition's push are the same search.
    search = engine.deps.knowledge_service.search_knowledge
    assert [c.kwargs["query"] for c in search.await_args_list] == [STATEMENT]
    assert case.state == CaseState.INVESTIGATING
    assert case.description == STATEMENT
    transitions = [
        a for a in case.action_history if a.to_state == CaseState.INVESTIGATING
    ]
    assert len(transitions) == 1
    assert result["metadata"]["status_transitioned"] is True
    # Scored as progress, as the transition turn was before.
    assert result["metadata"]["progress_made"] is True
    assert case.turns_without_progress == 0


async def test_the_model_flag_still_transitions_after_the_response():
    """A confirmation click carrying no value commits nothing at 0c; the
    model's flag on a bare "yes" commits Gate 1 in the response, so the turn
    is answered on ``InquiryResponse`` and transitions afterwards."""
    seam = _Seam(_inquiry_reply(statement=STATEMENT))
    engine, case = await _after_t1(seam)
    seam.inquiry = _inquiry_reply(confirmed=True)
    states_at_call = []
    seam_call = engine.generator.generate_structured_output.side_effect

    async def _seam(prompt, schema_model, **kw):
        states_at_call.append(case.state)
        return await seam_call(prompt, schema_model, **kw)

    engine.generator.generate_structured_output.side_effect = _seam
    result = await engine.process_turn(
        case,
        "yes",
        intent_type="confirmation",
        intent_data={"value": None, "proposal_id": gate1_offer_key(STATEMENT)},
        redaction_ctx=None,
    )

    assert seam.calls[-1][0] is InquiryResponse
    assert states_at_call == [CaseState.INQUIRY]
    assert case.state == CaseState.INVESTIGATING
    assert result["metadata"]["status_transitioned"] is True
    assert (
        len([a for a in case.action_history if a.to_state == CaseState.INVESTIGATING])
        == 1
    )


@pytest.mark.parametrize(
    "consent",
    [
        {"user_message": "yes but it is node-4 too"},
        {
            "user_message": "Not quite, let me clarify",
            "intent_type": "confirmation",
            "intent_data": {"value": False, "proposal_id": gate1_offer_key(STATEMENT)},
        },
    ],
    ids=["not_bare", "declined"],
)
async def test_a_decline_or_a_non_bare_reply_does_not_transition_early(consent):
    seam = _Seam(_inquiry_reply(statement=STATEMENT))
    engine, case = await _after_t1(seam)
    seam.inquiry = _inquiry_reply()

    await engine.process_turn(case, **consent, redaction_ctx=None)

    assert seam.calls[-1][0] is InquiryResponse
    assert case.state == CaseState.INQUIRY
    assert case.inquiry.problem_statement_confirmed is False
