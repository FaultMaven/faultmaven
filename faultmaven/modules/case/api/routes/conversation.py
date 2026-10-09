"""Conversation and turn-submission routes (fm#1707).

The message/turn surface of a case: the enhanced message history read,
session resume, the unified turn endpoint (query + files + pasted content),
the two 410-Gone legacy stubs the turn endpoint replaced, and evidence
reclassification. Split out of ``case/api/routes.py`` as its own sub-router
(A6); ``dependencies.py`` holds the DI accessors and parsing helpers every
handler here uses.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import ValidationError

from faultmaven.api.exception_handlers import llm_service_error_http_exception
from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.api.v1.dependencies import get_investigation_service
from faultmaven.config.idempotency_key import (
    IDEMPOTENCY_KEY_MAX_LENGTH,
    IDEMPOTENCY_KEY_MIN_LENGTH,
    IDEMPOTENCY_KEY_PATTERN,
    IDEMPOTENCY_KEY_REUSE,
    IDEMPOTENCY_REPLAYED_HEADER,
)
from faultmaven.config.turn_ceiling import resolve_turn_ceiling
from faultmaven.core.investigation.schemas import Attachment, TurnPayload
from faultmaven.core.investigation.turn_budget import (
    TurnDeadlineExceeded,
    bind_turn_deadline,
)
from faultmaven.exceptions import (
    CASE_TERMINAL,
    NotFoundError,
    PermissionDeniedException,
    ServiceException,
    SessionException,
    ValidationException,
)
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.infrastructure.protection.tenant_turn_cap import (
    TenantTurnCapExceeded,
    TenantTurnCapUnavailable,
)
from faultmaven.models.api import CaseMessagesResponse, DataType
from faultmaven.models.api_models import IntentType, QueryIntent, TurnResponse
from faultmaven.models.interfaces_case import ICaseService
from faultmaven.modules.auth.contracts import ISessionService, UserDTO
from faultmaven.modules.case.api.routes.dependencies import (
    _di_get_case_service_dependency,
    _di_get_session_service_dependency,
    _parse_observed_at,
    check_case_service_available,
    resolve_paste_source_meta,
)
from faultmaven.modules.case.api.title_generation import _auto_title_case_if_default
from faultmaven.modules.case.api.turn_idempotency import (
    IDEMPOTENCY_REPLAY_UNAVAILABLE,
    TURN_IN_PROGRESS,
    KeyedTurn,
    open_keyed_turn,
    replay_committed_turn,
    request_fingerprint,
)
from faultmaven.modules.case.contracts import TurnReceiptExistsError
from faultmaven.modules.case.exceptions import StaleCaseException

router = APIRouter(prefix="/cases", tags=["cases"])


logger = logging.getLogger(__name__)


# Conversation thread retrieval (messages)


@router.get(
    "/{case_id}/messages",
    response_model=CaseMessagesResponse,
    dependencies=[Depends(require_authentication)],
)
@trace("api_get_case_messages_enhanced")
async def get_case_messages_enhanced(
    case_id: str,
    response: Response,
    limit: int = Query(
        50, le=100, ge=1, description="Maximum number of messages to return"
    ),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    include_debug: bool = Query(
        False, description="Include debug information for troubleshooting"
    ),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: UserDTO = Depends(require_authentication),
) -> CaseMessagesResponse:
    """
    Retrieve conversation messages for a case with enhanced debugging info.
    Supports pagination and includes metadata about message retrieval status.
    """
    case_service = check_case_service_available(case_service)
    correlation_id = str(uuid.uuid4())
    response.headers["x-correlation-id"] = correlation_id

    try:
        # Verify user has access to the case
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Case not found or access denied",
            )

        # Use the enhanced message retrieval method
        message_response = await case_service.get_case_messages_enhanced(
            case_id=case_id, limit=limit, offset=offset, include_debug=include_debug
        )

        # Add headers for metadata. X-Total-Count is the canonical pagination
        # header used by every other list endpoint (and expected by the contract
        # probe / any generic paginating client); X-Message-Count is kept for
        # backward compatibility.
        response.headers["X-Total-Count"] = str(message_response.total_count)
        response.headers["X-Message-Count"] = str(message_response.total_count)
        response.headers["X-Retrieved-Count"] = str(message_response.retrieved_count)

        # Determine storage status
        storage_status = "success"
        if message_response.debug_info and message_response.debug_info.storage_errors:
            storage_status = (
                "error" if message_response.retrieved_count == 0 else "partial"
            )
        response.headers["X-Storage-Status"] = storage_status

        return message_response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Unexpected error in get_case_messages_enhanced: {e}",
            extra={"correlation_id": correlation_id},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get messages",
            headers={"x-correlation-id": correlation_id},
        )


@router.post(
    "/sessions/{session_id}/resume/{case_id}",
    response_model=Dict[str, Any],
    dependencies=[Depends(require_authentication)],
)
@trace("api_resume_case_in_session")
async def resume_case_in_session(
    session_id: str,
    case_id: str,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    session_service: ISessionService = Depends(_di_get_session_service_dependency),
    current_user: UserDTO = Depends(require_authentication),
) -> Dict[str, Any]:
    """
    Resume an existing case in a session

    Links the session to an existing case, allowing the user to continue
    a previous troubleshooting conversation.
    """
    case_service = check_case_service_available(case_service)

    try:
        # TWO gates, because this route names two resources (#1390, #1393).
        # `faultmaven/api/routes/sessions.py` states the rule its own surface
        # follows — the case gate "covers the case named in the path and
        # nothing else … Both halves are needed; neither is sufficient" — and
        # this route had neither.
        #
        # ── 1. The case ──────────────────────────────────────────────────
        # Enforced in BOTH places, deliberately (#1398).
        #
        # The authoritative gate is inside `link_session_to_case`: it is on
        # `ICaseService`, reachable by another route, the Slack agent or an
        # `fm-*` CLI, and a gate that lives only in this handler protects only
        # this handler. That one also folds in the existence check, so the
        # member asks one question of one load.
        #
        # This early check is a SECOND resolution of the same row, and it is
        # kept for ordering rather than for safety. The session check below
        # cannot always be evaluated — where no session store is configured it
        # answers 503 — and if it ran first it would answer BOTH parties the
        # same way, which is how `test_two_enterprise_surface_probe` stops
        # exercising the case boundary on this route at all ("a parametrisation
        # that never reaches the tenant check asserts nothing"). Refusing here
        # keeps the cross-tenant refusal attributable to the case, and makes it
        # cheap: no session lookup for a caller who was never going to pass.
        #
        # A case the caller cannot reach raises `NotFoundError` from the
        # service, which the app's handler answers as 404; a link that FAILS
        # returns False and is a 500 below. Keeping those apart is the point —
        # conflating them reported a working resume as an absence (#1390).
        #
        # Owner ∪ shared-to-my-teams, matching `submit_turn` and the service's
        # own gate. That is a deliberate departure from the method-based rule
        # in `sessions.py`, which picks `owner_only` from the HTTP method
        # because "a share grants read visibility, not the right to write"
        # (ADR-017 D4) — and this is a POST that writes `cases.last_activity`
        # through `update_activity_timestamp`.
        #
        # The exception is bounded: a teammate who may POST a turn into a
        # shared case already writes messages, turn history AND that same
        # activity stamp, so refusing the resume while admitting the turn would
        # leave the extension able to read and write a case it cannot open. The
        # only row this path touches that a read share does not already cover
        # is `last_activity`, which is bookkeeping about access rather than
        # case content, and the teammate's own turn bumps it moments later.
        case = await case_service.get_case(case_id, current_user.user_id)
        if case is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Case not found or resume not permitted",
            )

        # ── 2. The session ───────────────────────────────────────────────
        # Without this, naming SOMEONE ELSE'S session id retargets their
        # `session:{id}:current_case_id` pointer at a case of the caller's
        # choosing: the owner's next turn either lands in the caller's case (if
        # they can reach it) or silently abandons the case they were working —
        # and the write lands before any of this route's failure paths, so the
        # pre-fix 404 never prevented it.
        #
        # Ordered AFTER the case gate on purpose. Both answer 404, so neither
        # ordering leaks anything; but the two-enterprise probe drives this
        # route with a session id belonging to nobody, so checking the session
        # first would refuse BOTH parties there and the case half would stop
        # being exercised — the "parametrisation that never reaches the tenant
        # check asserts nothing" failure that probe's own docstring warns about.
        #
        # `get_session` RAISES rather than returning None when it cannot
        # answer — `ServiceException("Session store not configured")` is the
        # shipped case. Treating it as a nullable return let that escape to the
        # handler's bare `except` and answer 500 on a request the server simply
        # could not evaluate. An unevaluable gate is a 503, which is the rule
        # `faultmaven/api/routes/sessions.py` already states for its own:
        # "503 if the case service is unavailable (the gate cannot be
        # evaluated, so nothing is served)".
        try:
            session = await session_service.get_session(session_id, validate=True)
        except (ServiceException, SessionException) as exc:
            # BOTH families. `ServiceException("Session store not configured")`
            # is the unconfigured case; a store that IS configured and
            # unreachable raises `SessionStoreException`, which descends from
            # `SessionException` and NOT from `ServiceException`. Catching one
            # made two spellings of "the gate could not be evaluated" answer
            # 503 and 500 respectively — the inconsistency this is fixing.
            logger.warning(f"Cannot evaluate session ownership for {session_id}: {exc}")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Session service unavailable",
            )

        if session is None or session.user_id != current_user.user_id:
            # One answer for "no such session" and "not yours": naming
            # another user's session must not be distinguishable from naming
            # one that does not exist. 404 rather than 401 — the caller IS
            # authenticated, and 401 would tell a client to re-authenticate
            # for a request that will never succeed.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Session not found or resume not permitted",
            )

        success = await case_service.resume_case_in_session(
            case_id, session_id, current_user.user_id
        )

        if not success:
            # NOT a 404. A case the caller cannot reach raised `NotFoundError`
            # inside the service and never got here, and the session is theirs
            # — so a falsy result is the link itself failing (a repository
            # error, a session store that refused the write), the server's
            # problem.
            # Reporting it as "not found or not permitted" is the same
            # half-success-as-absence shape this endpoint was fixed for: the
            # client abandons a case that is fine instead of retrying.
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to resume case",
            )

        return {
            "case_id": case_id,
            "success": True,
            "message": "Case resumed in session",
        }

    except HTTPException:
        raise
    except NotFoundError:
        # The access verdict from `link_session_to_case`. Re-raised so the
        # app's handler answers 404; the bare handler below would make it a
        # 500 and tell the caller the server broke on a request it was simply
        # not allowed to make.
        raise
    except ValidationException as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to resume case: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to resume case (unexpected error)",
        )


#: The turn route's documented non-2xx answers that a client branches on by
#: ``x-error-code`` (#1888). Declared so the contract describes what the route
#: does: an undocumented response is invisible to every client generator and
#: to every contract differ.
_TURN_RESPONSES: Dict[int | str, Dict[str, Any]] = {
    200: {
        "description": (
            "The turn's response. A retry of a committed turn under the same "
            f"`Idempotency-Key` is answered with that turn's response, and "
            f"carries `{IDEMPOTENCY_REPLAYED_HEADER}: true`."
        ),
        "headers": {
            IDEMPOTENCY_REPLAYED_HEADER: {
                "description": (
                    "`true` when this response replays a turn that already "
                    "committed under this `Idempotency-Key`; absent otherwise."
                ),
                "schema": {"type": "string", "enum": ["true"]},
            }
        },
    },
    409: {
        "description": (
            "Conflict. Told apart by `x-error-code`: "
            f"`{TURN_IN_PROGRESS}` (a turn with this `Idempotency-Key` is still "
            "running: retry with the same key after `Retry-After` seconds, the "
            "longest the running turn can still hold its claim — an upper "
            "bound, not when it finishes; it may finish sooner); "
            f"`{IDEMPOTENCY_KEY_REUSE}` (the key was used for a different "
            "turn); "
            f"`{IDEMPOTENCY_REPLAY_UNAVAILABLE}` (the turn committed but its "
            "response can no longer be replayed: reload the case); "
            "`CASE_VERSION_CONFLICT` (another writer changed the case while "
            "this turn ran; nothing committed); "
            f"`{CASE_TERMINAL}` (the case is resolved or closed and refuses "
            "new data, a status change or a file reclassification; a "
            "text-only question is still answered)."
        ),
        "headers": {
            "x-error-code": {
                "description": "Which conflict.",
                "schema": {
                    "type": "string",
                    "enum": [
                        TURN_IN_PROGRESS,
                        IDEMPOTENCY_KEY_REUSE,
                        IDEMPOTENCY_REPLAY_UNAVAILABLE,
                        "CASE_VERSION_CONFLICT",
                        CASE_TERMINAL,
                    ],
                },
            },
            "Retry-After": {
                "description": (
                    f"Seconds, on `{TURN_IN_PROGRESS}` only: the longest the "
                    "running turn can still hold its claim (an upper bound, "
                    "not when it finishes)."
                ),
                "schema": {"type": "integer"},
            },
        },
    },
    504: {
        "description": (
            "Timeout; nothing of the turn committed. Told apart by "
            "`x-error-code`: `REQUEST_TIMEOUT` (the turn used its whole ceiling, "
            "`limits.turnCeilingSeconds` on `GET /api/v1/meta/capabilities`, on "
            "this input; the same input is likely to exhaust it again, so a "
            "client retries at most once, and no `Retry-After` is sent); "
            "`LLM_TIMEOUT` (the AI provider timed out: transient, retry after "
            "`Retry-After` seconds)."
        ),
        "headers": {
            "x-error-code": {
                "description": "Which timeout.",
                "schema": {
                    "type": "string",
                    "enum": ["REQUEST_TIMEOUT", "LLM_TIMEOUT"],
                },
            },
            "Retry-After": {
                "description": "Seconds, on `LLM_TIMEOUT` only.",
                "schema": {"type": "integer"},
            },
        },
    },
}


@router.post(
    "/{case_id}/turns",
    response_model=TurnResponse,
    responses=_TURN_RESPONSES,
    dependencies=[Depends(require_authentication)],
)
@trace("api_submit_turn")
async def submit_turn(
    case_id: str,
    request: Request,
    query: Optional[str] = Form(None),
    # One file per turn (fm#694), enforced by the framework rather than by hand:
    # `max_length=1` both rejects a second file with the app's normalized 422
    # AND publishes `maxItems: 1` into the OpenAPI schema, so the narrowing is
    # visible to clients and to scripts/check_contract_version.py. A hand-rolled
    # `len(files) > 1` raise enforced the same rule invisibly.
    #
    # The cap is on `files` ALONE. `pasted_content` is a separate form field
    # that legitimately rides alongside a file as a second attachment, and that
    # is a shipped path — counting it here would break paste+file turns.
    #
    # One thing this does NOT do, stated so nobody reads more into it: it is
    # correctness-only, not a cost control. Starlette parses and spools the
    # ENTIRE multipart body (main.py patches Request.form with max_files=1000)
    # before pydantic validates, so a 50-file request is fully read off the
    # wire and written to temp files before the 422.
    #
    # It is also not what bounds the clarification emitter. A one-file turn
    # still carries two attachments when a paste rides along, and both can
    # fail classification; the emitter clarifies every failure rather than
    # reasoning from this cap (#1222).
    files: List[UploadFile] = File(
        default=[],
        max_length=1,
        description=(
            "At most ONE file per turn. Submit each additional file as its own "
            "turn. `pasted_content` is a separate field and does not count "
            "toward this limit, so a turn may carry one file *and* a paste."
        ),
    ),
    pasted_content: Optional[str] = Form(None),
    intent_type: Optional[str] = Form(None),
    intent_data: Optional[str] = Form(None),
    input_type: Optional[str] = Form(None),
    source_url: Optional[str] = Form(None),
    observed_at: Optional[str] = Form(None),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    investigation_service=Depends(get_investigation_service),
    current_user: UserDTO = Depends(require_authentication),
    # Injected by FastAPI; the default only serves direct calls.
    http_response: Response = None,
    # ``Annotated`` so the Python default is a real ``None`` for direct calls.
    # The grammar is the one every layer reads (``config.idempotency_key``), so a
    # bad key is a published 422 here.
    idempotency_key: Annotated[
        Optional[str],
        Header(
            alias="Idempotency-Key",
            min_length=IDEMPOTENCY_KEY_MIN_LENGTH,
            max_length=IDEMPOTENCY_KEY_MAX_LENGTH,
            pattern=IDEMPOTENCY_KEY_PATTERN,
            description=(
                "Optional. Identifies this turn across retries: a retry under "
                "the same key returns the committed turn instead of running it "
                "again. Stable per turn (the client's message id), new for "
                "every new turn."
            ),
        ),
    ] = None,
) -> TurnResponse:
    """Submit a turn to a case investigation.

    A turn consists of an optional query and/or optional attachments.
    Attachments are preprocessed through Tier 0+1 before the LLM sees them.
    If no query is provided with attachments, an implicit query is generated.

    **One file per turn.** `files` accepts at most one item (`maxItems: 1`);
    more than one is rejected with 422. Submit each additional file as its own
    turn. `pasted_content` is a separate field and does not count against this
    limit, so a turn may legitimately carry one file *and* a paste.

    **Auto-titling:** a case still carrying its auto-generated `Case-YYMMDD-N`
    placeholder is named from its own content as part of processing the turn, if
    it now has enough substance to name — so the name is already in place when
    this responds. Clients do not need to call `POST /cases/{case_id}/title` for
    this; that endpoint remains for user-initiated (re)naming. A case is
    auto-titled at most once — the moment a real title lands, later turns leave
    it alone. Naming is best-effort and time-bounded: it can never fail or
    delay the turn itself.

    **Retries (`Idempotency-Key`).** A turn sent with an `Idempotency-Key`
    commits a receipt with the turn, in the same transaction. A request with a
    key this caller already used on this case:

    - for the same turn (same fields, same file content), once it committed →
      **200** with that turn's response, unchanged, and
      `X-Idempotency-Replayed: true`. Nothing runs and nothing is charged.
    - for a different turn → **409** `x-error-code: IDEMPOTENCY_KEY_REUSE`.
    - while the first is still running → **409** `x-error-code:
      TURN_IN_PROGRESS` with `Retry-After`; retry with the same key after it.
      `Retry-After` is the longest the first can still hold its claim, an upper
      bound, not when it finishes.

    A response lost after the commit (a disconnect, a timeout on the client's
    side) is recovered by retrying with the same key. Without a key, every
    request runs as a new turn.

    **Timing.** No turn is answered later than `limits.turnResponseBoundSeconds`
    on `GET /api/v1/meta/capabilities` (the turn ceiling plus the commit and
    auto-title steps after it); size a client timeout as that plus a network
    margin, and re-read it per session, because an operator can switch the chat
    provider and with it the ceiling. A **504** commits nothing:
    `REQUEST_TIMEOUT` means the turn used its whole ceiling on this input and is
    likely to do so again, so retry at most once (it carries no `Retry-After`);
    `LLM_TIMEOUT` is a transient provider timeout, retried after `Retry-After`.
    """
    import json

    case_service = check_case_service_available(case_service)
    correlation_id = str(uuid.uuid4())
    # The keyed turn's claim, held from the idempotency step until the turn is
    # fully answered (released in ``finally``, #1888).
    keyed: Optional[KeyedTurn] = None

    try:
        # An EMPTY turn — no query, no file, no paste — is accepted: it is a
        # bare @mention in Slack, and the service answers it with a state-aware
        # orientation (where the case stands, what to do next) rather than the
        # 400 the client used to have to swallow.

        # Validate case_id
        if not case_id or case_id.strip() in ("", "undefined", "null"):
            raise HTTPException(
                status_code=400,
                detail="Valid case_id is required",
                headers={"x-correlation-id": correlation_id},
            )

        # Size cap on text-shaped form fields. Multipart files are bounded by
        # Starlette via `_upload_max_bytes`, but `query` and `pasted_content`
        # are raw form fields — without this guard a 50MB paste would be
        # accepted, decoded, classified, and copied through the pipeline
        # before any extractor's own cap fired. Trivial DoS surface otherwise.
        from faultmaven.config.settings import get_settings as _get_settings

        _max_text_bytes = _get_settings().upload.max_upload_size_mb * 1024 * 1024
        if pasted_content and len(pasted_content.encode("utf-8")) > _max_text_bytes:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"pasted_content exceeds the {_max_text_bytes // (1024 * 1024)}MB "
                    f"limit. Upload as a file instead."
                ),
                headers={"x-correlation-id": correlation_id},
            )
        if query and len(query.encode("utf-8")) > _max_text_bytes:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"query exceeds the {_max_text_bytes // (1024 * 1024)}MB limit."
                ),
                headers={"x-correlation-id": correlation_id},
            )

        # Per-attachment size cap. Starlette >= 1.1 bounds only non-file form
        # fields via max_part_size (see main.py); file parts reach the route
        # unbounded, so the same MAX_UPLOAD_SIZE_MB limit is enforced here.
        for upload in files:
            if upload.size is not None and upload.size > _max_text_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"{upload.filename or 'attachment'} exceeds the "
                        f"{_max_text_bytes // (1024 * 1024)}MB upload limit."
                    ),
                    headers={"x-correlation-id": correlation_id},
                )

        # Verify case exists and user has access
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(
                status_code=404,
                detail="Case not found or access denied",
                headers={"x-correlation-id": correlation_id},
            )

        # Each file's bytes, read ONCE: the idempotency step fingerprints them
        # and the attachment build below reuses them (#1888). A fingerprint of
        # ``UploadFile`` metadata alone would let two different files of one
        # name and size share a key.
        file_contents = [(f, await f.read()) for f in files]

        # The turn's ceiling and response bound, resolved ONCE for the chat
        # provider in force (#1905): the in-flight claim, the deadline and the
        # timeout's log line all read the same numbers, even if an operator
        # switches the provider while this turn runs.
        from faultmaven.config.settings import get_settings

        turn_ceiling = resolve_turn_ceiling(get_settings())

        # The idempotency step (#1888): after the case lookup (a case the
        # caller cannot see stays a 404) and BEFORE the terminal-case gates,
        # so a retried closing turn replays instead of meeting the closed case
        # its own first attempt made. Order and rationale:
        # ``modules/case/api/turn_idempotency.py``.
        if idempotency_key:
            keyed = await open_keyed_turn(
                redis=getattr(request.app.state, "redis_client", None),
                case=case,
                author_id=current_user.user_id,
                idempotency_key=idempotency_key,
                fingerprint=request_fingerprint(
                    query=query,
                    pasted_content=pasted_content,
                    intent_type=intent_type,
                    intent_data=intent_data,
                    input_type=input_type,
                    source_url=source_url,
                    observed_at=observed_at,
                    files=[(f.filename or "", content) for f, content in file_contents],
                ),
                case_service=case_service,
                response_bound_seconds=turn_ceiling.response_bound_seconds,
                correlation_id=correlation_id,
            )
            if keyed.replay is not None:
                if http_response is not None:
                    http_response.headers[IDEMPOTENCY_REPLAYED_HEADER] = "true"
                return keyed.replay

        # Terminal cases: allow text-only Q&A, block evidence and state
        # transitions. Each refusal is labelled `CASE_TERMINAL` (#1907).
        if case.is_terminal:
            if files or pasted_content:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Cannot submit new data to a closed case. Only questions about the case are allowed.",
                    headers={
                        "x-correlation-id": correlation_id,
                        "x-error-code": CASE_TERMINAL,
                    },
                )
            if intent_type == "status_transition":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Cannot change status of a closed case.",
                    headers={
                        "x-correlation-id": correlation_id,
                        "x-error-code": CASE_TERMINAL,
                    },
                )
            if intent_type == "file_reclassification":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Cannot reclassify files on a closed case.",
                    headers={
                        "x-correlation-id": correlation_id,
                        "x-error-code": CASE_TERMINAL,
                    },
                )

        # Build attachments list
        # Every attachment carries source_metadata so the classifier knows the
        # input origin and can apply the correct confidence boosts:
        #   file_upload  → user selected a local OS file
        #   text_paste   → user pasted raw text into the scratchpad
        #   page_capture → browser extension captured a web page (has source URL)
        attachments = []
        for f, content in file_contents:
            attachments.append(
                Attachment(
                    content=content,
                    filename=f.filename or "unnamed_file",
                    content_type=f.content_type or "application/octet-stream",
                    source_metadata={"source_type": "file_upload"},
                )
            )
        if pasted_content:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

            # When the caller says the content was OBSERVED. Distinct from the
            # `ts` above, which is ingestion time and is all the synthetic
            # filename can ever carry. A caller forwarding something it did not
            # just witness (the Slack agent relaying an alert posted hours ago)
            # is the only party that knows this, so an unparseable value is
            # dropped to None rather than guessed at: unknown is honest, and
            # "now" would be the exact false claim this field exists to stop.
            observed_at_dt = _parse_observed_at(observed_at, correlation_id)

            # Determine source type from the explicit `input_type` form field.
            # The frontend (UnifiedInputBar.tsx) always sets this since the
            # text_paste pathway shipped — see faultmaven-copilot:
            # src/shared/ui/components/UnifiedInputBar.tsx:226-256.
            #
            # The legacy `--- Page Content (URL) ---` body header is no
            # longer recognized as a page-capture signal: it was a write-
            # around (a paste shaped that way bypassed Tier-1 extraction by
            # entering the page-capture passthrough), and the explicit
            # `input_type` field has fully replaced it.
            source_meta, filename_prefix = resolve_paste_source_meta(
                input_type, source_url
            )
            filename = f"{filename_prefix}{ts}.txt"

            attachments.append(
                Attachment(
                    content=pasted_content.encode("utf-8"),
                    filename=filename,
                    content_type="text/plain",
                    source_metadata=source_meta,
                    observed_at=observed_at_dt,
                )
            )

        # Build intent
        intent = None
        if intent_type:
            data = json.loads(intent_data) if intent_data else {}
            # Remove 'type' from data to avoid conflict with explicit type= arg
            data.pop("type", None)
            # Defensive: a malformed intent from a client must not crash the turn
            # with an unhandled error -> 500. Two failure modes, both -> 422:
            #   (a) an unrecognized intent_type (e.g. a suggestion action_type
            #       like 'free_speech') -> IntentType() ValueError;
            #   (b) a valid intent_type with invalid/missing fields (e.g. a
            #       'status_transition' without to_state) -> QueryIntent
            #       ValidationError.
            try:
                parsed_intent_type = IntentType(intent_type)
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Unknown intent_type {intent_type!r}. Valid values: "
                        + ", ".join(t.value for t in IntentType)
                        + "."
                    ),
                    headers={"x-correlation-id": correlation_id},
                )
            try:
                intent = QueryIntent(type=parsed_intent_type, **data)
            except (ValidationError, ValueError) as e:
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid fields for intent_type={intent_type!r}: {e}",
                    headers={"x-correlation-id": correlation_id},
                )

        payload = TurnPayload(query=query, attachments=attachments, intent=intent)

        # Process turn with configurable timeout (provider-aware — see ISS-058).
        try:
            agent_timeout = turn_ceiling.ceiling_seconds
            provider_name = turn_ceiling.provider or "default"
            logger.info(
                f"Processing turn for case {case_id} with {agent_timeout}s timeout "
                f"(provider={provider_name})"
            )
            # Bind the same ceiling as a DEADLINE for the duration of the turn,
            # so the LLM retry ladder inside can budget against the cancellation
            # that would otherwise cut it mid-attempt (#1278, #1292). This is the
            # only site that knows both the ceiling and the instant it starts;
            # deriving either independently downstream is exactly the drift the
            # two settings already have between them. Scoped to the turn and
            # nothing else — auto-titling below has its own timeout and must not
            # be charged to the turn budget.
            #
            # The ``wait_for`` bounds the PREPARATION only, which commits
            # nothing; the commit runs outside it (#1882). A cancellation that
            # lands inside a commit leaves its outcome unknown, and one that
            # lands after it answers 504 for a turn that committed. Instead
            # ``commit_turn`` checks, before it starts, that the budget still
            # holds the commit's reserve (``TURN_COMMIT_RESERVE_SECONDS``, which
            # every LLM step leaves unspent) and answers the same 504 when it
            # does not, with nothing committed; once started, the commit runs
            # to its end. So a 2xx means the whole turn committed and a non-2xx
            # means none of it did.
            with bind_turn_deadline(agent_timeout):
                prepared = await asyncio.wait_for(
                    investigation_service.prepare_turn(
                        case_id=case_id, user_id=current_user.user_id, payload=payload
                    ),
                    timeout=agent_timeout,
                )
                try:
                    response = await investigation_service.commit_turn(
                        prepared,
                        receipt_key=keyed.receipt_key if keyed is not None else None,
                    )
                except TurnReceiptExistsError:
                    # Another request under this key committed while this one
                    # ran without a claim (``turn_idempotency``'s degraded
                    # mode); nothing of this one committed. Answer with the
                    # committed turn, as its retry would be.
                    replay = await replay_committed_turn(
                        keyed=keyed,
                        case=case,
                        case_service=case_service,
                        correlation_id=correlation_id,
                    )
                    if http_response is not None:
                        http_response.headers[IDEMPOTENCY_REPLAYED_HEADER] = "true"
                    return replay

            # Name the case from its own content. Called unconditionally: whether
            # the case is *titleable* is decided inside, against the case as it
            # stands after this turn, by the same gate the title endpoint uses.
            #
            # Awaited BEFORE returning rather than deferred to a background task,
            # which orders it ahead of the next turn's load and so keeps that
            # turn's full-row save from writing the placeholder back over the
            # generated title. It costs the turn ~1ms on the extractive path and
            # up to AUTO_TITLE_TIMEOUT_SECONDS in the worst case, once per case.
            # See _auto_title_case_if_default — the placement is load-bearing.
            await _auto_title_case_if_default(
                case_id=case_id,
                user_id=current_user.user_id,
                case_service=case_service,
                llm_provider=getattr(request.app.state, "llm_provider", None),
            )

            return response

        except (asyncio.TimeoutError, TurnDeadlineExceeded) as timed_out:
            # Both mean the same thing to the client, and are answered the
            # same way: the turn ran out of time and NOTHING of it was
            # committed. ``TurnDeadlineExceeded`` is the preparation finishing
            # with less than the commit's reserve left (#1882).
            #
            # No ``Retry-After`` (#1905): the turn exhausted the ceiling on THIS
            # input, so the same input is likely to exhaust it again, at full
            # LLM cost. A retry is safe but rarely useful; the route documents
            # "at most once", and a header inviting a timed re-run would say
            # the opposite.
            logger.error(
                f"Turn processing timed out for case {case_id} after "
                f"{turn_ceiling.ceiling_seconds}s "
                f"(provider={turn_ceiling.provider or 'default'})"
                + (
                    f": {timed_out}"
                    if isinstance(timed_out, TurnDeadlineExceeded)
                    else ""
                )
            )
            raise HTTPException(
                status_code=504,
                detail="Request timeout - processing is taking longer than expected. Please try again.",
                headers={
                    "x-correlation-id": correlation_id,
                    "x-error-code": "REQUEST_TIMEOUT",
                },
            )

    except StaleCaseException as e:
        # OCC conflict — another writer updated the case while this turn
        # was in flight. We deliberately do NOT silently retry: LLM turns
        # are expensive and non-idempotent (tool calls trigger external
        # side effects, tokens get spent). Surface the conflict to the
        # client so it can reload and decide whether to re-submit.
        logger.warning(
            f"Stale case on turn submission for {case_id}: "
            f"expected v{e.expected_version}, db v{e.actual_version}"
        )
        raise HTTPException(
            status_code=409,
            detail=(
                "Case state changed while processing this turn. "
                "Reload the case and resubmit if still applicable."
            ),
            headers={
                "x-correlation-id": correlation_id,
                "x-error-code": "CASE_VERSION_CONFLICT",
                "x-expected-version": str(e.expected_version),
                "x-actual-version": str(e.actual_version),
            },
        )
    except TenantTurnCapExceeded as capped:
        # The per-tenant daily cap (ADR-016 D5.3). Raised from
        # ``InvestigationService.process_turn`` once the request is known to be
        # a real turn on a case this caller may write to; the route's job is
        # only to render it. 429 with a distinct ``x-error-code`` because a
        # rate-limit 429 and a cap 429 want opposite reactions from a client:
        # one means slow down, this one means come back tomorrow.
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=capped.user_message,
            headers={
                "x-correlation-id": correlation_id,
                "x-error-code": "TENANT_TURN_CAP_EXCEEDED",
                "Retry-After": str(capped.retry_after_seconds),
            },
        )
    except TenantTurnCapUnavailable as unavailable:
        # Fail closed, but do not claim the caller spent an allowance they did
        # not: telling somebody their day is gone when the ledger merely failed
        # to write is a false statement about their own account, and it sends
        # them away until midnight for a fault that may clear in seconds.
        logger.error(
            "Turn refused: the tenant turn cap could not be applied: %s",
            unavailable,
            extra={"correlation_id": correlation_id},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Your usage allowance could not be checked just now. "
                "Please try again in a moment."
            ),
            headers={
                "x-correlation-id": correlation_id,
                "x-error-code": "TENANT_TURN_CAP_UNAVAILABLE",
                "Retry-After": "10",
            },
        )
    except NotFoundError as e:
        raise HTTPException(
            status_code=404,
            detail=str(e),
            headers={"x-correlation-id": correlation_id},
        )
    except PermissionDeniedException as e:
        raise HTTPException(
            status_code=403,
            detail=str(e),
            headers={"x-correlation-id": correlation_id},
        )
    except HTTPException:
        raise
    except ServiceException as e:
        logger.error(
            f"Turn processing failed: {e}",
            extra={"correlation_id": correlation_id},
        )
        # Classify off the typed provider metadata (LLMException.status_code /
        # retryable on the __cause__ chain), not the message string — see
        # llm_service_error_http_exception. Covers billing (→402), rate limits,
        # over-capacity, timeouts, provider 4xx/5xx, and schema-parse failures.
        raise llm_service_error_http_exception(e, correlation_id)
    except Exception as e:
        logger.error(
            f"Unexpected error processing turn: {e}",
            exc_info=True,
            extra={"correlation_id": correlation_id},
        )
        raise HTTPException(
            status_code=500,
            detail=f"An unexpected error occurred. Error ID: {correlation_id}",
            headers={
                "x-correlation-id": correlation_id,
                "x-error-code": "UNEXPECTED_ERROR",
                "Retry-After": "10",
            },
        )
    finally:
        # Only now is the turn fully answered: the settlement has returned
        # and the auto-title has landed, so a duplicate let in from here on
        # finds the receipt. Released any earlier, it could miss the receipt
        # and run the turn again (#1888).
        if keyed is not None:
            await keyed.release()


@router.post("/{case_id}/queries")
async def submit_case_query_gone(case_id: str):
    """DELETED: Use POST /{case_id}/turns instead."""
    raise HTTPException(
        status_code=410,
        detail="This endpoint has been removed. Use POST /cases/{case_id}/turns instead.",
    )


@router.patch("/{case_id}/evidence/{evidence_id}/classification")
@trace("api_reclassify_evidence")
async def reclassify_evidence(
    case_id: str,
    evidence_id: str,
    body: Dict[str, Any] = Body(
        ...,
        description=(
            "Request body: {'data_type': '<DataType value>'}. The data_type "
            "must be one of the enum values in faultmaven.models.api.DataType "
            "(e.g. 'logs_and_errors', 'structured_config')."
        ),
    ),
    investigation_service=Depends(get_investigation_service),
    current_user: UserDTO = Depends(require_authentication),
):
    """Reclassify an existing evidence row under a user-specified data type.

    Phase 1.5 — the escape hatch for "the classifier was confidently
    wrong". Re-runs the preprocessing pipeline on the stored raw file
    with ``user_override=data_type``, overwrites the evidence's
    structural index, and appends to its extractor.attempts history.

    Gated by ``FAULTMAVEN_RECLASSIFY_ENABLED``. Returns 404 when the
    flag is off so the endpoint is invisible in production by default.

    Error responses (dispatched by ``api/exception_handlers.py``):

    - ``404`` — feature disabled, case not found, or evidence not in case
      (``NotFoundError``).
    - ``409`` — evidence has no backing file (``ConflictError`` with
      ``conflict_reason="no_backing_file"``).
    - ``403`` — caller does not own the case (``AuthorizationError``).
    - ``422`` — invalid or missing ``data_type``, OR the case is terminal
      (both ``ValidationException``). A closed or resolved investigation
      accepts questions, not mutation; the terminal refusal is raised after
      the evidence lookup, so a missing evidence id is still a ``404``.
    - ``500`` — storage/preprocessing failure (``ServiceException``).
    """
    from faultmaven.config.settings import get_settings

    settings = get_settings()
    if not settings.preprocessing.reclassify_enabled:
        raise NotFoundError(message="Reclassification endpoint is not enabled")

    data_type_raw = body.get("data_type") if isinstance(body, dict) else None
    if not data_type_raw or not isinstance(data_type_raw, str):
        raise ValidationException("Request body must include 'data_type' (string)")

    try:
        data_type = DataType(data_type_raw)
    except ValueError:
        valid = ", ".join(t.value for t in DataType)
        raise ValidationException(
            f"Unknown data_type '{data_type_raw}'. Valid: {valid}"
        )

    updated_evidence = await investigation_service.reclassify_evidence(
        case_id=case_id,
        evidence_id=evidence_id,
        user_id=current_user.user_id,
        data_type=data_type,
        trigger="api",
    )

    return {
        "evidence_id": updated_evidence.evidence_id,
        "source_type": (
            updated_evidence.source_type.value
            if hasattr(updated_evidence.source_type, "value")
            else str(updated_evidence.source_type)
        ),
        "summary": updated_evidence.summary,
        "metadata": updated_evidence.metadata,
    }
