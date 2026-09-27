from faultmaven.core.investigation.evidence_need_surfacing import (
    is_ask_exhausted,
    select_surfaced_causal_needs,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    EvidenceCategory,
    InvestigationStage,
    NeedObtainability,
    NeedPurpose,
    NeedState,
)

from .history import (
    _EVIDENCE_NEEDS_RENDER_CAP,
    _PRIORITY_ORDER,
    _render_ask_history,
    _truncate_request_text,
)


def _render_need_line(need) -> tuple[str, str]:
    """Render one need into (header_line, motivator_line)."""
    purpose_label = (
        "SYMPTOM" if need.purpose == NeedPurpose.SYMPTOM_VERIFICATION else "CAUSAL"
    )
    status_suffix = (
        f", {need.state.value.upper()}" if need.state != NeedState.PENDING else ""
    )
    header = (
        f"  - [{need.need_id}] {_truncate_request_text(need.request_text)} "
        f"({purpose_label}, {need.priority.value.upper()}{status_suffix}"
        f"{_render_ask_history(need)})"
    )
    if need.purpose == NeedPurpose.SYMPTOM_VERIFICATION:
        motivator_line = "      motivated_by: problem_statement"
    else:
        ids = need.motivating_hypothesis_ids
        motivator_line = (
            f"      motivated_by: [{', '.join(ids)}]"
            if ids
            else "      motivated_by: []"
        )
    return header, motivator_line


def _render_finding_line(ev) -> str:
    """Render one confirmed presence-evidence row as a re-check line.

    The re-verification checklist (MITIGATION/TREATMENT) is anchored on
    the confirmed ``symptom_evidence`` / ``causal_evidence`` rows — the
    canonical record of what was established — NOT on FULFILLED needs.
    Needs are demand-side and gap-conditional (created only when the
    verifying data wasn't already in hand), so a need-anchored checklist
    silently omits every symptom/cause that was confirmed from
    already-available data. Evidence rows exist for every confirmed
    finding, so this checklist is complete.
    """
    label = "SYMPTOM" if ev.category == EvidenceCategory.SYMPTOM_EVIDENCE else "CAUSE"
    summary = ev.summary or ev.extract or "(no summary)"
    return (
        f"  - [{ev.evidence_id}] {_truncate_request_text(summary)} "
        f"({label}, established turn {ev.collected_at_turn})"
    )


#: Heading for the suppressed-asks section of ``<evidence_needs>`` (#1079).
#: Pre-wrapped so the rendered block keeps the same line discipline as the rest
#: of the prompt regardless of how the source is formatted.
_EXHAUSTED_SECTION_HEADER = (
    "Asks the engine has STOPPED surfacing (repeated without result — the",
    "suggestion no longer reaches the user, so re-asking reaches no one).",
    "Dispose of each — obtainability=unobtainable if the data cannot be",
    "gathered, else supersede it — and proceed on what you have. Exception:",
    "if the user has said the data is coming (queued, or produced by a step",
    "that has not finished), leave the need as it is — it stays in the pool",
    "and still matches the upload when it arrives:",
)


def _build_evidence_needs_block(case: Case) -> str:
    """Render the ``<evidence_needs>`` context block.

    Returns ``""`` (progressive activation, design §10.6) when:

    - The pool is empty.
    - All needs are filtered out by status (no PENDING/PARTIALLY_MET
      in DIAGNOSIS; no PENDING/PARTIALLY_MET/FULFILLED in
      MITIGATION/TREATMENT).
    - The case is not INVESTIGATING (terminal/inquiry have their own
      surfaces).

    Filtering rules (design §8.4):

    - Default (DIAGNOSIS stage): render PENDING + PARTIALLY_MET needs as
      "outstanding needs" — data to look for during upload review.
      FULFILLED and SUPERSEDED are excluded to save tokens.
    - MITIGATION / TREATMENT: render two clearly-labelled sections —
      outstanding needs as above, plus a "re-verification checklist"
      built from the confirmed presence-evidence rows
      (``symptom_evidence`` / ``causal_evidence``) so the agent can
      confirm the fix held by re-checking the data that established each
      symptom/cause. The checklist is anchored on evidence rows, NOT on
      FULFILLED needs: needs are gap-conditional (created only when the
      verifying data wasn't already in hand), so a need-anchored
      checklist omits every finding confirmed from already-available
      data — the common case. Either section is omitted if empty.

    Output shape (DIAGNOSIS):

        <evidence_needs>
        Unexpected findings outside the entries below are equally
        important and may lead to new hypotheses or revised needs.

        Outstanding needs (data to look for in uploads):

          - [eneed_001] Response time metrics (SYMPTOM, HIGH)
              motivated_by: problem_statement
          - [eneed_003] DB connection pool metrics (CAUSAL, HIGH, asked 3× (last turn 9))
              motivated_by: [hyp_001, hyp_003]
        </evidence_needs>

    The ``asked N×`` fragment is the durable ask history (#1079) — see
    ``_render_ask_history``. It replaces the mention-decay rule's old
    instruction to count prior mentions out of conversation history, which is
    unreachable past the verbatim window.

    Ask-exhausted needs (``is_ask_exhausted``) are lifted out of the outstanding
    list into their own section, because the engine has stopped shipping their
    EVIDENCE suggestions and listing them among live asks would misdescribe the
    system to the model:

        Asks the engine has STOPPED surfacing (repeated without result — the
        suggestion no longer reaches the user, so re-asking reaches no one).
        Dispose of each — obtainability=unobtainable if the data cannot be
        gathered, else supersede it — and proceed on what you have. Exception:
        if the user has said the data is coming (queued, or produced by a step
        that has not finished), leave the need as it is — it stays in the pool
        and still matches the upload when it arrives:

          - [eneed_003] app-side STS debug output (CAUSAL, MEDIUM, asked 3× (last turn 12))
              motivated_by: [hyp_002]

    They are still outstanding: still matched against inbound uploads, still
    counted by the verification-status rollup, and still the model's to dispose
    of. Only the mechanical re-offer stopped. They are excluded from the
    surface-cap rotation and from the "…and N more not shown" overflow for the
    same reason UNOBTAINABLE needs are — a slot spent on an ask nobody is making
    starves a live discriminator.

    Output shape (MITIGATION/TREATMENT, both sections populated):

        <evidence_needs>
        Unexpected findings outside the entries below are equally
        important and may lead to new hypotheses or revised needs.

        Outstanding needs (data to look for in uploads):

          - [eneed_004] App connection timeout logs (CAUSAL, MEDIUM)
              motivated_by: [hyp_001]

        Re-verification checklist (confirmed findings — re-check each to
        confirm the fix held; emit *_absence_evidence when a signature is gone):

          - [ev_abc123] API p99 latency 8.9s during incident (SYMPTOM, established turn 5)
          - [ev_def456] audit_events Seq Scan, no index on created_at (CAUSE, established turn 8)
        </evidence_needs>
    """
    if case.state != CaseState.INVESTIGATING:
        return ""
    # NOTE: do NOT early-return on an empty `evidence_needs` pool. The
    # MITIGATION/TREATMENT re-verification checklist is sourced from
    # presence-evidence rows, which exist even when no need was ever
    # created (the common gap-free case). The `not outstanding and not
    # re_verification` check below is the correct emptiness gate.

    in_post_diagnosis = case.current_stage in (
        InvestigationStage.MITIGATION,
        InvestigationStage.TREATMENT,
    )

    outstanding_all = [n for n in case.evidence_needs if n.is_outstanding]
    # Ask-exhausted needs are split out BEFORE the surface cap and rendered in
    # their own section (#1079). They are still outstanding and still matched
    # against uploads, but the engine has stopped putting them to the user, so
    # rendering them among the live asks would state something untrue — the
    # model would read a pending ask it can still make, and re-make it into a
    # channel that is closed. The section says what actually happened instead.
    #
    # Dropping them from the block entirely was the other option and is worse:
    # the pool stays keyed on `request_text`, so a need the model cannot see is
    # a need it re-authors, and the duplicate arrives with an empty ask history
    # — resetting the very counter the suppression is computed from.
    exhausted = [n for n in outstanding_all if is_ask_exhausted(n, case.current_turn)]
    exhausted_ids = {n.need_id for n in exhausted}
    outstanding_all = [n for n in outstanding_all if n.need_id not in exhausted_ids]
    # Surface-cap the causal asks (engine-differential + LLM-emitted causal) to the
    # rotating top-≤N (select_surfaced_causal_needs) so a broad retrieval-seeded
    # differential can't flood the user; SYMPTOM needs are unaffected. All needs stay
    # PENDING — this only bounds what is SHOWN, and rotates under non-progress so no
    # answerable ask is permanently hidden (#604).
    _surfaced_causal_ids = {n.need_id for n in select_surfaced_causal_needs(case)}
    # Causal needs the surface cap held back this turn. Counted into the overflow
    # notice below so the "…and N more not shown" signal reflects the TRUE hidden
    # demand — otherwise these vanish from `outstanding` and the LLM is told the ask
    # list is near-complete while live discriminators are withheld (anti-anchoring §6.1).
    # UNOBTAINABLE needs are excluded: they were deliberately dropped from the
    # surfaced set (declared un-gettable), so counting them as "more not shown"
    # would nudge the model to chase data it already declared a wall on.
    hidden_causal = sum(
        1
        for n in outstanding_all
        if n.purpose == NeedPurpose.CAUSAL_VERIFICATION
        and n.need_id not in _surfaced_causal_ids
        and n.obtainability != NeedObtainability.UNOBTAINABLE
    )
    outstanding = [
        n
        for n in outstanding_all
        if n.purpose != NeedPurpose.CAUSAL_VERIFICATION
        or n.need_id in _surfaced_causal_ids
    ]
    # Re-verification checklist is anchored on confirmed presence-evidence
    # rows (symptom/causal), NOT FULFILLED needs. Evidence rows exist for
    # every confirmed finding; FULFILLED needs are gap-conditional and
    # gap-rare, so a need-anchored checklist silently omits findings
    # confirmed from already-available data. See _render_finding_line.
    re_verification = (
        [
            ev
            for ev in case.evidence
            if ev.category
            in (EvidenceCategory.SYMPTOM_EVIDENCE, EvidenceCategory.CAUSAL_EVIDENCE)
        ]
        if in_post_diagnosis
        else []
    )

    if not outstanding and not re_verification and not exhausted:
        return ""

    # Re-verification findings: chronological by the turn they were established.
    re_verification.sort(key=lambda ev: ev.collected_at_turn)

    # Render-cap budget: up to _EVIDENCE_NEEDS_RENDER_CAP entries per section so neither
    # starves the other — generous enough that real cases never hit the cap.
    #
    # The surface cap already chose ≤ _SURFACED_CAUSAL_CAP causal asks to show; those are
    # RESERVED a render slot. Otherwise the priority sort — causal needs are MEDIUM,
    # symptom needs HIGH — could push all of them past _EVIDENCE_NEEDS_RENDER_CAP behind a
    # wall of HIGH symptom needs, silently dropping the asks the surface cap just picked.
    # Only out_rest needs priority-sorting (it feeds the render-cap slice); the reserved
    # causal needs are kept regardless of priority, and out_rendered is sorted once at the
    # end for display (stable sort preserves the repo's created_at/need_id tie order).
    out_reserved = [n for n in outstanding if n.need_id in _surfaced_causal_ids]
    out_rest = [n for n in outstanding if n.need_id not in _surfaced_causal_ids]
    out_rest.sort(key=lambda n: _PRIORITY_ORDER[n.priority])
    out_rendered = (
        out_reserved
        + out_rest[: max(0, _EVIDENCE_NEEDS_RENDER_CAP - len(out_reserved))]
    )
    out_rendered.sort(key=lambda n: _PRIORITY_ORDER[n.priority])
    # Overflow = needs dropped by the render cap PLUS causal needs the surface cap
    # held back (`hidden_causal`), so the notice never under-reports the hidden demand.
    out_overflow = (len(outstanding) - len(out_rendered)) + hidden_causal
    reverif_rendered = re_verification[:_EVIDENCE_NEEDS_RENDER_CAP]
    reverif_overflow = len(re_verification) - len(reverif_rendered)

    lines: list[str] = ["<evidence_needs>"]
    # Anti-anchoring framing — design §6.1. Emitted once at the block
    # opening regardless of which sections fire so the LLM never treats
    # the list as exhaustive, including during re-verification (where
    # evidence that the fix introduced a new problem is exactly the
    # kind of finding this sentence keeps in view).
    lines.append(
        "Unexpected findings outside the entries below are equally important "
        "and may lead to new hypotheses or revised needs."
    )
    lines.append("")

    if outstanding:
        lines.append("Outstanding needs (data to look for in uploads):")
        lines.append("")
        for need in out_rendered:
            header, motivator_line = _render_need_line(need)
            lines.append(header)
            lines.append(motivator_line)
        if out_overflow > 0:
            lines.append("")
            lines.append(
                f"  …and {out_overflow} more outstanding need(s) not shown "
                f"(cap reached)."
            )

    if exhausted:
        if outstanding:
            lines.append("")
        # A statement of engine state, not a fourth restatement of the decay
        # rule. The suppression has already happened whether or not the model
        # acts on this; what the block owes the model is an accurate picture of
        # which asks still reach the user.
        lines.extend(_EXHAUSTED_SECTION_HEADER)
        lines.append("")
        for need in exhausted[:_EVIDENCE_NEEDS_RENDER_CAP]:
            header, motivator_line = _render_need_line(need)
            lines.append(header)
            lines.append(motivator_line)
        exhausted_overflow = max(0, len(exhausted) - _EVIDENCE_NEEDS_RENDER_CAP)
        if exhausted_overflow > 0:
            lines.append("")
            lines.append(
                f"  …and {exhausted_overflow} more suppressed ask(s) not shown "
                f"(cap reached)."
            )

    if re_verification:
        if outstanding or exhausted:
            lines.append("")
        lines.append("Re-verification checklist (confirmed findings — re-check each to")
        lines.append(
            "confirm the fix held; emit *_absence_evidence when a signature is gone):"
        )
        lines.append("")
        for ev in reverif_rendered:
            lines.append(_render_finding_line(ev))
        if reverif_overflow > 0:
            lines.append("")
            lines.append(
                f"  …and {reverif_overflow} more re-verification need(s) not "
                f"shown (cap reached)."
            )

    lines.append("</evidence_needs>")
    return "\n".join(lines)


def _build_candidate_solutions_block(case: Case) -> str:
    """Render the ``<candidate_solutions>`` block (R9).

    When a runbook-seeded cause has been *confirmed* (its root counterfactually
    validated), surface that runbook's structured ``interventions`` as CANDIDATE
    fixes so the LLM proposes them via ``solutions_to_add`` — with the
    intervention quadrant carried through — instead of re-deriving the fix from
    prose. A *prior, not a directive*: each candidate still requires the user to
    accept and verify, and the M5 gate is unchanged.

    Returns ``""`` unless the case is INVESTIGATING and a confirmed seeded cause
    carries captured interventions (``confirmed_cause_interventions``). Only the
    removed KB cause seeder ever captured them (fm#1295), so this renders for
    legacy cases only and is empty — like every optional block with nothing to
    show — for every case opened after 2026-09-02. Goes with
    ``seeded_provenance``'s sunset.
    """
    if case.state != CaseState.INVESTIGATING:
        return ""

    from faultmaven.core.investigation.seeded_provenance import (
        confirmed_cause_interventions,
    )

    interventions = confirmed_cause_interventions(case)
    if not interventions:
        return ""

    lines = [
        "<candidate_solutions>",
        "The confirmed root cause was seeded from a runbook that documents these",
        "interventions. They are CANDIDATE fixes for the established cause — a",
        "prior, not a directive. Weigh each against the case evidence; each still",
        "requires the user to accept and verify (the solution gate is unchanged).",
        "When you propose one via solutions_to_add, set `quadrant` to the listed",
        "quadrant so the fix is recorded against the right causal rung.",
        "",
    ]
    for iv in interventions:
        quadrant = iv.get("quadrant") or "?"
        text = " ".join((iv.get("text") or "").split())
        if len(text) > 300:
            text = text[:297] + "..."
        lines.append(f"- [{quadrant}] {text}")
    lines.append("</candidate_solutions>")
    return "\n".join(lines)
