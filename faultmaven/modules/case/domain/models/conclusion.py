import re
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, model_validator

# ============================================================
# Conclusion Models (Section 10)
# ============================================================


class ConfidenceLevel(str, Enum):
    """
    Categorical confidence levels.
    Maps to numeric confidence scores.
    """

    SPECULATION = "speculation"
    """
    Low confidence guess.
    Score: < 0.5
    """

    PROBABLE = "probable"
    """
    Likely but not certain.
    Score: 0.5 - 0.69
    """

    CONFIDENT = "confident"
    """
    High confidence.
    Score: 0.7 - 0.89
    """

    VERIFIED = "verified"
    """
    Evidence-backed certainty.
    Score: >= 0.9
    """

    @staticmethod
    def from_score(score: float) -> "ConfidenceLevel":
        """Convert numeric score to categorical level"""
        if score < 0.5:
            return ConfidenceLevel.SPECULATION
        elif score < 0.7:
            return ConfidenceLevel.PROBABLE
        elif score < 0.9:
            return ConfidenceLevel.CONFIDENT
        else:
            return ConfidenceLevel.VERIFIED


class WorkingConclusion(BaseModel):
    """
    Agent current best understanding of the problem.
    Updated iteratively as investigation progresses.

    Less authoritative than RootCauseConclusion.
    """

    statement: str = Field(
        description="Current conclusion statement", min_length=1, max_length=1000
    )

    likelihood: float = Field(
        ge=0.0, le=1.0, description="Likelihood of this conclusion (0.0-1.0)"
    )

    reasoning: str = Field(
        description="Why agent believes this conclusion", max_length=2000
    )

    supporting_evidence_ids: List[str] = Field(
        default_factory=list, description="Evidence IDs supporting this conclusion"
    )

    caveats: List[str] = Field(
        default_factory=list, description="Limitations or uncertainties"
    )

    mirrors_root_cause_conclusion: bool = Field(
        default=False,
        description=(
            "True when this working conclusion is a MIRROR of the case's "
            "RootCauseConclusion rather than an independent read of the live "
            "hypothesis differential (#987). Load-bearing: the working "
            "conclusion is one of `cause_identification_leg`'s BACKSTOP legs, "
            "and it is read from the PREVIOUS turn (it regenerates after the "
            "recompute). A mirror carries the RCC's likelihood, so without this "
            "flag a retracted conclusion would keep satisfying the backstop for "
            "one further turn through its own stale mirror — the retraction is "
            "supposed to make every consumer see one truth. A mirror is never "
            "an independent signal anyway: whenever one exists the `rcc` leg "
            "already governs."
        ),
    )

    # ============================================================
    # Metadata
    # ============================================================
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When this conclusion was formed/updated",
    )

    supersedes_conclusion_at: Optional[datetime] = Field(
        default=None, description="Timestamp of previous conclusion this replaces"
    )


# =============================================================================
# RootCauseConclusion display contract (#1097)
#
# Two of the conclusion's fields are rendered VERBATIM to users — by the
# resolution summary, and (for the mechanism) into any runbook harvested from
# the case. Before #1097 both carried engine-internal notation: ``established_by``
# held the id-bearing audit line the confirm-stamp writes onto its node link,
# and ``mechanism`` ended with the graph's synthetic PROBLEM terminal.
#
# The producers are fixed at the source, but terminal cases NEVER recompute, so
# every case resolved before that keeps the internal form in the stored row.
# These normalize at the read, and live here — beside the model whose fields
# they describe — so the report and the runbook conversion share one definition
# without either taking a dependency on the investigation engine.
# =============================================================================

CONFIRMED_ESTABLISHED_BY = (
    "confirmation that the problem was resolved, together with evidence "
    "that this cause was no longer present"
)
"""The prose provenance of a counterfactually confirmed cause — the two legs of
the promotion (the user's handshake, and evidence the cause was gone with the
problem) without the evidence/node ids or the M2 grade shorthand, which mean
nothing outside the engine. The engine's confirm-stamp writes this; its
id-bearing audit twin goes on the node's evidence-link reasoning."""

# Engine ids are a fixed prefix plus 12 hex and cannot occur in the prose form,
# so this identifies a pre-#1097 row exactly rather than by heuristic.
_ENGINE_ID_IN_PROSE = re.compile(r"\b(?:ev|cn)_[0-9a-f]{12}\b")

_MECHANISM_PROBLEM_TAIL = " → the problem"


def established_by_for_display(stored: Optional[str]) -> Optional[str]:
    """What a reader should see for ``RootCauseConclusion.established_by``.

    A stored value carrying engine ids is, by construction, the old
    confirm-stamp provenance — that path was the field's ONLY writer — so it is
    replaced with the prose form of the SAME fact rather than suppressed: the
    provenance is real and worth stating, only its rendering was wrong.

    Anything else, including an empty value, passes through untouched.
    """
    if stored and _ENGINE_ID_IN_PROSE.search(stored):
        return CONFIRMED_ESTABLISHED_BY
    return stored


def mechanism_for_display(stored: Optional[str]) -> Optional[str]:
    """What a reader should see for ``RootCauseConclusion.mechanism``.

    Strips the trailing synthetic PROBLEM node. The arrows BETWEEN rungs are
    real content — they are the chain — so only the dangling terminal goes: it
    restated the surrounding heading ("How it produced the symptom") in the
    graph's own notation.

    Narrow on purpose: an exact suffix match, not an arrow-splitting rewrite, so
    a mechanism whose last rung legitimately ends in those words is the only
    false positive available, and losing it costs nothing.
    """
    if stored and stored.endswith(_MECHANISM_PROBLEM_TAIL):
        return stored[: -len(_MECHANISM_PROBLEM_TAIL)]
    return stored


# The resolution summary's own rendered labels. Anchoring on them keeps the
# rewrite below scoped to the two lines the generator produced, so nothing in a
# user's evidence or an LLM's prose can be caught by it. They are LOAD-BEARING:
# changing the wording in ``report_generation_service`` silently stops
# normalizing rows written before it (there is a matching note at the generator).
_REPORT_ESTABLISHED_PREFIX = "_Established by: "
_REPORT_ESTABLISHED_SUFFIX = "._"
_REPORT_MECHANISM_PREFIX = "**How it produced the symptom:** "


def normalize_stored_report_content(content: Optional[str]) -> Optional[str]:
    """Rewrite the two pre-#1097 lines in a STORED report, or return it as-is.

    A resolution summary is not rendered on read — it is generated once at the
    terminal transition and persisted as markdown — so fixing the producers left
    every case resolved before #1097 serving the engine notation forever. This
    is the same normalization the two conclusion FIELDS get, applied to the
    materialized document, and it reuses those very functions rather than
    re-deriving the detection so the two can never drift apart.

    Called where a stored report BECOMES a ``CaseReport`` (the repositories'
    ``_row_to_report``), not at each presentation site. That placement is the
    point: applied per-reader it is a discipline every future consumer has to
    opt into, and the PR that introduced it had already missed one — the report
    download endpoint, in a different module, served the raw column. Applied at
    the boundary it is a property of any report loaded from storage.

    Line-scoped and label-anchored: only a line the generator itself wrote as
    the provenance or the mechanism is considered, and only its VALUE is passed
    to the field normalizer that owns it. A line either normalizer leaves
    unchanged is left byte-identical, so a current report round-trips exactly
    and the rewrite is self-limiting — a report generated after #1097 can never
    match.

    Deliberately not a backfill: regenerating a stored summary would rewrite a
    historical record with everything the generator has learned since. One
    consequence worth knowing — a report EDIT reads then writes, so an edited
    legacy row is persisted normalized. That is a strict improvement, not a
    second rendering path.
    """
    if not content:
        return content

    lines = content.split("\n")
    changed = False
    for i, line in enumerate(lines):
        if line.startswith(_REPORT_ESTABLISHED_PREFIX) and line.endswith(
            _REPORT_ESTABLISHED_SUFFIX
        ):
            value = line[
                len(_REPORT_ESTABLISHED_PREFIX) : -len(_REPORT_ESTABLISHED_SUFFIX)
            ]
            fixed = established_by_for_display(value)
            if fixed != value:
                lines[i] = (
                    f"{_REPORT_ESTABLISHED_PREFIX}{fixed}{_REPORT_ESTABLISHED_SUFFIX}"
                )
                changed = True
        elif line.startswith(_REPORT_MECHANISM_PREFIX):
            value = line[len(_REPORT_MECHANISM_PREFIX) :]
            fixed = mechanism_for_display(value)
            if fixed != value:
                lines[i] = f"{_REPORT_MECHANISM_PREFIX}{fixed}"
                changed = True

    return "\n".join(lines) if changed else content


class RootCauseConclusion(BaseModel):
    """
    Final determination of root cause.
    More authoritative than WorkingConclusion.
    """

    root_cause: str = Field(
        description="Definitive statement of root cause", min_length=1, max_length=1000
    )

    confidence_level: ConfidenceLevel = Field(
        description="Categorical confidence level"
    )

    likelihood: float = Field(
        ge=0.0, le=1.0, description="Numeric likelihood score (0.0-1.0)"
    )

    mechanism: str = Field(
        description="How this root cause led to the symptom", max_length=2000
    )

    # ============================================================
    # Evidence Basis
    # ============================================================
    evidence_basis: List[str] = Field(
        default_factory=list, description="Evidence IDs supporting this conclusion"
    )

    validated_hypothesis_id: Optional[str] = Field(
        default=None,
        description="If identified via hypothesis validation, the hypothesis ID",
    )

    names_root_node_id: Optional[str] = Field(
        default=None,
        description=(
            "Attribution hint (INV-35): the cn_ root node the LLM named as this "
            "conclusion's cause. Resolved to validated_hypothesis_id by the "
            "engine at cause-state recompute (the authoritative link, §7.7); a "
            "conclusion without it falls back to the lexical scan."
        ),
    )

    # ============================================================
    # Contributing Factors
    # ============================================================
    contributing_factors: List[str] = Field(
        default_factory=list,
        description=(
            "The cause's CO-NECESSARY conditions (#1096): the statements of "
            "VALIDATED causal nodes that share an M7 AND-set with the chain this "
            "conclusion mirrors, and are not themselves on it. Engine-derived "
            "from the graph (``causal_graph.validated_and_conjuncts``) at every "
            "mint, never authored — a conclusion renders one chain, so without "
            "this a cause the investigation established as a conjunction would "
            "be surfaced as its first conjunct alone. The LLM has no schema "
            "field for it by design (§7.7 single authority: the engine does not "
            "blend LLM prose into text it renders from the graph)."
        ),
    )

    # ============================================================
    # Metadata
    # ============================================================
    determined_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When root cause was determined",
    )

    determined_by: str = Field(
        default="agent", description="Who determined: 'agent' or user_id"
    )

    established_by: Optional[str] = Field(
        default=None,
        max_length=500,
        description=(
            "PROVENANCE (#987): how this conclusion came to be established, in "
            "USER-FACING prose — e.g. 'confirmation that the problem was "
            "resolved, together with evidence that this cause was no longer "
            "present'. The resolution summary renders this verbatim, so it "
            "carries NO evidence/node ids and no milestone shorthand; the "
            "id-bearing audit form of the same promotion lives on the node's "
            "evidence-link reasoning (#1097 — this description said "
            "'human-readable' from the start and its own example did not obey "
            "it, which is how the ids reached the user). "
            "Set when the engine PROMOTES a cause from confirmation plus "
            "evidence rather than from chain validation alone, so the "
            "structured record carries how it was established instead of a "
            "bare assertion. None on conclusions the LLM authored directly "
            "(their provenance is the transcript) and on the per-turn chain "
            "mirror (its provenance is the validated chain itself)."
        ),
    )

    # ============================================================
    # Validation
    # ============================================================
    @model_validator(mode="after")
    def confidence_consistency(self):
        """Ensure confidence_level matches likelihood"""
        level = self.confidence_level
        score = self.likelihood

        if level and score is not None:
            expected_level = ConfidenceLevel.from_score(score)
            if level != expected_level:
                raise ValueError(
                    f"confidence_level {level} does not match likelihood {score} (expected {expected_level})"
                )

        return self
