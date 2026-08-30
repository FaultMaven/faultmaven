"""Which stored suggestions may still answer a typed message.

``case.last_suggestions`` is SERVER-SIDE memory, not a render. The cards a
client draws come from ``clarification + suggested_follow_ups`` in the
TurnResponse; this list exists so a user who TYPES an answer instead of
clicking lands on the same intent a click would have carried. Everything here
is about one question: which stored entries may ``IntentResolver`` still match
a typed message against?

Two facts make that question non-trivial:

1. An offer has to outlive its turn (#1245). A user who ignores a clarification
   and says something else, then comes back to it, must still be able to
   answer — so the set cannot simply be "last turn's output". But an offer that
   never expires accumulates, and the resolver picks among the stored choices,
   so an unbounded set is an unbounded chance of resolving an answer onto the
   wrong file.
2. The list is rewritten ONLY on ``process_turn``'s success path (fm#918). A
   mid-turn engine save that is never followed by the final one commits turn
   N's state beside turn N-1's suggestions; the standalone close/transition
   endpoints move the case without touching the list at all. So "it is in the
   row" is not evidence that a turn put it there *for now*.

One mechanism answers both: every stored entry carries the turn that OFFERED
it, and liveness is an age bound on that stamp. Fact 1 is a wide window with a
hard span cap; fact 2 is the same predicate applied where no turn wrote — an
entry left behind by a non-turn writer ages out on the clock rather than
needing every writer to remember to clear it.

WHICH clock, precisely. The stamp is the in-flight ``case.current_turn``, and
the number a LATER turn compares it against is the one that survives a save —
``effective_current_turn``, the last ``turn_history`` entry.

**Since #1264 those are equal on every route.** ``turn_history`` used to be
appended only by the engine, so a SERVICE-dispatched turn (a clarification
click, a greeting) or a terminal short-circuit appended nothing and the
persisted counter stood still across it. It no longer does:
``investigation_service._backfill_consumed_turn`` records a turn for every route
that consumes a number, so the two clocks advance together.

What that changed here, and it is a real behaviour change rather than a
tidy-up: **the window is now measured in turns of any kind, not in turns that
reached the engine.** A run of clarification clicks DOES age the remaining
questions — where before it did not, and this module argued that not ageing
them was the behaviour you want because the user is visibly working through the
menu. That argument is still worth making; it is simply no longer made by the
turn clock. If the old reach is wanted, raise ``CLARIFICATION_CARRY_TURNS``,
which now means what it says. Pinned by
``tests/unit/modules/agent/test_turn_counter_advances_1264.py::
TestAClarificationClickCostsAWindowTurn``.

Still load-bearing, and unaffected: two different turns could historically mint
the same ``offered_turn``, so it is not an identity and must never be used as a
discriminator, in copy or in ordering. See
``investigation_service._admit_clarification_entries``. Legacy cases persisted
before #1264 still contain such collisions.
"""

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

from faultmaven.models.api_models import IntentType

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case

__all__ = [
    "CLARIFICATION_CARRY_TURNS",
    "CLARIFICATION_SPAN_CAP",
    "FOLLOW_UP_CARRY_TURNS",
    "OFFERED_DATA_TYPE_KEY",
    "OFFERED_TURN_KEY",
    "entry_file_id",
    "entry_match_keys",
    "entry_offered_turn",
    "file_data_types",
    "is_clarification_entry",
    "live_suggestions",
    "normalize_choice_text",
    "suggestion_is_live",
]

#: Turn number the entry was minted on. Rides inside the entry dict, which both
#: repositories persist as opaque JSON (``to_json_compatible`` on the way out,
#: ``metadata.get("last_suggestions")`` on the way back), so this costs no
#: migration and no repository change. fm#918 proposed wrapping the whole list
#: (``{"turn": N, "suggestions": [...]}``); a per-entry key is what the
#: carry-forward actually needs, because a stored set is heterogeneous the
#: moment anything is carried — this turn's choices and a two-turn-old one sit
#: in the same list and cannot share one stamp.
OFFERED_TURN_KEY = "offered_turn"

#: The target file's ``data_type`` as it stood when the question was asked. A
#: clarification is about a file that has not been classified; the ONLY writer
#: of ``UploadedFile.data_type`` after intake is
#: ``_file_row_with_reclassification`` (both reclassification paths), so a
#: changed value means the question was answered — by this turn's handler, by
#: ``PATCH /evidence/{id}/classification``, or by anything added later.
#: Comparing it is what keeps a widened window from also widening fm#918's
#: out-of-band exposure, and it needs no cooperation from the writer.
#:
#: Deliberately lossy in one direction and never the other. ``data_type`` holds
#: an ``EvidenceSourceType``, a 12→6 projection of ``DataType``, so a
#: reclassification WITHIN a source type (logs_and_errors → command_output,
#: both ``logs``) leaves it unchanged and the question stays live. That is a
#: missed drop, never a wrong one: the value cannot change except by
#: reclassification, so this can never retire a question the user has not
#: answered.
OFFERED_DATA_TYPE_KEY = "offered_data_type"

#: Turns after the offering turn that a clarification choice stays answerable
#: by typed text. Offered on turn T, answerable on T+1..T+3.
#:
#: Alternatives that are not bounds: "until the attachment is referenced again"
#: and "until the case leaves the stage" are CONDITIONS — neither terminates, so
#: an ignored question lives forever and the accumulation is exactly what the
#: wrong-file exposure is made of. A span cap alone is not a bound either: a
#: single stale question is never evicted by anything.
#:
#: Three rather than the ``_ASK_DECAY_AGE_TURNS = 2`` used to stop the engine's
#: mechanical re-offer of an unanswered evidence ask, because the costs differ.
#: That ask is RE-RENDERED every turn, so living longer means nagging; a carried
#: clarification is invisible (see the module note above), so living longer
#: costs only resolver exposure — which the span cap bounds independently.
CLARIFICATION_CARRY_TURNS = 3

#: Turns an engine follow-up (confirmation, status transition, hypothesis
#: action) stays answerable: exactly the next one, which is the window the
#: system has always had. A follow-up is about the turn that produced it —
#: "Yes, mark as resolved" means nothing once the proposal it belonged to is
#: gone — so it must NOT inherit the clarification window. This is also the half
#: that closes fm#918's mid-turn-save exposure: the engine appends
#: ``turn_history`` at its Step 6 and saves at Step 7, so a row committed by a
#: save whose final assignment never ran carries turn N in the persisted counter
#: and a stamp of N-1, which is out of window on the retry turn.
FOLLOW_UP_CARRY_TURNS = 1

#: Distinct attachments whose clarification choices may be on offer at once. A
#: HARD cap on the resolver's choice list, independent of user behaviour.
#:
#: Three is the smallest cap that leaves the carry-forward non-vacuous on every
#: turn shape the route permits: a turn carries at most one file plus one paste,
#: so it can mint choices for two attachments of its own, and a cap of two would
#: make a paste+file turn evict every older question by construction. Three is
#: "this turn's two, plus one question still open from before". The ceiling on
#: the choice list is therefore 3 attachments × 4 choices = 12 clarification
#: entries, against the 2 × 4 = 8 that #1236 assessed and accepted — and against
#: shipped behaviour before #1245, where the carried set had NO cap at all.
CLARIFICATION_SPAN_CAP = 3


def normalize_choice_text(text: Optional[str]) -> str:
    """Fold a stored string the way the matcher compares it.

    ONE definition, because two consumers must agree exactly or the system
    lies to itself: ``IntentResolver._exact_match`` decides what a typed
    message matches, and ``entry_match_keys`` decides which stored entries
    would answer to the same typing. If the two normalisations drift, the
    admission rule reports a set as unambiguous that the matcher can still
    resolve two ways — which is #1245's round-one defect in a new place.
    """
    return (text or "").lower().strip().rstrip(".!?")


def entry_match_keys(entry: Dict[str, Any]) -> Set[str]:
    """Every normalised string a typed message can match this entry by.

    ``payload`` and ``label``. A set rather than an ordered pair because the
    question asked of this is "could the same typing reach two different
    attachments?", which does not care which field answered.

    ``body`` is deliberately absent, and its absence is not an oversight. The
    matcher never reads it, and it is never shown alone: it appears only as
    the tail of ``f"{label} — {body}"`` in the classifier's numbered choice
    list, where the label half already separates the attachments. Including
    it would also be actively wrong here — ``body`` is derived from the data
    type alone ("Treat as documentation."), so EVERY pair of attachments
    collides on it, and a collision-aware admission rule keyed on it would
    admit exactly one attachment, ever.
    """
    return {
        key
        for key in (
            normalize_choice_text(entry.get("payload")),
            normalize_choice_text(entry.get("label")),
        )
        if key
    }


def is_clarification_entry(entry: Dict[str, Any]) -> bool:
    """Is this stored entry one of the clarification choices?"""
    intent = entry.get("intent") or {}
    return intent.get("type") == IntentType.FILE_RECLASSIFICATION.value


def entry_file_id(entry: Dict[str, Any]) -> Optional[str]:
    """The attachment a clarification entry targets, or None."""
    intent = entry.get("intent") or {}
    file_id = intent.get("file_id")
    return file_id if isinstance(file_id, str) and file_id else None


def entry_offered_turn(entry: Dict[str, Any]) -> Optional[int]:
    """The turn that offered this entry, or None when it carries no stamp.

    ``bool`` is excluded explicitly because it is an ``int`` subclass, so a
    ``True`` landing here would behave as turn 1 in the arithmetic below.
    """
    offered = entry.get(OFFERED_TURN_KEY)
    if isinstance(offered, bool) or not isinstance(offered, int):
        return None
    return offered


def suggestion_is_live(
    entry: Dict[str, Any],
    *,
    as_of_turn: int,
    file_data_types: Dict[str, Optional[str]],
    case_is_terminal: bool,
) -> bool:
    """May the resolver still match a typed message against this entry?

    ``as_of_turn`` is the turn doing the asking, on the PERSISTED clock: the
    reader passes the in-flight ``case.current_turn`` (which is the reloaded
    counter plus one), and the writer passes ``effective_current_turn + 1``,
    which is the same number the next read will compute. Since #1264 those two
    expressions agree on every route, because every consumed turn now records
    one — but the writer keeps deriving from ``effective_current_turn`` so the
    seam stays correct by construction rather than by the two happening to
    match, and so a route that ever stops recording again shows up as a bug in
    the clock rather than as silently dropped questions here.

    An entry with no stamp is NOT live. Every stamped entry was written by the
    turn seam; an unstamped one is either a row persisted before this rule
    existed or something a non-turn writer left behind, and those are the same
    epistemic position as fm#918's exposures — nothing here knows what turn it
    belongs to. The deploy-time cost is bounded and one-sided: a case with an
    in-flight offer loses TYPED matching for one turn (clicking still works — a
    click sends its intent on the request and never consults this list), and
    the next turn mints a stamped set. Reading an absent stamp as "current"
    instead would re-arm every pre-existing row permanently, since the paths
    that leave one behind are precisely the paths that never rewrite it.
    """
    if not isinstance(entry, dict) or not entry.get("intent"):
        return False

    offered = entry_offered_turn(entry)
    if offered is None:
        return False

    age = as_of_turn - offered
    if age < 0:
        # The clock ran backwards (a restored or reconciled turn counter).
        # Refuse rather than guess: an entry we cannot age is one we cannot
        # bound.
        return False

    is_clarification = is_clarification_entry(entry)
    window = CLARIFICATION_CARRY_TURNS if is_clarification else FOLLOW_UP_CARRY_TURNS
    if age > window:
        return False

    if not is_clarification:
        return True

    # A clarification click mutates files and evidence, so
    # ``_handle_file_reclassification`` refuses on a terminal case (422).
    # Minting the intent anyway would turn an ordinary typed message on a
    # closed case into an error response; drop the choice instead.
    if case_is_terminal:
        return False

    file_id = entry_file_id(entry)
    if file_id is None or file_id not in file_data_types:
        return False
    return file_data_types[file_id] == entry.get(OFFERED_DATA_TYPE_KEY)


def file_data_types(case: "Case") -> Dict[str, Optional[str]]:
    """``file_id`` → current ``data_type``, for the referent check."""
    return {uf.file_id: uf.data_type for uf in (case.uploaded_files or [])}


def live_suggestions(
    stored: Optional[List[Dict[str, Any]]],
    case: "Case",
    *,
    as_of_turn: int,
) -> List[Dict[str, Any]]:
    """The stored entries still answerable on ``as_of_turn``, in order.

    The single liveness rule, used by BOTH sides of the seam: the adoption
    site filters what the resolver may see, and the write site filters what is
    stored for next turn. One predicate, so a question cannot be alive in
    storage and dead to the reader (or the reverse) — provided both sides pass
    the same ``as_of_turn``, which is the whole of the note on
    ``suggestion_is_live``.
    """
    if not stored:
        return []
    types = file_data_types(case)
    case_is_terminal = bool(getattr(case, "is_terminal", False))
    return [
        entry
        for entry in stored
        if suggestion_is_live(
            entry,
            as_of_turn=as_of_turn,
            file_data_types=types,
            case_is_terminal=case_is_terminal,
        )
    ]
