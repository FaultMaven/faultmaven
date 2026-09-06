"""PostgreSQL obtainability persistence path (verification-status Phase 3).

The SQLite round-trip is covered in ``tests/unit/modules/case/test_evidence_needs.py``,
but the PostgreSQL repository is a separate implementation whose real path is
behind ``@pytest.mark.cloud`` (no Postgres in CI) — exactly the "PostgreSQL-only
dark path slips past SQLite CI" class this suite guards. These tests need no live
Postgres: the row→model reconstruction is pure, and a column/param drop is
observable in the module source.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from faultmaven.modules.case.domain.models import NeedObtainability
from faultmaven.modules.case.infrastructure import (
    postgresql_hybrid_case_repository as _repo_module,
)
from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository import (
    PostgreSQLHybridCaseRepository,
)

_REPO_SOURCE = Path(_repo_module.__file__).read_text()


def _repo() -> PostgreSQLHybridCaseRepository:
    session = MagicMock()
    dialect = MagicMock()
    dialect.name = "postgresql"
    session.bind = MagicMock()
    session.bind.dialect = dialect
    return PostgreSQLHybridCaseRepository(session)


def _row(obtainability: str | None):
    # Column order per the SELECT: need_id, purpose, request_text, rationale,
    # priority, state, motivating_hypothesis_ids, superseded_reason,
    # created_at_turn, created_at, updated_at, obtainability.
    now = datetime.now(UTC)
    return (
        "eneed_000000000001",
        "causal_verification",
        "pool saturation metrics",
        "distinguishes exhaustion from timeout",
        "medium",
        "pending",
        [],  # JSONB list (asyncpg returns a Python list)
        None,
        3,
        now,
        now,
        obtainability,
    )


@pytest.mark.unit
class TestPgObtainabilityReconstruction:
    def test_row_index_11_maps_to_obtainability(self):
        need = _repo()._row_to_evidence_need(
            _row("unobtainable"),
            case_id="case_ce0000000001",
            fulfilling_evidence_ids=[],
        )
        assert need is not None
        assert need.obtainability == NeedObtainability.UNOBTAINABLE

    def test_none_obtainability_defaults_unknown(self):
        need = _repo()._row_to_evidence_need(
            _row(None),
            case_id="case_ce0000000001",
            fulfilling_evidence_ids=[],
        )
        assert need is not None
        assert need.obtainability == NeedObtainability.UNKNOWN


@pytest.mark.unit
class TestPgObtainabilitySql:
    """Structural guard: a dropped column/param would silently ship green on a
    Postgres deploy. Scan the source so any future reintroduction is caught."""

    def test_select_includes_obtainability(self):
        select_block = _REPO_SOURCE.split("FROM evidence_needs")[0]
        assert "obtainability" in select_block.rsplit("SELECT", 1)[1]

    def test_insert_and_param_include_obtainability(self):
        insert_block = _REPO_SOURCE.split("INSERT INTO evidence_needs")[1].split(
            "junction"
        )[0]
        # column list + VALUES placeholder + ON CONFLICT update + bound param
        assert "obtainability" in insert_block
        assert ":obtainability" in insert_block
        assert '"obtainability": need.obtainability.value' in insert_block


# ============================================================
# Ask history persistence (#1079)
# ============================================================


def _row_with_surfaced(surfaced, engine_inferred=False):
    """As ``_row``, plus the trailing ``surfaced_turns`` / ``engine_inferred``
    columns (#1079)."""
    return _row("unknown") + (surfaced, engine_inferred)


@pytest.mark.unit
class TestPgSurfacedTurnsReconstruction:
    """``surfaced_turns`` is what makes the ask count durable across turns.
    Losing it on the Postgres path would put Cloud back in the fm#1079 state —
    every repeat reading as a first mention — while SQLite CI stayed green.
    """

    def test_jsonb_list_maps_to_surfaced_turns(self):
        need = _repo()._row_to_evidence_need(
            _row_with_surfaced([3, 7, 11]),
            case_id="case_ce0000000001",
            fulfilling_evidence_ids=[],
        )
        assert need is not None
        assert need.surfaced_turns == [3, 7, 11]
        assert need.times_surfaced == 3
        assert need.last_surfaced_turn == 11

    def test_json_string_is_tolerated(self):
        """Dialect-compatibility paths hand back a JSON string, not a list."""
        need = _repo()._row_to_evidence_need(
            _row_with_surfaced("[4, 9]"),
            case_id="case_ce0000000001",
            fulfilling_evidence_ids=[],
        )
        assert need is not None
        assert need.surfaced_turns == [4, 9]

    @pytest.mark.parametrize("bad", [None, "not json"])
    def test_absent_or_corrupt_reads_as_never_surfaced(self, bad):
        """Fail-safe direction: understating the count keeps a live ask
        visible rather than silencing it on a bad blob."""
        need = _repo()._row_to_evidence_need(
            _row_with_surfaced(bad),
            case_id="case_ce0000000001",
            fulfilling_evidence_ids=[],
        )
        assert need is not None
        assert need.surfaced_turns == []

    def test_pre_migration_row_length_is_tolerated(self):
        """A short row (no surfaced_turns column) must not raise."""
        need = _repo()._row_to_evidence_need(
            _row("unknown"),
            case_id="case_ce0000000001",
            fulfilling_evidence_ids=[],
        )
        assert need is not None
        assert need.surfaced_turns == []


@pytest.mark.unit
class TestPgSurfacedTurnsSql:
    def test_engine_inferred_reconstructs(self):
        """Provenance drives the anti-anchoring exclusion; losing it on the
        Postgres path re-arms the stand-down every turn in Cloud only."""
        need = _repo()._row_to_evidence_need(
            _row_with_surfaced([3], engine_inferred=True),
            case_id="case_ce0000000001",
            fulfilling_evidence_ids=[],
        )
        assert need is not None
        assert need.engine_inferred is True

    def test_engine_inferred_defaults_false_on_short_row(self):
        need = _repo()._row_to_evidence_need(
            _row("unknown"),
            case_id="case_ce0000000001",
            fulfilling_evidence_ids=[],
        )
        assert need is not None
        assert need.engine_inferred is False

    def test_select_includes_surfaced_turns(self):
        select_block = _REPO_SOURCE.split("FROM evidence_needs")[0]
        assert "surfaced_turns" in select_block.rsplit("SELECT", 1)[1]
        assert "engine_inferred" in select_block.rsplit("SELECT", 1)[1]

    def test_insert_and_param_include_surfaced_turns(self):
        insert_block = _REPO_SOURCE.split("INSERT INTO evidence_needs")[1].split(
            "junction"
        )[0]
        assert "surfaced_turns = EXCLUDED.surfaced_turns" in insert_block
        assert '"surfaced_turns": json.dumps(need.surfaced_turns)' in insert_block
        assert "engine_inferred = EXCLUDED.engine_inferred" in insert_block
        assert '"engine_inferred": need.engine_inferred' in insert_block


@pytest.mark.unit
class TestSurfacedTurnsMigrationTypeIsJsonbOnPostgres:
    """The schema baseline must declare ``surfaced_turns`` with the PostgreSQL
    JSONB variant, not a bare ``sa.Text()``.

    The repository writes this column through ``_cast('surfaced_turns')``, which
    emits ``CAST(... AS JSONB)`` on PostgreSQL. A TEXT column would accept every
    write on SQLite (where the cast is a no-op) and reject every write in Cloud
    — green CI, broken deploy. Same class of PostgreSQL-only dark path the rest
    of this module guards.
    """

    def test_migration_declares_the_jsonb_variant(self):
        # Globbed, not spelled: the baseline's filename carries its revision
        # hash, so a rename would read here as a missing declaration rather
        # than as the rename it is.
        versions = Path(__file__).resolve().parents[5] / "alembic" / "versions"
        baselines = sorted(versions.glob("*_001_enterprise_baseline.py"))
        assert baselines, "the enterprise baseline migration is gone"
        source = "\n".join(p.read_text() for p in baselines)

        assert "surfaced_turns" in source
        assert "with_variant(" in source and "postgresql.JSONB" in source, (
            "surfaced_turns must carry the postgresql JSONB variant — a bare "
            "sa.Text() rejects the repository's CAST(... AS JSONB) write on "
            "every PostgreSQL save while SQLite CI stays green"
        )
