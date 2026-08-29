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

import asyncio
import itertools
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from faultmaven.exceptions import ConflictError, ServiceUnavailableException
from faultmaven.infrastructure.persistence.models import (
    Base,
    CaseModel,
    EnterpriseModel,
    KnowledgeItemModel,
    KnowledgeSuggestionModel,
    OrganizationModel,
    UserModel,
)
from faultmaven.modules.knowledge.contracts import SuggestionConcurrencyError
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
        max_unreviewed_suggestions=capacity,
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
# 3. The store is bounded by REFUSING, and never deletes
# ---------------------------------------------------------------------------


async def _publish_knowledge_item(factory, item_id: str) -> str:
    """Insert a real ``knowledge_items`` row.

    ``knowledge_suggestions.knowledge_item_id`` is a foreign key, and this
    fixture runs with ``PRAGMA foreign_keys=ON``, so a made-up id fails the
    write. Minting one here is also what makes the "nothing is deleted"
    assertions mean something: the link they check points at a row that exists.
    """
    async with factory() as session:
        session.add(
            KnowledgeItemModel(
                item_id=item_id,
                title="Extracted runbook",
                content="## Problem\n...",
                item_type="runbook",
                scope="global",
            )
        )
        await session.commit()
    return item_id


async def _seed_approved(factory, suggestion_id, *, org=ORG_ID, item_id=None):
    repository = DatabaseSuggestionRepository(factory)
    if item_id is not None:
        await _publish_knowledge_item(factory, item_id)
    await repository.save(
        KnowledgeSuggestion(
            suggestion_id=suggestion_id,
            organization_id=org,
            case_id=CASE_ID,
            status=SuggestionStatus.APPROVED,
            suggested_title="Old decision",
            suggested_content="## Problem\n...",
            extracted_by=USER_ID,
            pii_scan_status=PIIScanStatus.CLEAN,
            knowledge_item_id=item_id,
        )
    )


class TestNothingIsEverDeletedFromTheTable:
    """The #1214 cap evicted decided rows to make room. Over a durable table
    that is permanent destruction of the case → runbook provenance, so the
    policy changed — and these pins say so against a real database."""

    async def test_decided_rows_survive_an_extract_at_capacity(self, db):
        _db_path, factory = db
        await _seed_approved(factory, "sug_old", item_id="kb_abcdef0123456789")
        await _seed_approved(factory, "sug_recent", item_id="kb_0123456789abcdef")

        created = await _extract(_service(factory, capacity=1))

        repository = DatabaseSuggestionRepository(factory)
        assert await repository.get("sug_old") is not None
        assert await repository.get("sug_recent") is not None
        assert await repository.get(created.suggestion_id) is not None
        assert await repository.count_for_organization(ORG_ID) == 3

    async def test_the_knowledge_item_link_is_what_would_have_been_lost(self, db):
        """``knowledge_items`` has no back-pointer, so this column IS the only
        record that a given case produced a given runbook."""
        _db_path, factory = db
        await _seed_approved(factory, "sug_linked", item_id="kb_abcdef0123456789")

        await _extract(_service(factory, capacity=1))

        kept = await DatabaseSuggestionRepository(factory).get("sug_linked")
        assert kept is not None
        assert kept.knowledge_item_id == "kb_abcdef0123456789"


class TestTheUnreviewedQueueIsTheCeiling:
    async def test_a_queue_of_unreviewed_work_refuses_instead_of_evicting(self, db):
        """The one thing in this store that exists nowhere else."""
        _db_path, factory = db
        service = _service(factory, capacity=1)
        pending = await _extract(service)

        with pytest.raises(ServiceUnavailableException, match="at capacity"):
            await _extract(service)

        assert await service.get_suggestion(pending.suggestion_id) is not None

    async def test_decided_rows_do_not_consume_the_quota(self, db):
        _db_path, factory = db
        for i in range(3):
            await _seed_approved(factory, f"sug_done_{i}")

        created = await _extract(_service(factory, capacity=1))

        assert (
            await DatabaseSuggestionRepository(factory).get(created.suggestion_id)
            is not None
        )

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
# 3b. Concurrent writers cannot silently overwrite each other
# ---------------------------------------------------------------------------


class TestConcurrentWritesAreRejectedNotMerged:
    """The lost-update class that shared storage creates.

    While the store was one live in-process object every caller mutated the
    same instance, so "two reviewers" was not expressible. Reading detached
    copies makes it the default: both hold the row at version N, and without a
    check the second full-row write replays its stale snapshot over the first.
    """

    async def test_a_stale_write_is_refused(self, db):
        _db_path, factory = db
        repository = DatabaseSuggestionRepository(factory)
        suggestion = await _extract(_service(factory))

        first = await repository.get(suggestion.suggestion_id)
        second = await repository.get(suggestion.suggestion_id)
        assert first.version == second.version

        first.suggested_title = "Reviewer A's title"
        await repository.save(first)

        second.suggested_title = "Reviewer B's title"
        with pytest.raises(SuggestionConcurrencyError):
            await repository.save(second)

        assert (
            await repository.get(suggestion.suggestion_id)
        ).suggested_title == "Reviewer A's title"

    async def test_a_rejection_is_not_reverted_by_a_concurrent_edit(self, db):
        """The concrete harm, through the service rather than the repository: a
        reviewer's in-flight edit must not undo another reviewer's decision."""
        _db_path, factory = db
        worker_a, worker_b = _service(factory), _service(factory)
        suggestion = await _extract(worker_a)

        # B loads the row (its edit will carry this version)...
        stale = await worker_b.get_suggestion(suggestion.suggestion_id)
        # ...and A decides it first.
        await worker_a.reject_suggestion(
            suggestion_id=suggestion.suggestion_id,
            reviewed_by=ADMIN_ID,
            rejection_reason="not reusable",
            organization_id=ORG_ID,
        )

        stale.suggested_title = "B's edit"
        with pytest.raises(SuggestionConcurrencyError):
            await worker_b._repository.save(stale)

        final = await worker_a.get_suggestion(suggestion.suggestion_id)
        assert final.status is SuggestionStatus.REJECTED
        assert final.rejection_reason == "not reusable"

    async def test_the_version_advances_on_every_write(self, db):
        _db_path, factory = db
        repository = DatabaseSuggestionRepository(factory)
        suggestion = await _extract(_service(factory))

        first = await repository.get(suggestion.suggestion_id)
        assert first.version >= 1

        saved = await repository.save(first)
        assert saved.version == first.version + 1
        assert (
            await repository.get(suggestion.suggestion_id)
        ).version == first.version + 1

    async def test_a_sequence_of_saves_on_a_reloaded_copy_keeps_working(self, db):
        """The complement — the check must bite only on a STALE write, or it
        would break the ordinary edit-then-approve loop."""
        _db_path, factory = db
        service = _service(factory)
        suggestion = await _extract(service)

        for title in ("first edit", "second edit", "third edit"):
            updated = await service.update_suggestion(
                suggestion_id=suggestion.suggestion_id,
                title=title,
                content="## Problem\nStill a problem.\n",
                organization_id=ORG_ID,
            )
            assert updated is not None

        assert (
            await service.get_suggestion(suggestion.suggestion_id)
        ).suggested_title == "third edit"


class TestConcurrentApprovalPublishesOnce:
    """The approve TOCTOU. ``is_approved()`` reads a detached copy, so on two
    pods both pass it; the decision is taken by the optimistically-locked
    UPDATE, and the loser rolls its own publish back out of the global corpus.
    """

    @staticmethod
    def _publishing_knowledge_service(factory, published):
        """A publisher that writes REAL ``knowledge_items`` rows.

        A double minting ids out of thin air would sail past the very foreign
        key this test is about — ``knowledge_suggestions.knowledge_item_id``
        references ``knowledge_items``, and the fixture enforces it — so the
        duplicate-publish assertion would be measuring a list in the test
        rather than the corpus.
        """
        knowledge = MagicMock()
        minted = itertools.count()

        async def _upload(**_kwargs):
            item_id = f"kb_{next(minted):016x}"
            await _publish_knowledge_item(factory, item_id)
            published.append(item_id)
            return {"document_id": item_id}

        async def _rollback(item_id):
            async with factory() as session:
                await session.execute(
                    delete(KnowledgeItemModel).where(
                        KnowledgeItemModel.item_id == item_id
                    )
                )
                await session.commit()
            published.remove(item_id)
            return {"document_id": item_id, "residue": []}

        knowledge.upload_document = AsyncMock(side_effect=_upload)
        knowledge.rollback_uploaded_document = AsyncMock(side_effect=_rollback)
        return knowledge

    @staticmethod
    async def _knowledge_item_ids(factory):
        """What the corpus ACTUALLY holds — not what the double recorded."""
        async with factory() as session:
            rows = (
                (await session.execute(select(KnowledgeItemModel.item_id)))
                .scalars()
                .all()
            )
        return list(rows)

    @staticmethod
    def _pod(factory, knowledge):
        return SuggestionService(
            knowledge_service=knowledge,
            sanitizer=None,
            llm_provider=None,
            suggestion_repository=DatabaseSuggestionRepository(factory),
        )

    async def _ready_suggestion(self, factory):
        service = _service(factory)
        suggestion = await _extract(service)
        stored = await service.get_suggestion(suggestion.suggestion_id)
        stored.pii_scan_status = PIIScanStatus.CLEAN
        await service._repository.save(stored)
        return suggestion.suggestion_id

    async def test_the_second_approval_leaves_exactly_one_knowledge_item(self, db):
        _db_path, factory = db
        suggestion_id = await self._ready_suggestion(factory)

        published: list = []
        knowledge = self._publishing_knowledge_service(factory, published)
        pod_a = self._pod(factory, knowledge)
        pod_b = self._pod(factory, knowledge)

        # Both pods load the row BEFORE either commits — the TOCTOU window.
        a_view = await pod_a.get_suggestion_visible(
            suggestion_id, organization_id=ORG_ID
        )
        b_view = await pod_b.get_suggestion_visible(
            suggestion_id, organization_id=ORG_ID
        )
        assert a_view.version == b_view.version

        result = await pod_a.approve_suggestion(
            suggestion_id=suggestion_id,
            reviewed_by=ADMIN_ID,
            organization_id=ORG_ID,
        )
        assert result is not None

        with pytest.raises(ConflictError) as excinfo:
            await pod_b.approve_suggestion(
                suggestion_id=suggestion_id,
                reviewed_by=ADMIN_ID,
                organization_id=ORG_ID,
            )
        assert excinfo.value.conflict_reason in (
            "already_approved",
            "concurrent_modification",
        )

        in_corpus = await self._knowledge_item_ids(factory)
        assert len(in_corpus) == 1, (
            f"the global corpus kept {len(in_corpus)} knowledge_items rows for "
            f"one suggestion; a duplicate was published and not rolled back"
        )
        assert published == in_corpus
        linked = await pod_a.get_suggestion(suggestion_id)
        assert linked.status is SuggestionStatus.APPROVED
        assert (
            linked.knowledge_item_id == published[0]
        ), "the surviving knowledge item is not the one the suggestion links to"

    async def test_a_truly_concurrent_pair_still_publishes_once(self, db):
        """Both approvals in flight at once, not sequenced by the test.
        Whichever loses must remove its own item."""
        _db_path, factory = db
        suggestion_id = await self._ready_suggestion(factory)

        published: list = []
        knowledge = self._publishing_knowledge_service(factory, published)
        pods = [self._pod(factory, knowledge) for _ in range(2)]

        results = await asyncio.gather(
            *(
                pod.approve_suggestion(
                    suggestion_id=suggestion_id,
                    reviewed_by=ADMIN_ID,
                    organization_id=ORG_ID,
                )
                for pod in pods
            ),
            return_exceptions=True,
        )

        succeeded = [r for r in results if isinstance(r, dict)]
        refused = [r for r in results if isinstance(r, ConflictError)]
        assert len(succeeded) == 1, f"expected exactly one winner, got {results}"
        assert len(refused) == 1, f"expected exactly one refusal, got {results}"
        in_corpus = await self._knowledge_item_ids(factory)
        assert (
            len(in_corpus) == 1
        ), f"the global corpus kept {len(in_corpus)} knowledge_items rows"
        assert succeeded[0]["knowledge_item_id"] == in_corpus[0]


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
