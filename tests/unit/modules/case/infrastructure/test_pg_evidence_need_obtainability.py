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
