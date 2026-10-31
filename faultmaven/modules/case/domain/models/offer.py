"""The offer record: what a case offers the user to answer, and every answer.

An offer is a question only the user can answer, about the case itself: to
resolve it, to close it, or to accept a revised problem statement. The record
holds the one offer open now (``OfferRecord.open``), the answers given to
every earlier one (``OfferRecord.answers``), and a remembered request for what
a resolution still needs (``OfferRecord.resolve_request``, which is not an
offer). It is case data, persisted as ``cases.metadata["offer_record"]`` and
reached only through ``modules/case/contracts.py``.

The models are frozen. The rules that write the record (open, answer, lapse)
and the projection that reads it live in ``core/investigation/offers.py``,
whose docstring states them once.
"""

from enum import StrEnum
from typing import Optional

from pydantic import BaseModel, ConfigDict

from .lifecycle import ClosureReason
from .problem import PendingRevision


class OfferKind(StrEnum):
    """What an offer asks the user to do."""

    RESOLVE = "resolve"
    CLOSE = "close"
    REVISION = "revision"

    @property
    def to_state(self) -> Optional[str]:
        """The case state a terminal offer moves the case to; None for a
        revision, which moves no case state."""
        return _TO_STATE.get(self)


_TO_STATE = {OfferKind.RESOLVE: "resolved", OfferKind.CLOSE: "closed"}


class OfferOrigin(StrEnum):
    """Who asked: the engine on case state, the model in its reply, or the
    user from a control."""

    ENGINE = "engine"
    MODEL = "model"
    USER = "user"


class OfferBasis(StrEnum):
    """What earned the offer. Its origin is derived (``OfferBasis.origin``)."""

    #: The resolution backstop: the case confirms the resolution (INV-43).
    BACKSTOP = "backstop"
    #: The deferred-implementation disposition, close or its resolve pivot.
    DEFERRED = "deferred"
    #: The close a false-alarm finding calls for.
    FALSE_ALARM = "false_alarm"
    #: The stabilized close on a mitigation with an infeasible RCA.
    RCA_INFEASIBLE = "rca_infeasible"
    #: The close after a resolution request the next turn did not meet.
    NEEDS_INFO_UNMET = "needs_info_unmet"
    #: The model's proposed transition, or the resolve a met request earns.
    MODEL = "model"
    #: The user's status-menu Close (and its INV-37 pivot).
    MENU = "menu"
    #: The "Mark it resolved" chip re-opening a declined resolution.
    REOPEN = "reopen"
    #: A revised problem statement the evidence calls for.
    REVISION = "revision"

    @property
    def origin(self) -> OfferOrigin:
        return _ORIGINS[self]


_ORIGINS = {
    OfferBasis.BACKSTOP: OfferOrigin.ENGINE,
    OfferBasis.DEFERRED: OfferOrigin.ENGINE,
    OfferBasis.FALSE_ALARM: OfferOrigin.ENGINE,
    OfferBasis.RCA_INFEASIBLE: OfferOrigin.ENGINE,
    OfferBasis.NEEDS_INFO_UNMET: OfferOrigin.ENGINE,
    OfferBasis.MODEL: OfferOrigin.MODEL,
    OfferBasis.REVISION: OfferOrigin.MODEL,
    OfferBasis.MENU: OfferOrigin.USER,
    OfferBasis.REOPEN: OfferOrigin.USER,
}


class AnswerKind(StrEnum):
    """How an offer stopped being open."""

    ACCEPTED = "accepted"
    DECLINED = "declined"
    #: Taken back unanswered: a question about it, an upload, a menu close
    #: past a revision, the engine on a finding the user disputed.
    WITHDRAWN = "withdrawn"
    #: Its premise went, with nobody answering (the case left INVESTIGATING,
    #: its finding was cleared).
    LAPSED = "lapsed"
    #: A close the user accepted that the INV-37 re-check turned into a
    #: resolve proposal: the case was resolvable, so it did not close.
    PIVOTED = "pivoted"
    #: A revision replaced by a re-wording before the user answered it.
    SUPERSEDED = "superseded"


class AnswerVia(StrEnum):
    """Where the answer came from."""

    CLICK = "click"
    TOKEN = "token"
    TYPED = "typed"
    MENU = "menu"
    ENGINE = "engine"


class OfferPremise(BaseModel):
    """The state an answer was given against. A kind reads only its own parts:
    a resolve or close answer its solution and confirmation ids, a false-alarm
    close the finding's turn, a revision its wording key.

    The ids are sorted and complete. A decline given on turn N is refreshed to
    cover what turn N itself recorded (``offers.lapse_offers``), so the
    model's same-turn updates never un-answer it."""

    model_config = ConfigDict(frozen=True)

    solution_ids: tuple[str, ...] = ()
    confirmation_ids: tuple[str, ...] = ()
    finding_turn: Optional[int] = None
    wording_key: Optional[str] = None


class Offer(BaseModel):
    """The one offer open on a case."""

    model_config = ConfigDict(frozen=True)

    #: Opaque and random (``secrets.token_hex(8)``), minted at open. Every
    #: confirmation card carries it, and a click answers only the offer it
    #: names (#1812).
    proposal_id: str
    kind: OfferKind
    basis: OfferBasis
    #: Derived at open for a close offer (``derive_closure_reason``); None
    #: otherwise.
    closure_reason: Optional[ClosureReason] = None
    summary: str = ""
    evidence_ids: tuple[str, ...] = ()
    opened_at_turn: int = 0
    #: A resolve offer opened by the INV-37 pivot of a close the user accepted;
    #: it carries the close's basis.
    pivoted_from_close: bool = False
    #: A revision offer's payload: the revised statement and the cause work
    #: staged while it waits.
    revision: Optional[PendingRevision] = None

    @property
    def origin(self) -> OfferOrigin:
        return self.basis.origin


class OfferAnswer(BaseModel):
    """How one offer was answered, and the state it was answered against."""

    model_config = ConfigDict(frozen=True)

    proposal_id: str
    kind: OfferKind
    basis: OfferBasis
    closure_reason: Optional[ClosureReason] = None
    answer: AnswerKind
    via: AnswerVia
    #: The case's effective driver when the user answered (ruling 7); None
    #: when no one did (a lapse, an engine withdrawal).
    answered_by: Optional[str] = None
    at_turn: int = 0
    premise: OfferPremise = OfferPremise()
    #: The turn a decline stopped binding: a solution or confirmation it never
    #: saw was recorded, or its finding went. Never cleared.
    lapsed_at_turn: Optional[int] = None


class ResolveRequest(BaseModel):
    """What a resolution still needs, asked for when the model proposed one
    the case had not earned (#2408). Not an offer: no chips, nothing to
    confirm. Read once, by the next turn's re-check."""

    model_config = ConfigDict(frozen=True)

    missing: tuple[str, ...] = ()
    at_turn: int = 0


class OfferRecord(BaseModel):
    """At most one open offer, every answer, and a remembered request."""

    model_config = ConfigDict(frozen=True)

    open: Optional[Offer] = None
    #: Oldest first. Capped (``offers.MAX_ANSWERS``); a standing decline is
    #: never evicted.
    answers: tuple[OfferAnswer, ...] = ()
    resolve_request: Optional[ResolveRequest] = None
