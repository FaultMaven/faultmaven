"""Structural regression tests for INQUIRY_TEMPLATE.

Pin the load-bearing sections so future prompt cleanup PRs don't silently
remove pieces that drive specific agent behaviors. Same shape as
``test_path_conditional_diagnosis_prompts.py`` (for the DIAGNOSIS blocks)
and ``test_gate2_pending_reminder.py`` (for the Gate-2 reminder envelope).

These tests don't validate prompt EFFECTIVENESS — that's an eval concern.
They validate that the structural pieces the design depends on are
PRESENT. A "cleanup" PR that removes any of these without replacement
should fail this test and be forced to justify the removal.
"""

from __future__ import annotations

import pytest

from faultmaven.core.investigation.prompts.templates import INQUIRY_TEMPLATE


@pytest.mark.unit
class TestInquiryDisciplineStructure:
    """The four-discipline framing is the load-bearing addition.
    Each discipline encodes a specific behavior the LLM must learn:
    keen detection, intent sensitivity, turn-by-turn judgement,
    refine-and-re-present. Removing any one re-opens the failure
    mode it addresses.
    """

    def test_role_section_present(self):
        assert "YOUR ROLE IN INQUIRY" in INQUIRY_TEMPLATE
        # The single-gate principle — load-bearing for case progression.
        assert "SINGLE gate to INVESTIGATING" in INQUIRY_TEMPLATE

    def test_all_four_disciplines_present(self):
        assert "Four disciplines" in INQUIRY_TEMPLATE
        for d in [
            "KEEN ON PROBLEM DETECTION",
            "SENSITIVE TO USER INTENT",
            "ADJUST YOUR JUDGEMENT",
            "REFINE OR REPLACE THE PROBLEM STATEMENT",
        ]:
            assert d in INQUIRY_TEMPLATE, (
                f"Discipline '{d}' missing from INQUIRY_TEMPLATE. "
                f"This is one of the four agent disciplines instilled by "
                f"PR #375; removing it re-opens the case_febfafa305e9 "
                f"failure shape."
            )

    def test_refine_and_re_present_operational_rule_present(self):
        """The 4th discipline's operational corollary: refinements must
        be SHOWN to the user (not held internally). Without this,
        'refine internally' is technically discipline-compliant but
        operationally identical to the failure mode."""
        assert "REFINE + RE-PRESENT" in INQUIRY_TEMPLATE


@pytest.mark.unit
class TestInquiryLaneDiscipline:
    """The 'WHAT YOU MUST NOT DO' list draws the prose-layer line between
    INQUIRY and INVESTIGATING activities. Schema enforces the structured
    side (no hypotheses_to_add etc. on InquiryStateUpdate); this list
    covers the prose parallels that schema can't reach.
    """

    def test_forbidden_prose_activities_listed(self):
        for forbidden in [
            "Causal claims",
            "Hypothesis formation",
            "Solution emission",
            "Diagnostic narrative",
        ]:
            assert forbidden in INQUIRY_TEMPLATE, (
                f"Forbidden activity '{forbidden}' missing from the "
                f"WHAT YOU MUST NOT DO list. This list is the prose-layer "
                f"defense against the agent jumping to INVESTIGATING "
                f"work in INQUIRY (case_febfafa305e9 failure mode)."
            )

    def test_describe_vs_explain_principle_present(self):
        """The one-line principle that gives the LLM a heuristic for
        distinguishing INQUIRY-allowed from INVESTIGATING-forbidden."""
        assert "DESCRIBE vs EXPLAIN" in INQUIRY_TEMPLATE

    def test_schema_layer_clarification_present(self):
        """The header notes that structured-emission INV-07 protection
        already exists at the schema layer. Removing this clarification
        could lead future maintainers to add redundant engine-side
        backstop for a problem that's already structurally enforced.
        See PR #375 review feedback (Point 1)."""
        assert "schema already prevents" in INQUIRY_TEMPLATE


@pytest.mark.unit
class TestInquiryIntentRecognition:
    """The intent-recognition framework prevents the dead-end where the
    agent keeps pushing investigation onto a user who's just exploring.
    Three modes (knowledge / problem-detection / ambiguous) must all be
    represented so the agent has explicit guidance for each."""

    def test_recognizing_user_intent_section_present(self):
        assert "RECOGNIZING USER INTENT" in INQUIRY_TEMPLATE

    def test_three_intent_modes_present(self):
        for mode in [
            "KNOWLEDGE / EXPLORATORY",
            "PROBLEM DETECTION",
            "AMBIGUOUS",
        ]:
            assert mode in INQUIRY_TEMPLATE, (
                f"Intent mode '{mode}' missing. The three modes are the "
                f"structural shape that prevents the dead-end pattern "
                f"identified in the PR #375 design discussion."
            )

    def test_malfunction_discriminator_present(self):
        """The intent decision pivots on one question — is a system
        MALFUNCTIONING? — stated once at the top of RECOGNIZING USER
        INTENT. This is the load-bearing discriminator: without it,
        task/operational help (rotate a credential, configure a setting)
        gets mis-read as a fault, a problem statement is proposed, and the
        engine then re-emits the Gate-1 confirm/refine pair every turn
        (INV-01) — the 'same suggestions repeated' bug on case_28d15d4ab5f4
        (Cloudflare token rotation). Whitespace-normalized so prompt
        re-wrapping doesn't break it."""
        normalized = " ".join(INQUIRY_TEMPLATE.split())
        assert "is a system MALFUNCTIONING" in normalized
        # The verbs-are-not-faults clause is the operational teeth.
        assert "are not faults by themselves" in normalized

    def test_knowledge_exploratory_covers_task_help(self):
        """Task/operational help folds INTO the KNOWLEDGE / EXPLORATORY
        mode rather than getting its own bullet — a planned operation
        with nothing broken is the 'no fault' branch. Pins that the mode
        explicitly names task help so the fold-in can't silently regress
        to a questions-only definition."""
        normalized = " ".join(INQUIRY_TEMPLATE.split())
        assert "wants help performing a task" in normalized

    def test_exploratory_indefinite_inquiry_legitimacy_stated(self):
        """Pins the principle that a case staying in INQUIRY with no
        problem detected is a legitimate state (a successful consultation),
        not a stall. Without this the LLM might force a problem statement
        on every interaction. Whitespace-normalized check survives prompt
        re-wrapping."""
        normalized = " ".join(INQUIRY_TEMPLATE.split())
        assert "successful consultation" in normalized


@pytest.mark.unit
class TestInquiryUserAgencyOverAdvancement:
    """The user — not the agent — controls when the case advances to
    INVESTIGATING. This principle directly addresses the case_febfafa305e9
    failure mode where the agent did investigation work in INQUIRY,
    effectively driving the case forward without user consent."""

    def test_no_authority_to_advance_phrase_present(self):
        """The 'no authority to advance' wording is load-bearing — it
        explicitly names the agent's lack of authority to bypass user
        confirmation. Suggested by PR #375 review (Point 2)."""
        assert "no authority to advance" in INQUIRY_TEMPLATE


@pytest.mark.unit
class TestInquiryTransitionIntentRulesPreserved:
    """PR #370 (PR-A) added explicit prohibitions on the INQUIRY → RESOLVED
    edge with concrete prose guidance. PR #375 must preserve these rules
    when rewriting INQUIRY_TEMPLATE."""

    def test_inquiry_to_resolved_invalid_edge_rule_present(self):
        assert "INQUIRY → RESOLVED (NOT a valid edge" in INQUIRY_TEMPLATE

    def test_inquiry_to_closed_rule_present(self):
        assert "INQUIRY → CLOSED (handshake required)" in INQUIRY_TEMPLATE
