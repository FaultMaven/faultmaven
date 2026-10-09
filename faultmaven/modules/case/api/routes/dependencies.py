"""Case-route dependency helpers (fm#1707).

Shared FastAPI ``Depends`` providers and small guard/parsing helpers used by
more than one sub-router in this package: the DI accessors that read the
request-scoped case/session services and the runbook-dedup KB off the
container (patchable at test time), the case-state guards
(``check_case_service_available``, ``require_case_not_terminal``), and a
handful of pure parsing/formatting helpers (``_safe_enum_value``,
``resolve_paste_source_meta``, ``_parse_observed_at``,
``_resolve_agent_timeout``). Every sub-router in this package imports what it
needs from here; this module imports nothing from its siblings.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Request, status

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.exceptions import CASE_TERMINAL
from faultmaven.infrastructure.llm.router import resolve_chat_provider_name
from faultmaven.models.interfaces_case import ICaseService
from faultmaven.modules.auth.contracts import ISessionService, UserDTO
from faultmaven.modules.case.domain.models.evidence import is_default_case_title

logger = logging.getLogger(__name__)


_is_default_case_title = is_default_case_title


def _safe_enum_value(value):
    """Safely extract enum value, return string if already string."""
    if hasattr(value, "value"):
        return value.value
    return str(value)


def resolve_paste_source_meta(
    input_type: Optional[str], source_url: Optional[str]
) -> tuple[dict, str]:
    """Source metadata + filename prefix for a ``pasted_content`` attachment.

    Returns ``(source_meta, filename_prefix)``. The origin discrimination feeds
    the classifier's confidence boost downstream:

    - ``page_capture`` — the browser extension captured a web page
    - ``text_paste``   — raw text pasted by a user, or relayed by an agent

    ``source_url`` stays scoped to ``page_capture``. It means the URL the
    CONTENT came from — the classifier consults it at Priority 3 (0.88-0.94),
    ahead of the content rules, precisely because for a capture the page IS the
    content's origin. A relay link is a different thing: the Slack agent's
    permalink points at the message that forwarded an alert, not at where the
    alert's text came from, and feeding it to a content classifier would let a
    pasted excerpt be typed by whatever page someone happened to copy it out of.
    Recording relay provenance is worth doing, but it needs its own field and a
    consumer; overloading this one is not the way.

    This is a module-level function rather than inline branching because the
    test suite previously kept its own hand-copied mirror of the logic, which
    meant a change to the route left the mirror stale and the tests green.
    Tests import THIS.
    """

    if input_type == "page_capture":
        meta = {"source_type": "page_capture"}
        prefix = "page-capture-"
        if source_url:
            meta["source_url"] = source_url
    else:
        meta = {"source_type": "text_paste"}
        prefix = "pasted-content-"
    return meta, prefix


def _parse_observed_at(raw: Optional[str], correlation_id: str) -> Optional[datetime]:
    """Parse the caller-supplied ``observed_at`` into an aware UTC instant.

    Fails to ``None`` — never to "now" and never to a 4xx. This field is a
    voluntary provenance hint from a forwarding caller; a client that sends a
    malformed one should still get its turn processed, just without a claim
    about when the content was observed. Substituting the current time would
    manufacture the exact false currency the field exists to prevent.

    A naive timestamp is read as UTC (the wire contract is UTC) and a future
    one is rejected: content cannot have been observed after it was submitted,
    so a future value means a broken clock or a bad conversion, and trusting it
    would make stale evidence look fresher than it is — the unsafe direction.
    """

    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        logger.warning(
            "Ignoring un-parseable observed_at %r (correlation_id=%s)",
            raw[:64],
            correlation_id,
        )
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    # Small tolerance so ordinary clock skew between the caller and this host
    # doesn't discard a legitimate just-now observation.
    if parsed > datetime.now(timezone.utc) + timedelta(minutes=5):
        logger.warning(
            "Ignoring future observed_at %s (correlation_id=%s)",
            parsed.isoformat(),
            correlation_id,
        )
        return None
    return parsed


def _resolve_agent_timeout(settings) -> tuple[float, str]:
    """Resolve the per-provider agent-level timeout for the active CHAT_PROVIDER.

    Mirrors the LLM-router's ``_resolve_timeout`` shape (ISS-054) but applies to
    the agent-level (turn-wide) ceiling enforced via ``asyncio.wait_for``.

    Returns a ``(timeout_seconds, provider_name_for_logging)`` tuple. The
    returned name is the resolved provider string (or ``"default"`` when the
    setting is missing entirely) so log lines can attribute timeouts.

    See ISS-058.
    """
    # Resolved by the SAME helper the LLM router uses for its own per-provider
    # timeout lookup. The two sides of the turn budget must agree on which
    # provider they are talking about, or a comparison between them compares
    # two different providers' timeouts.
    provider_name = resolve_chat_provider_name(settings)
    timeout = float(settings.agent.timeout_for_provider(provider_name))
    return timeout, provider_name or "default"


async def _di_get_case_service_dependency(request: Request) -> Optional[ICaseService]:
    """Runtime wrapper so patched dependency is honored in tests."""
    # Import inside to resolve the patched function at call time
    from faultmaven.api.v1.dependencies import get_case_service as _getter

    return await _getter(request)


async def _di_get_session_service_dependency(request: Request) -> ISessionService:
    """Runtime wrapper so patched dependency is honored in tests."""
    from faultmaven.api.v1.dependencies import get_session_service as _getter

    return await _getter(request)


async def _di_get_runbook_kb_dependency(request: Request):
    """Get the DI-provided runbook-dedup KB (fm#1030).

    The container binds this reader to the SAME ChromaDB collection the KB
    writer writes (``create_runbook_dedup_kb``). The route must not build its
    own over ``container.vector_store`` — that store binds the
    settings-derived collection name, which diverges from the production
    writer's hardcoded ``KB_COLLECTION`` the moment ``CHROMADB_COLLECTION``
    is overridden, silently reinstating the empty-result dedup.
    """
    try:
        container = request.app.extra.get("di_container")
        if container:
            return getattr(container, "runbook_kb", None)
        return None
    except Exception:
        return None


def check_case_service_available(case_service: Optional[ICaseService]) -> ICaseService:
    """Check if case service is available and raise appropriate error if not"""
    if case_service is None:
        # For protected endpoints that require authentication, return 401 instead of 500
        # This prevents pre-auth calls from getting 500 errors
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required - case service unavailable",
        )
    return case_service


def require_case_not_terminal(case) -> None:
    """Reject write operations on terminal (RESOLVED/CLOSED) cases.

    409 ``x-error-code: CASE_TERMINAL``, the label every terminal-case
    refusal carries (#1907).
    """
    if case.is_terminal:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Case is in terminal state and read-only. No further modifications allowed.",
            headers={"x-error-code": CASE_TERMINAL},
        )


async def _di_get_creator_service_channel(
    raw_request: Request,
    current_user: UserDTO = Depends(require_authentication),
) -> Optional[str]:
    """Resolve the creating account's ``service_channel`` for source stamping.

    The **channel**, not the kind (ADR-017 D6): there are exactly two kinds of
    account — a human and a service — and which integration a service account
    serves is a separate attribute, so a second integration is a new value here
    rather than a third account kind. Reading the kind would answer 'service'
    for every integration and could no longer say which one.

    Best-effort: falls back to ``None`` if the user service is unavailable or
    the lookup fails, so case creation never depends on it.
    """
    user_service = getattr(raw_request.app.state, "user_service", None)
    if user_service is None:
        return None
    try:
        user = await user_service.get_user(current_user.user_id)
        return getattr(user, "service_channel", None) if user else None
    except Exception as e:
        # Don't fail case creation on this — but do NOT swallow silently: a
        # Slack case mislabeled 'copilot' (source is immutable) is otherwise
        # undetectable.
        logger.warning(
            "Could not resolve service_channel for user %s; case source will "
            "default to 'copilot': %s",
            getattr(current_user, "user_id", "?"),
            e,
        )
        return None
