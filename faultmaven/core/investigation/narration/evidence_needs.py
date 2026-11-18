"""The ``<evidence_needs>`` block rendered from the needs pool's typed view and
the case outcome record (#2811 S2b-1b-ii, #2615 stage S2).

``render_evidence_needs`` is pure: its need sections read the needs pool
(``NeedsView``, ruling 2 of 2026-11-17) and a need's text by id
(``content(id)``, actions and needs) alone. Its re-verification checklist is
``recheck_checklist``'s, rendered from the record (``bindable``'s rows and
the dated attempts, ``_dated``), each row's text and established turn by id
(``content(id, row=True)``, rows only), and, where no row verified the
problem, the rows passed fresh (``EvidenceNeedsFresh``). Where the checklist
could not be read (the fold, the fresh rows or a row's text failed), it is
the one declared line ``CHECKLIST_OMITTED``.

Where it may differ from a fresh read, the checklist's heading reads the
record's dated attempts, so it names the row where only the record still
dates an attempt: an ``accepted_in_turn`` carried final after the action's
was cleared, or a final FAILED attempt whose action is gone (declared; the
tests build both by hand, and neither is known to be reachable).
"""

from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from faultmaven.modules.case.contracts import (
    CaseOutcome,
    CaseState,
    NeedObtainability,
    NeedPriority,
    NeedPurpose,
    NeedState,
)

from .fix_verification import CaseIdentity, Content, _dated
from .needs import NeedsView, NeedView, _truncate_request_text

# Cap on rendered needs per section to keep token cost bounded. Each
# rendered need is ~80 chars of header (capped via
# ``narration.needs._REQUEST_TEXT_RENDER_CAP``) + ~80 chars of motivator line.
# Single-section case (DIAGNOSIS, or MITIGATION/TREATMENT with one of
# outstanding/re-verification empty): ~600 tokens worst case at 15
# needs. Both-sections case (MITIGATION/TREATMENT with both populated):
# up to 30 needs total, ~1200 tokens worst case. Typical cases stay
# well under either bound because the LLM emits short request_text.
_EVIDENCE_NEEDS_RENDER_CAP = 15

# Priority sort key — HIGH first so the LLM sees the urgent demand
# without scrolling. Keyed by enum member rather than ``.value`` so a
# future ``NeedPriority`` addition raises ``KeyError`` here instead of
# silently sinking the new bucket to the bottom of the list.
_PRIORITY_ORDER: dict[NeedPriority, int] = {
    NeedPriority.HIGH: 0,
    NeedPriority.MEDIUM: 1,
    NeedPriority.LOW: 2,
}

#: The checklist's one line when the record or a row's text could not be read
#: this turn (``recheck_checklist``), in ``fix_verification.OMITTED``'s words.
CHECKLIST_OMITTED = "The re-check rows could not be read this turn."

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


@dataclass(frozen=True)
class EvidenceNeedsFresh:
    """The checklist's problem rows where no row verified the problem
    (``problem.verifying_row_ids`` empty), read fresh at the call site (the
    S2b-1b-ii ruling): every symptom_evidence row, stable by its established
    turn, engine-authored ones included. Ids only."""

    uncited_symptom_row_ids: tuple[str, ...] = ()


def _render_ask_history(need: NeedView) -> str:
    """The ``asked N×`` fragment for a need's header line, or ``""`` if never.

    This is the stored counter the mention-decay rule used to tell the model to
    reconstruct "by scanning your prior turns in the conversation history"
    (#1079). That scan cannot work: ``HISTORY_VERBATIM_TURNS`` is 3, and older
    turns collapse to ``_build_turn_summary``, which records milestones,
    artifact counts and 200 chars of the reply — never what was asked for. Past
    three turns every repeat read as a first mention, so the ask never decayed.

    Stating the count as a fact removes the reconstruction entirely. The last
    turn is included because "asked 4×, last on turn 14" and "asked 4×, last on
    turn 6" call for opposite responses — the first is a live loop, the second
    is an old ask the user has moved past.
    """
    count = need.times_surfaced
    if count <= 0:
        return ""
    if count == 1:
        return f", asked once (turn {need.last_surfaced_turn})"
    return f", asked {count}× (last turn {need.last_surfaced_turn})"


def _render_need_line(need: NeedView, content) -> tuple[str, str]:
    """Render one need into (header_line, motivator_line); a need whose text
    does not read raises."""
    found = content(need.need_id)
    if found is None:
        raise LookupError(f"no text reads for need {need.need_id}")
    purpose_label = (
        "SYMPTOM" if need.purpose == NeedPurpose.SYMPTOM_VERIFICATION else "CAUSAL"
    )
    status_suffix = (
        f", {need.state.value.upper()}" if need.state != NeedState.PENDING else ""
    )
    header = (
        f"  - [{need.need_id}] {_truncate_request_text(found.text)} "
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


def _render_finding_line(row_id: str, found: Content, label: str) -> str:
    """Render one confirmed presence-evidence row as a re-check line.

    The re-verification checklist (MITIGATION/TREATMENT) is anchored on
    the confirmed ``symptom_evidence`` / ``causal_evidence`` rows — the
    canonical record of what was established — NOT on FULFILLED needs.
    Needs are demand-side and gap-conditional (created only when the
    verifying data wasn't already in hand), so a need-anchored checklist
    silently omits every symptom/cause that was confirmed from
    already-available data. Evidence rows exist for every confirmed
    finding; the checklist lists those a re-check can bind to
    (``recheck_checklist``).
    """
    summary = found.text or "(no summary)"
    return (
        f"  - [{row_id}] {_truncate_request_text(summary)} "
        f"({label}, established turn {found.established_turn})"
    )


def recheck_checklist(
    outcome: CaseOutcome,
    content: Callable[[str], Optional[Content]],
    fresh: Optional[EvidenceNeedsFresh],
) -> tuple[str, ...]:
    """The re-verification checklist's lines, or ``()`` with no row: the rows
    a re-check can name in ``rechecks`` and bind to (#1906 D1, the record's
    ``bindable``), the rows that showed the problem, then the cited basis of
    the cause pool (the rows its terminal verdicts cite, #2557). With no
    verifying row on record (a legacy case) the problem half is ``fresh``'s
    every symptom_evidence row: the model is asked to cite one; ``fresh`` is
    required there, and its absence raises ``ValueError``. The verifying rows
    keep their recorded order; the cause rows and ``fresh``'s are
    chronological. A row whose text does not read raises."""
    if outcome.problem.verifying_row_ids:
        problem = outcome.bindable.problem_row_ids
    elif fresh is None:
        raise ValueError(
            "recheck_checklist: no row verified the problem, so the fresh "
            "symptom rows (EvidenceNeedsFresh) are required"
        )
    else:
        problem = fresh.uncited_symptom_row_ids
    rows = [("SYMPTOM", i) for i in problem] + [
        ("CAUSE", i) for i in outcome.bindable.cause_row_ids
    ]
    if not rows:
        return ()
    lines = ["Re-verification checklist (confirmed findings — re-check each to"]
    if _dated(outcome):
        # After a dated fix attempt, a re-check names what it re-checks (#1906
        # D1), whatever the stage label (MITIGATION outranks TREATMENT while a
        # workaround is pending); a mitigation's relief does not (the
        # mitigation template). The record's dated attempts: the case's
        # ``fix_application_turn``, which the unbound-row feedback
        # (``_flag_unbound_post_fix_rows``) reads, but for an attempt only the
        # record still dates.
        lines.append(
            "confirm the fix held; emit *_absence_evidence when a signature " "is gone,"
        )
        lines.append("naming the row it re-checks in rechecks):")
    else:
        lines.append(
            "confirm the fix held; emit *_absence_evidence when a signature "
            "is gone):"
        )
    lines.append("")
    for label, row_id in rows[:_EVIDENCE_NEEDS_RENDER_CAP]:
        found = content(row_id)
        if found is None:
            raise LookupError(f"no text reads for row {row_id}")
        lines.append(_render_finding_line(row_id, found, label))
    overflow = len(rows) - _EVIDENCE_NEEDS_RENDER_CAP
    if overflow > 0:
        lines.append("")
        lines.append(
            f"  …and {overflow} more re-verification need(s) not "
            f"shown (cap reached)."
        )
    return tuple(lines)


def render_evidence_needs(
    identity: CaseIdentity,
    content: Callable[[str], Optional[Content]],
    needs: Callable[[], NeedsView],
    checklist: Optional[Sequence[str]],
) -> str:
    """Render the ``<evidence_needs>`` context block.

    ``needs`` is called only on an INVESTIGATING case. ``checklist`` is
    ``recheck_checklist``'s lines (``()`` outside MITIGATION and TREATMENT),
    or None where it could not be read: then the section is the one line
    ``CHECKLIST_OMITTED``, which prints even where no row would have shown.

    Returns ``""`` (progressive activation, design §10.6) when:

    - No need is outstanding (PENDING or PARTIALLY_MET; the STOPPED ones
      included) and the checklist has no line.
    - The case is not INVESTIGATING (terminal/inquiry have their own
      surfaces).

    Filtering rules (design §8.4):

    - Default (DIAGNOSIS stage): render PENDING + PARTIALLY_MET needs as
      "outstanding needs" — data to look for during upload review.
      FULFILLED and SUPERSEDED are excluded to save tokens.
    - MITIGATION / TREATMENT: render two clearly-labelled sections —
      outstanding needs as above, plus a "re-verification checklist"
      built from the confirmed presence-evidence rows a re-check can bind
      to (``recheck_checklist``) so the agent can confirm the fix held
      by re-checking the data that established each symptom/cause. The checklist is anchored on evidence rows, NOT on
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
    if identity.state != CaseState.INVESTIGATING:
        return ""
    # NOTE: do NOT early-return on an empty `evidence_needs` pool. The
    # MITIGATION/TREATMENT re-verification checklist is sourced from
    # presence-evidence rows, which exist even when no need was ever
    # created (the common gap-free case). The `not outstanding and not
    # re_verification` check below is the correct emptiness gate.
    pool = needs().needs
    outstanding_all = [n for n in pool if n.outstanding]
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
    exhausted = [n for n in outstanding_all if n.exhausted]
    exhausted_ids = {n.need_id for n in exhausted}
    outstanding_all = [n for n in outstanding_all if n.need_id not in exhausted_ids]
    # Surface-cap the causal asks (engine-differential + LLM-emitted causal) to the
    # rotating top-≤N (select_surfaced_causal_needs) so a broad retrieval-seeded
    # differential can't flood the user; SYMPTOM needs are unaffected. All needs stay
    # PENDING — this only bounds what is SHOWN, and rotates under non-progress so no
    # answerable ask is permanently hidden (#604).
    _surfaced_causal_ids = {n.need_id for n in pool if n.surfaced}
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
    # rows, NOT FULFILLED needs. Evidence rows exist for every confirmed
    # finding; FULFILLED needs are gap-conditional and gap-rare, so a
    # need-anchored checklist silently omits findings confirmed from
    # already-available data. Narrowed to the rows a re-check binds to
    # (#1906 D1): symptom rows first, in their recorded verifying order (or
    # chronological, read fresh, with none verifying), then cause rows,
    # chronological.
    re_verification = (CHECKLIST_OMITTED,) if checklist is None else checklist

    if not outstanding and not re_verification and not exhausted:
        return ""

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
            lines.extend(_render_need_line(need, content))
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
            lines.extend(_render_need_line(need, content))
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
        lines.extend(re_verification)

    lines.append("</evidence_needs>")
    return "\n".join(lines)
