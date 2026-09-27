"""Classification-clarification cards: the choices and the on-offer set.

Owns the cooperative-clarification UX for an upload the heuristic classifier
could not confidently type: how the copy names the attachment
(``_clarification_subject`` / ``_upload_subject`` / the label qualifier), the
DECIDE suggestions and narration note built for every ``classification_failed``
attachment, and the admission rule that decides which of this turn's and
earlier turns' clarification choices may be on offer together (#1245, fm#918).
"""

from typing import Any, Dict, List, Optional, Set, Tuple

from faultmaven.core.investigation.suggestion_liveness import (
    CLARIFICATION_SPAN_CAP,
    OFFERED_DATA_TYPE_KEY,
    OFFERED_TURN_KEY,
    entry_file_id,
    entry_match_keys,
    file_data_types,
    is_clarification_entry,
    live_suggestions,
)
from faultmaven.models.api_models import (
    IntentType,
    SuggestedActionResponse,
)

# Cross-module imports via contracts (Principle 2: Vertical Modules with Contracts)
from faultmaven.modules.agent.domain.services.investigation_service.attachments import (
    _is_paste_upload,
    _PreprocessedAttachment,
)
from faultmaven.modules.case.contracts import (
    Case,
)

# DataType string value → human-friendly phrasing for cooperative clarification
# suggestions. Users see `label` on the suggestion card; `long` goes into the
# pre-composed query_submit payload. UNANALYZABLE is intentionally omitted —
# not a choice users can meaningfully select. UNSTRUCTURED_TEXT is handled as
# the "Something else" fallback separately to avoid duplicate suggestions.
_CLARIFICATION_FRIENDLY_NAMES: Dict[str, Dict[str, str]] = {
    "logs_and_errors": {"label": "Application logs", "long": "application logs"},
    "error_report": {
        "label": "Error report",
        "long": "an error report or stack trace",
    },
    "trace_data": {"label": "Trace data", "long": "trace data"},
    "metrics_and_performance": {
        "label": "Metrics",
        "long": "metrics or performance data",
    },
    "profiling_data": {"label": "Profiling data", "long": "profiling data"},
    "command_output": {"label": "Command output", "long": "command output"},
    "structured_config": {"label": "Configuration", "long": "configuration"},
    "source_code": {"label": "Source code", "long": "source code"},
    "documentation": {"label": "Documentation", "long": "documentation or notes"},
    "visual_evidence": {"label": "Screenshot", "long": "a screenshot or image"},
}

# Fallback phrasing used for the "Something else" option and applied when the
# classifier's only suggested type is UNSTRUCTURED_TEXT.
_CLARIFICATION_FALLBACK_LABEL = "Something else"
_CLARIFICATION_FALLBACK_LONG = "unstructured text"

# Choice seeds for paste-provenance clarifications. Pasted text in an incident
# thread is overwhelmingly command output or logs (product guidance: command
# output, regardless of content, is usually pasted) — and a paste that reached
# clarification has, by definition, weak content signals, so the provenance
# prior outranks the classifier's sub-threshold guesses. Seeds go first; the
# classifier's own suggestions fill the remaining slots.
_PASTE_CLARIFICATION_SEEDS = ["command_output", "logs_and_errors"]


def _clarification_subject(target: "_PreprocessedAttachment") -> str:
    """How the clarification copy names the thing being classified.

    A minted name ("pasted-content-…", "page-capture-…") refers to the
    transport, not anything the user recognizes — call it what they did.
    Real files keep their filename.

    The capture arm is not hypothetical: classification_failed is decided
    BEFORE the page_capture passthrough in ``classify_and_extract``, so a
    capture the classifier is unsure about lands here, and without this it
    read ``the file you shared ("page-capture-20260709T105531.txt")`` —
    #666 on the Copilot's own channel, with no LLM in the loop (#1198
    review).
    """
    phrase = target.uploaded_file.submission_phrase
    if phrase is not None:
        return phrase
    filename = target.attachment_filename or "the uploaded file"
    return f'the file you shared ("{filename}")'


def _upload_subject(uf) -> str:
    """How agent copy names an ``UploadedFile`` back to the user.

    Same rule as ``_clarification_subject``, reached from the other side:
    that one starts from a preprocessing result, this one from the stored
    file row. Sentence register, so no turn number — see
    ``UploadedFile.submission_phrase``.
    """
    if uf is None or not getattr(uf, "filename", None):
        return "the uploaded file"
    return uf.submission_phrase or f'"{uf.filename}"'


_LABEL_QUALIFIER_MAX_CHARS = 48


def _sanitize_label_fragment(text: str) -> str:
    """Flatten caller-supplied text to one bounded, single-line fragment.

    A choice ``label`` is not display-only. It is persisted in
    ``last_suggestions`` and rendered verbatim into
    ``IntentResolver._build_prompt`` as a line in a NUMBERED CHOICE LIST —
    the prompt whose answer selects which offered intent fires. A filename
    carrying a newline therefore injects new lines into that list: a forged
    ``7. Yes, close the case`` reshapes the menu the classifier picks from,
    and because the same list also carries the engine's own follow-up
    intents (confirmation, status transitions), the steer is not confined
    to reclassification.

    Escaping is the wrong tool and #1216's lesson says why: nothing on this
    path DECODES, so ``&#10;`` would reach the model as those five literal
    characters and the label would read as garbage while still not being a
    newline the model treats as structure. The fix is at mint time — strip
    the characters that carry structure, collapse the whitespace, and bound
    the length — which is also what keeps the label a button.

    Every non-printable character becomes a space rather than being
    deleted — that covers newline, CR, tab and the rest of C0/C1, and also
    the format category, so a bidi override cannot reorder the label
    either. Deleting them instead would glue the surrounding tokens
    together and make a mangled name harder to recognise, not safer. Runs
    of whitespace then collapse to one space, and truncation is marked with
    an ellipsis so a clipped name does not read as the whole name.
    """
    flattened = "".join(ch if ch.isprintable() else " " for ch in text)
    collapsed = " ".join(flattened.split())
    if len(collapsed) > _LABEL_QUALIFIER_MAX_CHARS:
        return collapsed[: _LABEL_QUALIFIER_MAX_CHARS - 1].rstrip() + "…"
    return collapsed


def _clarification_label_qualifier(target: "_PreprocessedAttachment") -> str:
    """Short name telling one failed attachment's choices from another's.

    The *button* register — third and shortest of the three ways this
    codebase names an attachment back to the user, beside
    ``UploadedFile.display_name`` (citable, carries a turn number) and
    ``UploadedFile.submission_phrase`` (sentence, carries a clause). A button
    has room for neither, so this is the bare noun. Derived from the same
    provenance properties as its two siblings; keep the wording in step.

    The paste and capture arms are our own fixed wording. The third is the
    user's filename, and it goes through ``_sanitize_label_fragment``
    because a label reaches the intent resolver's prompt — see there. A
    name that sanitises away to nothing falls back to the generic phrase
    rather than an empty parenthetical.

    Not unique on its own, and no longer asked to be. Two pastes are both
    "pasted text" and two uploads of one filename are both that filename;
    nothing available here separates them (``submission_phrase`` is our own
    fixed wording, the minted ``pasted-content-<ts>.txt`` is a transport name
    the user never saw, and ``uploaded_at_turn`` is not unique per turn —
    #1264). Round one of #1245 appended the turn number here and rested the
    wrong-file guarantee on it; that guarantee now lives in
    ``_admit_clarification_entries``, which simply does not offer two
    attachments a typed answer could not tell apart. This is left to do the
    job a button can actually do: help the reader see which attachment a card
    is about.
    """
    uf = target.uploaded_file
    if uf.is_page_capture:
        base = "captured page"
    elif uf.is_pasted:
        base = "pasted text"
    else:
        raw = target.attachment_filename or uf.filename or ""
        base = _sanitize_label_fragment(raw) or "the uploaded file"
    return base


def _reclassification_intent(file_id: str, dt_value: str) -> Dict[str, Any]:
    """The engine-owned intent a clarification choice carries."""
    return {
        "type": IntentType.FILE_RECLASSIFICATION.value,
        "file_id": file_id,
        "data_type": dt_value,
    }


def _clarification_suggestions_for_failed(
    failed: List["_PreprocessedAttachment"],
) -> List[SuggestedActionResponse]:
    """Emit DECIDE suggestions for EVERY attachment that hit
    classification_failed.

    Takes the ALREADY-FILTERED list, so this and the narration note cannot
    disagree about which attachments failed — see
    ``_build_classification_clarification``, the one place the filter runs.

    One set of choices per failed attachment: up to 3 type-specific plus a
    "Something else" fallback each, and at least the fallback for any
    attachment that failed.

    **Every** failure, not just the first. The emitter used to clarify
    ``failed[0]`` and justify it from the per-turn file limit — "the limit is
    1, so we expect at most one classification_failed result". The premise
    was false: the limit is on ``files`` ALONE (``maxItems: 1``), and
    ``pasted_content`` is a separate form field that legitimately rides
    alongside a file as a second attachment. The paste/capture arm reaches
    ``classification_failed`` on its own — see ``_clarification_subject`` —
    so a turn where both fell below threshold left the second attachment
    with no choices, no ``file_reclassification`` intent and no recovery
    path, silently misclassified (#1222). Do not reintroduce a
    count-bounded shortcut here: the recovery path has to exist for whatever
    the route lets through, not for what one field's cap implies.

    Choice sources, per attachment: for a chosen file, the classifier's
    ``suggested_types``; for pasted text, the war-room seeds (command output
    / logs) come first — see ``_PASTE_CLARIFICATION_SEEDS`` — then the
    classifier's suggestions fill the remaining slots. Dedup and the
    three-choice budget are per attachment, so one attachment's choices
    never consume another's. The copy names the subject the way the user
    knows it (``_clarification_subject``).

    Every label carries the attachment's short name
    (``_clarification_label_qualifier``) — "Documentation (mystery.txt)",
    "Documentation (pasted text)" — because two cards both reading
    "Documentation" are indistinguishable on screen and in the resolver's
    numbered choice list.

    UNCONDITIONALLY, which is a change from the "only when more than one
    attachment failed this turn" rule #1236 shipped and #1245 round one
    widened to "more than one is on offer". Both were premised on the label
    being needed only to separate cards from EACH OTHER. The real hazard is
    the bare label as a standing generic: a question now outlives its turn,
    so "Documentation" minted on a lone turn 1 stays matchable on turn 5,
    and a user typing that shorthand while looking at turn 5's qualified
    cards resolved onto turn 1's file — oldest-wins, against every other
    ordering rule in this seam. A label that always names its subject has no
    generic form to be captured by.

    Qualifiers are NOT relied on to be unique — see
    ``_clarification_label_qualifier`` and ``_admit_clarification_entries``.

    A turn mints at most ONE synthetic
    name (#1198): ``pasted_content`` is a single form field, so a turn
    carries one paste or one capture, never two, and everything else is a
    user-chosen filename. Note this is a property of the *names*, not a
    count: two attachments could only share a qualifier by sharing a
    filename, and then ``payload`` collides too — such a pair is
    indistinguishable in any wording, so nothing is lost by not guarding it.

    Each suggestion carries an engine-owned ``file_reclassification`` intent
    (file_id + target DataType) so any client that forwards suggestion intent
    on click — the cross-client contract — resolves the choice through the
    structured reclassification handler, never as a free-text turn the LLM
    might act on literally (e.g. by deep-analyzing the file instead of
    re-labeling it). The ``payload`` remains the human-readable record of the
    choice; intent routing takes precedence over it server-side.

    Returns an empty list when no classification failure occurred this turn.
    """
    if not failed:
        return []

    suggestions: List[SuggestedActionResponse] = []

    for target in failed:
        subject = _clarification_subject(target)
        file_id = target.uploaded_file.file_id
        suffix = f" ({_clarification_label_qualifier(target)})"
        candidates = list(target.suggested_types or [])
        if _is_paste_upload(target):
            candidates = _PASTE_CLARIFICATION_SEEDS + candidates

        seen: set = set()
        emitted = 0

        for dt_value in candidates:
            if dt_value in seen:
                continue
            seen.add(dt_value)
            # unstructured_text collapses into the fallback — don't surface twice
            if dt_value == "unstructured_text":
                continue
            friendly = _CLARIFICATION_FRIENDLY_NAMES.get(dt_value)
            if friendly is None:
                continue
            suggestions.append(
                SuggestedActionResponse(
                    label=f'{friendly["label"]}{suffix}',
                    type="DECIDE",
                    payload=f'Treat {subject} as {friendly["long"]}.',
                    body=f'Treat as {friendly["long"]}.',
                    intent=_reclassification_intent(file_id, dt_value),
                )
            )
            emitted += 1
            if emitted >= 3:
                break

        # Always include the "Something else" fallback — last position for
        # this attachment.
        suggestions.append(
            SuggestedActionResponse(
                label=f"{_CLARIFICATION_FALLBACK_LABEL}{suffix}",
                type="DECIDE",
                payload=f"Treat {subject} as {_CLARIFICATION_FALLBACK_LONG}.",
                body=f"Treat as {_CLARIFICATION_FALLBACK_LONG}.",
                intent=_reclassification_intent(file_id, "unstructured_text"),
            )
        )

    return suggestions


def _clarification_note_for_failed(
    failed: List["_PreprocessedAttachment"],
) -> Optional[str]:
    """The narration bridge for this turn's clarification choices, or None.

    Takes the ALREADY-FILTERED list for the reason given on
    ``_clarification_suggestions_for_failed``: the note names exactly the
    attachments the choices target, by construction rather than by prose.

    The clarification suggestions are engine-emitted, so the LLM's own
    response usually says nothing about the content it couldn't classify —
    without this note the choices ("Treat as documentation.") read as
    disconnected nonsense under an unrelated investigation reply. Appended
    deterministically so every client gets the same context, phrased in the
    user's terms (a paste is "the text you pasted", never its synthetic
    snippet name).

    Names **every** failed attachment, for the same reason the emitter
    clarifies every one: the note used to pick the first via ``next(...)``,
    so a paste+file turn where both failed offered choices for two things
    while naming one (#1222). Wording is unchanged for the single-failure
    case, which is every turn carrying only one attachment.
    """
    subjects = [_clarification_subject(r) for r in failed]
    if not subjects:
        return None
    if len(subjects) == 1:
        named, pronoun = subjects[0], "it"
    else:
        named = f"{', '.join(subjects[:-1])} or {subjects[-1]}"
        pronoun = "them"
    return (
        f"\n\nOne more thing — I couldn't confidently classify "
        f"{named}, so I haven't analyzed {pronoun} yet. "
        f"How should I treat {pronoun}?"
    )


def _build_classification_clarification(
    preprocess_results: List["_PreprocessedAttachment"],
) -> Tuple[List[SuggestedActionResponse], Optional[str]]:
    """This turn's clarification choices and the note that introduces them.

    The ONE place ``classification_failed`` is filtered. Both halves are
    built from the same list object, so the note cannot name a different
    set of attachments than the choices target — an invariant that was
    prose (two call sites each re-deriving the filter) until it was made
    structural here. Nothing else should re-derive it.

    Takes nothing but this turn's results. Round one of #1245 threaded the
    carried attachments in here so the qualifier could be decided over the
    whole on-offer span; that coupling is gone with the qualifier's
    correctness role — the emitter describes THIS turn, and whether two
    attachments can be told apart is settled later, on the set that is
    actually stored.
    """
    failed = [r for r in preprocess_results if r.classification_failed]
    return (
        _clarification_suggestions_for_failed(failed),
        _clarification_note_for_failed(failed),
    )


# ============================================================
# Clarification set assembly (#1245, fm#918)
# ============================================================
#
# The liveness RULE — which stored entries may still answer a typed message —
# lives beside its consumer in ``core/investigation/suggestion_liveness.py``.
# What lives here is the ASSEMBLY: which of this turn's choices and which
# still-open earlier ones end up in the set that rule is applied to.
#
# Round one of #1245 tried to keep that set unambiguous by MINTING UNIQUE
# TEXT — a qualifier appended to each label, decided from how many
# attachments failed this turn and discriminated by the turn number. Review
# found three independent ways that guarantee fails, and they are worth
# stating because each is a trap the next person will re-lay:
#
#   - It covered ``label`` but not ``payload``, and ``_exact_match`` tests
#     PAYLOAD FIRST. Two pastes produce byte-identical payloads
#     ("Treat the text you pasted as documentation."), so the older card's
#     own wording resolved onto the newer file.
#   - It discriminated by ``uploaded_at_turn``, which is not unique per turn:
#     the persisted counter stands still across a SERVICE-dispatched turn
#     (#1264), so two attachments can carry the same number.
#   - It decided qualification BEFORE the span cap ran, so the set the
#     uniqueness claim was argued over is not the set that gets stored.
#
# So uniqueness is no longer a property of the wording. It is a property of
# ADMISSION: an attachment joins the on-offer set only if none of its
# matchable strings already belongs to an admitted one
# (``_admit_clarification_entries``), decided on the final set, using the
# matcher's own normalisation, with no clock involved. The qualifier is now
# unconditional and exists for the READER's benefit — telling two cards apart
# on screen — not to carry a correctness guarantee it cannot keep.
#
# ``IntentResolver`` holds the third line: a typed string that would resolve
# two ways resolves to neither. Admission should make that unreachable for
# clarifications; the guard is what makes it true regardless.


def _carry_forward_unresolved_clarifications(
    previous_suggestions: Optional[List[Dict[str, Any]]],
    case: "Case",
    resolved_file_id: Optional[str],
    *,
    as_of_turn: int,
) -> List[Dict[str, Any]]:
    """Clarification choices for attachments this turn left open.

    ``last_suggestions`` is rebuilt from scratch every turn, so before #1222
    the whole list collapsed the moment a turn produced no clarification of
    its own. With one failed attachment that cost nothing — the only pending
    question had just been answered. Once the emitter clarifies EVERY failure,
    answering one question deleted the others: the paste's four choices
    vanished from server-side memory the moment the user resolved the file.
    #1222 fixed that for the turn that RESOLVES an attachment.

    It stayed broken for the turn that IGNORES the question (#1245) — the far
    commoner shape, and identical for one attachment or two. The previous
    scoping (``resolved_file_id is None`` → carry nothing) was justified as
    self-limiting, "the carried set only ever shrinks, one file per
    reclassification". That was false: the turns route accepts an intent
    ALONGSIDE ``files`` and ``pasted_content``, so a reclassification turn that
    also uploads two failing attachments carries one file out and mints two in,
    growing the span by one every turn without limit.

    So the scoping is gone in both directions: the carry runs on every turn,
    and what bounds it is the liveness rule plus ``_admit_clarification_entries``
    (applied by the caller, over the whole assembled set). An answered question
    is still dropped — ``resolved_file_id`` names it exactly, and the referent
    check in ``suggestion_is_live`` catches the same thing arriving from
    anywhere else.

    Only clarification choices are carried. An engine follow-up was about the
    turn that produced it and does not outlive it; that is the
    ``FOLLOW_UP_CARRY_TURNS`` window, enforced by the shared liveness rule
    rather than by this filter, so both sites agree.
    """
    return [
        entry
        for entry in live_suggestions(previous_suggestions, case, as_of_turn=as_of_turn)
        if is_clarification_entry(entry) and entry_file_id(entry) != resolved_file_id
    ]


def _admit_clarification_entries(
    entries: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """The clarification entries that may be on offer together.

    Two rules, both applied per ATTACHMENT and both newest-first:

    **Unambiguous.** An attachment is admitted only if none of its matchable
    strings (``entry_match_keys`` — payload and label, folded the way the
    matcher folds them) already belongs to an admitted attachment. This is
    where the wrong-file guarantee actually lives. Two pastes read "the text
    you pasted" in every payload and no wording available to us separates
    them: ``submission_phrase`` is ours by design, the filename is a minted
    transport name the user never saw, and the turn number is not unique
    (#1264). Rather than offer two indistinguishable menus and let the matcher
    pick, the older one is not offered at all — which is also where it was
    before #1245, so nothing regresses.

    **Bounded.** At most ``CLARIFICATION_SPAN_CAP`` attachments, so the
    resolver's choice list cannot grow with user behaviour.

    Order is list position, newest first, and NOT ``offered_turn``. Sorting by
    the stamp looks more robust and is strictly worse: two turns can share a
    stamp (#1264), so it supplies no ordering exactly when a tie-break matters,
    while position is well-defined always. The list arrives newest-first by
    construction — this turn's choices are prepended, carried ones keep their
    relative order — and ``_stored_suggestions`` is the single place that
    builds it.

    Whole attachments are admitted or dropped, never split. Keeping two of an
    attachment's four choices leaves a menu that looks complete and silently no
    longer offers "documentation" — a wrong answer is worse than a missing
    question.
    """
    admitted: Dict[str, None] = {}
    claimed: Set[str] = set()
    # Refusal has to be remembered, not re-decided per entry. An attachment
    # turned away on its first choice would otherwise be let in on a later one
    # whose wording happens not to collide — readmitting it with a menu missing
    # exactly the options that clashed, which is the partial menu this refuses
    # to build. Pinned by
    # ``test_a_collision_refuses_the_whole_attachment_not_one_choice``.
    rejected: Set[str] = set()

    for entry in entries:
        file_id = entry_file_id(entry)
        if file_id is None or file_id in rejected:
            continue
        if file_id in admitted:
            # Another choice for an attachment already in: claim its wording
            # too, so a later attachment colliding with THIS card is refused
            # as surely as one colliding with the card that got it admitted.
            claimed |= entry_match_keys(entry)
            continue
        if len(admitted) >= CLARIFICATION_SPAN_CAP:
            # Full. Nothing further can be admitted, and ``claimed`` exists
            # only to refuse admissions, so the rest of the list is work with
            # no consequence — the final filter drops it either way.
            break
        keys = entry_match_keys(entry)
        if keys & claimed:
            # An already-admitted attachment answers to this wording; a second
            # one behind the same strings could only be reached by guessing.
            rejected.add(file_id)
            continue
        admitted[file_id] = None
        claimed |= keys

    return [e for e in entries if entry_file_id(e) in admitted]


def _stored_suggestions(
    *,
    case: "Case",
    clarification: List[SuggestedActionResponse],
    carried: List[Dict[str, Any]],
    follow_ups: List[Dict[str, Any]],
    offered_turn: int,
    as_of_turn: int,
) -> List[Dict[str, Any]]:
    """The ``last_suggestions`` value to persist at the end of a turn.

    Order is load-bearing twice over: ``IntentResolver._exact_match`` returns
    the FIRST match, and ``_admit_clarification_entries`` reads position as its
    ordering. Newest first — this turn's choices, then the carried ones
    oldest-last, then the engine's follow-ups.

    Follow-ups are stamped too. They are not carried (their window is one
    turn), and the stamp is what expires them when an ORDINARY turn does not
    rewrite the list. It is NOT what covers fm#918's mid-turn-save exposure —
    the two saves that commit a row mid-turn run before the turn is recorded,
    so the stamp ages to 1 and stays in window; what covers that is the
    terminal guard in ``suggestion_is_live``. See ``FOLLOW_UP_CARRY_TURNS``.

    Everything assembled is then put through the liveness rule at
    ``as_of_turn``, the number the NEXT read will use. Filtering the fresh
    entries too is not belt-and-braces: a turn can end TERMINAL while carrying
    a ``classification_failed`` attachment, and the guard on
    ``suggestion_is_live`` already says a clarification is dead on a closed
    case (the handler answers 422). Storing them anyway made this function's
    own contract — what is stored is what the next read accepts — false for
    exactly the set where it mattered.
    """
    types = file_data_types(case)
    fresh = [
        {
            "label": s.label,
            "action_type": s.type,
            "payload": s.payload,
            "body": s.body,
            "intent": s.intent,
            OFFERED_TURN_KEY: offered_turn,
            OFFERED_DATA_TYPE_KEY: types.get((s.intent or {}).get("file_id")),
        }
        for s in clarification
    ]
    # Files this turn built fresh choices for win: the same attachment must
    # never appear twice, and the surviving wording has to be the one the user
    # was just shown.
    fresh_ids = {(s.intent or {}).get("file_id") for s in clarification}
    assembled = _admit_clarification_entries(
        fresh + [e for e in carried if entry_file_id(e) not in fresh_ids]
    ) + [{**f, OFFERED_TURN_KEY: offered_turn} for f in follow_ups if f.get("intent")]
    return live_suggestions(assembled, case, as_of_turn=as_of_turn)
