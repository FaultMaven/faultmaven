"""Finding system output the user typed into a chat message, and offering it back (#1930).

What the user writes in a chat message is a claim; only data they submit is
evidence. Users still paste output into the message box. The model names each
such block by its first and last line (``BaseInteractionResponse.
chat_data_blocks``); this module finds those lines in the user's OWN message and
records one ``ChatDataOffer`` holding the blocks as the user wrote them. The user
then declares what it is: save it as data, or re-submit a corrected copy. An
offer the user ignores leaves the block a claim.

A message that reports the state of the user's system with no data showing it
("it's stable", "it's working now") gets a request for the output that would
show it (``StateReport``), for verification and for the record.

The model never supplies the text that is saved. It only points: a line it
copied wrong, or a line that is not in the message, finds nothing, and that
block is dropped. Dropping is the safe direction — the block stays a claim, as
it would have without the offer.
"""

import logging
from typing import Callable, List, Optional, Sequence, Tuple

from faultmaven.infrastructure.observability.evidence_metrics import (
    CHAT_DATA_OFFER_EVENTS_TOTAL,
)
from faultmaven.modules.case.contracts import (
    MAX_CHAT_DATA_BLOCK_CHARS,
    MAX_CHAT_DATA_BLOCKS,
    Case,
    ChatDataOffer,
    block_key,
)

logger = logging.getLogger(__name__)

#: The engine result's metadata key naming the offer a turn made, if any.
TURN_METADATA_CHAT_DATA_OFFER = "chat_data_offer_id"


def _comparable(line: str) -> str:
    """A line as an anchor is compared: surrounding whitespace ignored.

    A model copying a line drops indentation and trailing spaces far more often
    than it alters a character, and neither carries meaning for finding the
    line. The block itself keeps the message's own whitespace.
    """
    return line.strip()


def locate_blocks(
    message: str, anchors: Sequence[Tuple[str, str]]
) -> Tuple[List[str], int]:
    """The blocks the anchors name in ``message``, and how many found nothing.

    Each anchor is ``(first_line, last_line)``, matched against WHOLE lines of
    the message. The last line is the first match at or after the first
    match of the first line (searching from the end of the previous block);
    the block then starts at the LAST match of the first line before it. So
    the block is the shortest the anchors name: a first line that also occurs
    earlier as the user's prose ("error", then the output that starts with
    "error") does not pull that prose in. The block is every line from one to
    the other, as the message has them. Blocks never overlap, and an anchor
    that matches nothing, or names an empty line, is counted as unmatched.
    """
    lines = message.splitlines()
    comparable = [_comparable(line) for line in lines]
    blocks: List[str] = []
    unmatched = 0
    cursor = 0
    for first, last in anchors:
        first_key, last_key = _comparable(first or ""), _comparable(last or "")
        if not first_key or not last_key:
            unmatched += 1
            continue
        start = next(
            (i for i in range(cursor, len(lines)) if comparable[i] == first_key), None
        )
        end = (
            next(
                (j for j in range(start, len(lines)) if comparable[j] == last_key),
                None,
            )
            if start is not None
            else None
        )
        if start is None or end is None:
            unmatched += 1
            continue
        start = max(i for i in range(start, end + 1) if comparable[i] == first_key)
        blocks.append("\n".join(lines[start : end + 1]))
        cursor = end + 1
    return blocks, unmatched


def _offered_block_keys(case: Case) -> set[str]:
    """Every block already offered on this case, whatever became of it: the
    user saw it once, and offering it again would be nagging."""
    return {key for offer in case.chat_data_offers for key in offer.block_keys}


def _state_request_open(case: Case) -> bool:
    """Is a state request still waiting? Then a new state report is not asked
    about again: asking every time the user says "still stable" would be
    nagging, not cooperation. A request closes on the first later turn that
    submits data (``close_state_requests``)."""
    return any(
        offer.request and offer.decided_at_turn is None
        for offer in case.chat_data_offers
    )


def close_state_requests(case: Case) -> None:
    """A turn submitted data: every waiting state request is answered by it.

    Keyed on the TURN carrying data, not on a new ``UploadedFile`` row: a
    re-submission that matches an earlier file by content adds no row, and
    would otherwise leave the request waiting for the rest of the case."""
    for offer in case.chat_data_offers:
        if offer.request and offer.decided_at_turn is None:
            offer.decided_at_turn = case.current_turn


def mint_chat_data_offer(
    case: Case,
    *,
    user_message: str,
    named_blocks: Optional[Sequence[object]],
    state_report: Optional[object] = None,
    submitted_data: bool = False,
    reverse: Callable[[str], str] = lambda text: text,
) -> Optional[ChatDataOffer]:
    """Record the offer for this turn's message, or None when nothing is offered.

    ``named_blocks`` are the model's ``ChatDataBlock`` entries. ``reverse`` undoes
    the turn's PII redaction: the model saw a redacted message, so its anchors
    carry placeholders the stored message does not. A block already offered on
    this case is not offered again.

    ``state_report`` is the model's ``StateReport``: the message reports the
    state of the user's system with no data showing it, and names the output
    that would. It is asked for unless an earlier request is still waiting with
    nothing submitted since. A turn that submitted data closes the earlier
    requests first (``close_state_requests``) and does not stop this one: the
    model sets a request only when it judged this turn's data does not show
    the reported state, and the engine cannot tell which upload shows what
    (#1938).

    Nothing is offered on a terminal case (it takes no new data) or for an empty
    message.
    """
    if submitted_data:
        close_state_requests(case)
    if not (user_message or "").strip() or case.is_terminal:
        return None
    blocks: List[str] = []
    if named_blocks:
        anchors = [
            (
                reverse(getattr(block, "first_line", "") or ""),
                reverse(getattr(block, "last_line", "") or ""),
            )
            for block in list(named_blocks)[:MAX_CHAT_DATA_BLOCKS]
        ]
        found, unmatched = locate_blocks(user_message, anchors)
        if unmatched:
            CHAT_DATA_OFFER_EVENTS_TOTAL.labels(event="block_unmatched").inc(unmatched)
            logger.info(
                "Case %s turn %s: %d named chat-data block(s) not found in the "
                "message",
                case.case_id,
                case.current_turn,
                unmatched,
            )
        # Seeded with the case's earlier offers and grown as this message's own
        # blocks are taken, so output pasted twice in one message is offered
        # once.
        seen = _offered_block_keys(case)
        for block in found:
            key = block_key(block)
            if (
                block.strip()
                and len(block) <= MAX_CHAT_DATA_BLOCK_CHARS
                and key not in seen
            ):
                seen.add(key)
                blocks.append(block)
    request = (getattr(state_report, "request", None) or "").strip() or None
    if request is not None and _state_request_open(case):
        request = None
    if not blocks and request is None:
        return None
    offer = ChatDataOffer(
        turn=case.current_turn, blocks=blocks, request=reverse(request or "") or None
    )
    case.chat_data_offers.append(offer)
    CHAT_DATA_OFFER_EVENTS_TOTAL.labels(
        event="offered" if blocks else "state_requested"
    ).inc()
    return offer


def find_offer(case: Case, offer_id: Optional[str]) -> Optional[ChatDataOffer]:
    """The case's offer with this id, or None."""
    if not offer_id:
        return None
    return next((o for o in case.chat_data_offers if o.offer_id == offer_id), None)
