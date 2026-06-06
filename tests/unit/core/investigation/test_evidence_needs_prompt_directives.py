"""Phase 5 tests: evidence-needs prompt directives.

Pins the per-block composition of the evidence-needs prompt fragments:

- ``_EVIDENCE_NEEDS_LIFECYCLE_BLOCK`` — universal, in every INVESTIGATING
  dispatch block.
- ``_EVIDENCE_NEEDS_RCA_POOL_EVAL_BLOCK`` — RCA (DIAGNOSIS) only.
- ``_EVIDENCE_NEEDS_REVERIFICATION_ADDENDUM`` — MITIGATION and TREATMENT.

The tests use substring matches against the block constants so a
future refactor that moves text around without losing meaning still
passes. What we want to catch: a block dropping out of a dispatch
block entirely, or the directive vocabulary drifting away from what
the schema apply-layer expects.

NOTE (investigation-flow redesign): the old symptom-only addendum and
the ``_PRE_PATH_DIAGNOSIS_BLOCK`` / ``_SYMPTOM_VALIDATION_BLOCK`` path
fragments were removed with the path fork. There is now a single
``_RCA_DIAGNOSIS_BLOCK`` DIAGNOSIS dispatch block in the unified flow.

Run:
    pytest tests/unit/core/investigation/test_evidence_needs_prompt_directives.py -v
"""

from __future__ import annotations

import pytest

from faultmaven.core.investigation.prompts.templates import (
    _EVIDENCE_NEEDS_LIFECYCLE_BLOCK,
    _EVIDENCE_NEEDS_RCA_POOL_EVAL_BLOCK,
    _EVIDENCE_NEEDS_REVERIFICATION_ADDENDUM,
    _RCA_DIAGNOSIS_BLOCK,
    INVESTIGATION_BASE,
    MITIGATION_INSTRUCTIONS,
    TREATMENT_INSTRUCTIONS,
)

# A short, distinctive substring from each block — used as the
# "is this block composed in?" probe. Chosen to be unique enough that
# unrelated text won't accidentally match.
_LIFECYCLE_PROBE = "EVIDENCE NEEDS (demand-side pool)"
_POOL_EVAL_PROBE = "POOL EVALUATION (at hypothesis creation)"
_REVERIFICATION_PROBE = "RE-VERIFICATION:"


# ============================================================
# Lifecycle block — present in every INVESTIGATING dispatch block
# ============================================================


@pytest.mark.unit
class TestLifecycleBlockComposedInAllDispatchBlocks:
    """The shared lifecycle block defines the cross-stage contract
    (event-driven emission, link inbound evidence, same-turn IDs,
    mutability, mention-decay). Every INVESTIGATING dispatch block
    must compose it so the LLM sees the contract regardless of which
    branch it's on this turn."""

    @pytest.mark.parametrize(
        "block,block_name",
        [
            (_RCA_DIAGNOSIS_BLOCK, "_RCA_DIAGNOSIS_BLOCK"),
            (MITIGATION_INSTRUCTIONS, "MITIGATION_INSTRUCTIONS"),
            (TREATMENT_INSTRUCTIONS, "TREATMENT_INSTRUCTIONS"),
        ],
    )
    def test_lifecycle_block_present(self, block, block_name):
        assert _LIFECYCLE_PROBE in block, (
            f"{block_name} is missing the shared evidence-needs lifecycle "
            "block — the LLM won't know about event-driven emission, "
            "inbound-evidence linking, or mention decay."
        )

    def test_lifecycle_block_appears_exactly_once_per_dispatch(self):
        """A double-composition would burn tokens and risk conflicting
        signals if one copy drifted. Single emission per block."""
        for block, name in [
            (_RCA_DIAGNOSIS_BLOCK, "_RCA_DIAGNOSIS_BLOCK"),
            (MITIGATION_INSTRUCTIONS, "MITIGATION_INSTRUCTIONS"),
            (TREATMENT_INSTRUCTIONS, "TREATMENT_INSTRUCTIONS"),
        ]:
            assert block.count(_LIFECYCLE_PROBE) == 1, (
                f"{name} composes the lifecycle block "
                f"{block.count(_LIFECYCLE_PROBE)} times (expected 1)."
            )


# ============================================================
# Lifecycle block content — pinned directive vocabulary
# ============================================================


@pytest.mark.unit
class TestLifecycleBlockContent:
    """The five universal directives, identified by their bold headers.
    Drift on any of these means the prompt no longer covers a known
    failure mode."""

    @pytest.mark.parametrize(
        "directive_marker",
        [
            "Event-driven emission",
            "Link inbound evidence to PENDING needs",
            "Same-turn IDs",
            "Mutability",
            "Mention decay",
        ],
    )
    def test_directive_present(self, directive_marker):
        assert directive_marker in _EVIDENCE_NEEDS_LIFECYCLE_BLOCK

    def test_mention_decay_references_conversation_history_mechanism(self):
        """Per design §10.5 the decay rule has no stored counter — the
        LLM scans its prior turns to count mentions. Without that
        framing the LLM may treat every turn as a first mention."""
        assert "prior turns" in _EVIDENCE_NEEDS_LIFECYCLE_BLOCK
        assert "no stored counter" in _EVIDENCE_NEEDS_LIFECYCLE_BLOCK

    def test_references_evidence_need_id_field(self):
        """The mention-decay rule requires populating ``evidence_need_id``
        on EVIDENCE-type suggestions (Phase 6 frontend linkage)."""
        assert "evidence_need_id" in _EVIDENCE_NEEDS_LIFECYCLE_BLOCK

    def test_references_new_index_n_pattern(self):
        """Same-turn ID resolution rides on the existing
        ``new_index_N`` placeholder pattern — the LLM needs to know
        the syntax."""
        assert "new_index_N" in _EVIDENCE_NEEDS_LIFECYCLE_BLOCK

    def test_references_fulfilling_evidence_ids(self):
        """Inbound-evidence linking is the load-bearing mechanism for
        needs to advance through PENDING → FULFILLED."""
        assert "fulfilling_evidence_ids" in _EVIDENCE_NEEDS_LIFECYCLE_BLOCK

    def test_does_not_restate_anti_anchoring_framing(self):
        """The anti-anchoring sentence (\"unexpected findings are
        equally important\") is rendered in <evidence_needs> by the
        context builder. Restating it in the prompt burns tokens for
        no signal."""
        assert "Unexpected findings" not in _EVIDENCE_NEEDS_LIFECYCLE_BLOCK


# ============================================================
# RCA pool-evaluation block — only in _RCA_DIAGNOSIS_BLOCK
# ============================================================


@pytest.mark.unit
class TestPoolEvalBlockLocation:
    """The 3-step pool evaluation at hypothesis creation (design §5.2)
    only applies where hypotheses can be created. That's
    _RCA_DIAGNOSIS_BLOCK on this dispatch."""

    def test_pool_eval_present_in_rca_block(self):
        assert _POOL_EVAL_PROBE in _RCA_DIAGNOSIS_BLOCK

    @pytest.mark.parametrize(
        "block,block_name",
        [
            (MITIGATION_INSTRUCTIONS, "MITIGATION_INSTRUCTIONS"),
            (TREATMENT_INSTRUCTIONS, "TREATMENT_INSTRUCTIONS"),
        ],
    )
    def test_pool_eval_absent_from_non_rca_blocks(self, block, block_name):
        """Hypothesis creation is gated outside DIAGNOSIS — pool
        evaluation would emit causal needs that the engine then
        rejects, wasting a turn."""
        assert _POOL_EVAL_PROBE not in block


@pytest.mark.unit
class TestPoolEvalBlockContent:
    """The three steps of pool evaluation map to specific schema fields.
    Renaming any of them would break the LLM's ability to follow the
    rule."""

    def test_step_1_references_hypothesis_evidence_links(self):
        assert "hypothesis_evidence_links" in _EVIDENCE_NEEDS_RCA_POOL_EVAL_BLOCK

    def test_step_2_references_motivating_hypothesis_ids(self):
        assert "motivating_hypothesis_ids" in _EVIDENCE_NEEDS_RCA_POOL_EVAL_BLOCK

    def test_step_3_references_causal_verification_purpose(self):
        assert "causal_verification" in _EVIDENCE_NEEDS_RCA_POOL_EVAL_BLOCK

    def test_step_3_references_evidence_need_updates(self):
        assert "evidence_need_updates" in _EVIDENCE_NEEDS_RCA_POOL_EVAL_BLOCK

    def test_step_2_covers_both_pending_and_partially_met(self):
        """context_builder renders both PENDING and PARTIALLY_MET in
        <evidence_needs>. A directive that says only "PENDING needs"
        would leave the LLM uncertain whether a PARTIALLY_MET need
        can take new motivator IDs — and could lead it to create a
        duplicate need rather than share into the partial one."""
        assert "PENDING / PARTIALLY_MET" in _EVIDENCE_NEEDS_RCA_POOL_EVAL_BLOCK


# ============================================================
# Re-verification addendum — only in MITIGATION and TREATMENT
# ============================================================


@pytest.mark.unit
class TestReVerificationAddendumLocation:
    """FULFILLED needs surface as a re-verification checklist only in
    MITIGATION/TREATMENT stages (design §8.4). The prompt addendum
    explains that surface to the LLM."""

    @pytest.mark.parametrize(
        "block,block_name",
        [
            (MITIGATION_INSTRUCTIONS, "MITIGATION_INSTRUCTIONS"),
            (TREATMENT_INSTRUCTIONS, "TREATMENT_INSTRUCTIONS"),
        ],
    )
    def test_reverification_present_in_post_diagnosis_stages(self, block, block_name):
        assert _REVERIFICATION_PROBE in block

    def test_reverification_absent_from_diagnosis_block(self):
        """The DIAGNOSIS dispatch block doesn't render FULFILLED needs —
        the re-verification framing would describe a checklist that
        isn't there."""
        assert _REVERIFICATION_PROBE not in _RCA_DIAGNOSIS_BLOCK

    def test_addendum_does_not_claim_causal_gating(self):
        """Causal-need gating is stage-specific (gated in MITIGATION,
        permitted in TREATMENT's failure path under extended
        diagnosis). The shared addendum stays neutral; gating notes
        live at each stage's existing 'no hypothesis formation'
        anchor."""
        # ``causal_absence_evidence`` does mention the word causal —
        # the gating-claim guard is about causal_verification *needs*,
        # not the absence-evidence category. Check the precise rule
        # doesn't appear.
        assert "causal_verification" not in _EVIDENCE_NEEDS_REVERIFICATION_ADDENDUM
        assert "Causal need creation" not in _EVIDENCE_NEEDS_REVERIFICATION_ADDENDUM


@pytest.mark.unit
class TestReVerificationSuccessCaseDirective:
    """Re-verification has two outcomes; both must be covered. The
    success-case (signature gone) directive triggers absence-evidence
    emission — the load-bearing audit record proving the fix held.
    Without it Phase 1's absence-evidence enums sit unused."""

    def test_success_case_directive_present(self):
        """If the signature is GONE, emit absence_evidence."""
        assert "signature is GONE" in _EVIDENCE_NEEDS_REVERIFICATION_ADDENDUM

    def test_failure_case_directive_present(self):
        """If the signature REAPPEARS, surface as a new finding."""
        assert (
            "signature REAPPEARS" in _EVIDENCE_NEEDS_REVERIFICATION_ADDENDUM
            or "did not hold" in _EVIDENCE_NEEDS_REVERIFICATION_ADDENDUM
        )

    def test_references_symptom_absence_evidence_category(self):
        assert "symptom_absence_evidence" in _EVIDENCE_NEEDS_REVERIFICATION_ADDENDUM

    def test_references_causal_absence_evidence_category(self):
        assert "causal_absence_evidence" in _EVIDENCE_NEEDS_REVERIFICATION_ADDENDUM

    def test_absence_rows_stand_alone_not_linked_to_hypothesis(self):
        """Both absence categories are stand-alone resolution audit rows.
        They must NOT be linked to a hypothesis: a successful fix confirms
        the root-cause hypothesis, and the apply-layer coerces any
        non-SUPPORTS stance to a likelihood penalty, so a confidence-
        bearing link would erode the very hypothesis the fix proves."""
        addendum = _EVIDENCE_NEEDS_REVERIFICATION_ADDENDUM
        assert "STAND-ALONE" in addendum
        assert "do NOT link" in addendum
        # The backwards-semantics link must be gone.
        assert "hypothesis_evidence_links" not in addendum
        assert "CONTRADICTS" not in addendum


# ============================================================
# MITIGATION causal-need gating extension at the existing anchor
# ============================================================


@pytest.mark.unit
class TestEvidenceClassificationDecisionTreeExtension:
    """The universal classification decision tree in INVESTIGATION_BASE
    must enumerate all six categories so the absence-evidence emission
    rule in the re-verification addendum doesn't contradict the
    universal rule the LLM reads first. INQUIRY_TEMPLATE keeps the
    legacy four-category enumeration — pre-investigation has no
    re-verification context."""

    def test_tree_header_acknowledges_six_categories(self):
        assert "(6 categories)" in INVESTIGATION_BASE

    def test_tree_has_re_verification_step(self):
        """Step 4 covers the re-verification case — without it the
        decision tree gives no path to the absence categories."""
        assert "RE-CHECKING" in INVESTIGATION_BASE

    def test_tree_references_symptom_absence_evidence(self):
        assert "symptom_absence_evidence" in INVESTIGATION_BASE

    def test_tree_references_causal_absence_evidence(self):
        assert "causal_absence_evidence" in INVESTIGATION_BASE

    def test_category_field_enumeration_includes_absence_categories(self):
        """The detailed required-fields enumeration must list the
        absence variants — otherwise a careful LLM might reject them
        as invalid."""
        site_idx = INVESTIGATION_BASE.find("category: One of:")
        assert site_idx != -1
        site_block = INVESTIGATION_BASE[site_idx : site_idx + 500]
        assert "symptom_absence_evidence" in site_block
        assert "causal_absence_evidence" in site_block


@pytest.mark.unit
class TestMitigationCausalNeedsGatingExtension:
    """The existing MITIGATION 'do not form hypotheses' line was
    extended to also forbid causal-purpose evidence_need_updates. The
    constraint is symmetric with hypothesis gating because causal
    needs presuppose a hypothesis to anchor."""

    def test_causal_evidence_need_updates_explicitly_gated(self):
        assert (
            "causal-purpose\n  evidence_need_updates" in MITIGATION_INSTRUCTIONS
            or "causal-purpose evidence_need_updates" in MITIGATION_INSTRUCTIONS
        )

    def test_fresh_symptom_needs_still_allowed_clarified(self):
        """The gating note also clarifies symptom needs remain
        allowed if new symptoms emerge during mitigation work — the
        LLM shouldn't over-restrict itself."""
        assert "symptom-purpose needs are still allowed" in MITIGATION_INSTRUCTIONS or (
            "symptom" in MITIGATION_INSTRUCTIONS.lower()
            and "still allowed" in MITIGATION_INSTRUCTIONS
        )


@pytest.mark.unit
class TestStageEvidenceTypeListingsIncludeAbsence:
    """The per-stage ``EVIDENCE TYPES FOR THIS STAGE:`` enumerations in
    MITIGATION and TREATMENT must list the absence variants now that
    re-verification emits them. Without these listings the prompt's
    descriptive surface contradicts the decision-tree step 4 directive
    in INVESTIGATION_BASE — a confused LLM might infer the stage's
    "allowed types" exclude absence variants.

    Pairs with the decision-tree extension pinned in
    ``TestEvidenceClassificationDecisionTreeExtension`` (Phase 5)."""

    def test_mitigation_lists_symptom_absence_evidence(self):
        assert "symptom_absence_evidence" in MITIGATION_INSTRUCTIONS

    def test_mitigation_lists_causal_absence_evidence(self):
        assert "causal_absence_evidence" in MITIGATION_INSTRUCTIONS

    def test_treatment_lists_symptom_absence_evidence(self):
        assert "symptom_absence_evidence" in TREATMENT_INSTRUCTIONS

    def test_treatment_lists_causal_absence_evidence(self):
        assert "causal_absence_evidence" in TREATMENT_INSTRUCTIONS

    def test_absence_variants_appear_under_evidence_types_heading(self):
        """Both absence variants must appear within the ``EVIDENCE TYPES FOR
        THIS STAGE:`` enumeration (where the LLM looks for "what categories
        are valid here"), not just incidentally elsewhere in the dispatch.

        The section is delimited by the heading and the next bold ``**...**``
        sub-heading — the absence bullets are each described in detail now
        (causal_absence is the required RESOLVED proof in TREATMENT), so the
        listing is longer than a fixed-width window would capture.
        """
        heading = "**EVIDENCE TYPES FOR THIS STAGE:**"
        for block in (MITIGATION_INSTRUCTIONS, TREATMENT_INSTRUCTIONS):
            heading_idx = block.find(heading)
            assert heading_idx != -1
            body_start = heading_idx + len(heading)
            # The enumeration runs until the next bold sub-heading.
            next_heading = block.find("\n**", body_start)
            section = (
                block[body_start:next_heading]
                if next_heading != -1
                else block[body_start:]
            )
            assert "symptom_absence_evidence" in section
            assert "causal_absence_evidence" in section
