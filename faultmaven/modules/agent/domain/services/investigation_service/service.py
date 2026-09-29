"""Investigation Service - Manages milestone-based troubleshooting workflow

Purpose: Orchestrate investigation turns and milestone progress tracking

This service wraps the MilestoneEngine and provides:
- Access control for investigations
- Case retrieval and persistence
- Turn creation and processing
- Progress tracking and reporting
- Integration with session management
"""

import copy
import logging
from enum import Enum
from typing import Any, Dict, List, Optional

from faultmaven.config.tenant_context import get_current_billing_organization_id
from faultmaven.core.investigation.case_telemetry import (
    TurnPath,
    emit_case_turn,
)
from faultmaven.core.investigation.intent_resolver import IntentResolver
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.schemas import TurnPayload
from faultmaven.core.investigation.suggestion_liveness import (
    live_suggestions,
)
from faultmaven.core.investigation.turn_uploads import report_turn_uploads
from faultmaven.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedException,
    ServiceException,
    ValidationException,
)
from faultmaven.infrastructure.observability.evidence_metrics import (
    EVIDENCE_RECLASSIFICATION_TOTAL,
)
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.infrastructure.protection.tenant_turn_cap import (
    TenantTurnCapError,
    billing_subject_for,
)
from faultmaven.models.api import DataType
from faultmaven.models.api_models import (
    IntentType,
    QueryIntent,
    TurnResponse,
)
from faultmaven.modules.agent.domain.services.investigation_service.attachments import (
    _engine_attachment_metadata,
    _preprocess_turn_uploads,
)
from faultmaven.modules.agent.domain.services.investigation_service.intent_gates import (
    _minted_intent_swallows_gate_consent,
)
from faultmaven.modules.agent.domain.services.investigation_service.reclassification import (
    _handle_file_reclassification,
    _reclassified_collections,
    _reextract_under_override,
)
from faultmaven.modules.agent.domain.services.investigation_service.turn_messages import (
    _build_user_message,
    _save_and_emit_turn,
)
from faultmaven.modules.agent.domain.services.investigation_service.turn_response import (
    _build_turn_response,
)
from faultmaven.modules.agent.domain.services.investigation_service.turn_results import (
    _absorb_engine_result,
)
from faultmaven.modules.agent.domain.services.orientation import (
    OUT_OF_BAND_MARKER,
    OrientationKind,
    back_to_investigation_follow_up,
    build_orientation,
    describe_issue_follow_up,
    detect_orientation,
)
from faultmaven.modules.agent.domain.services.out_of_band import (
    OutOfBandKind,
    OutOfBandTriage,
    answer_out_of_band,
    has_investigation_history,
)
from faultmaven.modules.agent.domain.services.query_classifier import (
    classify_query,
)
from faultmaven.modules.case.contracts import (
    Case,
    TurnOutcome,
)
from faultmaven.modules.case.contracts import ICaseRepository as CaseRepository
from faultmaven.modules.case.domain.models.evidence import (
    Evidence,
)

# Cross-module imports via contracts (Principle 2: Vertical Modules with Contracts)
from faultmaven.modules.case.domain.services.case_action_manager import (
    earned_edge_refusal,
)
from faultmaven.modules.case.exceptions import StaleCaseException

logger = logging.getLogger(__name__)


# Longest filename fragment a choice label will carry. Long enough to keep
# real names recognisable, short enough that a pathological one cannot
# dominate the resolver's choice list.


# ============================================================
# Intent dispatch
# ============================================================
#
# The IntentType enum is the contract between the API layer and the
# investigation pipeline. Each value must have a defined route here, or
# the system can't promise it can handle requests carrying that intent.
# Historically the dispatch lived as a scattered ``if / elif / else: raise``
# chain in ``process_turn``; new enum values could be added (slice 1 of the
# investigation-gates work did exactly this) without updating the dispatch,
# and the gap surfaced only at runtime as a 500.
#
# The dispatch table below is the single source of truth. The boot check
# in ``InvestigationService.__init__`` validates completeness against the
# IntentType enum — a new enum value without an entry here fails service
# construction, which fails app startup and CI.


class _IntentDispatchKind(str, Enum):
    """How an intent reaches its handler.

    SERVICE — a method on InvestigationService (special-cased: no LLM call,
              or pre-LLM bookkeeping).
    ENGINE  — delegated to ``engine.process_turn`` with intent_type +
              intent_data threaded through; the engine dispatches
              internally to a per-intent handler.
    NOT_IMPLEMENTED — the enum value exists in the API contract but the
              system does not yet handle it. Runtime requests raise
              ValidationException (422) with a clear "not implemented"
              message. Use this for known gaps; remove the enum value if
              the gap is permanent.
    """

    SERVICE = "service"
    ENGINE = "engine"
    NOT_IMPLEMENTED = "not_implemented"


_INTENT_DISPATCH: Dict[IntentType, _IntentDispatchKind] = {
    IntentType.STATUS_TRANSITION: _IntentDispatchKind.SERVICE,
    IntentType.CONFIRMATION: _IntentDispatchKind.SERVICE,
    IntentType.HYPOTHESIS_ACTION: _IntentDispatchKind.SERVICE,
    IntentType.GREETING: _IntentDispatchKind.SERVICE,
    # FILE_RECLASSIFICATION resolves a classification_failed upload: the
    # clarification DECIDE suggestions carry this intent (file_id + target
    # DataType) and the handler re-runs preprocessing mechanically — no LLM
    # call, so it can never mistake the choice for an analysis request.
    IntentType.FILE_RECLASSIFICATION: _IntentDispatchKind.SERVICE,
    IntentType.CONVERSATION: _IntentDispatchKind.ENGINE,
    # EVIDENCE_NEED (renamed from EVIDENCE_REQUEST in Phase 2 of the
    # evidence-needs redesign) is in the IntentType enum and has a
    # QueryIntent validator requiring evidence_need_id, but no handler
    # is wired yet — the pool model surfaces needs through EVIDENCE-type
    # suggestions and the LLM matches uploads to needs at file-
    # processing time, so a user-initiated intent isn't needed for the
    # MVP. Stays NOT_IMPLEMENTED until a frontend feature specifically
    # requires it (e.g., a "tell me more about this need" button). See
    # docs/architecture/investigation-engine/evidence-needs-design.md §9.3.
    IntentType.EVIDENCE_NEED: _IntentDispatchKind.NOT_IMPLEMENTED,
}


def _validate_intent_dispatch_completeness() -> None:
    """Validate that every ``IntentType`` enum value has a dispatch route.

    Raises RuntimeError at service construction time (and therefore at app
    startup and CI) if the dispatch table is incomplete. This converts the
    silent-runtime-500 failure mode of the prior elif chain into a
    fail-fast contract: a new enum value cannot ship without a dispatch
    decision (service handler, engine handler, or explicit not-implemented).
    """
    defined = set(IntentType)
    routed = set(_INTENT_DISPATCH.keys())
    missing = defined - routed
    extra = routed - defined
    if missing or extra:
        parts = []
        if missing:
            parts.append(
                f"IntentType values without a dispatch entry: "
                f"{sorted(v.value for v in missing)}"
            )
        if extra:
            parts.append(
                f"_INTENT_DISPATCH entries that are not IntentType values: "
                f"{sorted(v.value for v in extra)}"
            )
        raise RuntimeError(
            "InvestigationService intent dispatch is incomplete. "
            + " ".join(parts)
            + " Update _INTENT_DISPATCH in investigation_service.py or the "
            "IntentType enum so the two agree."
        )


class InvestigationService:
    """
    Service for managing investigation turns and milestone progress.

    Coordinates between:
    - MilestoneEngine (core investigation logic)
    - CaseRepository (persistence)
    - Access control (user permissions)
    """

    def __init__(
        self,
        milestone_engine: MilestoneEngine,
        case_repository: CaseRepository,
        preprocessing_service=None,
        file_storage_service=None,
        turn_cap=None,
    ):
        """
        Initialize investigation service.

        Args:
            milestone_engine: Core investigation engine with LLM integration
            case_repository: Case persistence layer
            preprocessing_service: Classification and extraction pipeline
            file_storage_service: Raw file storage (local/S3)
            turn_cap: Per-tenant daily turn cap (ADR-016 D5.3). Left ``None``
                by every caller that does not care: the default is built once,
                lazily, and under single-tenant it answers "uncapped" from the
                deployment mode without touching a port — so a test that never
                heard of the cap neither reaches a database nor has to blank a
                guard out to get its turn served.
        """
        self.engine = milestone_engine
        self.repository = case_repository
        self.preprocessing_service = preprocessing_service
        self.file_storage_service = file_storage_service
        self._turn_cap = turn_cap
        self.intent_resolver = IntentResolver(milestone_engine.deps.llm_provider)
        self.out_of_band_triage = OutOfBandTriage(milestone_engine.deps.llm_provider)
        # Fail-fast: refuse to construct if the intent dispatch table is
        # missing any IntentType value (or vice-versa). The system cannot
        # honor the API contract if it can't route every advertised intent.
        _validate_intent_dispatch_completeness()

    @property
    def turn_cap(self):
        """The cap service, built on first use if nobody injected one."""
        if self._turn_cap is None:
            from faultmaven.infrastructure.protection.tenant_turn_cap import (
                UnconfiguredTurnCap,
            )

            self._turn_cap = UnconfiguredTurnCap()
        return self._turn_cap

    @trace("investigation_service_process_turn")
    async def process_turn(
        self, case_id: str, user_id: str, payload: TurnPayload
    ) -> TurnResponse:
        """
        Process a user turn through the two-step pipeline.

        Step 1: Preprocess any attachments (classify + extract, before LLM).
        Step 2: LLM inference with query + evidence context.

        Args:
            case_id: Case identifier
            user_id: User making the request
            payload: Turn payload with optional query and/or attachments

        Returns:
            TurnResponse with agent response, milestones, progress, and attachment results

        Raises:
            NotFoundError: If case not found
            PermissionDeniedException: If user not authorized
            ServiceException: If turn processing fails
        """
        # #1142: bound before the try so the error path can tell "the case was
        # never loaded" from "a turn was consumed and then failed", and so a
        # failure AFTER the success row is emitted does not produce a second row
        # for the same turn.
        case = None
        turn_consumed = False
        turn_row_emitted = False

        def _emit_error_row() -> None:
            """One row for a turn that was consumed and then failed (#1142).

            Carries the volume facts off ``payload`` — they are known whatever
            failed, and omitting them reports the user as having gone silent on
            a turn they pasted 4 KB into, which is the mirror image of the
            misattribution the ``error`` label exists to prevent.
            """
            if turn_consumed and not turn_row_emitted and case is not None:
                emit_case_turn(
                    case,
                    path=TurnPath.ERROR,
                    user_message_chars=len(payload.query or ""),
                    attachment_count=len(payload.attachments or []),
                )

        try:
            # 1. Retrieve case and verify access
            case = await self.repository.get(case_id)
            classification, next_turn, processing_mode = (
                await self._verify_access_and_reserve(
                    case=case, case_id=case_id, payload=payload, user_id=user_id
                )
            )

            # ── STEP 1: PRE-LLM DATA INGESTION ──

            # Post-010 strict evidence model: preprocessing creates only
            # UploadedFile rows (no auto-Evidence). Evidence is born
            # later when the LLM emits ``evidence_to_add`` during
            # INVESTIGATING. Track the UploadedFiles created this turn
            # so the implicit-query helper can describe what the user
            # submitted; ``case.uploaded_files`` already had each row
            # appended inside ``_preprocess_attachment``.
            classification, preprocess_results, query, uploaded_files_this_turn = (
                await _preprocess_turn_uploads(
                    self.file_storage_service,
                    self.preprocessing_service,
                    self.repository,
                    case=case,
                    case_id=case_id,
                    classification=classification,
                    next_turn=next_turn,
                    payload=payload,
                    processing_mode=processing_mode,
                    user_id=user_id,
                )
            )

            # 2. Build user message and update case in-memory (NOT persisted yet).
            #    What the deferral actually buys: nothing is committed BEFORE the
            #    LLM runs, so a turn that fails in the LLM call leaves no orphaned
            #    user message and no inflated turn count, and the client can retry
            #    the same turn. That is the whole of it.
            #
            #    ⚠️ It does NOT make the turn atomic, and it does NOT commit the
            #    user message and the agent's reply together. Two earlier versions
            #    of this comment claimed one or the other; both were false, so
            #    check this against the code before trusting it:
            #
            #      - On an engine-routed turn ``MilestoneEngine`` saves the case
            #        UNCONDITIONALLY at its Step 7 (``_persist_turn``,
            #        ``milestone_engine/turn_completion.py``) — before returning, and therefore
            #        before the agent reply is appended by step 4 below. The user
            #        message is durable at that point and the reply is not. A
            #        failure in the window between them (reverse-redaction,
            #        clarification building, response assembly) leaves exactly the
            #        orphaned-user-message + inflated-turn state this comment used
            #        to promise was impossible.
            #      - The deterministic and terminal branches commit the same
            #        ``case`` object earlier still, at their own ``save(case)``
            #        sites.
            #
            #    So: an LLM failure commits nothing; a post-LLM failure can commit
            #    a half turn. Do not reason about this path as all-or-nothing.
            intent, intent_type, orientation_kind, user_message_obj, was_terminal = (
                _build_user_message(
                    case=case,
                    case_id=case_id,
                    next_turn=next_turn,
                    payload=payload,
                    query=query,
                    user_id=user_id,
                )
            )
            # Gates the error-path row. ``case is not None`` is NOT the same
            # question: a failure between the load and this line (an extractor
            # or storage error inside attachment preprocessing, say) leaves a
            # bound ``case`` whose ``current_turn`` is still the PREVIOUS turn's
            # — a row emitted there would collide with that turn's real row on
            # the documented (case_id, turn) dedup key, and on turn 1 would
            # invent a row for turn 0.
            turn_consumed = True

            # ── STEP 2: LLM INFERENCE ──
            # Heuristic check for greetings if intent is CONVERSATION (default)
            #
            # Never on a turn that carried an attachment (#1229). The heuristic
            # reads the message text alone, so "hi" plus a genuinely new log
            # matched ``^(hi|hello|...)$`` and routed to ``_handle_greeting`` —
            # which answers from a static string, never calls the engine, and
            # therefore reported no upload, armed no progress arm, and left the
            # two #1224 degradation warnings unreachable. The row was already
            # committed and dedup-classified by then; only the engine was not
            # told. A turn that delivers data is not a greeting, whatever the
            # covering text says — the same judgement #708 applies one block
            # below when it re-routes a generic cover note to Directed
            # Analysis. Any attachment disqualifies, not just a novel one: a
            # re-submission still belongs on the path that knows what to do
            # with a duplicate.
            attachment_metadata, intent_type, oob_kind, result = (
                await self._dispatch_turn(
                    case=case,
                    case_id=case_id,
                    classification=classification,
                    intent=intent,
                    intent_type=intent_type,
                    next_turn=next_turn,
                    orientation_kind=orientation_kind,
                    payload=payload,
                    preprocess_results=preprocess_results,
                    query=query,
                    user_id=user_id,
                    user_message_obj=user_message_obj,
                )
            )

            # 3. Processing succeeded — extract updated case
            (
                agent_response_text,
                clarification,
                raw_follow_ups,
                turn_meta,
                turn_telemetry,
                updated_case,
            ) = _absorb_engine_result(
                preprocess_results=preprocess_results, query=query, result=result
            )

            # 4. Append the agent response and save.
            #    ⚠️ This is NOT an atomic commit of both messages, though it used
            #    to say so ("commits both messages together, guaranteeing no
            #    half-completed turns"). On an engine-routed turn the engine has
            #    ALREADY committed the user message at its Step 7 save, so by the
            #    time control reaches here a half-completed turn is exactly what
            #    is in the database, and this save completes it rather than
            #    preventing it.
            #
            #    It IS the single commit for both only when no engine save
            #    intervened — GREETING and FILE_RECLASSIFICATION. The other three
            #    SERVICE intents (STATUS_TRANSITION, CONFIRMATION,
            #    HYPOTHESIS_ACTION) delegate to ``engine.process_turn`` from their
            #    handlers, so they hit Step 7 just like an engine-routed turn.
            #    "Service-dispatched" is NOT a synonym for "no engine save".
            #    See the STEP-2 comment for the full ordering.
            # An empty ``agent_response`` is a FAILED turn, not a quiet one.
            # Blank content aborts the aggregate save and takes the user's turn
            # with it, for a turn already charged — so the row kind records
            # it, honestly, as ``EMPTY_AGENT_RESPONSE_TEXT`` flagged
            # ``MESSAGE_METADATA_AGENT_SYNTHESIZED`` rather than dropping it or
            # leaving it blank (#1433, #1452).
            # A PERSISTENCE BACKSTOP, not a policy (#1442). ``MilestoneEngine``
            # owns response synthesis: it holds the provider's stop reason and
            # names an unusable answer by it (withheld, truncated, empty, no
            # signal). This layer receives only a string, so anything it wrote
            # would be blind — it therefore writes nothing contextual, and
            # exists only so a raw "" whose text never came through the
            # engine's synthesis (the out-of-band answer, for one, is generated
            # by this service) cannot abort the aggregate save.
            #
            # ``turn_meta`` is passed, not copied: it is the ONE binding of
            # this turn's metadata, aliased with ``result["metadata"]``
            # (#1270), and the flag is written into it in place. A fresh dict
            # would sever that and the readers would stop seeing each other —
            # which is why there is no ``or {}`` fallback here. It is bound by
            # ``result.setdefault("metadata", {})`` far above and has already
            # been ``.pop()``-ed from by then, so a None would have raised long
            # before this line.
            agent_response_text = await _save_and_emit_turn(
                self.repository,
                agent_response_text=agent_response_text,
                attachment_metadata=attachment_metadata,
                # The intent ``_build_user_message`` settled on — the client's,
                # minus a GREETING it re-derives from the text — not a mint.
                intent=intent,
                intent_type=intent_type,
                oob_kind=oob_kind,
                payload=payload,
                turn_meta=turn_meta,
                turn_telemetry=turn_telemetry,
                updated_case=updated_case,
                was_terminal=was_terminal,
            )
            turn_row_emitted = True

            # 5. Build TurnResponse
            return _build_turn_response(
                agent_response_text=agent_response_text,
                case_id=case_id,
                clarification=clarification,
                payload=payload,
                preprocess_results=preprocess_results,
                raw_follow_ups=raw_follow_ups,
                turn_meta=turn_meta,
                updated_case=updated_case,
                uploaded_files_this_turn=uploaded_files_this_turn,
            )

        except (
            NotFoundError,
            PermissionDeniedException,
            StaleCaseException,
            TenantTurnCapError,
            ValidationException,
        ):
            # NotFoundError → 404, PermissionDeniedException → 403,
            # StaleCaseException → 409, TenantTurnCapExceeded → 429,
            # TenantTurnCapUnavailable → 503, ValidationException → 422.
            # All must pass through unwrapped so the FastAPI exception
            # handlers can map them to the correct HTTP status; wrapping
            # them in ServiceException would mask the contract error as 500.
            # The cap pair is here for exactly that reason: wrapped, a
            # capped tenant would be told its turn failed with a 500 and the
            # route's 429 arm would be unreachable code.
            #
            # #1142: these get a row too when the turn was already consumed.
            # StaleCaseException is the case that matters — on an engine-routed
            # turn the engine has ALREADY committed the incremented
            # ``current_turn`` at its own save, so an OCC conflict on the
            # service's save leaves a durably consumed turn with no row, and a
            # gap shortens every streak computed over the stream.
            _emit_error_row()
            raise
        except Exception as e:
            # #1142: the turn number was consumed at STEP 1 and the request then
            # failed, so without a row here the stream shows a gap on exactly
            # the turns where something went wrong. The point of labelling it is
            # attribution: a provider outage or a tool-loop failure must not read
            # as an idle engine. ``case`` may be unbound if the failure preceded
            # the load, and the case may never have been saved at this turn
            # number, so a consumer dedups on (case_id, turn) preferring the
            # non-error row.
            _emit_error_row()
            logger.error(f"Failed to process turn for case {case_id}: {e}")
            # Preserve a typed error_code (e.g. QUOTA_EXHAUSTED billing) through
            # the wrap so the route handler can map it to a precise HTTP status
            # instead of a generic 500.
            raise ServiceException(
                f"Turn processing failed: {str(e)}",
                details={"error_code": getattr(e, "error_code", None)},
            ) from e

    async def _verify_access_and_reserve(self, *, case, case_id, payload, user_id):
        """Refuse a missing/unauthorized case, reserve the daily turn cap, then classify the query."""
        if not case:
            raise NotFoundError("Case", case_id)

        if case.user_id != user_id:
            logger.warning(
                f"User {user_id} denied access to case {case_id} (owner: {case.user_id})"
            )
            raise PermissionDeniedException(
                f"User {user_id} not authorized for case {case_id}"
            )

        # ── The per-tenant daily turn cap (ADR-016 D5.3) ──
        # HERE, and the position is the decision. Everything that can refuse
        # this request for a reason that is not "you have spent your day"
        # has already run: the route's own validation (oversize → 413,
        # unknown intent → 422, a closed case → 409; an EMPTY turn is
        # accepted since #1343 and charged like any other — it is answered
        # with an orientation), the route's case lookup, and the two refusals
        # immediately above. So a malformed turn, a probe at another
        # tenant's case id, and a turn to a case that does not exist all
        # cost the tenant nothing — where a route-level guard charged them
        # a unit each, and a cross-tenant probe charged the *prober*.
        #
        # And it is before STEP 1, which classifies and preprocesses every
        # attachment: a capped tenant must not have its files extracted and
        # written to storage for a turn that will not run.
        #
        # In the service rather than at the route because this is where the
        # invariant actually lives: every caller of ``process_turn`` is
        # bounded by construction, rather than every caller having to
        # remember a dependency. The OpenAPI inventory test remains as the
        # secondary check that no second HTTP door appears.
        #
        # #1329 asked for out-of-band turns (small talk, trivia, questions
        # about FaultMaven itself) to be exempt from this charge. The issue
        # owner ruled otherwise (issue comment, 2026-09-05): the cap bounds
        # COMPUTE, not diagnostic progress, and an exemption keyed on a
        # classifier's verdict is a free channel — phrase the extra turns as
        # tangents. So every message is charged here, unconditionally, and
        # the out-of-band lane below changes only what the turn DOES with
        # the charge: no engine, no case mutation, no place in the
        # investigation's history.
        # Charged to the BILLING subject, not the tenant (ADR-017 D5): the
        # organization when one pays for this account, the account itself
        # when none does. Metering the enterprise instead would make two
        # departments of one company share an allowance neither agreed to.
        await self.turn_cap.reserve(
            billing_subject_for(get_current_billing_organization_id(), user_id)
        )

        next_turn = case.current_turn + 1

        classification = classify_query(
            payload.query or "",
            has_attachments=payload.has_attachments,
        )
        processing_mode = classification.mode.value
        return classification, next_turn, processing_mode

    async def _dispatch_turn(
        self,
        *,
        case,
        case_id,
        classification,
        intent,
        intent_type,
        next_turn,
        orientation_kind,
        payload,
        preprocess_results,
        query,
        user_id,
        user_message_obj,
    ):
        """Resolve intent (orientation, typed-choice, out-of-band triage) and dispatch the turn to its handler."""
        if (
            intent_type == IntentType.CONVERSATION
            and not payload.has_attachments
            # A blank or greeting-shaped reply over a pending terminal
            # proposal is an answer to THAT question; the engine's own
            # gate handling re-presents or withdraws it. Same guard as
            # the out-of-band lane below.
            and not getattr(case, "pending_transition", None)
        ):
            # ``query`` may be empty here: a bare @mention in Slack arrives
            # with no text and no file, and used to be refused by the route.
            # That is the EMPTY orientation — "where are we, what can I do".
            orientation_kind = detect_orientation(query)
            if orientation_kind is not None:
                intent_type = IntentType.GREETING
                # Tag the user row like an aside (#1329): the history
                # renderers and the investigation-turn count read this key.
                user_message_obj["metadata"]["out_of_band"] = OUT_OF_BAND_MARKER
                user_message_obj["metadata"]["orientation"] = orientation_kind.value
                logger.info(
                    "Orientation turn (%s) on case %s",
                    orientation_kind.value,
                    case_id,
                )

        # Intent resolution: match typed text against the choices still
        # on offer. Only runs when no structured intent was sent and the
        # case has live intent-bearing suggestions.
        #
        # ``live_suggestions``, not the raw field: the row is rewritten
        # only on this method's success path, so what is stored is not by
        # itself evidence that a turn put it there for now (fm#918).
        # ``case.current_turn`` is already this turn's number here (set
        # just above with the user message), so an entry offered on the
        # immediately preceding turn ages to 1.
        #
        # Computed inside the guard, not above it: the cheap conditions
        # reject the great majority of turns, and this walks every stored
        # entry and indexes every uploaded file to answer a question those
        # turns never ask.

        # Set when the INV-26 guard below refuses a mint. The refusal is a
        # POSITIVE judgement — this message is a substantive answer to a
        # gate — so it is carried to the out-of-band lane rather than
        # recomputed there; see the lane's own note.
        gate_reply_refused = False

        # Set when the intent below was MINTED from typed text rather than sent
        # by a click. The engine names how a terminal transition was confirmed
        # (#1748), and a typed "ok" the resolver turned into a confirmation is
        # still a typed "ok", not a click.
        intent_minted = False

        if (
            intent_type == IntentType.CONVERSATION
            and query
            and not payload.has_attachments
            and case.last_suggestions
        ):
            on_offer = live_suggestions(
                case.last_suggestions, case, as_of_turn=case.current_turn
            )
            resolved_intent = (
                await self.intent_resolver.resolve(
                    user_message=query,
                    last_suggestions=on_offer,
                )
                if on_offer
                else None
            )
            if resolved_intent:
                try:
                    resolved_qi = QueryIntent(**resolved_intent)
                    if _minted_intent_swallows_gate_consent(case, resolved_qi, query):
                        # INV-26 guard (#721, widened by fm#918): the
                        # resolver's classifier tier matched substantive
                        # typed text ("yes but what about the replication
                        # lag?") to a suggestion whose intent would COMMIT
                        # A GATE — the pending TERMINAL transition, or
                        # INQUIRY's Gate 1. Substantive input is never
                        # consent — drop the minted intent so the message
                        # flows through as a normal turn (where a pending
                        # transition exists its own escape lane withdraws
                        # the proposal and processes the message; the
                        # engine can re-propose from fresher state).
                        gate_reply_refused = True
                        logger.info(
                            "Discarded classifier-minted intent "
                            f"{resolved_qi.type.value} for case "
                            f"{case.case_id}: substantive reply must not "
                            "commit a gate (INV-26, #721/fm#918)"
                        )
                    else:
                        intent = resolved_qi
                        intent_type = resolved_qi.type
                        intent_minted = True
                        logger.info(
                            f"Intent resolved from suggestions: {intent_type.value} "
                            f"for message: '{query[:50]}...'"
                        )
                except Exception:
                    logger.warning(
                        "Failed to parse resolved intent, "
                        "falling back to conversation",
                        exc_info=True,
                    )

        # Dispatch on the boot-validated routing table. ``intent_type``
        # is guaranteed to be present in ``_INTENT_DISPATCH`` because
        # _validate_intent_dispatch_completeness ran at service
        # construction and would have refused to start otherwise.
        # ── #1329: is this text-only CONVERSATION turn incident work at all? ──
        # Decided AFTER the typed-choice resolution and the greeting
        # heuristic above (a resolved or minted intent is never an aside),
        # never on a turn that carries an attachment or a structured
        # intent, never while a gate reply is pending, and never on a
        # terminal case (its Q&A path has its own cards and refuses new
        # data). The turn is already charged; what the verdict changes is
        # the route: an aside skips the engine and is recorded OUT_OF_BAND.
        #
        # ``gate_reply_refused`` carries the INV-26 guard's verdict here.
        # The guard refuses a mint precisely because the message IS a
        # substantive answer to a gate — so triaging it afterwards can
        # only get it wrong, and getting it wrong is expensive: an aside
        # verdict answers from a small prompt with no case context,
        # records ``TurnOutcome.OUT_OF_BAND``, and renders in later
        # prompts as an off-topic exchange, so the engine never learns the
        # user questioned its problem statement. #721's arm was exempt by
        # construction (it REQUIRED a pending transition, which this lane
        # already excludes); fm#918's Gate-1 arm is defined by the absence
        # of one, so the exemption has to be carried explicitly.
        #
        # It only ever ADDS coverage for the no-pending case, because a
        # pending row already excludes this lane one line up — and that
        # overlap is the point: the guard can refuse with a pending row
        # too (the two gate arms are an OR, not an if/else), and there the
        # conjunct is inert rather than wrong.
        #
        # It is NOT the same rule as ``pending_transition``, and the
        # difference is worth knowing: that one suppresses this lane on
        # EVERY turn while a gate is open, whereas this is turn-local —
        # it is a verdict the guard reached on THIS message, so it exists
        # only where the guard ran, which needs a live intent-bearing card
        # on offer. A Gate-1 case whose ``last_suggestions`` has since
        # been emptied (an aside or an orientation turn stores no
        # intent-bearing follow-up, so the next ``_stored_suggestions``
        # writes None) answers the gate with no exemption and is triaged.
        # That is pre-existing rather than introduced here, and narrowing
        # it would mean keying on ``_gate1_is_pending`` instead — which
        # suppresses the aside lane for a whole phase and is #1329's
        # design call, not this guard's.
        oob_kind: Optional[OutOfBandKind] = None
        if (
            intent_type == IntentType.CONVERSATION
            and intent is None
            and query
            and not payload.has_attachments
            and not case.is_terminal
            and not getattr(case, "pending_transition", None)
            and not gate_reply_refused
        ):
            oob_kind = await self.out_of_band_triage.triage(case, query, classification)
            if oob_kind is not None:
                user_message_obj["metadata"]["out_of_band"] = oob_kind.value
                logger.info(
                    "Out-of-band turn on case %s turn %s: %s (#1329)",
                    case_id,
                    next_turn,
                    oob_kind.value,
                )

        dispatch_kind = _INTENT_DISPATCH[intent_type]

        # Attachment metadata for the engine. Post-010: uploads create only
        # an UploadedFile (no auto-Evidence), so the file facts are sourced
        # directly from those rows — the #1201 fix: the row is the record of
        # what was submitted, its filename is not.
        #
        # Walks ``preprocess_results`` rather than ``uploaded_files_this_turn``
        # (the same set, in the same order — one result per attachment, and
        # that list is built from ``result.uploaded_file``) because the
        # result also carries ``duplicate_of``, the only place novelty is
        # still knowable by the time the engine runs (#1210).
        #
        # Built HERE, above the dispatch, rather than inside the ENGINE
        # branch: the SERVICE-routed handlers below delegate to the very
        # same ``engine.process_turn`` and used to hand it
        # ``attachments=None`` even though ``_preprocess_attachment`` had
        # already run and committed a row for every attachment on the turn.
        # An upload riding a suggestion-chip intent was persisted and
        # dedup-classified, and the engine was told nothing arrived (#1229).
        attachment_metadata = [
            _engine_attachment_metadata(res) for res in preprocess_results
        ]

        if oob_kind is not None and intent_type == IntentType.CONVERSATION:
            # #1329: an aside. Answered outside the investigation; the
            # message clock still advanced above, and the TurnProgress this
            # produces carries OUT_OF_BAND so nothing downstream counts it
            # as diagnostic work. Guarded on CONVERSATION so a heuristic or
            # resolved intent (a greeting, a typed choice) keeps its own
            # route even when triage had an opinion.
            result = await self._handle_out_of_band(
                case=case, user_message=query or "", kind=oob_kind
            )
        elif dispatch_kind == _IntentDispatchKind.NOT_IMPLEMENTED:
            # Intent value is defined in the IntentType enum (API
            # contract) but no handler exists in this build. Surface as
            # 422 with a clear message rather than a 500 — this is a
            # contract gap, not a server failure.
            raise ValidationException(
                f"Intent type '{intent_type.value}' is defined in the "
                "API but not implemented in this build. Either drop the "
                "enum value or add a handler in investigation_service.",
                {"intent_type": intent_type.value},
            )

        elif dispatch_kind == _IntentDispatchKind.SERVICE:
            # Service-level handlers — special-cased because each does
            # pre-LLM work (no LLM call, state mutation only, etc.)
            # and the handler signatures vary.
            if intent_type == IntentType.STATUS_TRANSITION:
                result = await self._handle_status_transition(
                    case=case,
                    user_message=query or "",
                    from_state=intent.from_state if intent else None,
                    to_state=intent.to_state if intent else None,
                    user_confirmed=(
                        (intent.user_confirmed or False) if intent else False
                    ),
                    user_id=user_id,
                    attachments=attachment_metadata or None,
                    typed=intent_minted,
                )
            elif intent_type == IntentType.CONFIRMATION:
                result = await self._handle_confirmation(
                    case=case,
                    user_message=query or "",
                    confirmation_value=(intent.confirmation_value if intent else None),
                    user_id=user_id,
                    attachments=attachment_metadata or None,
                    typed=intent_minted,
                )
            elif intent_type == IntentType.HYPOTHESIS_ACTION:
                result = await self._handle_hypothesis_action(
                    case=case,
                    user_message=query or "",
                    hypothesis_id=intent.hypothesis_id if intent else None,
                    action=intent.action if intent else None,
                    user_id=user_id,
                    attachments=attachment_metadata or None,
                )
            elif intent_type == IntentType.GREETING:
                result = await self._handle_greeting(
                    case=case,
                    attachments=attachment_metadata or None,
                    kind=orientation_kind or OrientationKind.GREETING,
                )
            elif intent_type == IntentType.FILE_RECLASSIFICATION:
                result = await _handle_file_reclassification(
                    self.file_storage_service,
                    self.preprocessing_service,
                    case=case,
                    file_id=intent.file_id if intent else None,
                    data_type_value=intent.data_type if intent else None,
                    attachments=attachment_metadata or None,
                )
            else:
                # Dispatch table claims SERVICE but there's no handler.
                # This is a developer error (added entry but not a
                # method); a 500 is correct here — the user did
                # nothing wrong.
                raise ServiceException(
                    f"Internal: SERVICE-routed intent "
                    f"'{intent_type.value}' has no handler method. "
                    "Update the if/elif chain in process_turn."
                )
        elif dispatch_kind == _IntentDispatchKind.ENGINE:
            # Engine-routed: thread intent_type + intent_data through
            # to ``engine.process_turn``, which dispatches internally
            # to the per-intent handler in milestone_engine.
            #
            # DA evidence search is handled inside MilestoneEngine's
            # tool loop. The same LLM that tracks hypotheses searches
            # evidence directly during generation — no pre-fetch or
            # separate gathering step needed.

            result = await self.engine.process_turn(
                case=case,
                user_message=query or "",
                attachments=attachment_metadata or None,
                intent_type=intent_type.value,
                intent_data={
                    **(intent.model_dump(exclude_unset=True) if intent else {}),
                    "query_mode": classification.mode.value,
                },
                # The turn's authenticated principal. Kept out of
                # ``intent_data`` deliberately: that dict is built from the
                # client-supplied intent payload, and the KB read allowlist
                # must not be keyed on anything a client can set.
                user_id=user_id,
            )
        else:
            # Defensive: _IntentDispatchKind only has three values
            # and NOT_IMPLEMENTED / SERVICE / ENGINE are all handled
            # above. A new dispatch kind without a corresponding
            # branch would land here — developer error, 500.
            raise ServiceException(
                f"Internal: Unknown dispatch kind '{dispatch_kind}' "
                f"for intent '{intent_type.value}'."
            )
        return attachment_metadata, intent_type, oob_kind, result

    @trace("investigation_service_get_progress")
    async def get_progress(self, case_id: str, user_id: str) -> Dict[str, Any]:
        """
        Get current investigation progress.

        Args:
            case_id: Case identifier
            user_id: User making the request

        Returns:
            Progress summary with:
            - case_id, status, current_stage
            - milestones_completed, pending_milestones
            - current_turn

        Raises:
            NotFoundError: If case not found
            PermissionDeniedException: If user not authorized
        """
        try:
            # Retrieve case
            case = await self.repository.get(case_id)
            if not case:
                raise NotFoundError("Case", case_id)

            # Check permissions
            if case.user_id != user_id:
                logger.warning(
                    f"User {user_id} denied access to case {case_id} (owner: {case.user_id})"
                )
                raise PermissionDeniedException(
                    f"User {user_id} not authorized for case {case_id}"
                )

            # Return progress summary
            return {
                "case_id": case.case_id,
                "state": case.state.value,
                "current_stage": (
                    case.current_stage.value if case.current_stage else None
                ),
                "milestones_completed": case.progress.completed_milestones,
                "pending_milestones": case.progress.pending_milestones,
                "current_turn": case.current_turn,
            }

        except (NotFoundError, PermissionDeniedException):
            raise
        except Exception as e:
            logger.error(f"Failed to get progress for case {case_id}: {e}")
            raise ServiceException(f"Progress retrieval failed: {str(e)}") from e

    # ============================================================
    # Attachment Preprocessing
    # ============================================================

    # ============================================================
    # Intent-Based Query Handlers
    # ============================================================

    async def _handle_status_transition(
        self,
        case: "Case",
        user_message: str,
        from_state: Optional[str],
        to_state: Optional[str],
        user_confirmed: bool,
        user_id: Optional[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        typed: bool = False,
    ) -> Dict[str, Any]:
        """Handle status transition intent with validation.

        Args:
            case: Case entity
            user_message: User's message explaining the transition
            from_state: Expected current status
            to_state: Requested new status
            user_confirmed: Whether user confirmed the transition
            user_id: Authenticated principal for the turn (keys the agent's
                KB read allowlist)
            attachments: The turn's engine attachment metadata. Passed through
                verbatim — this handler used to hardcode ``None`` while
                ``_preprocess_attachment`` had already committed a row for every
                attachment, so an upload riding a dropdown/chip intent was
                invisible to the engine (#1229).
            typed: True when the intent was minted from typed text by the
                intent resolver, not sent by a click. Passed to the engine as
                ``intent_data["typed"]``, which names how a terminal transition
                was confirmed (#1748); a clicked intent carries no such key.

        Returns:
            Result dict with agent response and updated case
        """
        logger.info(
            f"Processing status transition: {from_state} → {to_state} "
            f"(confirmed={user_confirmed}) for case {case.case_id}"
        )

        # Validate transition request
        if not to_state:
            raise ValidationException(
                "to_state is required for status_transition intent",
                {"field": "to_state"},
            )

        # Neither INVESTIGATING (#1608) nor RESOLVED (contract 9.0.0) is a
        # user-selectable case action: each is earned from case content — a
        # confirmed problem statement, a confirmed root-cause elimination — and
        # performed by a handshake the agent opens.
        #
        # Rejected HERE, at the boundary, for two reasons. It is a client-input
        # error, so it deserves a 422 rather than the 500 + ``Retry-After`` a
        # bare engine-side raise produces — and older clients will keep sending
        # it for as long as an extension takes to auto-update, so the wrong
        # shape would be told to retry a permanently invalid request and would
        # land in the error-rate SLO.
        #
        # Boundary placement also matters for correctness, not just status
        # codes. ``process_turn`` cancels a contradicting pending transition
        # and records the fm#1122 decline signature BEFORE it reaches the
        # per-target branches; raising from there unwinds without a save, so
        # the standing close offer survives with no decline recorded and the
        # engine re-fires it next turn — the exact re-nag fm#1122 exists to
        # prevent. Refusing before the engine runs touches no state at all.
        #
        # DERIVED from ``USER_SELECTABLE_ACTIONS`` rather than restated: the
        # two used to be independent facts that could disagree in either
        # direction with nothing failing.
        refusal = earned_edge_refusal(case.state, to_state)
        if refusal:
            raise ValidationException(refusal, {"field": "to_state", "value": to_state})

        # Delegate to milestone engine with structured intent
        result = await self.engine.process_turn(
            case=case,
            user_message=user_message,
            attachments=attachments,
            intent_type="status_transition",
            intent_data={
                "from_state": from_state,
                "to_state": to_state,
                "user_confirmed": user_confirmed,
                **({"typed": True} if typed else {}),
            },
            user_id=user_id,
        )

        return result

    async def _handle_confirmation(
        self,
        case: "Case",
        user_message: str,
        confirmation_value: Optional[bool],
        user_id: Optional[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        typed: bool = False,
    ) -> Dict[str, Any]:
        """Handle yes/no confirmation intent.

        Args:
            case: Case entity
            user_message: User's confirmation message
            confirmation_value: True for yes, False for no
            user_id: Authenticated principal for the turn (keys the agent's
                KB read allowlist)
            attachments: The turn's engine attachment metadata (see
                ``_handle_status_transition``; #1229).
            typed: The intent was minted from typed text, not clicked (see
                ``_handle_status_transition``; #1748).

        Returns:
            Result dict with agent response and updated case
        """
        logger.info(
            f"Processing confirmation: {confirmation_value} for case {case.case_id}"
        )

        result = await self.engine.process_turn(
            case=case,
            user_message=user_message,
            attachments=attachments,
            intent_type="confirmation",
            intent_data={
                "value": confirmation_value,
                **({"typed": True} if typed else {}),
            },
            user_id=user_id,
        )

        return result

    async def _handle_hypothesis_action(
        self,
        case: "Case",
        user_message: str,
        hypothesis_id: Optional[str],
        action: Optional[str],
        user_id: Optional[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Handle hypothesis action intent (validate/refute/retire).

        Args:
            case: Case entity
            user_message: User's message about the hypothesis
            hypothesis_id: Target hypothesis ID
            action: Action to perform
            user_id: Authenticated principal for the turn (keys the agent's
                KB read allowlist)
            attachments: The turn's engine attachment metadata (see
                ``_handle_status_transition``; #1229).

        Returns:
            Result dict with agent response and updated case
        """
        logger.info(
            f"Processing hypothesis action: {action} on {hypothesis_id} for case {case.case_id}"
        )

        if not hypothesis_id or not action:
            raise ValidationException(
                "hypothesis_id and action required for hypothesis_action intent",
                {"field": "hypothesis_id" if not hypothesis_id else "action"},
            )

        result = await self.engine.process_turn(
            case=case,
            user_message=user_message,
            attachments=attachments,
            intent_type="hypothesis_action",
            intent_data={"hypothesis_id": hypothesis_id, "action": action},
            user_id=user_id,
        )

        return result

    async def _handle_greeting(
        self,
        case: "Case",
        attachments: Optional[List[Dict[str, Any]]] = None,
        kind: OrientationKind = OrientationKind.GREETING,
    ) -> Dict[str, Any]:
        """Answer an orientation turn — greeting, "help", or an empty message —
        from the case's own state, without an LLM.

        The reply says where the investigation stands (state, stage, the last
        thing asked for) and what the user can do next; see
        ``orientation.build_orientation`` for the per-state wording. The
        intent is always server-minted (``detect_orientation``); a client-sent
        GREETING is re-derived in ``process_turn``.

        Args:
            case: Case entity
            attachments: The turn's engine attachment metadata (#1229). Always
                empty on this route now — the heuristic never fires on a turn
                that carried an attachment and the client-sent intent is no
                longer obeyed — but reported anyway: "normally empty" is not
                "provably empty", and a dropped signal is what #1229 is about.
            kind: Which orientation the text asked for.
        """
        logger.info(
            "Processing orientation (%s) for case %s in state %s",
            kind.value,
            case.case_id,
            case.state.value,
        )
        reply = build_orientation(case, kind)
        return {
            "agent_response": reply["agent_response"],
            "suggested_follow_ups": reply["suggested_follow_ups"],
            "case_updated": case,
            "metadata": {
                "progress_made": False,
                "milestones_completed": [],
                # Recorded as an aside (#1329): not investigation work, so it is
                # excluded from every investigative-turn count and hidden from
                # every history fidelity — the engine must not ground its next
                # turn on a recap of itself.
                "outcome": TurnOutcome.OUT_OF_BAND.value,
                "out_of_band": OUT_OF_BAND_MARKER,
                "orientation": kind.value,
                **report_turn_uploads(case.case_id, case.current_turn, attachments),
            },
        }

    async def _handle_out_of_band(
        self, case: "Case", user_message: str, kind: OutOfBandKind
    ) -> Dict[str, Any]:
        """Answer an aside without touching the investigation (#1329).

        Same result shape as ``_handle_greeting``: the turn is charged and the
        message clock has advanced, so the turn IS consumed and
        ``_backfill_consumed_turn`` records it — with ``OUT_OF_BAND`` as its
        outcome, which is what keeps it out of every investigative-turn count
        and out of every fidelity of the conversation history. The reply comes
        from a small fixed prompt on the synthesis role; the engine, the tools
        and the case context are never involved.
        """
        agent_response = await answer_out_of_band(
            self.engine.deps.llm_provider, case, user_message, kind
        )
        return {
            "agent_response": agent_response,
            # The chips have to agree with the reply. Offering "Back to: X" on a
            # case with no investigation history points the user at work that
            # never happened; the greeting lane's own wording is what belongs
            # there, and sharing it keeps one action from having two names.
            "suggested_follow_ups": [
                (
                    back_to_investigation_follow_up(case)
                    if has_investigation_history(case)
                    else describe_issue_follow_up()
                ),
                {"label": "Ask another question", "action_type": "FREE_SPEECH"},
            ],
            "case_updated": case,
            "metadata": {
                "progress_made": False,
                "milestones_completed": [],
                "outcome": TurnOutcome.OUT_OF_BAND.value,
                "out_of_band": kind.value,
                **report_turn_uploads(case.case_id, case.current_turn, None),
            },
        }

    @trace("investigation_service_reclassify_evidence")
    async def reclassify_evidence(
        self,
        case_id: str,
        evidence_id: str,
        user_id: str,
        data_type: DataType,
        trigger: str = "api",
        in_flight_case: Optional["Case"] = None,
    ) -> Evidence:
        """Re-run preprocessing on the file behind an existing evidence row
        under a user-specified data type.

        Phase 1.5 — implements the "escape hatch" for confident
        misclassification. The caller (PATCH endpoint or
        ``reclassify_evidence`` agent tool) provides the new data type;
        this method fetches the stored raw bytes, re-runs extraction
        under ``user_override=data_type``, and updates the **backing
        UploadedFile**'s preprocessing artifacts (``data_type``,
        ``summary``, ``structural_index``, and the coverage window) —
        these live with the file, not on Evidence. **Every** Evidence row
        backed by that file has its ``source_type`` re-aligned, not just
        the addressed one (#1470): the file has one classification, and
        how many claims cite it is not a property of the classification.
        The LLM-authored ``summary`` and ``extract`` fields on Evidence
        are left untouched (they are claim content, not preprocessing
        output).

        Args:
            case_id: Case owning the evidence.
            evidence_id: Evidence being reclassified.
            user_id: User making the request (authorisation check).
            data_type: Target data type (DataType enum value).
            trigger: Where the request came from — ``api`` (direct
                PATCH) or ``agent_tool`` (reclassify_evidence tool).
                Labels the observability counter.
            in_flight_case: The case aggregate a turn is currently holding,
                when this call is made from INSIDE that turn. Supplying it
                moves the write onto the turn's own object and hands
                persistence back to the turn; omitting it (the PATCH
                endpoint, which runs outside any turn) keeps the
                load-mutate-save this method has always done. See the
                WRITE MODEL note below — this parameter is #1465's fix.

        Returns:
            The ADDRESSED Evidence row, with the re-aligned
            ``source_type`` and the re-extraction's ``metadata``. Its
            siblings on the same file are re-aligned too but not
            returned — the caller asked about one row. The
            structural_index / summary / data_type / coverage updates
            land on the backing UploadedFile in the same case.

        Raises:
            NotFoundError: case or evidence not found. Mapped to HTTP 404
                by the global exception handler.
            AuthorizationError: user does not own the case. Mapped to
                HTTP 403.
            ValidationException: the case is TERMINAL. A closed or
                resolved investigation accepts questions, not mutation —
                the same refusal ``_handle_file_reclassification`` has
                always made. Mapped to HTTP 422, which
                ``PATCH /cases/{case_id}/evidence/{evidence_id}/classification``
                already publishes; this adds a condition under an existing
                code rather than a new code, so no contract bump is owed.
                Raised AFTER the evidence lookup, so a missing evidence id is
                still a 404 whatever state the case is in.
            ConflictError: evidence has no backing file —
                reclassification requires stored raw bytes to re-extract.
                Mapped to HTTP 409 with
                ``conflict_reason="no_backing_file"`` in the body so
                callers can branch programmatically.
            ServiceException: any other failure (storage fetch,
                preprocessing). Mapped to HTTP 500.

        WRITE MODEL (#1465).
            This method used to load-mutate-save unconditionally, which is
            correct for ``trigger="api"`` — the PATCH endpoint runs outside
            any turn and owns its write. On ``trigger="agent_tool"`` the LLM
            calls it from inside the engine's tool loop, and a turn ends with
            ONE aggregate save of the case object it has been holding since it
            started. That save has no idea a row was written underneath it, so
            it wrote the pre-reclassification aggregate back over this
            method's work: the file row, the Evidence row and the fm#918
            clarification drop alike. The model was told the reclassification
            succeeded and the case ended the turn as though it never happened.

            The defect is in the ORDERING, not in the values, so the fix is
            where the write lands rather than what it contains. A caller
            inside a turn passes the aggregate it is holding; this method
            applies the change to THAT object and does not save. The turn's
            existing end-of-turn save is then the write — one save per turn,
            which is the write model the engine already has.

            The rejected alternative is making the end-of-turn save merge
            rather than replace. It is worse on the same argument: the
            aggregate save is what makes a turn atomic, and a merging save
            would have to decide, field by field, whether a difference is a
            concurrent write to keep or a deliberate revert to apply — a
            question neither side of the merge carries the information to
            answer. It would also make every other in-turn write ambiguous to
            pay for one caller.

            What this does NOT close is the concurrency reach #1465 also
            names: a ``PATCH`` landing from another request while a turn is
            mid-flight is still a lost update, because the per-case lock lives
            in ``MilestoneEngine.process_turn`` and nothing outside a turn
            takes it. That needs a lock rather than a parameter and is a
            separate change.
        """
        if in_flight_case is not None:
            if in_flight_case.case_id != case_id:
                # A turn may only write its OWN case. Reaching another one
                # through the in-flight handle would write it without its
                # lock and then persist it on the wrong turn's save.
                raise ValidationException(
                    "in_flight_case does not belong to the addressed case",
                    {
                        "case_id": case_id,
                        "in_flight_case_id": in_flight_case.case_id,
                    },
                )
            case = in_flight_case
        else:
            case = await self.repository.get(case_id)
        if not case:
            raise NotFoundError("Case", case_id)
        if case.user_id != user_id:
            raise AuthorizationError(
                f"User {user_id} not authorized for case {case_id}"
            )

        evidence_index: Optional[int] = None
        for i, ev in enumerate(case.evidence or []):
            if ev.evidence_id == evidence_id:
                evidence_index = i
                break
        if evidence_index is None:
            raise NotFoundError("Evidence", evidence_id)

        evidence = case.evidence[evidence_index]
        # AFTER the evidence lookup, so a closed case answers 404 for an
        # evidence id that does not exist exactly as an open one does. Before
        # the lookup it answered 422 there, which made the case's state
        # decide what a missing row is called.
        # Terminal guard, matching ``_handle_file_reclassification`` — the
        # asymmetry between the two paths is closed here because THIS change
        # is what made it bite.
        #
        # The terminal short-circuit does not protect this method; it
        # short-circuits INTO a tool loop. ``_process_terminal_qa`` builds
        # ``_build_da_tool_schemas()`` — every registered tool, no name
        # filter, so ``reclassify_evidence`` is in the menu whenever the flag
        # is on — hands it a ``ToolContext`` carrying ``in_memory_case=case``,
        # and returns that same object as ``case_updated`` for ``process_turn``
        # to save. So on a CLOSED case the model can call the tool and the
        # terminal turn's own save commits the write.
        #
        # Before the in-flight write model above, that was accidentally
        # harmless: the tool wrote its own freshly-loaded copy and the
        # terminal turn's aggregate save overwrote it — the #1465 lost update
        # was protecting closed cases. Measured on ``origin/main``: the row
        # came back unchanged. Removing the lost update removes that
        # accident, so the guard the sibling handler has always had has to be
        # here too, or a case whose own handler documents "no state mutations"
        # becomes persistently mutable.
        if case.is_terminal:
            raise ValidationException(
                "Cannot reclassify evidence on a closed case — the "
                "investigation is terminal; only questions about the case "
                "are accepted.",
                {"case_state": case.state.value},
            )

        file_meta = case.find_uploaded_file(evidence.source_file_id)
        storage_ref = file_meta.storage_ref if file_meta else None
        if not storage_ref:
            raise ConflictError(
                f"Evidence {evidence_id} has no stored raw file — "
                "reclassification requires re-running the extractor "
                "over the original content, which is not available for "
                "evidence that was created without file storage.",
                resource_type="evidence",
                resource_id=evidence_id,
                conflict_reason="no_backing_file",
            )
        # storage_ref non-None (checked above) implies file_meta is present.
        preprocessing_result, new_source_type = await _reextract_under_override(
            self.file_storage_service,
            self.preprocessing_service,
            file_meta,
            data_type,
            previous_metadata=evidence.metadata,
        )

        # Lift the updated evidence_metadata block from the result.
        pp_metadata = preprocessing_result.extraction_metadata
        new_evidence_metadata: Optional[Dict[str, Any]] = None
        if isinstance(pp_metadata, dict):
            candidate = pp_metadata.get("evidence_metadata")
            if isinstance(candidate, dict):
                new_evidence_metadata = candidate

        previous_type = evidence.source_type.value
        new_type = preprocessing_result.data_type.value

        # Post-010 routing: preprocessing artifacts (data_type, summary,
        # structural_index) describe the FILE and land on
        # ``uploaded_files``. Evidence carries the LLM's claim — we only
        # re-align ``source_type`` so the agent sees consistent data on
        # the next turn. The LLM-authored ``summary`` and ``extract``
        # fields on Evidence are left untouched: they are claim content,
        # not preprocessing output.
        #
        # Through the shared seam (#1470) so EVERY Evidence row backed by
        # this file is re-aligned, not just the addressed one. The file has
        # one classification; how many claims cite it is not a property of
        # the classification, and leaving the siblings behind described one
        # file with two contradictory source types.
        (
            new_files_list,
            new_evidence_list,
            dropped_suggestions,
        ) = _reclassified_collections(
            case, evidence.source_file_id, preprocessing_result, new_source_type
        )

        # The ADDRESSED row additionally takes the re-extraction's
        # ``evidence_metadata`` block — the classification confidence and the
        # extractor-attempt trail for the request that was made about THIS
        # row. Deliberately not fanned out to the siblings: the trail records
        # what was asked of this evidence, and stamping a request nobody made
        # onto a neighbouring row would make the observability trail lie.
        #
        # CONDITIONAL, because this REPLACES the metadata the seam merged
        # rather than adding to it. Unconditional, a result carrying no
        # ``evidence_metadata`` set the addressed row's metadata to ``None``
        # while its siblings kept the merged verdict — one file, two answers,
        # which is the shape #1470 exists to close. Latent today because
        # ``_build_result`` always writes the block; latent in the one row the
        # caller actually asked about.
        #
        # Deep-copied for the reason the seam deep-copies: this block IS
        # ``preprocessing_result.extraction_metadata["evidence_metadata"]``,
        # and ``model_copy(deep=True)`` does not copy ``update`` values — so
        # without this the persisted row holds a live reference into the
        # preprocessing result.
        if new_evidence_metadata is not None:
            updated_evidence = new_evidence_list[evidence_index].model_copy(
                update={"metadata": copy.deepcopy(new_evidence_metadata)},
                deep=True,
            )
            new_evidence_list[evidence_index] = updated_evidence
        else:
            updated_evidence = new_evidence_list[evidence_index]

        # fm#918 exposure 1: this path writes ``UploadedFile.data_type``
        # without going through the turn seam, so nothing else rewrites
        # ``last_suggestions`` and the file's clarification choices stay
        # armed for the TYPED arm — the resolver reads this list, and
        # this is what empties it. A DECIDE **click** is not covered
        # and is not meant to be: a click carries its intent on the
        # request and never consults this list
        # (``suggestion_is_live``), so a card the client still shows
        # can still be clicked and still reaches the same end state.
        # That is consent rather than inference, which is the whole
        # distinction INV-26 rests on — but it does mean "exposure 1
        # is closed" is true of typing and not of clicking. Raised on
        # fm#918 rather than decided here.
        #
        # Answering the question here is exact; the referent
        # check in ``suggestion_is_live`` compares the stored value, and
        # on a row written before #583 that was the 12→6 projection,
        # which cannot see a reclassification WITHIN a source type
        # (logs_and_errors → command_output, both ``logs``) — how a
        # typed "Application logs (x.log)" on the next turn overwrote
        # the answer the user had just given here.
        #
        # Now true of BOTH triggers. It used to be true of ``api`` only:
        # on ``trigger="agent_tool"`` this whole write — the file row, the
        # evidence rows and this drop alike — was clobbered by the
        # end-of-turn aggregate save of the case the turn was holding
        # (#1465). The write model below is what closed that. The drop itself
        # now comes back from the seam with the collections.

        if in_flight_case is not None:
            # IN-TURN (#1465). The caller is holding this exact object and
            # will save it at the end of the turn, so the change is applied
            # IN PLACE and nothing is persisted here. A ``model_copy`` would
            # be the bug: the copy is not what the turn is holding, so the
            # aggregate save would write the unmodified original back.
            #
            # Three whole-field assignments, no in-place list mutation: the
            # lists above are new lists built from the case's own, so this
            # leaves any list the caller may still be holding alone.
            case.uploaded_files = new_files_list
            case.evidence = new_evidence_list
            case.last_suggestions = dropped_suggestions
        else:
            # OUT OF BAND (the PATCH endpoint). No turn owns this write, so
            # this method does — load-mutate-save, exactly as before.
            updated_case = case.model_copy(
                update={
                    "evidence": new_evidence_list,
                    "uploaded_files": new_files_list,
                    "last_suggestions": dropped_suggestions,
                },
                deep=True,
            )
            await self.repository.save(updated_case)

        # WHAT THIS COUNTS, per trigger, because the answer is no longer the
        # same for all three and reading it wrong misreads the graph.
        #
        # ``api`` counts a COMMITTED reclassification — this method owns the
        # save and it has already run above. ``agent_tool`` counts an APPLIED
        # one: the write is on the turn's aggregate and the turn's own save
        # commits it, so a turn that dies after this line leaves a count with
        # no row behind it.
        #
        # That is deliberate rather than overlooked, and it is what makes the
        # labels comparable: ``clarification`` (the turn-seam sibling) has
        # always counted applications for exactly the same reason — it hands
        # its case back for ``process_turn`` to save and increments before
        # that happens. Aligning ``agent_tool`` with the other TURN-BORNE
        # trigger, rather than with the out-of-band one it no longer
        # resembles, keeps "reclassifications this deployment performed" a
        # sum worth taking. A strictly committed counter needs the count to
        # move to whoever owns persistence; noted on the PR as follow-up.
        EVIDENCE_RECLASSIFICATION_TOTAL.labels(
            from_type=str(previous_type or "unknown"),
            to_type=new_type,
            trigger=trigger,
        ).inc()

        logger.info(
            "Reclassified evidence %s in case %s: %s -> %s (trigger=%s)",
            evidence_id,
            case_id,
            previous_type,
            new_type,
            trigger,
        )

        return updated_evidence
