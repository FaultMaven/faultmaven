"""#1464 — a DECLINE on the Gate-1 card must commit no gate.

The engine's section 0c confirmation branch was blind to the answer the click
carried. ``InvestigationService._handle_confirmation`` forwards
``intent_data={"value": confirmation_value}`` faithfully; the value then died
at the branch, which committed ``problem_statement_confirmed`` +
``decided_to_investigate`` off ``intent_type`` alone. So the engine's own
decline affordance — ``_investigation_confirmation_suggestions()``'s "Not
quite, let me clarify", ``confirmation_value: False`` — started the
investigation on the statement the user was asking to refine, and
``_check_automatic_transitions`` fired INQUIRY → INVESTIGATING. Reachable by
an ordinary DECIDE click; no resolver, no feature flag.

Measured before the fix, through ``InvestigationService.process_turn``:

    === DECIDE click: "Not quite, let me clarify" (confirmation_value=False) ===
      problem_statement_confirmed = True
      decided_to_investigate      = True
      case.state                  = investigating

Two things are pinned here.

1. **Behaviour** — the decline commits nothing and does not transition, the
   confirm still does both, and the decline is not turned into a no-op turn:
   it falls through to normal LLM processing, where the statement (still
   mutable, precisely because Gate 1 did not commit) can be refined and the
   confirmation pair is re-offered on the refined text.

2. **The rule, over every implementation of it** — "a branch keyed on
   ``intent_type == 'confirmation'`` must read the value out of
   ``intent_data``". ``_process_turn_impl`` has **three** such branches: the
   two in section 0b (``intent_confirms`` / ``intent_declines``, both always
   compliant) and this one. The scan that found that number ships as
   ``TestEveryConfirmationBranchReadsTheValue``, and it carries its own
   positive control: the same scan is run over a synthetic value-blind branch
   and must report it, so a scan that has stopped biting fails rather than
   passing quietly.
"""

import ast
import inspect
import json
import textwrap
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import (
    MilestoneEngine,
    _gate1_is_pending,
    _investigation_confirmation_suggestions,
)
from faultmaven.core.investigation.schemas import TurnPayload
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
    StructuredOutputMode,
    StructuredOutputStrategy,
)
from faultmaven.models.api_models import IntentType, QueryIntent
from faultmaven.models.interfaces import ILLMProvider
from faultmaven.modules.agent.domain.services.investigation_service import (
    InvestigationService,
)
from faultmaven.modules.case.contracts import Case, CaseState, InquiryData

PROPOSED = "Read replica db-2 is lagging behind the primary by >30s since 09:00 UTC"
REFINED = "Read replica db-2 serves stale rows to the checkout API since 09:00 UTC"

#: The canned payload behind "Not quite, let me clarify".
DECLINE_PAYLOAD = _investigation_confirmation_suggestions()[1]["payload"]
#: The canned payload behind "Yes, let's investigate".
CONFIRM_PAYLOAD = _investigation_confirmation_suggestions()[0]["payload"]
#: A decline carrying substance — long enough, and a question. 0b routes it
#: through its substantive-decline arm rather than the canned-acknowledgment
#: one, so the turn reaches 0c. fm#918 measured the exposure on this message.
SUBSTANTIVE_DECLINE = (
    "no - but is the problem statement about the replica or the primary?"
)


class _LLM(ILLMProvider):
    """Answers every structured call with a scripted INQUIRY response."""

    def __init__(self, state_updates=None):
        self.state_updates = state_updates or {}
        self.calls = 0

    async def generate(self, prompt, **kwargs):
        self.calls += 1
        return json.dumps(
            {
                "agent_response": "Which part is off - the symptom or the scope?",
                "state_updates": self.state_updates,
            }
        )

    async def generate_stream(self, prompt, **kwargs):
        yield "mock"

    async def generate_with_history(self, messages, **kwargs):
        return await self.generate("")

    def get_structured_output_strategy(self, schema):
        return StructuredOutputStrategy(
            capability=StructuredOutputCapability.STRICT,
            mode=StructuredOutputMode.JSON_SCHEMA_STRICT,
            include_schema_in_prompt=False,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "S", "strict": True, "schema": schema},
            },
        )


def _inquiry_case_awaiting_gate_one() -> Case:
    """An INQUIRY case with a statement proposed on an earlier turn."""
    case = Case(
        case_id="case_1464deadbeef",
        title="Replica lag",
        state=CaseState.INQUIRY,
        user_id="user_1464",
        enterprise_id="org_1464",
        description="replica lag alerts",
        inquiry=InquiryData(thread_id="thread_1464"),
    )
    case.inquiry.proposed_problem_statement = PROPOSED
    case.inquiry.problem_statement_confirmed = False
    case.inquiry.problem_statement_confirmed_at = None
    case.inquiry.decided_to_investigate = False
    case.inquiry.decision_made_at = None
    case.pending_transition = None
    return case


def _repo(case):
    repo = MagicMock()
    repo.get = AsyncMock(return_value=case)
    repo.save = AsyncMock(side_effect=lambda c: c)
    repo.get_case_messages = AsyncMock(return_value=[])
    return repo


def _engine(case, llm=None):
    return MilestoneEngine(llm or _LLM(), _repo(case), investigation_tools=MagicMock())


async def _decide_click(case: Case, confirmation_value: bool, payload_text: str):
    """The DECIDE click as the product delivers it.

    Through ``InvestigationService.process_turn``, so the value crosses the
    real service→engine seam (``_handle_confirmation``) rather than being
    handed to the engine directly. Only the LLM provider is a double.
    """
    svc = InvestigationService(_engine(case), _repo(case))
    return await svc.process_turn(
        case_id=case.case_id,
        user_id=case.user_id,
        payload=TurnPayload(
            query=payload_text,
            intent=QueryIntent(
                type=IntentType.CONFIRMATION, confirmation_value=confirmation_value
            ),
        ),
    )


def _assert_gate_one_uncommitted(case: Case) -> None:
    assert case.inquiry.problem_statement_confirmed is False, (
        "#1464: a DECLINE committed Gate 1 — the investigation starts on the "
        "statement the user asked to refine"
    )
    assert case.inquiry.problem_statement_confirmed_at is None
    assert case.inquiry.decided_to_investigate is False
    assert case.inquiry.decision_made_at is None
    assert case.state == CaseState.INQUIRY, (
        "#1464: Gate 1 alone drives _check_automatic_transitions, so a "
        "committed decline also transitions INQUIRY -> INVESTIGATING"
    )


@pytest.mark.unit
@pytest.mark.asyncio
class TestGateOneCommitsOnConsentOnly:
    """Driven through ``InvestigationService.process_turn`` — the click's path."""

    async def test_decline_commits_no_gate(self):
        case = _inquiry_case_awaiting_gate_one()
        await _decide_click(
            case, confirmation_value=False, payload_text=DECLINE_PAYLOAD
        )
        _assert_gate_one_uncommitted(case)

    async def test_confirm_still_commits_and_transitions(self):
        """The positive control. "Yes, let's investigate" is what the card is
        FOR; if this were blocked the fix would be a regression, not a fix."""
        case = _inquiry_case_awaiting_gate_one()
        await _decide_click(case, confirmation_value=True, payload_text=CONFIRM_PAYLOAD)
        assert case.inquiry.problem_statement_confirmed is True
        assert case.inquiry.decided_to_investigate is True
        assert case.state == CaseState.INVESTIGATING

    async def test_a_confirmation_intent_with_no_value_commits_nothing(self):
        """Only an explicit ``True`` is consent.

        ``QueryIntent`` refuses to validate a confirmation without a value, so
        this shape cannot arrive from the API — it is what a caller reaching
        the engine directly can still produce, and the commit rule is written
        to be safe there rather than to depend on the validator holding.
        """
        case = _inquiry_case_awaiting_gate_one()
        engine = _engine(case)
        await engine.process_turn(
            case=case,
            user_message="hmm",
            intent_type="confirmation",
            intent_data={},
        )
        _assert_gate_one_uncommitted(case)

    async def test_decline_is_not_a_no_op_turn(self):
        """A decline must still be ANSWERED, and must leave the statement
        refinable.

        Section 0b's substantive-decline arm sets the precedent: withdraw the
        proposal, then process the message normally so nothing the user said
        is swallowed by the gate. Gate 1's decline does the same by falling
        through — and because it committed nothing,
        ``proposed_problem_statement`` is still mutable, so the LLM rewrites
        it on this very turn and the confirmation pair is re-offered on the
        refined text.
        """
        case = _inquiry_case_awaiting_gate_one()
        llm = _LLM(state_updates={"proposed_problem_statement": REFINED})
        engine = _engine(case, llm)
        result = await engine.process_turn(
            case=case,
            user_message=DECLINE_PAYLOAD,
            intent_type="confirmation",
            intent_data={"value": False},
        )

        assert llm.calls > 0, "the decline never reached the LLM — it was a no-op turn"
        assert case.inquiry.proposed_problem_statement == REFINED, (
            "the statement stayed frozen on a decline; Gate 1's non-commit is "
            "what keeps it mutable"
        )
        _assert_gate_one_uncommitted(case)
        assert _gate1_is_pending(case) is True
        labels = [s.get("label") for s in (result.get("suggested_follow_ups") or [])]
        assert "Yes, let's investigate" in labels, (
            "the confirmation pair must be re-offered on the refined statement "
            f"- got {labels}"
        )

    @pytest.mark.parametrize(
        "pending",
        [
            None,
            {"to_state": "closed", "summary": "close it?"},
            {"needs_info": True, "to_state": "closed"},
        ],
        ids=["no-pending", "pending-close", "pending-needs-info"],
    )
    async def test_the_decline_holds_on_every_pending_shape(self, pending):
        """fm#918's lesson, applied to the fix.

        That exposure survived review because every Gate-1 fixture had
        ``pending_transition=None``, so the one shape that mattered was never
        exercised. These three reach 0c by three different routes:

          - ``no-pending`` — straight to 0c;
          - ``pending-close`` — 0b's decline arm cancels the pending, then
            falls through to 0c because the message is substantive;
          - ``pending-needs-info`` — 0b is skipped wholesale
            (``elif not case.pending_transition.get("needs_info")``) and the
            intent lands in 0c directly.

        Drop any one and a fix that holds only on the shape it was written
        against passes.
        """
        case = _inquiry_case_awaiting_gate_one()
        case.pending_transition = dict(pending) if pending else None
        engine = _engine(case)
        await engine.process_turn(
            case=case,
            user_message=SUBSTANTIVE_DECLINE,
            intent_type="confirmation",
            intent_data={"value": False},
        )
        _assert_gate_one_uncommitted(case)

    async def test_the_narrow_minted_intent_guard_is_safe_because_of_this(self):
        """Ties #1468's guard to the engine behaviour that licenses it.

        ``InvestigationService._minted_intent_swallows_gate_consent`` guarded
        BOTH arms of its Gate-1 test while 0c was value-blind, and #1464
        narrowed it to the affirmative. What makes the narrowing safe is that
        an ADOPTED declining mint now commits nothing — measured here rather
        than asserted in a docstring, so reverting the engine fix fails this
        test as well as the ones above.
        """
        case = _inquiry_case_awaiting_gate_one()
        assert (
            InvestigationService._minted_intent_swallows_gate_consent(
                case,
                QueryIntent(type=IntentType.CONFIRMATION, confirmation_value=False),
                "not quite - is the problem statement about the replica or the primary?",
            )
            is False
        ), "the guard was not narrowed; its docstring and the engine disagree"

        engine = _engine(case)
        await engine.process_turn(
            case=case,
            user_message="not quite - is the problem statement about the replica or the primary?",
            intent_type="confirmation",
            intent_data={"value": False},
        )
        _assert_gate_one_uncommitted(case)


# =============================================================================
# The rule, over every implementation of it
# =============================================================================


def _confirmation_branch_sites(source: str) -> list[ast.stmt]:
    """Every statement in ``source`` that decides off ``intent_type == "confirmation"``.

    Returns the smallest enclosing STATEMENT of each such comparison: the
    ``if``/``elif`` whose test it is, or the assignment whose value it is.
    """
    tree = ast.parse(textwrap.dedent(source))
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    sites: list[ast.stmt] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not (isinstance(node.left, ast.Name) and node.left.id == "intent_type"):
            continue
        if len(node.ops) != 1 or not isinstance(node.ops[0], ast.Eq):
            continue
        right = node.comparators[0]
        if not (isinstance(right, ast.Constant) and right.value == "confirmation"):
            continue
        cur: ast.AST = node
        while not isinstance(cur, ast.stmt):
            cur = parents[cur]
        if cur not in sites:
            sites.append(cur)
    return sites


def _reads_the_confirmation_value(site: ast.stmt) -> bool:
    """Whether ``site`` consults ``intent_data``'s ``"value"``.

    For an ``if``/``elif`` only the test and the OWN body count — walking
    ``orelse`` would let a later ``elif`` in the same chain vouch for a
    value-blind one.
    """
    if isinstance(site, ast.If):
        roots: list[ast.AST] = [site.test, *site.body]
    else:
        roots = [site]

    saw_intent_data = False
    saw_value_read = False
    for root in roots:
        for n in ast.walk(root):
            if isinstance(n, ast.Name) and n.id == "intent_data":
                saw_intent_data = True
            elif (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "get"
                and n.args
                and isinstance(n.args[0], ast.Constant)
                and n.args[0].value == "value"
            ):
                saw_value_read = True
            elif (
                isinstance(n, ast.Subscript)
                and isinstance(n.slice, ast.Constant)
                and n.slice.value == "value"
            ):
                saw_value_read = True
    return saw_intent_data and saw_value_read


#: A value-blind branch, for the scan's positive control. Shaped exactly like
#: 0c was before #1464.
_VALUE_BLIND_SOURCE = """
def turn(intent_type, intent_data, case):
    if intent_type == "status_transition":
        pass
    elif intent_type == "confirmation":
        case.inquiry.problem_statement_confirmed = True
    elif intent_type == "hypothesis_action":
        # A later arm in the SAME chain that DOES read the value, so a scan
        # walking ``orelse`` would vouch for the blind branch above.
        if (intent_data or {}).get("value"):
            pass
"""


@pytest.mark.unit
class TestEveryConfirmationBranchReadsTheValue:
    """One rule, three implementations — stated, scanned, and shipped as this.

    ``MilestoneEngine._process_turn_impl`` is where the rule can be violated:
    it is the only function that dispatches on the raw ``intent_type`` string,
    and the only one that commits a gate off it. N = 3 branches there decide
    off ``intent_type == "confirmation"``:

      - section 0b, ``intent_confirms``  — reads ``.get("value") is True``
      - section 0b, ``intent_declines``  — reads ``.get("value") is False``
      - section 0c, the Gate-1 commit    — read nothing until #1464

    A fourth added without a value read fails this.
    """

    SOURCE = inspect.getsource(MilestoneEngine._process_turn_impl)

    def test_the_scan_bites(self):
        """Positive control, run FIRST: a scan that has stopped reporting
        violations passes the real check for the wrong reason."""
        blind = _confirmation_branch_sites(_VALUE_BLIND_SOURCE)
        assert len(blind) == 1
        assert _reads_the_confirmation_value(blind[0]) is False, (
            "the scan vouched for a branch that commits Gate 1 without reading "
            "the value — it cannot report the defect it exists to report"
        )

    def test_the_scan_still_finds_the_three_known_branches(self):
        sites = _confirmation_branch_sites(self.SOURCE)
        assert len(sites) >= 3, (
            "found "
            f"{len(sites)} confirmation branches in _process_turn_impl, expected "
            "at least the 3 known ones (0b intent_confirms, 0b intent_declines, "
            "0c Gate-1 commit) — the scan is no longer looking at live code"
        )

    def test_every_branch_reads_the_value(self):
        offenders = [
            site.lineno
            for site in _confirmation_branch_sites(self.SOURCE)
            if not _reads_the_confirmation_value(site)
        ]
        assert offenders == [], (
            "branches deciding off intent_type == 'confirmation' without "
            f"reading intent_data['value'] at _process_turn_impl-relative "
            f"lines {offenders}. A confirmation intent carries the user's "
            "answer; a branch that ignores it answers the gate for them "
            "(#1464)."
        )
