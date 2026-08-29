"""Knowledge Suggestion Repository — database and in-memory implementations.

The durable store behind ``SuggestionService`` (#1227). Until this existed the
service kept its suggestions in a process-local dict on a composition-root
singleton, which meant three things, all of them live in the shipped cloud
topology (``faultmaven-enterprise-infra`` runs the API at ``replicas: 3`` with
no ``sessionAffinity`` and no ingress stickiness):

* a restart destroyed every pending review;
* an extract handled by one pod was invisible to the approve handled by
  another, so approval 404'd on roughly a coin flip;
* nothing bounded the store, so a long-lived process accumulated full
  LLM-authored articles for its whole lifetime.

The interface these implement is ``modules/knowledge/contracts``'s
:class:`ISuggestionRepository`; read it for the two invariants callers rely on
(detached-copy reads, optimistically-locked writes). This module holds only the
implementations and the abstract base they share.

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

**Nothing here deletes.** There is no eviction primitive, deliberately: an
approved suggestion's ``knowledge_item_id`` is the only case → runbook link that
exists (``knowledge_items`` carries no back-pointer), so deleting the row
destroys the provenance the knowledge flywheel is built to accumulate. The
store is bounded by refusing new extractions when the *unreviewed* queue is
full — see ``SuggestionService._refuse_if_review_queue_full``.
"""

import json
import logging
from abc import ABC, abstractmethod
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, select, update

from faultmaven.infrastructure.persistence.models import KnowledgeSuggestionModel
from faultmaven.modules.knowledge.contracts import SuggestionConcurrencyError
from faultmaven.modules.knowledge.domain.models.suggestion import (
    KnowledgeSuggestion,
    PIIScanStatus,
    SuggestionStatus,
)
from faultmaven.utils.serialization import decode_json_blob

logger = logging.getLogger(__name__)

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
    it was written as UTC. Left alone it reaches ``to_api_response`` and the
    extract route, both of which serialise it with ``to_json_compatible``, and
    the review inbox publishes ``extracted_at``/``created_at`` with no offset —
    which a client parses as *local* time, so the "2h ago" lineage footer reads
    hours wrong in either direction depending on where the reviewer sits. The
    same normalisation, for the same reason, as ``operator_grant_repository``.

    It also keeps every ``datetime`` the service sees comparable with the
    ``datetime.now(timezone.utc)`` values it mints.
    """
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _decode_list(value: Any) -> List[str]:
    """Decode a ``JsonBlob`` column that holds a JSON list of strings.

    The list counterpart of ``utils.serialization.decode_json_blob``, which
    handles only the dict case (and is used directly for the two dict-shaped
    columns here). Both exist because ``JsonBlob`` is
    ``Text().with_variant(JSONB, "postgresql")``, so what comes back depends on
    the backend AND on the writer: SQLite hands back the JSON string this
    repository wrote; PostgreSQL hands back the same ``str``, because
    ``json.dumps`` binds a JSON *string scalar* into JSONB; and a value that
    reached the column through the migration's ``server_default '[]'`` comes
    back from JSONB as a real ``list``. All three shapes are live in one column
    on a PostgreSQL deployment, so handling one and not the others loses the
    value silently.
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


class SuggestionRepository(ABC):
    """Abstract base shared by the two implementations.

    The *interface* callers depend on is
    ``modules.knowledge.contracts.ISuggestionRepository``; this class exists so
    the two implementations below cannot silently diverge on the method set,
    the same split the case module makes between ``ICaseRepository`` (contract)
    and ``CaseRepository`` (base).
    """

    #: See ``ISuggestionRepository.is_durable``. A class attribute rather than a
    #: computed property: durability is a property of the implementation, and
    #: choosing an implementation whose claim is true for the configured
    #: database is the composition root's job, not this object's.
    IS_DURABLE: bool = False

    @property
    def is_durable(self) -> bool:
        return self.IS_DURABLE

    @abstractmethod
    async def save(self, suggestion: KnowledgeSuggestion) -> KnowledgeSuggestion:
        """Insert or optimistically-locked update; see the contract."""

    @abstractmethod
    async def get(self, suggestion_id: str) -> Optional[KnowledgeSuggestion]:
        """Load one suggestion by id — UNSCOPED (the trusted internal load)."""

    @abstractmethod
    async def get_for_organization(
        self, suggestion_id: str, organization_id: str
    ) -> Optional[KnowledgeSuggestion]:
        """Load one suggestion by id, scoped to ``organization_id``."""

    @abstractmethod
    async def list_for_organization(
        self,
        organization_id: str,
        *,
        status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Tuple[List[KnowledgeSuggestion], int]:
        """One page of an organization's suggestions, newest first, and a total."""

    @abstractmethod
    async def count_for_organization(
        self,
        organization_id: str,
        *,
        statuses: Optional[Sequence[SuggestionStatus]] = None,
    ) -> int:
        """Count an organization's suggestions, optionally in given statuses."""


class DatabaseSuggestionRepository(SuggestionRepository):
    """``knowledge_suggestions``-backed store, one session per operation."""

    IS_DURABLE = True

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
            # ``copy=True``: the decoded dict may be the ORM row's own JSONB
            # value on PostgreSQL, and the domain object is handed out to
            # callers who mutate it.
            pii_scan_result=decode_json_blob(row.pii_scan_result, copy=True),
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
            metadata=decode_json_blob(row.suggestion_metadata, copy=True),
            version=row.version or 1,
        )

    @staticmethod
    def _json_param(value: Any, *, dialect: str) -> Any:
        """Bind a ``JsonBlob`` value in the shape that column actually wants.

        ``JsonBlob`` is ``Text().with_variant(JSONB, "postgresql")``, so the
        two backends want DIFFERENT Python values and handing both the same one
        is wrong on one of them:

        * SQLite's column is TEXT and takes the serialised string;
        * PostgreSQL's is JSONB and takes the object, which SQLAlchemy encodes
          into a real JSON array/object.

        Passing a pre-``json.dumps``'d string to JSONB does not fail — it
        stores a JSON **string scalar**, so ``jsonb_typeof`` answers ``string``
        where the migration's own ``server_default '[]'`` stored an ``array``.
        Measured on PostgreSQL 16 before this helper existed: a row written here
        held ``"[\"err one\"]"`` while a defaulted row held ``[]``, i.e. two
        shapes in one column. The readers below cope with both, so nothing was
        broken — but every JSONB operator (``@>``, ``jsonb_array_length``, a GIN
        index) silently misses the written rows, and "the column's own default
        disagrees with its writer" is a trap to remove rather than document.
        """
        if value is None:
            return None
        if dialect == "postgresql":
            return value
        return json.dumps(value)

    @classmethod
    def _column_values(
        cls, suggestion: KnowledgeSuggestion, *, dialect: str
    ) -> Dict[str, Any]:
        """The column payload for an insert or update, EXCLUDING ``version``.

        ``version`` is the repository's own bookkeeping and is set by the write
        path, never copied from the caller's snapshot.

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
            "pii_scan_result": cls._json_param(
                suggestion.pii_scan_result, dialect=dialect
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
            "validation_errors": cls._json_param(
                list(suggestion.validation_errors), dialect=dialect
            ),
            "validation_warnings": cls._json_param(
                list(suggestion.validation_warnings), dialect=dialect
            ),
            "created_at": suggestion.created_at,
            "updated_at": suggestion.updated_at,
            "suggestion_metadata": cls._json_param(
                suggestion.metadata or {}, dialect=dialect
            ),
        }

    # -- operations -------------------------------------------------------

    async def save(self, suggestion: KnowledgeSuggestion) -> KnowledgeSuggestion:
        """Insert the row, or update it in place if the id already exists.

        Upsert rather than separate create/update because every caller in
        ``SuggestionService`` is "persist whatever this object now says": the
        extract path writes a new row, the review paths write back a loaded
        one, and forcing them to know which is which buys nothing.

        The UPDATE is a single conditional statement —
        ``WHERE suggestion_id = :id AND version = :loaded`` — so the read of the
        current version and the write that supersedes it are one atomic step.
        A read-then-write pair would reintroduce the race it exists to close.

        Raises:
            SuggestionConcurrencyError: the row moved since it was loaded (or,
                on an insert, the id was taken between the check and the write).
            SuggestionRepositoryError: anything else the store refused.
        """
        expected_version = suggestion.version or 1
        next_version = expected_version + 1
        try:
            async with self._session_factory() as session:
                values = self._column_values(
                    suggestion, dialect=session.get_bind().dialect.name
                )
                exists = (
                    await session.execute(
                        select(KnowledgeSuggestionModel.suggestion_id).where(
                            KnowledgeSuggestionModel.suggestion_id
                            == suggestion.suggestion_id
                        )
                    )
                ).scalar_one_or_none()

                if exists is None:
                    session.add(
                        KnowledgeSuggestionModel(
                            suggestion_id=suggestion.suggestion_id,
                            version=expected_version,
                            **values,
                        )
                    )
                    await session.commit()
                    stored_version = expected_version
                else:
                    result = await session.execute(
                        update(KnowledgeSuggestionModel)
                        .where(
                            KnowledgeSuggestionModel.suggestion_id
                            == suggestion.suggestion_id,
                            KnowledgeSuggestionModel.version == expected_version,
                        )
                        .values(version=next_version, **values)
                        .execution_options(synchronize_session=False)
                    )
                    if (result.rowcount or 0) == 0:
                        await session.rollback()
                        raise SuggestionConcurrencyError(suggestion.suggestion_id)
                    await session.commit()
                    stored_version = next_version
        except SuggestionConcurrencyError:
            raise
        except Exception as exc:
            raise SuggestionRepositoryError(
                f"Failed to save suggestion {suggestion.suggestion_id}: {exc}"
            ) from exc

        saved = deepcopy(suggestion)
        saved.version = stored_version
        return saved

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

    async def count_for_organization(
        self,
        organization_id: str,
        *,
        statuses: Optional[Sequence[SuggestionStatus]] = None,
    ) -> int:
        if not organization_id:
            return 0
        async with self._session_factory() as session:
            predicates = [KnowledgeSuggestionModel.organization_id == organization_id]
            if statuses:
                predicates.append(
                    KnowledgeSuggestionModel.status.in_([s.value for s in statuses])
                )
            total = (
                await session.execute(
                    select(func.count())
                    .select_from(KnowledgeSuggestionModel)
                    .where(*predicates)
                )
            ).scalar_one()
            return int(total)


class InMemorySuggestionRepository(SuggestionRepository):
    """Process-local store — a TEST DOUBLE, and the no-database fallback.

    This is what ``SuggestionService._suggestions_store`` used to be, moved
    behind the repository seam and demoted (#1227). It is built in exactly two
    places: by a test, and by ``create_suggestion_service`` when
    ``persistent_database_configured()`` says there is no database to write to
    — the same predicate every other factory keys off (fm#1128). It reports
    ``is_durable == False``, so a deployment that lands here says so on
    ``GET /admin/config/status`` instead of claiming a durability it does not
    have.

    Copies on the way in AND on the way out, and enforces the same version
    check, because the database repository does: a double that diverged on
    either would let the service pass a test the database would fail.
    """

    IS_DURABLE = False

    def __init__(self) -> None:
        self._items: Dict[str, KnowledgeSuggestion] = {}

    # -- synchronous affordances, for test setup only ---------------------
    #
    # The repository protocol is async because the database one has to be.
    # These two exist so a test whose setup or assertion is synchronous (a
    # ``TestClient`` route test, say) does not have to reach into ``_items``
    # itself. They copy on both sides exactly as ``save``/``get`` do, so a test
    # using them sees the same detached-copy semantics the service does.

    def seed(self, *suggestions: KnowledgeSuggestion) -> None:
        """Put suggestions in the store without awaiting."""
        for suggestion in suggestions:
            self._items[suggestion.suggestion_id] = deepcopy(suggestion)

    def peek(self, suggestion_id: str) -> Optional[KnowledgeSuggestion]:
        """Read one back without awaiting; ``None`` if absent."""
        stored = self._items.get(suggestion_id)
        return deepcopy(stored) if stored is not None else None

    # -- protocol ---------------------------------------------------------

    async def save(self, suggestion: KnowledgeSuggestion) -> KnowledgeSuggestion:
        existing = self._items.get(suggestion.suggestion_id)
        expected_version = suggestion.version or 1
        if existing is None:
            stored_version = expected_version
        else:
            if (existing.version or 1) != expected_version:
                raise SuggestionConcurrencyError(suggestion.suggestion_id)
            stored_version = expected_version + 1

        stored = deepcopy(suggestion)
        stored.version = stored_version
        self._items[suggestion.suggestion_id] = stored
        return deepcopy(stored)

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

    async def count_for_organization(
        self,
        organization_id: str,
        *,
        statuses: Optional[Sequence[SuggestionStatus]] = None,
    ) -> int:
        if not organization_id:
            return 0
        wanted = set(statuses) if statuses else None
        return sum(
            1
            for s in self._items.values()
            if s.organization_id == organization_id
            and (wanted is None or s.status in wanted)
        )
