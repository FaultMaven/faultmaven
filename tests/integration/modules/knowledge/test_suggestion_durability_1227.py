"""The suggestion store is durable and shared, not a per-worker dict (#1227).

``SuggestionService`` became a correctly-wired composition-root singleton in
#1214, but its store stayed an in-process ``dict``. Three consequences, and
#1226 (merged as #1238) is what made them urgent rather than tech debt: with
extraction emitting the v4 runbook schema a suggestion can be approved without a
human reshape, so the suggestion IS the deliverable and losing it is a product
failure rather than the loss of a draft nobody was going to keep.

1. **Non-durable** — a restart destroyed every pending review.
2. **Invisible across workers** — the shipped cloud topology runs the API at
   ``replicas: 3`` (``kubernetes/apps/faultmaven/base/faultmaven-api/
   deployment.yaml``, onprem overlay patches to 2, an HPA on top) with **no**
   ``sessionAffinity`` and no ingress stickiness, so an extract handled by one
   pod and an approve handled by another was roughly a coin flip, and the
   approve answered 404 for an id the API had just issued.
3. **Unbounded** — nothing evicted, so a long-lived process accumulated full
   LLM-authored articles for its lifetime.

Every test here runs against a REAL file-backed SQLite database with
``PRAGMA foreign_keys=ON`` — the same enforcement ``infrastructure/persistence/
database.py`` installs in production, which is what makes the organization-id
test below a statement about the deployment rather than about a mock.

Restart is modelled the only way a test can model it: build a service, throw it
away, build another one over the same database file. Cross-worker is modelled
as two services constructed independently and never introduced to each other,
which is exactly what two uvicorn workers or two pods are.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from faultmaven.exceptions import ServiceUnavailableException
from faultmaven.infrastructure.persistence.models import (
    Base,
    CaseModel,
    EnterpriseModel,
    KnowledgeSuggestionModel,
    OrganizationModel,
    UserModel,
)
from faultmaven.modules.knowledge.domain.models.suggestion import (
    KnowledgeSuggestion,
    PIIScanStatus,
    SuggestionStatus,
)
from faultmaven.modules.knowledge.domain.services.suggestion_service import (
    SuggestionService,
)
from faultmaven.modules.knowledge.infrastructure.persistence.suggestion_repository import (  # noqa: E501
    DatabaseSuggestionRepository,
)

pytestmark = pytest.mark.integration

ENTERPRISE_ID = "00000000-0000-0000-0000-000000000002"
ORG_ID = "org-alpha-1111"
OTHER_ORG_ID = "org-beta-2222"
CASE_ID = "case_aabb11223344"
USER_ID = "user-extractor"
ADMIN_ID = "user-admin"


def _make_engine(db_path):
    """An engine with FK enforcement on, matching production's connect hook.

    Without the PRAGMA, SQLite ignores every foreign key and the
    ``organization_id`` test below would pass against the literal ``"default"``
    the extract route used to send — proving nothing about a deployment where
    ``database.py`` turns enforcement on.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _record):  # pragma: no cover - trivial
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


@pytest.fixture
async def db(tmp_path):
    """A file-backed database with the FK parents a suggestion row needs."""
    db_path = tmp_path / "suggestions.db"
    engine = _make_engine(db_path)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(
            EnterpriseModel(enterprise_id=ENTERPRISE_ID, name="Default", slug="default")
        )
        session.add(
            OrganizationModel(
                organization_id=ORG_ID,
                enterprise_id=ENTERPRISE_ID,
                name="Alpha",
                slug="alpha",
            )
        )
        session.add(
            OrganizationModel(
                organization_id=OTHER_ORG_ID,
                enterprise_id=ENTERPRISE_ID,
                name="Beta",
                slug="beta",
            )
        )
        # Both are REAL users: extracted_by / reviewed_by / pii_remediated_by
        # are foreign keys to `users`, so a made-up reviewer id fails the write
        # rather than being silently recorded.
        session.add(
            UserModel(
                user_id=USER_ID,
                enterprise_id=ENTERPRISE_ID,
                username="extractor",
                email="extractor@example.com",
                display_name="Extractor",
            )
        )
        session.add(
            UserModel(
                user_id=ADMIN_ID,
                enterprise_id=ENTERPRISE_ID,
                username="admin",
                email="admin@example.com",
                display_name="Admin",
            )
        )
        # Flushed BEFORE the case: SQLAlchemy's unit of work orders `cases`
        # ahead of `organizations` in a single flush (the organizations mapper
        # is pushed behind `users` by its nullable owner_id FK), which trips
        # the very FK enforcement this fixture turns on.
        await session.commit()
        session.add(
            CaseModel(
                case_id=CASE_ID,
                organization_id=ORG_ID,
                title="Connection pool exhaustion",
            )
        )
        await session.commit()
    yield db_path, factory
    await engine.dispose()


def _service(session_factory, *, capacity: Optional[int] = None) -> SuggestionService:
    """One worker's service — no LLM (extraction falls back to its template),
    no sanitizer (the scan marks CLEAN), a real database store."""
    return SuggestionService(
        knowledge_service=None,
        sanitizer=None,
        llm_provider=None,
        max_stored_suggestions=capacity,
        suggestion_repository=DatabaseSuggestionRepository(session_factory),
    )


async def _extract(service, *, organization_id: str = ORG_ID):
    return await service.extract_knowledge_from_case(
        case_id=CASE_ID,
        organization_id=organization_id,
        extracted_by=USER_ID,
    )


# ---------------------------------------------------------------------------
# 1. Durability across a restart
# ---------------------------------------------------------------------------


class TestTheSuggestionSurvivesARestart:
    async def test_a_pending_review_is_still_there_after_the_process_dies(self, db):
        """The whole service object — and with it the old dict — is discarded
        between the write and the read."""
        db_path, factory = db

        first_process = _service(factory)
        suggestion = await _extract(first_process)
        suggestion_id = suggestion.suggestion_id
        del first_process

        # A new engine as well as a new service: nothing but the file on disk
        # carries state across this line.
        restarted_engine = _make_engine(db_path)
        restarted_factory = async_sessionmaker(
            restarted_engine, class_=AsyncSession, expire_on_commit=False
        )
        try:
            second_process = _service(restarted_factory)
            reloaded = await second_process.get_suggestion(suggestion_id)

            assert reloaded is not None, (
                "the suggestion did not survive the restart — this is the "
                "non-durable store #1227 replaces"
            )
            assert reloaded.suggestion_id == suggestion_id
            assert reloaded.status is SuggestionStatus.PENDING_REVIEW
            assert reloaded.case_id == CASE_ID
            assert reloaded.organization_id == ORG_ID
        finally:
            await restarted_engine.dispose()

    async def test_the_content_survives_intact_not_just_the_row(self, db):
        """A row that came back with an empty article would satisfy "it
        survived" and be useless to the reviewer."""
        _db_path, factory = db

        suggestion = await _extract(_service(factory))
        reloaded = await _service(factory).get_suggestion(suggestion.suggestion_id)

        assert reloaded.suggested_content == suggestion.suggested_content
        assert reloaded.suggested_title == suggestion.suggested_title
        assert reloaded.suggested_content.strip(), "the article came back empty"

    async def test_the_quality_gate_verdict_survives(self, db):
        """``validation_passed`` / ``errors`` / ``warnings`` are the three
        fields #1226 added to the domain object and migration 045 adds columns
        for. Without the columns they are dropped on write and the reviewer is
        shown ``passed: null`` — "not yet evaluated" — about a draft the
        extractor HAD evaluated, which the API contract explicitly warns must
        never be read as "fine".
        """
        _db_path, factory = db

        suggestion = await _extract(_service(factory))
        assert suggestion.validation_passed is not None, (
            "extraction is supposed to record a verdict; this test proves "
            "nothing if it did not"
        )

        reloaded = await _service(factory).get_suggestion(suggestion.suggestion_id)

        assert reloaded.validation_passed == suggestion.validation_passed
        assert reloaded.validation_errors == suggestion.validation_errors
        assert reloaded.validation_warnings == suggestion.validation_warnings

    async def test_a_never_evaluated_verdict_stays_null_rather_than_false(self, db):
        """The three-valued reading is why the column is nullable with no
        server default: ``None`` means not yet evaluated, ``False`` means
        evaluated and refused, and collapsing the first into the second is a
        claim nobody made."""
        _db_path, factory = db
        repository = DatabaseSuggestionRepository(factory)

        await repository.save(
            KnowledgeSuggestion(
                suggestion_id="sug_unevaluated",
                organization_id=ORG_ID,
                case_id=CASE_ID,
                suggested_title="Never checked",
                suggested_content="## Problem\n...",
                extracted_by=USER_ID,
            )
        )

        reloaded = await repository.get("sug_unevaluated")
        assert reloaded.validation_passed is None
        assert reloaded.validation_errors == []


# ---------------------------------------------------------------------------
# 1b. What a read hands back is a DETACHED COPY
# ---------------------------------------------------------------------------


class TestAReadHandsBackADetachedCopy:
    """Mutating what a read returned changes nothing until it is saved.

    This is not a style preference — it is the database's actual behaviour
    (a new session per call, so no identity map across calls) and the whole
    reason ``SuggestionService`` now saves explicitly on every write path. It
    is pinned HERE, on both implementations, because the in-memory double is
    what the unit tests run against: a double that handed back its own live
    object would let the service forget a ``save()`` and still pass everywhere
    except production.
    """

    async def test_the_database_repository_returns_a_detached_copy(self, db):
        _db_path, factory = db
        repository = DatabaseSuggestionRepository(factory)
        suggestion = await _extract(_service(factory))

        loaded = await repository.get(suggestion.suggestion_id)
        loaded.suggested_title = "edited but never saved"

        again = await repository.get(suggestion.suggestion_id)
        assert again.suggested_title != "edited but never saved"

    async def test_the_in_memory_double_returns_a_detached_copy_too(self):
        """The double has to diverge from the database in NO observable way
        here, or it stops being able to catch a missing save."""
        from faultmaven.modules.knowledge.infrastructure.persistence.suggestion_repository import (  # noqa: E501
            InMemorySuggestionRepository,
        )

        repository = InMemorySuggestionRepository()
        await repository.save(
            KnowledgeSuggestion(
                suggestion_id="sug_copy_check",
                organization_id=ORG_ID,
                case_id=CASE_ID,
                suggested_title="Original",
                suggested_content="## Problem\n...",
                extracted_by=USER_ID,
            )
        )

        loaded = await repository.get("sug_copy_check")
        loaded.suggested_title = "edited but never saved"
        assert (await repository.get("sug_copy_check")).suggested_title == "Original"

        # ...and the object handed to save() is copied on the way IN as well,
        # so a later mutation of the caller's object does not leak into the
        # store behind its back.
        held = KnowledgeSuggestion(
            suggestion_id="sug_copy_check_2",
            organization_id=ORG_ID,
            case_id=CASE_ID,
            suggested_title="Original",
            suggested_content="## Problem\n...",
            extracted_by=USER_ID,
        )
        await repository.save(held)
        held.suggested_title = "mutated after the save"
        assert (await repository.get("sug_copy_check_2")).suggested_title == "Original"

    async def test_seeding_and_peeking_the_double_copy_as_well(self):
        """``seed``/``peek`` are the double's synchronous affordances for test
        setup. They have to obey the same rule, or a test using them measures
        different semantics from a test using ``save``/``get``."""
        from faultmaven.modules.knowledge.infrastructure.persistence.suggestion_repository import (  # noqa: E501
            InMemorySuggestionRepository,
        )

        repository = InMemorySuggestionRepository()
        seeded = KnowledgeSuggestion(
            suggestion_id="sug_seeded",
            organization_id=ORG_ID,
            case_id=CASE_ID,
            suggested_title="Original",
            suggested_content="## Problem\n...",
            extracted_by=USER_ID,
        )
        repository.seed(seeded)
        seeded.suggested_title = "mutated after seeding"
        assert repository.peek("sug_seeded").suggested_title == "Original"

        peeked = repository.peek("sug_seeded")
        peeked.suggested_title = "mutated after peeking"
        assert repository.peek("sug_seeded").suggested_title == "Original"


# ---------------------------------------------------------------------------
# 1c. Timestamps come back timezone-aware
# ---------------------------------------------------------------------------


class TestTimestampsComeBackAware:
    """SQLite has no timezone type, so every datetime it returns is naive even
    though it was written as UTC.

    Left un-normalised it reaches ``to_api_response`` and the extract route,
    both of which serialise with ``to_json_compatible``: the review inbox then
    publishes ``extracted_at`` and ``created_at`` with no offset, and a client
    parses them as LOCAL time — so the lineage footer ("extracted 2h ago")
    reads hours wrong in either direction depending on where the reviewer sits.
    """

    async def test_every_timestamp_on_a_reloaded_suggestion_carries_an_offset(self, db):
        _db_path, factory = db
        service = _service(factory)
        suggestion = await _extract(service)
        await service.reject_suggestion(
            suggestion_id=suggestion.suggestion_id,
            reviewed_by=ADMIN_ID,
            rejection_reason="not reusable",
            organization_id=ORG_ID,
        )

        reloaded = await _service(factory).get_suggestion(suggestion.suggestion_id)

        for field in ("created_at", "updated_at", "extracted_at", "reviewed_at"):
            value = getattr(reloaded, field)
            assert value is not None, f"{field} was not persisted"
            assert value.tzinfo is not None, (
                f"{field} came back naive; serialised with no offset a client "
                f"reads it as local time"
            )

    async def test_the_api_response_carries_the_offset(self, db):
        """The consequence, at the surface that actually publishes it."""
        _db_path, factory = db
        service = _service(factory)
        suggestion = await _extract(service)

        reloaded = await service.get_suggestion(suggestion.suggestion_id)
        body = service.to_api_response(reloaded, include_content=True)

        assert (
            body["extracted_at"].endswith("Z") or "+00:00" in body["extracted_at"]
        ), f"extracted_at published without an offset: {body['extracted_at']!r}"


# ---------------------------------------------------------------------------
# 2. Two independent services — the multi-worker symptom
# ---------------------------------------------------------------------------


class TestTwoWorkersSeeTheSameSuggestion:
    async def test_a_suggestion_extracted_by_one_worker_is_visible_to_another(self, db):
        """THE cross-pod defect. Two services built independently and never
        introduced — which is what two uvicorn workers, or two of the three
        replicas the cloud manifest declares, actually are."""
        _db_path, factory = db
        worker_a = _service(factory)
        worker_b = _service(factory)
        assert worker_a is not worker_b

        suggestion = await _extract(worker_a)

        found = await worker_b.get_suggestion_visible(
            suggestion.suggestion_id, organization_id=ORG_ID
        )
        assert found is not None, (
            "worker B could not see worker A's suggestion — this is the "
            "intermittent 404 that a stickiness-free 3-replica deployment hits "
            "on roughly two thirds of approvals"
        )
        assert found.suggested_content == suggestion.suggested_content

    async def test_a_review_inbox_listing_on_one_worker_shows_the_others_work(self, db):
        """The reviewer's actual first surface. A per-worker store made the
        inbox show whichever subset that pod happened to hold."""
        _db_path, factory = db
        worker_a, worker_b = _service(factory), _service(factory)

        first = await _extract(worker_a)
        second = await _extract(worker_b)

        listed = await _service(factory).list_suggestions(organization_id=ORG_ID)
        ids = {s.suggestion_id for s in listed["suggestions"]}
        assert {first.suggestion_id, second.suggestion_id} <= ids
        assert listed["total_count"] == 2

    async def test_a_decision_taken_on_one_worker_is_seen_by_the_other(self, db):
        """Not just visibility of the row — the WRITE side too. A rejection
        recorded on worker A has to be what worker B reads, or two reviewers
        can decide the same suggestion twice."""
        _db_path, factory = db
        worker_a, worker_b = _service(factory), _service(factory)
        suggestion = await _extract(worker_a)

        rejected = await worker_a.reject_suggestion(
            suggestion_id=suggestion.suggestion_id,
            reviewed_by=ADMIN_ID,
            rejection_reason="not reusable",
            organization_id=ORG_ID,
        )
        assert rejected is True

        seen = await worker_b.get_suggestion(suggestion.suggestion_id)
        assert seen.status is SuggestionStatus.REJECTED
        assert seen.rejection_reason == "not reusable"

    async def test_another_tenant_still_cannot_see_it(self, db):
        """Shared storage must not become shared visibility. The predicate is
        in SQL now rather than in a dict comprehension, so it is worth
        re-pinning at this layer."""
        _db_path, factory = db
        suggestion = await _extract(_service(factory))

        assert (
            await _service(factory).get_suggestion_visible(
                suggestion.suggestion_id, organization_id=OTHER_ORG_ID
            )
            is None
        )
        listed = await _service(factory).list_suggestions(organization_id=OTHER_ORG_ID)
        assert listed["suggestions"] == []


# ---------------------------------------------------------------------------
# 3. The store is still bounded — now per organization, over the table
# ---------------------------------------------------------------------------


async def _seed_terminal(factory, suggestion_id, *, org=ORG_ID, age_seconds=0):
    repository = DatabaseSuggestionRepository(factory)
    suggestion = KnowledgeSuggestion(
        suggestion_id=suggestion_id,
        organization_id=org,
        case_id=CASE_ID,
        status=SuggestionStatus.APPROVED,
        suggested_title="Old decision",
        suggested_content="## Problem\n...",
        extracted_by=USER_ID,
        pii_scan_status=PIIScanStatus.CLEAN,
    )
    suggestion.updated_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    await repository.save(suggestion)


class TestTheStoreIsStillBounded:
    async def test_the_oldest_reviewed_row_is_deleted_to_make_room(self, db):
        _db_path, factory = db
        await _seed_terminal(factory, "sug_old", age_seconds=900)
        await _seed_terminal(factory, "sug_recent", age_seconds=10)

        await _extract(_service(factory, capacity=2))

        repository = DatabaseSuggestionRepository(factory)
        assert await repository.get("sug_old") is None
        assert await repository.get("sug_recent") is not None
        assert await repository.count_for_organization(ORG_ID) == 2

    async def test_a_queue_of_unreviewed_work_refuses_instead_of_evicting(self, db):
        """The one thing in this store that exists nowhere else."""
        _db_path, factory = db
        service = _service(factory, capacity=1)
        pending = await _extract(service)

        with pytest.raises(ServiceUnavailableException, match="at capacity"):
            await _extract(service)

        assert await service.get_suggestion(pending.suggestion_id) is not None

    async def test_the_cap_is_scoped_to_one_organization(self, db):
        """The durable store is ONE table shared by every tenant, so a
        deployment-wide count would let another tenant's undrained inbox refuse
        this tenant's extraction. Org beta is full of unreviewed work; org
        alpha must still be able to extract."""
        _db_path, factory = db
        repository = DatabaseSuggestionRepository(factory)
        await repository.save(
            KnowledgeSuggestion(
                suggestion_id="sug_beta_pending",
                organization_id=OTHER_ORG_ID,
                case_id=CASE_ID,
                suggested_title="Beta's problem",
                suggested_content="## Problem\n...",
                extracted_by=USER_ID,
            )
        )

        suggestion = await _extract(_service(factory, capacity=1))

        assert suggestion is not None
        assert await repository.get("sug_beta_pending") is not None


# ---------------------------------------------------------------------------
# 4. The organization id has to be a real one
# ---------------------------------------------------------------------------


class TestTheOrganizationIdIsAForeignKey:
    async def test_the_literal_the_route_used_to_send_is_rejected(self, db):
        """``getattr(case, "organization_id", "default")`` — the extract
        route's fallback until #1227 — is not an organization id.

        It was inert while the store was a dict keyed by nothing. Against the
        table it fails the ``organizations`` foreign key outright, which is why
        the org-resolution fix had to ship WITH the durable store rather than
        after it. This test is the measurement of that claim, not a restatement
        of it.
        """
        _db_path, factory = db

        with pytest.raises(Exception) as excinfo:
            await _extract(_service(factory), organization_id="default")

        assert "foreign key" in str(excinfo.value).lower(), str(excinfo.value)

    async def test_a_real_organization_id_is_accepted(self, db):
        """The complement — without it the test above would pass on a store
        that refused everything."""
        _db_path, factory = db

        suggestion = await _extract(_service(factory), organization_id=ORG_ID)

        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        select(KnowledgeSuggestionModel).where(
                            KnowledgeSuggestionModel.suggestion_id
                            == suggestion.suggestion_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1
        assert rows[0].organization_id == ORG_ID
