"""A ``case_messages`` row: its markers, its flags, and its ONE constructor.

``case_messages`` requires non-blank content, and a message row is part of an
AGGREGATE save — so a blank one does not fail alone. It aborts the whole save,
taking the case row, its evidence, its hypotheses and its uploaded files with
it. Every writer used to have to know that independently, and three of them
had each been fixed separately, each inventing its own answer to "what does
blank mean here" (#1420, #1433, #1443). Nothing made a fourth inherit any of
it (#1452).

:func:`append_message_row` is now the only way a row reaches
``Case.messages``, and the blank-content decision is its ``kind`` argument
rather than something each caller re-derives. An architecture test
(``tests/unit/architecture/test_message_rows_have_one_constructor.py``) fails
on any other writer.

The row it builds is the shape the repositories hydrate — the eight
``case_messages`` columns a reload hands back — so an appended row and the same
row after a round trip are indistinguishable, and ``normalise_message_row`` has
nothing to complete on it.

Everything here is re-exported from :mod:`faultmaven.modules.case.contracts`,
which is where other modules import it from.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, Optional
from uuid import uuid4

if TYPE_CHECKING:
    from faultmaven.modules.case.domain.models import Case

logger = logging.getLogger(__name__)


# ============================================================
# Metadata keys
# ============================================================

#: Set on a user row whose content the SERVER wrote because the user sent no
#: message at all — a bare ``@FaultMaven`` (#1420). The row is real: the turn
#: is charged and it advances the message clock, and ``case_messages`` requires
#: non-blank content, so it can neither be omitted nor left empty.
#:
#: Anything that presents a user row as something the USER SAID must skip a row
#: carrying this. Quoting it back to the model reads as a reply the user never
#: made; counting it as title signal names the case after a placeholder
#: (#1434).
#:
#: It lives in the case module rather than beside a writer because it is part
#: of the shape of a ``case_messages`` row, which this module owns, and its
#: readers are in other modules.
MESSAGE_METADATA_USER_EMPTY = "user_message_empty"

#: Set on an ASSISTANT row whose content the SERVER wrote in place of an answer
#: the model did not usefully give (#1433, #1442, #1451). Two writers set it:
#: ``MilestoneEngine``, which synthesizes a placeholder keyed on the provider's
#: normalised stop reason (withheld by a safety filter, truncated, empty,
#: no signal), and :func:`append_message_row`'s ``AGENT_ANSWER`` backstop for a
#: raw empty answer that bypassed the engine.
#:
#: Shape-neutral on purpose: it was ``agent_response_empty`` until the engine's
#: placeholders began marking truncated and filtered turns too, which are not
#: empty. It carries no stop reason — the reason picks the placeholder TEXT,
#: and nothing reads it off the row.
#:
#: Anything that presents an assistant row as something the ASSISTANT SAID must
#: not quote a row carrying this: replayed to the model as its own prior words,
#: the placeholder reads as an answer it gave. See
#: :func:`is_server_written_assistant_row`.
MESSAGE_METADATA_AGENT_SYNTHESIZED = "agent_response_synthesized"


# ============================================================
# Markers
# ============================================================

#: Transcript text for a turn carrying no message at all — a bare
#: ``@FaultMaven``. The row is the user's half of a turn that really happened:
#: it is charged against the tenant cap and it advances the message clock, so
#: it is not something to omit. But ``case_messages`` requires non-blank
#: content, and writing ``""`` made the whole aggregate save fail its CHECK
#: constraint — taking the case row, its evidence and its hypotheses with it
#: (#1420).
#:
#: A marker rather than first-person prose, because the user said nothing and
#: the row must not pretend otherwise. What the turn WAS is already recorded
#: beside it, in the message metadata (``orientation: "empty"``).
EMPTY_TURN_TEXT = "(no message)"

#: Transcript text for an assistant row that arrived with no content (#1433).
#:
#: Deliberately NOT the same marker as ``EMPTY_TURN_TEXT``: an empty USER
#: message means the user chose to send nothing, while an empty ASSISTANT
#: message means the turn produced no answer, and a transcript a human reads to
#: understand an incident must not present the second as the first.
#:
#: Equally deliberately, it names NO CAUSE. The write site it guards is reached
#: after every dispatch kind converges, and two SERVICE intents — GREETING and
#: FILE_RECLASSIFICATION — answer with no LLM call at all, so text blaming "the
#: model" would, on those paths, blame something that never ran. (The other
#: SERVICE intents do delegate to ``engine.process_turn``; an earlier version
#: of this comment claimed otherwise, which is the same kind of unchecked
#: claim it replaced.) The engine, which holds the provider's stop reason,
#: names the cause in its own placeholder (#1442); this marker is only for a
#: raw ``""`` that never came through the engine's synthesis.
#:
#: It is stored rather than refused because the alternative is worse: the row
#: is part of an aggregate save, so a blank one aborts the whole thing and
#: takes the user's turn, the evidence and the hypotheses with it — for a turn
#: already charged against the tenant cap.
EMPTY_AGENT_RESPONSE_TEXT = "(this turn produced no answer)"


# ============================================================
# Readers' predicates
# ============================================================


def is_server_written_user_row(msg: dict) -> bool:
    """A USER row whose content the server wrote (#1420, #1434).

    Lives beside the key it reads because it has call sites across two modules
    (``context_builder``, ``case_service``) and the rule was implemented twice
    before this — once in each module — and the two copies had already
    diverged on the one thing that makes it safe: the role check.

    The role check is not cosmetic. An assistant row's ``metadata`` IS the
    engine's own per-turn metadata dict, which many handlers write into; a
    predicate that ignored ``role`` would silently delete the ASSISTANT's
    answer from the prompt the day anything stamped this key there.
    """
    if msg.get("role") != "user":
        return False
    return bool((msg.get("metadata") or {}).get(MESSAGE_METADATA_USER_EMPTY))


def is_server_written_assistant_row(msg: dict) -> bool:
    """An ASSISTANT row whose content the server wrote (#1451).

    The mirror of :func:`is_server_written_user_row`, and role-checked for the
    same reason in the other direction: the rule describes one kind of row, and
    a role-blind predicate would let a stray key on a USER row replace what the
    user actually typed with a line saying the assistant did not answer.

    A row this returns True for is not skipped by the prompt renderers — they
    render a neutral marker line in its place, because a turn the model failed
    to answer is information the next turn needs. What they must never do is
    quote it as ``ASSISTANT: <text>``.
    """
    if msg.get("role") != "assistant":
        return False
    return bool((msg.get("metadata") or {}).get(MESSAGE_METADATA_AGENT_SYNTHESIZED))


# ============================================================
# The constructor
# ============================================================


class MessageRowKind(str, Enum):
    """What a row IS, which decides its role and what blank content means.

    A blank row cannot be written, so each kind has to answer "what then?" —
    and the answers differ because the rows mean different things:

    ============== ========= ===================================================
    kind           role      blank content
    ============== ========= ===================================================
    USER_TURN      user      RECORDED as :data:`EMPTY_TURN_TEXT`, flagged
                             :data:`MESSAGE_METADATA_USER_EMPTY`. The turn
                             happened and was charged; the user said nothing.
    AGENT_ANSWER   assistant RECORDED as :data:`EMPTY_AGENT_RESPONSE_TEXT`,
                             flagged :data:`MESSAGE_METADATA_AGENT_SYNTHESIZED`.
                             A turn that produced no answer is a failure worth
                             recording, not a quiet one.
    INITIAL_MESSAGE user     ABSENT — no row. Nothing was said, and no turn was
                             charged for saying it.
    SYSTEM_NOTICE  system    ABSENT — no row. A notice with nothing to say is
                             not a notice; the server wrote the text, so blank
                             is a bug, and it is logged.
    ============== ========= ===================================================

    A new writer picks the kind that describes its row. A row that fits none
    of these is a new kind, with its own answer, added here — not a dict built
    at the call site.
    """

    USER_TURN = "user_turn"
    AGENT_ANSWER = "agent_answer"
    INITIAL_MESSAGE = "initial_message"
    SYSTEM_NOTICE = "system_notice"


_ROLE_OF: Dict[MessageRowKind, str] = {
    MessageRowKind.USER_TURN: "user",
    MessageRowKind.AGENT_ANSWER: "assistant",
    MessageRowKind.INITIAL_MESSAGE: "user",
    MessageRowKind.SYSTEM_NOTICE: "system",
}

#: ``msg_<uuid4 hex[:12]>``. ``CaseRepository._MESSAGE_ID_HEX`` mints the same
#: shape for a row that reaches it without one, so the two are
#: indistinguishable.
_MESSAGE_ID_HEX = 12


def append_message_row(
    case: "Case",
    kind: MessageRowKind,
    content: Optional[str],
    *,
    turn_number: int,
    author_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Build a ``case_messages`` row and append it to ``case.messages``.

    The only way a row reaches ``Case.messages``. Returns the row it appended —
    the SAME dict, so a caller may tag it afterwards (the orientation and
    out-of-band paths do) — or ``None`` when ``kind`` says a blank row is
    absent (see :class:`MessageRowKind`).

    Blank means blank after ``strip()``, not falsy: ``"   "`` is a TRUE Python
    value but SQL ``TRIM`` reduces it to nothing and the CHECK constraint
    refuses it (#1420), and ``"\\t"`` passes the constraint — one-argument
    ``TRIM`` strips only spaces — and persists as a blank-looking row. Both are
    blank here.

    ``metadata`` is used BY REFERENCE, and a flag the kind sets is written into
    it in place. That is load-bearing for ``AGENT_ANSWER``: the service passes
    the engine's per-turn metadata dict, which other readers of the turn share
    (#1270), and a copy would sever them.

    What it fills: ``message_id`` (minted), ``created_at`` (now — the only
    layer that witnesses the creation; the repository refuses to guess it at
    save, #1418/#1429) and ``token_count`` (``None``). What it refuses:
    a ``turn_number`` of ``None``, named here rather than at the save it would
    otherwise abort.

    It does not touch ``message_count``: callers maintain it, and two of them
    count differently (``+= 1`` on the turn path, ``len(messages)`` elsewhere),
    which is outside what a row is.
    """
    kind = MessageRowKind(kind)
    if turn_number is None:
        raise ValueError(
            f"{kind.value} row on case {getattr(case, 'case_id', '?')}: no "
            "turn_number. A case_messages row cannot be written without one, "
            "and the repository will not invent it."
        )
    if metadata is None:
        metadata = {}
    blank = not str(content or "").strip()

    if kind is MessageRowKind.USER_TURN:
        # Always written, True or False: a consumer tells the marker from real
        # content by it, and the orientation path is not the only one that can
        # reach the marker (the pending-transition path does not tag
        # ``out_of_band``).
        metadata[MESSAGE_METADATA_USER_EMPTY] = blank
        if blank:
            content = EMPTY_TURN_TEXT
    elif kind is MessageRowKind.AGENT_ANSWER:
        if blank:
            logger.warning(
                "Empty agent_response on case %s turn %s; recording the turn "
                "as answerless rather than aborting the save",
                getattr(case, "case_id", None),
                turn_number,
            )
            content = EMPTY_AGENT_RESPONSE_TEXT
            # Set, never cleared: the engine may already have flagged a
            # placeholder of its own, and an answered turn leaves the key
            # ABSENT rather than False.
            metadata[MESSAGE_METADATA_AGENT_SYNTHESIZED] = True
    elif kind is MessageRowKind.INITIAL_MESSAGE:
        if blank:
            return None
        content = str(content).strip()
    elif kind is MessageRowKind.SYSTEM_NOTICE:
        if blank:
            logger.warning(
                "Blank system notice on case %s turn %s (source %r); writing no row",
                getattr(case, "case_id", None),
                turn_number,
                metadata.get("source"),
            )
            return None
    else:  # pragma: no cover - a kind added to the enum without its answer
        raise ValueError(f"no blank-content policy for message row kind {kind!r}")

    row: Dict[str, Any] = {
        "message_id": f"msg_{uuid4().hex[:_MESSAGE_ID_HEX]}",
        "turn_number": turn_number,
        "role": _ROLE_OF[kind],
        "content": content,
        # The repository's canonical spelling, so the aggregate save's
        # normalisation is a no-op on this row.
        "created_at": datetime.now(timezone.utc).isoformat(),
        "author_id": author_id,
        "token_count": None,
        "metadata": metadata,
    }
    case.messages.append(row)
    return row
