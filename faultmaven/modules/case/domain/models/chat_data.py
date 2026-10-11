"""What the user tells FaultMaven in a chat message, offered back for them to declare (#1930).

What the user writes in a chat message is a claim; only data they submit (an
uploaded file or a paste, stored as an UploadedFile) is evidence. When a message
carries output a system produced, the engine finds it in the message and offers
it back: save it as data, or re-submit a corrected copy. The user, not the
server, declares what is data; an offer the user ignores leaves the message a
claim, so there is no "no" to click.

A message that reports the state of the user's system ("it's stable", "it's
working now") without submitting data showing it is answered the same way:
FaultMaven asks for the output that would show it, for verification and for the
record.

A ``ChatDataOffer`` is that offer's record on the case. It holds the blocks
exactly as they appear in the user's message (sliced from the message, never
the model's copy), so what a save stores is what the user wrote, and the output
asked for when the message reported a state.
"""

from enum import Enum
from typing import List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

#: Most blocks one offer carries; a message naming more is offered its first ones.
MAX_CHAT_DATA_BLOCKS = 5
#: Longest block an offer carries, in characters. A paste this long belongs in
#: the client's data box, which every client opens for a multi-line paste.
MAX_CHAT_DATA_BLOCK_CHARS = 100_000


class ChatDataOfferState(str, Enum):
    """Where an offer stands: ``OFFERED`` until the user saves it. Ignoring it
    is the user's "no", and leaves it ``OFFERED``; a save is final."""

    OFFERED = "offered"
    SAVED = "saved"


class ChatDataOffer(BaseModel):
    """One offer: the system-output blocks found in one user message."""

    offer_id: str = Field(
        default_factory=lambda: f"cdo_{uuid4().hex[:12]}",
        pattern=r"^cdo_[a-f0-9]{12}$",
    )
    turn: int = Field(
        description="The message clock of the user message the blocks were found in."
    )
    blocks: List[str] = Field(
        default_factory=list,
        max_length=MAX_CHAT_DATA_BLOCKS,
        description="Each block exactly as it appears in the user's message.",
    )
    request: Optional[str] = Field(
        default=None,
        description=(
            "The output asked for when the message reported the state of the "
            "user's system without data showing it."
        ),
    )
    state: ChatDataOfferState = ChatDataOfferState.OFFERED
    decided_at_turn: Optional[int] = None
    file_ids: List[str] = Field(
        default_factory=list,
        description="The uploaded file a save produced.",
    )

    @model_validator(mode="after")
    def _offers_something(self) -> "ChatDataOffer":
        if not self.blocks and not self.request:
            raise ValueError("a chat-data offer carries blocks, a request, or both")
        return self
