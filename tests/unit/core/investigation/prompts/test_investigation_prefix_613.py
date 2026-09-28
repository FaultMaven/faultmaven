"""The investigation prompt opens with a prefix that is byte-stable across turns (#613).

Provider prompt caches match on a byte-identical PREFIX. ``INVESTIGATION_BASE``
used to open with ``STATE`` and ``<case_identity>`` (which carries the current
time) and put ~370 lines of standing instructions after the case data, so its
first bytes changed on every turn and no turn could read the instructions back
from the cache. It is now laid out durable-first: the standing instructions,
then ``CACHE_BOUNDARY`` on its own line, then everything that changes per turn
— the DIAGNOSIS focus emphasis first — and finally the immutable
``<security_constraints>`` and the anti-padding closer, which end the prompt so
they are read last.

Two guards pin that layout:

- **Structure** — the template formatted with a unique sentinel per placeholder.
  Every placeholder must render BELOW the boundary unless it is on an explicit
  allowlist, each entry with its reason. The placeholder set is DERIVED from the
  template with ``string.Formatter().parse``, never listed by hand, so a
  per-turn placeholder added later lands on the failing side by default.
- **Assembly golden test** — two consecutive prompts for one case built through
  the real path (``get_prompt_for_case``), with every per-turn input varied
  between them — the diagnosis focus zone and the Zone-2 stale flip included.
  The text through the boundary line must be byte-identical, and must differ
  when the stage or the processing mode changes, so the comparison is not
  vacuous.
"""

from __future__ import annotations

import re
import string
from datetime import datetime, timedelta, timezone

import pytest
from freezegun import freeze_time

from faultmaven.core.investigation.prompts.context_builder.entity_highlights import (
    EntityHighlightGroup,
    EntityHighlightRow,
)
from faultmaven.core.investigation.prompts.fence import FENCE_ATTR
from faultmaven.core.investigation.prompts.templates.assembly import get_prompt_for_case
from faultmaven.core.investigation.prompts.templates.blocks import _PROMPT_FENCE_RULE
from faultmaven.core.investigation.prompts.templates.investigation import (
    INVESTIGATION_BASE,
)
from faultmaven.core.investigation.symptom_currency import STALE_AFTER
from faultmaven.infrastructure.llm.prompt_cache import CACHE_BOUNDARY
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CausalNode,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    InvestigationActionType,
    JournalEntry,
    NodeState,
    NodeType,
    ProblemVerification,
    ProposedAction,
    TurnOutcome,
    TurnProgress,
    UploadedFile,
    ValidationMethod,
    WorkingConclusion,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Structure guard
# ---------------------------------------------------------------------------

#: The ONLY placeholders allowed above ``CACHE_BOUNDARY``. Each renders the same
#: bytes on every turn of a case at one stage and processing mode; the reason
#: is the entry's comment. Anything not listed here must render below.
PREFIX_ALLOWLIST = {
    # Resolved from ``case.source``, which is stamped at case creation and never
    # changes (``assembly._page_capture_hint``).
    "page_capture_hint",
    # Constant per processing mode: the standard grounding block, or the
    # observation-time definition alone when knowledge_query / agent_meta
    # waive grounding. A mode turn misses the cache, which is correct.
    "evidence_grounding",
    # Constant per processing mode: the reasoning block, or "" when waived.
    "diagnostic_reasoning",
    # The stage instructions. Constant within a stage and processing mode, so
    # they change a few times a case at most — which makes them the prefix's
    # LAST part. The DIAGNOSIS focus emphasis, which moves with the milestones
    # and the wall clock, is NOT in them: it is {focus_emphasis}, in the tail.
    "adaptive_instructions",
}


def _placeholders(template: str) -> list[str]:
    """Every replacement field in ``template``, in order, derived from the parser."""
    return [f[1] for f in string.Formatter().parse(template) if f[1] is not None]


def _sentinel(name: str) -> str:
    return f"\x00<<{name}>>\x00"


def _rendered_with_sentinels() -> str:
    names = set(_placeholders(INVESTIGATION_BASE))
    return INVESTIGATION_BASE.format(**{n: _sentinel(n) for n in names})


PLACEHOLDERS = sorted(set(_placeholders(INVESTIGATION_BASE)))
PER_TURN = sorted(set(PLACEHOLDERS) - PREFIX_ALLOWLIST)


class TestTemplateStructure:
    def test_the_derived_placeholder_set_covers_the_template(self):
        """Guard the guard: the parser found the template's slots, and every
        allowlist entry is one of them (a stale entry would excuse nothing and
        hide that the list no longer describes the template)."""
        assert "identity" in PLACEHOLDERS and "user_message" in PLACEHOLDERS
        assert PREFIX_ALLOWLIST <= set(PLACEHOLDERS), sorted(
            PREFIX_ALLOWLIST - set(PLACEHOLDERS)
        )
        assert len(PER_TURN) >= 15

    def test_boundary_renders_exactly_once_on_its_own_line(self):
        rendered = _rendered_with_sentinels()
        assert rendered.count(CACHE_BOUNDARY) == 1
        assert f"\n{CACHE_BOUNDARY}\n" in rendered

    @pytest.mark.parametrize("name", PER_TURN)
    def test_per_turn_placeholder_renders_below_the_boundary(self, name):
        rendered = _rendered_with_sentinels()
        boundary = rendered.index(CACHE_BOUNDARY)
        first = rendered.index(_sentinel(name))
        assert first > boundary, (
            f"{{{name}}} renders above CACHE_BOUNDARY. Anything above it must be "
            "byte-identical on every turn, or no turn can read the prefix back "
            "from the provider's cache. Move it below the boundary, or — only if "
            "it is genuinely constant across turns — add it to PREFIX_ALLOWLIST "
            "with the reason."
        )

    @pytest.mark.parametrize("name", sorted(PREFIX_ALLOWLIST))
    def test_allowlisted_placeholder_renders_above_the_boundary(self, name):
        rendered = _rendered_with_sentinels()
        assert rendered.index(_sentinel(name)) < rendered.index(CACHE_BOUNDARY)

    def test_adaptive_instructions_is_the_last_part_of_the_prefix(self):
        """The prefix's only part that varies within a case — the stage
        instructions, which change with the stage or the processing mode — goes
        last. On providers that reuse the longest cached prefix (OpenAI, Gemini,
        Fireworks) a stage change then still reuses the standing instructions
        before it. Anthropic's one breakpoint sits at the boundary, so there a
        stage change re-writes the whole prefix, a few times a case."""
        rendered = _rendered_with_sentinels()
        boundary = rendered.index(CACHE_BOUNDARY)
        above = sorted(
            (rendered.index(_sentinel(n)), n)
            for n in PLACEHOLDERS
            if rendered.index(_sentinel(n)) < boundary
        )
        assert above[-1][1] == "adaptive_instructions"

    def test_focus_emphasis_opens_the_tail(self):
        """The DIAGNOSIS focus emphasis moves with the milestones and, in Zone 2,
        with the wall clock, so it is the first thing after the boundary line."""
        rendered = _rendered_with_sentinels()
        after = rendered[rendered.index(CACHE_BOUNDARY) + len(CACHE_BOUNDARY) :]
        assert after.startswith("\n" + _sentinel("focus_emphasis"))

    def test_closing_rules_end_the_prompt_after_the_user_message(self):
        """The immutable rules and the anti-padding closer are read LAST, after
        the untrusted case data and the user's message, with one line pointing
        back at the output-shaping rules in the prefix."""
        rendered = _rendered_with_sentinels()
        # The block itself, not the fence rule's mention of the tag name.
        opening = "<security_constraints>\n**IMMUTABLE RULES**"
        assert rendered.count(opening) == 1
        rules = rendered.index(opening)
        assert rules > rendered.index(_sentinel("user_message"))
        late = [n for n in PLACEHOLDERS if rendered.index(_sentinel(n)) > rules]
        assert not late, late
        closing = rendered[rules:]
        assert closing.rstrip().endswith("specific data or input would unblock you.")
        assert closing.index("</security_constraints>") < closing.index(
            "CRITICAL: Do NOT restate"
        )
        pointer = (
            "Compose your answer under the ASSISTANT ROLE, ACTION IMPACT, "
            "CONCISENESS and REASONING-FIRST rules above, and DIAGNOSTIC "
            "REASONING where this prompt includes it."
        )
        assert rendered[:rules].rstrip().endswith(pointer)
        assert rendered.index(pointer) > rendered.index(_sentinel("user_message"))

    def test_fence_rule_precedes_every_per_turn_placeholder(self):
        """#1256: the trust rule is stated before the first fenced block. The
        fenced blocks are rendered by per-turn placeholders, so preceding all of
        them precedes whichever renders the first fenced tag."""
        rendered = _rendered_with_sentinels()
        assert rendered.count(_PROMPT_FENCE_RULE) == 1
        rule_end = rendered.index(_PROMPT_FENCE_RULE) + len(_PROMPT_FENCE_RULE)
        late = [n for n in PER_TURN if rendered.index(_sentinel(n)) < rule_end]
        assert not late, late


# ---------------------------------------------------------------------------
# Assembly golden test (the real path)
# ---------------------------------------------------------------------------

FILE_ID = "file_613aaaaaaaaa"
TURN_A_TIME = "2026-09-28T10:00:00+00:00"
TURN_B_TIME = "2026-09-28T10:03:17+00:00"

#: Text that only turn B carries, one per per-turn input it varies. Each must
#: reach turn B's prompt BELOW the boundary, and none may appear in turn A's —
#: otherwise the input was not varied and the byte comparison proves nothing.
B_ONLY = {
    "evidence": "B-EVIDENCE-MARKER",
    "hypothesis": "B-HYPOTHESIS-MARKER",
    "journal": "B-JOURNAL-MARKER",
    "working_conclusion": "B-CONCLUSION-MARKER",
    "conversation": "B-CONVERSATION-MARKER",
    "user_message": "B-USER-MESSAGE-MARKER",
    "system_feedback": "B-FEEDBACK-MARKER",
    "pending_action": "B-PENDING-MARKER",
    "kb_results": "B-KB-MARKER",
    "entity_highlights": "10.61.3.99",
    "turn_number": "PROPOSED_IN_TURN: 4",
    "timestamp": f"CURRENT_TIME: {TURN_B_TIME}",
}


def _file() -> UploadedFile:
    return UploadedFile(
        file_id=FILE_ID,
        filename="checkout.log",
        size_bytes=512,
        content_type="text/plain",
        uploaded_at_turn=1,
        upload_source="file_upload",
        storage_ref="evidence/case_613/blob.txt",
        data_type="logs",
        summary="Checkout pod log.",
        structural_index="2026-09-28 ERROR CrashLoopBackOff\n",
    )


def _evidence(evidence_id: str, summary: str, turn: int) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        source_file_id=FILE_ID,
        summary=summary,
        extract="restart x40",
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        source_type=EvidenceSourceType.LOGS,
        primary_purpose="Test",
        collected_by="user_613",
        collected_at_turn=turn,
    )


def _record(turn: int, feedback: str | None = None) -> TurnProgress:
    return TurnProgress(
        turn_number=turn,
        progress_made=False,
        outcome=TurnOutcome.CONVERSATION,
        user_message_summary=f"user {turn}",
        agent_response_summary=f"agent {turn}",
        system_feedback=feedback,
    )


def _dated_symptom(observed: datetime) -> Evidence:
    """Symptom evidence whose content is dated — the input symptom currency reads."""
    return Evidence(
        evidence_id="ev_613000000009",
        summary="checkout 500s observed",
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        primary_purpose="Test",
        collected_by="user_613",
        collected_at_turn=1,
        coverage_start_ts=observed,
        coverage_end_ts=observed,
        coverage_source="iso8601",
    )


def _case(
    later: bool,
    *,
    symptom_verified: bool = False,
    observed: datetime | None = None,
    solution_accepted: bool = False,
) -> Case:
    """One case at turn 3 (A) or turn 4 (B). B is A plus one turn of work.

    The stage is derived from the gates (``progress.current_stage``):
    ``solution_accepted`` puts the case in TREATMENT, and DIAGNOSIS otherwise.
    """
    turn = 4 if later else 3
    case = Case(
        case_id="case_613aaaaaaaaa",
        title="Checkout crash-looping",
        description="Pods restart every 40s since the 10:40 deploy",
        user_id="user_613",
        enterprise_id="org_613",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            proposed_problem_statement="Checkout crash-looping",
        ),
        uploaded_files=[_file()],
        evidence=[_evidence("ev_613000000001", "Pods restart every 40s", 1)],
        current_turn=turn,
    )
    case.problem_verification = ProblemVerification(
        symptom_statement="checkout pods restart every ~40s",
        severity=CaseSeverity.HIGH,
    )
    case.progress.symptom_verified = symptom_verified
    case.progress.solution_accepted = solution_accepted
    if observed is not None:
        case.evidence.append(_dated_symptom(observed))
    case.turn_history = [_record(t) for t in range(1, turn)]
    case.messages = []
    for t in range(1, turn):
        case.messages.append(
            {"turn_number": t, "role": "user", "content": f"user said {t}"}
        )
        case.messages.append(
            {"turn_number": t, "role": "assistant", "content": f"agent said {t}"}
        )
    case.investigation_journal = [
        JournalEntry(turn=1, entry_type="finding", content="restarts began 10:41")
    ]
    case.working_conclusion = WorkingConclusion(
        statement="A-CONCLUSION: a bad deploy", likelihood=0.4, reasoning="timing"
    )
    if not later:
        return case

    case.evidence.append(
        _evidence("ev_613000000002", f"{B_ONLY['evidence']} OOMKilled x12", 3)
    )
    root = CausalNode(
        node_id="cn_613000000001",
        statement="memory limit lowered in the 10:40 deploy",
        node_type=NodeType.ROOT,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        actionable=True,
        generated_at_turn=3,
    )
    case.causal_nodes = {root.node_id: root}
    case.hypotheses = {
        "hyp_613000000001": Hypothesis(
            hypothesis_id="hyp_613000000001",
            statement=f"{B_ONLY['hypothesis']} memory limit too low",
            category=HypothesisCategory.CONFIG,
            state=HypothesisState.ACTIVE,
            generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
            rationale="OOMKilled rows follow the deploy",
            root_node_id=root.node_id,
            generated_at_turn=3,
        )
    }
    case.investigation_journal.append(
        JournalEntry(turn=3, entry_type="finding", content=B_ONLY["journal"])
    )
    case.working_conclusion = WorkingConclusion(
        statement=B_ONLY["working_conclusion"], likelihood=0.6, reasoning="OOM rows"
    )
    case.messages.append(
        {"turn_number": 3, "role": "user", "content": B_ONLY["conversation"]}
    )
    case.turn_history[-1] = _record(3, feedback=B_ONLY["system_feedback"])
    case.proposed_actions.append(
        ProposedAction(
            case_id=case.case_id,
            action_type=InvestigationActionType.DIAGNOSTIC,
            description=B_ONLY["pending_action"],
            commands=["kubectl describe pod checkout-0"],
            proposed_in_turn=4,
        )
    )
    return case


def _prompt(later: bool, when: str, **overrides) -> str:
    case_kwargs = {
        k: overrides.pop(k)
        for k in ("symptom_verified", "observed", "solution_accepted")
        if k in overrides
    }
    kwargs: dict = {}
    if later:
        kwargs["kb_results"] = [
            {"title": B_ONLY["kb_results"], "summary": "raise the limit"}
        ]
        kwargs["entity_highlight_groups"] = [
            EntityHighlightGroup(
                entity_type="ip",
                rows=(EntityHighlightRow(B_ONLY["entity_highlights"], 214, True),),
            )
        ]
    kwargs.update(overrides)
    message = B_ONLY["user_message"] if later else "what should I check first?"
    with freeze_time(when):
        return get_prompt_for_case(_case(later, **case_kwargs), message, **kwargs)


def _prefix(prompt: str) -> str:
    """The prompt through the end of the boundary line."""
    assert prompt.count(CACHE_BOUNDARY) == 1, "no boundary: the fallback prompt?"
    end = prompt.index(CACHE_BOUNDARY) + len(CACHE_BOUNDARY)
    return prompt[: end + 1]


def _fence_token(prompt: str) -> str:
    return re.search(rf'{FENCE_ATTR}="([0-9a-f]+)"', prompt).group(1)


def _tail(prompt: str) -> str:
    return prompt[len(_prefix(prompt)) :]


#: Distinctive lines of the focus emphasis, one per variant the tests render.
ZONE_1 = "INVESTIGATION PROGRESS: Symptom verification pending"
ZONE_2 = "Symptoms are confirmed."
ZONE_2_STALE = (
    "INVESTIGATION PROGRESS: Root cause analysis — anchor to the symptom's window"
)


class TestPrefixIsByteStableAcrossTurns:
    @pytest.mark.parametrize("symptom_verified", [False, True])
    def test_prefix_is_identical_when_every_per_turn_input_changes(
        self, symptom_verified
    ):
        a = _prompt(False, TURN_A_TIME, symptom_verified=symptom_verified)
        b = _prompt(True, TURN_B_TIME, symptom_verified=symptom_verified)

        # Non-vacuity: every input really changed, and each change reached B's
        # tail — so an identical prefix is a finding, not an accident.
        tail_b = b[len(_prefix(b)) :]
        for what, marker in B_ONLY.items():
            assert marker in tail_b, f"{what} did not reach turn B's prompt"
            assert marker not in a, f"{what} was not varied between the turns"
        assert f"CURRENT_TIME: {TURN_A_TIME}" in a
        assert _fence_token(a) != _fence_token(b)

        assert _prefix(a) == _prefix(b)

    def test_prefix_is_identical_across_a_focus_change(self):
        """Zone 1 on turn A, Zone 2 on turn B: the symptom was verified between
        them. The focus emphasis moves, in the tail; the prefix does not."""
        a = _prompt(False, TURN_A_TIME, symptom_verified=False)
        b = _prompt(True, TURN_B_TIME, symptom_verified=True)

        assert ZONE_1 in _tail(a) and ZONE_1 not in b
        assert ZONE_2 in _tail(b) and ZONE_2 not in a
        assert _prefix(a) == _prefix(b)

    def test_prefix_is_identical_across_the_zone2_stale_flip(self):
        """Zone 2 with a dated symptom: CURRENT on turn A, STALE on turn B, the
        clock alone having moved past ``STALE_AFTER``. The emphasis flips to
        the stale variant, in the tail; the prefix does not move."""
        observed = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)
        when_a = (observed + STALE_AFTER / 3).isoformat()
        when_b = (observed + STALE_AFTER + timedelta(minutes=15)).isoformat()
        a = _prompt(False, when_a, symptom_verified=True, observed=observed)
        b = _prompt(True, when_b, symptom_verified=True, observed=observed)

        assert ZONE_2 in _tail(a) and ZONE_2_STALE not in a
        assert ZONE_2_STALE in _tail(b)
        assert _prefix(a) == _prefix(b)

    @pytest.mark.parametrize("change", ["stage", "processing_mode"])
    def test_prefix_changes_with_the_stage_or_the_processing_mode(self, change):
        """Non-vacuity: the two inputs the prefix MAY vary with do move it."""
        a = _prompt(False, TURN_A_TIME)
        if change == "stage":
            b = _prompt(True, TURN_B_TIME, solution_accepted=True)
            assert "KB-RESOLUTION VARIANT" in _prefix(b)  # TREATMENT's block
        else:
            b = _prompt(True, TURN_B_TIME, processing_mode="knowledge_query")
        assert _prefix(a) != _prefix(b)

    def test_fence_rule_precedes_the_first_fenced_tag(self):
        """#1256 on the rendered prompt: the rule sits in the prefix, the one
        genuine declaration and the first fenced tag sit below the boundary."""
        prompt = _prompt(True, TURN_B_TIME)
        rule_end = prompt.index(_PROMPT_FENCE_RULE) + len(_PROMPT_FENCE_RULE)
        boundary = prompt.index(CACHE_BOUNDARY)
        declaration = prompt.index("\nFENCE: ")
        # The declaration line names the token itself; the first TAG is after it.
        declaration_end = prompt.index("\n", declaration + 1)
        first_fenced = prompt.index(
            f'{FENCE_ATTR}="{_fence_token(prompt)}"', declaration_end
        )
        assert rule_end < boundary < declaration < first_fenced
        assert (
            prompt[declaration_end:first_fenced].lstrip().startswith("<problem_context")
        )
