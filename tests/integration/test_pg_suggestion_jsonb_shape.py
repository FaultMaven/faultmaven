"""``knowledge_suggestions`` JSONB columns hold ONE shape on PostgreSQL (#1227).

PostgreSQL-only, and it has to be: the defect this pins cannot occur on SQLite,
where ``JsonBlob`` is plain TEXT and every value is a string by definition.

``JsonBlob`` is ``Text().with_variant(JSONB, "postgresql")``, so the two
backends want different Python values from a writer. Binding a pre-``json.dumps``'d
string to JSONB does not fail — it stores a JSON **string scalar**. Measured on
PostgreSQL 16 before the fix:

    sug_probe_written   errors=string   raw: "[\\"err one\\", \\"err two\\"]"
    sug_probe_default   errors=array    raw: []

Two shapes in one column, the second of them written by the column's OWN
``server_default '[]'`` in migration 045. Nothing was *broken* — the readers
cope with both — but every JSONB operator (``@>``, ``jsonb_array_length``, a GIN
index) silently misses the written rows, and a column whose default disagrees
with its writer is a trap.

The repository now binds the object on PostgreSQL and the serialised string on
SQLite, so both rows report ``array``. This suite is what stops that regressing:
a future edit that "simplifies" the binding back to an unconditional
``json.dumps`` passes every SQLite test in the tree.

Run it with a PostgreSQL to point at::

    docker run -d -e POSTGRES_PASSWORD=pw -p 5432:5432 postgres:16
    export DATABASE_URL=postgresql+asyncpg://postgres:pw@localhost:5432/postgres
    alembic upgrade head
    pytest tests/integration/test_pg_suggestion_jsonb_shape.py
"""

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from faultmaven.infrastructure.persistence.models import (
    CaseModel,
    EnterpriseModel,
    OrganizationModel,
)
from faultmaven.modules.knowledge.contracts import SuggestionConcurrencyError
from faultmaven.modules.knowledge.domain.models.suggestion import KnowledgeSuggestion
from faultmaven.modules.knowledge.infrastructure.persistence.suggestion_repository import (  # noqa: E501
    DatabaseSuggestionRepository,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
        reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
    ),
]


@pytest.fixture
async def pg(tmp_path_factory):
    """An engine on the PostgreSQL under test, plus disposable FK parents.

    Ids are per-run unique so concurrent or repeated runs cannot collide, and
    everything is removed afterwards — this suite runs against a shared
    database, not a throwaway file.
    """
    engine = create_async_engine(os.environ["DATABASE_URL"], future=True)
    assert (
        engine.dialect.name == "postgresql"
    ), f"must run against PostgreSQL; got dialect={engine.dialect.name!r}"

    suffix = uuid.uuid4().hex[:12]
    ids = {
        "enterprise": f"ent-jsonb-{suffix}",
        "organization": f"org-jsonb-{suffix}",
        "case": f"case-jsonb-{suffix}",
    }
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        session.add(
            EnterpriseModel(
                enterprise_id=ids["enterprise"], name="JSONB probe", slug=suffix
            )
        )
        await session.commit()
    async with factory() as session:
        session.add(
            OrganizationModel(
                organization_id=ids["organization"],
                enterprise_id=ids["enterprise"],
                name="JSONB probe",
                slug=f"org-{suffix}",
            )
        )
        await session.commit()
    async with factory() as session:
        session.add(
            CaseModel(
                case_id=ids["case"],
                organization_id=ids["organization"],
                title="JSONB probe",
            )
        )
        await session.commit()

    yield factory, ids

    async with factory() as session:
        for table, column in (
            ("knowledge_suggestions", "organization_id"),
            ("cases", "organization_id"),
            ("organizations", "organization_id"),
        ):
            await session.execute(
                text(f"DELETE FROM {table} WHERE {column} = :v"),  # nosec B608
                {"v": ids["organization"]},
            )
        await session.execute(
            text("DELETE FROM enterprises WHERE enterprise_id = :v"),
            {"v": ids["enterprise"]},
        )
        await session.commit()
    await engine.dispose()


def _suggestion(ids, suggestion_id, **kwargs):
    return KnowledgeSuggestion(
        suggestion_id=suggestion_id,
        organization_id=ids["organization"],
        case_id=ids["case"],
        suggested_title="JSONB probe",
        suggested_content="## Problem\n...",
        extracted_by="",
        **kwargs,
    )


async def _jsonb_typeof(factory, suggestion_id, column):
    async with factory() as session:
        return (
            await session.execute(
                text(
                    # nosec B608 — `column` is a literal from this module, never
                    # caller input; jsonb_typeof takes no parameterised identifier.
                    f"SELECT jsonb_typeof({column}) FROM knowledge_suggestions "
                    f"WHERE suggestion_id = :i"
                ),
                {"i": suggestion_id},
            )
        ).scalar_one()


class TestTheVerdictColumnsHoldJsonArrays:
    async def test_a_written_verdict_is_stored_as_an_array_not_a_string(self, pg):
        factory, ids = pg
        repository = DatabaseSuggestionRepository(factory)
        suggestion = _suggestion(ids, f"sug_w_{uuid.uuid4().hex[:8]}")
        suggestion.set_validation(False, ["err one", "err two"], ["warn one"])
        await repository.save(suggestion)

        for column in ("validation_errors", "validation_warnings"):
            assert (
                await _jsonb_typeof(factory, suggestion.suggestion_id, column)
            ) == "array", (
                f"{column} was stored as a JSON string scalar; every JSONB "
                f"operator will miss this row"
            )

    async def test_the_server_default_and_a_write_agree(self, pg):
        """The two ways a row can acquire a value must land on one shape."""
        factory, ids = pg
        repository = DatabaseSuggestionRepository(factory)

        written = _suggestion(ids, f"sug_w_{uuid.uuid4().hex[:8]}")
        written.set_validation(True, [], [])
        await repository.save(written)

        defaulted_id = f"sug_d_{uuid.uuid4().hex[:8]}"
        async with factory() as session:
            await session.execute(
                text(
                    "INSERT INTO knowledge_suggestions "
                    "(suggestion_id, organization_id, suggested_title, "
                    " suggested_content, extracted_at, created_at, updated_at) "
                    "VALUES (:i, :o, 't', 'c', now(), now(), now())"
                ),
                {"i": defaulted_id, "o": ids["organization"]},
            )
            await session.commit()

        written_type = await _jsonb_typeof(
            factory, written.suggestion_id, "validation_errors"
        )
        defaulted_type = await _jsonb_typeof(factory, defaulted_id, "validation_errors")
        assert written_type == defaulted_type == "array", (
            f"the column holds two shapes: a written row is {written_type!r} "
            f"and a defaulted row is {defaulted_type!r}"
        )

    async def test_a_jsonb_operator_actually_finds_a_written_row(self, pg):
        """The consequence, stated as SQL. Against a string scalar ``@>``
        matches nothing, which is how this would have been discovered — as a
        query that quietly returns no rows."""
        factory, ids = pg
        repository = DatabaseSuggestionRepository(factory)
        suggestion = _suggestion(ids, f"sug_w_{uuid.uuid4().hex[:8]}")
        suggestion.set_validation(False, ["Missing required section: Sources"], [])
        await repository.save(suggestion)

        async with factory() as session:
            found = (
                (
                    await session.execute(
                        text(
                            "SELECT suggestion_id FROM knowledge_suggestions "
                            "WHERE validation_errors @> "
                            "  '[\"Missing required section: Sources\"]'::jsonb"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert suggestion.suggestion_id in found

    async def test_the_dict_columns_are_objects_too(self, pg):
        factory, ids = pg
        repository = DatabaseSuggestionRepository(factory)
        suggestion = _suggestion(ids, f"sug_w_{uuid.uuid4().hex[:8]}")
        suggestion.metadata = {"k": "v"}
        suggestion.pii_scan_result = {"pii_detected": False}
        await repository.save(suggestion)

        assert (
            await _jsonb_typeof(factory, suggestion.suggestion_id, "metadata")
        ) == "object"
        assert (
            await _jsonb_typeof(factory, suggestion.suggestion_id, "pii_scan_result")
        ) == "object"

    async def test_every_shape_round_trips_back_through_the_repository(self, pg):
        """Reads must keep working for BOTH shapes: a deployment upgraded from
        an earlier build still holds string-scalar rows written before the fix,
        so the decoder cannot narrow to arrays only."""
        factory, ids = pg
        repository = DatabaseSuggestionRepository(factory)
        suggestion = _suggestion(ids, f"sug_w_{uuid.uuid4().hex[:8]}")
        suggestion.set_validation(False, ["err one"], ["warn one"])
        suggestion.metadata = {"k": "v"}
        await repository.save(suggestion)

        # A legacy row, written the pre-fix way: a JSON string scalar.
        async with factory() as session:
            await session.execute(
                text(
                    "UPDATE knowledge_suggestions "
                    "SET validation_errors = to_jsonb('[\"legacy err\"]'::text) "
                    "WHERE suggestion_id = :i"
                ),
                {"i": suggestion.suggestion_id},
            )
            await session.commit()
        assert (
            await _jsonb_typeof(factory, suggestion.suggestion_id, "validation_errors")
        ) == "string", "the fixture failed to produce the legacy shape"

        reloaded = await repository.get(suggestion.suggestion_id)
        assert reloaded.validation_errors == ["legacy err"]
        assert reloaded.validation_warnings == ["warn one"]
        assert reloaded.metadata == {"k": "v"}


class TestOptimisticConcurrencyOnPostgreSQL:
    """The guard is a conditional UPDATE, so it is worth proving on the engine
    it will actually run against — SQLite and PostgreSQL report ``rowcount``
    through different drivers."""

    async def test_a_stale_write_is_refused(self, pg):
        factory, ids = pg
        repository = DatabaseSuggestionRepository(factory)
        suggestion = _suggestion(ids, f"sug_w_{uuid.uuid4().hex[:8]}")
        await repository.save(suggestion)

        first = await repository.get(suggestion.suggestion_id)
        second = await repository.get(suggestion.suggestion_id)
        assert first.version == second.version

        first.suggested_title = "A wins"
        saved = await repository.save(first)
        assert saved.version == first.version + 1

        second.suggested_title = "B loses"
        with pytest.raises(SuggestionConcurrencyError):
            await repository.save(second)

        final = await repository.get(suggestion.suggestion_id)
        assert final.suggested_title == "A wins"
