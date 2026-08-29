"""Knowledge Suggestion Repository — database and in-memory implementations.

The durable store behind ``SuggestionService`` (#1227). Until this existed the
service kept its suggestions in a process-local dict on a composition-root
singleton, which meant three things, all of them live in the shipped cloud
topology (``faultmaven-enterprise-infra`` runs the API at ``replicas: 3`` with
no ``sessionAffinity`` and no ingress stickiness):

* a restart destroyed every pending review;
* an extract handled by one pod was invisible to the approve handled by
  another, so approval 404'd on roughly a coin flip;
* nothing ever evicted an entry, so a long-lived process accumulated full
  LLM-authored articles for its whole lifetime.

Sessionless by design. ``SuggestionService`` is a process singleton, so it
cannot hold an ``AsyncSession``; :class:`DatabaseSuggestionRepository` opens one
per operation through the injected factory (``get_db_session`` in production),
exactly as ``SessionlessCaseRepository`` and
``SessionlessOrganizationRepository`` do. That is also what keeps the
PostgreSQL RLS binding correct: the engine's ``begin`` listener samples the
tenant contextvar once per transaction, so one short transaction per call is
the shape that binds the right tenant.

Tenancy. ``knowledge_suggestions`` is one of the tables migration 018 put under
RLS, so PostgreSQL scopes it by ``app.current_org_id``. The queries here ALSO
carry an explicit ``organization_id`` predicate wherever the caller supplies
one — defence in depth, and the only isolation that exists on SQLite, which has
no RLS at all. Do not remove them.

Identity. Both implementations return a DETACHED copy of the stored suggestion:
mutating what a read handed you changes nothing until you ``save()`` it. The
database repository gets that for free (a new session per call, no identity map
across calls); :class:`InMemorySuggestionRepository` copies explicitly so the
double cannot pass a test the database would fail. That is the whole point of
the double — a store that handed back its own live object would let the service
forget a ``save()`` and still look correct in unit tests.
"""

import json
import logging
from abc import ABC, abstractmethod
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import delete, func, select

from faultmaven.infrastructure.persistence.models import KnowledgeSuggestionModel
from faultmaven.modules.knowledge.domain.models.suggestion import (
    KnowledgeSuggestion,
    PIIScanStatus,
    SuggestionStatus,
)

logger = logging.getLogger(__name__)

#: Statuses whose decision is already recorded somewhere else, so an entry in
#: this state is the eviction pool rather than work in progress. An APPROVED
#: suggestion has its knowledge item in the corpus; a REJECTED one has nothing
#: to publish. PENDING_REVIEW and DRAFT are NEVER evictable — they exist
#: nowhere else, and dropping one silently destroys work a reviewer has not
#: seen.
TERMINAL_STATUSES = (SuggestionStatus.APPROVED, SuggestionStatus.REJECTED)

#: Placeholder ``case_id`` for a suggestion whose source case has been deleted.
#: ``knowledge_suggestions.case_id`` is ON DELETE SET NULL — the suggestion is
#: deliberately on the permanence side of that split — but the domain object
#: requires a non-empty ``case_id``, so a NULL column has to hydrate as
#: something. It reads as the fact it is, and never goes back to the FK.
DELETED_CASE_MARKER = "<deleted case>"


class SuggestionRepositoryError(Exception):
    """Raised when a suggestion repository operation fails."""


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Stamp UTC onto a naive timestamp read back from the database.

    SQLite has no timezone type, so every value it returns is naive even though
    it was written as UTC. Left alone, the service's own eviction sort
    (``key=lambda s: s.updated_at``) mixes naive and aware datetimes and raises
    ``TypeError``, and the API serialiser publishes an offset-less timestamp a
    client reads as local time.
    """
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _decode_list(value: Any) -> List[str]:
    """Decode a ``JsonBlob`` column that holds a JSON list of strings.

    ``JsonBlob`` is ``Text().with_variant(JSONB, "postgresql")``, so what comes
    back depends on the backend AND on the writer: SQLite hands back the JSON
    string this repository wrote, and PostgreSQL hands back the same ``str``
    because ``json.dumps`` binds a JSON *string scalar* into JSONB. A writer
    binding a real list produces the decoded shape instead. Handling one and
    not the other loses the value silently, so both are handled — the same rule
    ``utils.serialization.decode_json_blob`` applies to the dict case.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, (str, bytes)):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return []
        if isinstance(decoded, list):
            return [str(v) for v in decoded]
    return []


def _decode_dict(value: Any) -> Optional[Dict[str, Any]]:
    """Decode a ``JsonBlob`` column that holds a JSON object, or ``None``."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes)):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return None
        if isinstance(decoded, dict):
            return decoded
    return None


class SuggestionRepository(ABC):
    """Abstract store for :class:`KnowledgeSuggestion`."""

    @abstractmethod
    async def save(self, suggestion: KnowledgeSuggestion) -> KnowledgeSuggestion:
        """Insert or update ``suggestion``, returning what was persisted."""

    @abstractmethod
    async def get(self, suggestion_id: str) -> Optional[KnowledgeSuggestion]:
        """Load one suggestion by id — UNSCOPED (the trusted internal load)."""

    @abstractmethod
    async def get_for_organization(
        self, suggestion_id: str, organization_id: str
    ) -> Optional[KnowledgeSuggestion]:
        """Load one suggestion by id, scoped to ``organization_id``.

        ``None`` both for an absent id and for one owned by another tenant, so
        the two are indistinguishable to the caller.
        """

    @abstractmethod
    async def list_for_organization(
        self,
        organization_id: str,
        *,
        status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Tuple[List[KnowledgeSuggestion], int]:
        """Return one page of an organization's suggestions and the total count.

        Newest first, by ``created_at``.
        """

    @abstractmethod
    async def count_for_organization(self, organization_id: str) -> int:
        """Total suggestions stored for ``organization_id``."""

    @abstractmethod
    async def list_terminal_for_organization(
        self, organization_id: str
    ) -> List[KnowledgeSuggestion]:
        """Every APPROVED/REJECTED suggestion for ``organization_id``.

        Oldest decision first, by ``updated_at`` — the eviction order.
        """

    @abstractmethod
    async def delete_many(self, suggestion_ids: Sequence[str]) -> int:
        """Delete the named suggestions; returns how many rows went away."""


class DatabaseSuggestionRepository(SuggestionRepository):
    """``knowledge_suggestions``-backed store, one session per operation."""

    def __init__(self, session_factory: Optional[Callable[[], Any]] = None):
        """Args:
        session_factory: zero-arg callable returning an async context
            manager that yields an ``AsyncSession``. Defaults to
            ``infrastructure.persistence.database.get_db_session``, imported
            lazily so constructing the repository does not build an engine.
        """
        if session_factory is None:
            from faultmaven.infrastructure.persistence.database import get_db_session

            session_factory = get_db_session
        self._session_factory = session_factory

    # -- mapping ----------------------------------------------------------

    @staticmethod
    def _to_domain(row: KnowledgeSuggestionModel) -> KnowledgeSuggestion:
        return KnowledgeSuggestion(
            suggestion_id=row.suggestion_id,
            organization_id=row.organization_id,
            case_id=row.case_id or DELETED_CASE_MARKER,
            status=SuggestionStatus(row.status),
            suggested_title=row.suggested_title or "",
            suggested_content=row.suggested_content or "",
            suggested_type=row.suggested_type or "troubleshooting_guide",
            extracted_by=row.extracted_by or "",
            extracted_at=_as_utc(row.extracted_at),
            include_messages=bool(row.include_messages),
            include_evidence=bool(row.include_evidence),
            pii_scan_status=PIIScanStatus(row.pii_scan_status),
            pii_scan_result=_decode_dict(row.pii_scan_result),
            pii_remediated_by=row.pii_remediated_by,
            pii_remediated_at=_as_utc(row.pii_remediated_at),
            source_case_title=row.source_case_title or "",
            message_count=row.message_count or 0,
            evidence_count=row.evidence_count or 0,
            reviewed_by=row.reviewed_by,
            reviewed_at=_as_utc(row.reviewed_at),
            review_notes=row.review_notes,
            rejection_reason=row.rejection_reason,
            knowledge_item_id=row.knowledge_item_id,
            validation_passed=row.validation_passed,
            validation_errors=_decode_list(row.validation_errors),
            validation_warnings=_decode_list(row.validation_warnings),
            created_at=_as_utc(row.created_at),
            updated_at=_as_utc(row.updated_at),
            metadata=_decode_dict(row.suggestion_metadata),
        )

    @staticmethod
    def _column_values(suggestion: KnowledgeSuggestion) -> Dict[str, Any]:
        """The column payload for an insert or update.

        ``case_id`` and ``extracted_by`` are real foreign keys, and the domain
        carries them as plain strings. A value that never named a row (the
        deleted-case marker, or an empty extractor) is written as NULL rather
        than offered to the FK, which would reject the whole write.
        """
        case_id = suggestion.case_id
        if not case_id or case_id == DELETED_CASE_MARKER:
            case_id = None
        return {
            "organization_id": suggestion.organization_id,
            "case_id": case_id,
            "knowledge_item_id": suggestion.knowledge_item_id,
            "status": suggestion.status.value,
            "suggested_title": suggestion.suggested_title,
            "suggested_content": suggestion.suggested_content,
            "suggested_type": suggestion.suggested_type,
            "extracted_by": suggestion.extracted_by or None,
            "extracted_at": suggestion.extracted_at,
            "include_messages": bool(suggestion.include_messages),
            "include_evidence": bool(suggestion.include_evidence),
            "pii_scan_status": suggestion.pii_scan_status.value,
            "pii_scan_result": (
                json.dumps(suggestion.pii_scan_result)
                if suggestion.pii_scan_result is not None
                else None
            ),
            "pii_remediated_by": suggestion.pii_remediated_by,
            "pii_remediated_at": suggestion.pii_remediated_at,
            "source_case_title": suggestion.source_case_title,
            "message_count": suggestion.message_count,
            "evidence_count": suggestion.evidence_count,
            "reviewed_by": suggestion.reviewed_by,
            "reviewed_at": suggestion.reviewed_at,
            "review_notes": suggestion.review_notes,
            "rejection_reason": suggestion.rejection_reason,
            "validation_passed": suggestion.validation_passed,
            "validation_errors": json.dumps(list(suggestion.validation_errors)),
            "validation_warnings": json.dumps(list(suggestion.validation_warnings)),
            "created_at": suggestion.created_at,
            "updated_at": suggestion.updated_at,
            "suggestion_metadata": json.dumps(suggestion.metadata or {}),
        }

    # -- operations -------------------------------------------------------

    async def save(self, suggestion: KnowledgeSuggestion) -> KnowledgeSuggestion:
        """Insert the row, or update it in place if the id already exists.

        Upsert rather than separate create/update because every caller in
        ``SuggestionService`` is "persist whatever this object now says": the
        extract path writes a new row, the review paths write back a loaded
        one, and forcing them to know which is which buys nothing.
        """
        values = self._column_values(suggestion)
        try:
            async with self._session_factory() as session:
                row = await session.get(
                    KnowledgeSuggestionModel, suggestion.suggestion_id
                )
                if row is None:
                    session.add(
                        KnowledgeSuggestionModel(
                            suggestion_id=suggestion.suggestion_id, **values
                        )
                    )
                else:
                    for key, value in values.items():
                        setattr(row, key, value)
                await session.commit()
        except Exception as exc:
            raise SuggestionRepositoryError(
                f"Failed to save suggestion {suggestion.suggestion_id}: {exc}"
            ) from exc
        return deepcopy(suggestion)

    async def get(self, suggestion_id: str) -> Optional[KnowledgeSuggestion]:
        if not suggestion_id:
            return None
        async with self._session_factory() as session:
            row = await session.get(KnowledgeSuggestionModel, suggestion_id)
            return self._to_domain(row) if row is not None else None

    async def get_for_organization(
        self, suggestion_id: str, organization_id: str
    ) -> Optional[KnowledgeSuggestion]:
        if not suggestion_id or not organization_id:
            return None
        async with self._session_factory() as session:
            stmt = select(KnowledgeSuggestionModel).where(
                KnowledgeSuggestionModel.suggestion_id == suggestion_id,
                KnowledgeSuggestionModel.organization_id == organization_id,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            return self._to_domain(row) if row is not None else None

    async def list_for_organization(
        self,
        organization_id: str,
        *,
        status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Tuple[List[KnowledgeSuggestion], int]:
        if not organization_id:
            return [], 0
        async with self._session_factory() as session:
            predicates = [KnowledgeSuggestionModel.organization_id == organization_id]
            if status:
                predicates.append(KnowledgeSuggestionModel.status == status)

            total = (
                await session.execute(
                    select(func.count())
                    .select_from(KnowledgeSuggestionModel)
                    .where(*predicates)
                )
            ).scalar_one()

            stmt = (
                select(KnowledgeSuggestionModel)
                .where(*predicates)
                .order_by(KnowledgeSuggestionModel.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [self._to_domain(row) for row in rows], int(total)

    async def count_for_organization(self, organization_id: str) -> int:
        if not organization_id:
            return 0
        async with self._session_factory() as session:
            total = (
                await session.execute(
                    select(func.count())
                    .select_from(KnowledgeSuggestionModel)
                    .where(KnowledgeSuggestionModel.organization_id == organization_id)
                )
            ).scalar_one()
            return int(total)

    async def list_terminal_for_organization(
        self, organization_id: str
    ) -> List[KnowledgeSuggestion]:
        if not organization_id:
            return []
        async with self._session_factory() as session:
            stmt = (
                select(KnowledgeSuggestionModel)
                .where(
                    KnowledgeSuggestionModel.organization_id == organization_id,
                    KnowledgeSuggestionModel.status.in_(
                        [s.value for s in TERMINAL_STATUSES]
                    ),
                )
                .order_by(KnowledgeSuggestionModel.updated_at.asc())
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [self._to_domain(row) for row in rows]

    async def delete_many(self, suggestion_ids: Sequence[str]) -> int:
        ids = [sid for sid in suggestion_ids if sid]
        if not ids:
            return 0
        async with self._session_factory() as session:
            result = await session.execute(
                delete(KnowledgeSuggestionModel).where(
                    KnowledgeSuggestionModel.suggestion_id.in_(ids)
                )
            )
            await session.commit()
            return int(result.rowcount or 0)


class InMemorySuggestionRepository(SuggestionRepository):
    """Process-local store — a TEST DOUBLE, not a deployment option.

    This is what ``SuggestionService._suggestions_store`` used to be, moved
    behind the repository seam and demoted (#1227). Nothing in the composition
    root builds one: ``create_suggestion_service`` takes a
    :class:`DatabaseSuggestionRepository`, and a service handed no repository at
    all refuses rather than degrading to this. It exists so unit tests can drive
    the service without a database.

    Copies on the way in AND on the way out, because the database repository
    does: a double that handed back its own live object would let the service
    mutate a loaded suggestion, forget to ``save()`` it, and still pass.
    """

    def __init__(self) -> None:
        self._items: Dict[str, KnowledgeSuggestion] = {}

    async def save(self, suggestion: KnowledgeSuggestion) -> KnowledgeSuggestion:
        self._items[suggestion.suggestion_id] = deepcopy(suggestion)
        return deepcopy(suggestion)

    async def get(self, suggestion_id: str) -> Optional[KnowledgeSuggestion]:
        stored = self._items.get(suggestion_id)
        return deepcopy(stored) if stored is not None else None

    async def get_for_organization(
        self, suggestion_id: str, organization_id: str
    ) -> Optional[KnowledgeSuggestion]:
        if not suggestion_id or not organization_id:
            return None
        stored = self._items.get(suggestion_id)
        if stored is None or stored.organization_id != organization_id:
            return None
        return deepcopy(stored)

    async def list_for_organization(
        self,
        organization_id: str,
        *,
        status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Tuple[List[KnowledgeSuggestion], int]:
        if not organization_id:
            return [], 0
        matches = [
            s for s in self._items.values() if s.organization_id == organization_id
        ]
        if status:
            matches = [s for s in matches if s.status.value == status]
        matches.sort(key=lambda s: s.created_at, reverse=True)
        total = len(matches)
        page = matches[offset : offset + limit]
        return [deepcopy(s) for s in page], total

    async def count_for_organization(self, organization_id: str) -> int:
        if not organization_id:
            return 0
        return sum(
            1 for s in self._items.values() if s.organization_id == organization_id
        )

    async def list_terminal_for_organization(
        self, organization_id: str
    ) -> List[KnowledgeSuggestion]:
        if not organization_id:
            return []
        terminal = [
            s
            for s in self._items.values()
            if s.organization_id == organization_id and s.status in TERMINAL_STATUSES
        ]
        terminal.sort(key=lambda s: s.updated_at)
        return [deepcopy(s) for s in terminal]

    async def delete_many(self, suggestion_ids: Sequence[str]) -> int:
        removed = 0
        for suggestion_id in suggestion_ids:
            if self._items.pop(suggestion_id, None) is not None:
                removed += 1
        return removed
