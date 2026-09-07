"""``cases.closure_reason``: the column in the schema, the vocabulary in the model.

This module used to be ``test_migration_040_closure_reason.py`` and exercised
migration 040's data transform — the one-off rewrite that mapped the retired
``closed_after_investigation`` onto the vocabulary that replaced it. ADR-017
collapsed the whole pre-existing chain into a single clean baseline
(``alembic/versions/*_001_enterprise_baseline.py``, no data migration, no
compatibility layer), so 040 no longer exists as a step and there is nothing
left to run: the rows it was written for were discarded rather than converted.

What survives the collapse is not the transform but the two facts it existed to
protect, and they now live in two different places:

* the **column** — ``cases.closure_reason``, nullable, declared by the
  baseline. Asserted against a really-migrated database rather than a
  hand-built table, because "the migration declares it" is the claim, and a
  toy ``CREATE TABLE`` in the test would assert only that the test can type.
* the **vocabulary** — enforced on the ``Case`` model, NOT by the database.
  The baseline puts no CHECK constraint on the column, so a row carrying the
  retired value would be stored happily and refused at hydration. That is why
  the original module's last test said the retired value "fails case hydration
  rather than merely looking stale": with the migration gone, that read-side
  refusal is the *only* thing standing between a bad write and a bad case.

Set-membership of the vocabulary is covered by
``tests/unit/core/investigation/test_symptom_verification_currency.py``
(``TestClosureReasonsAreReasons``); this module deliberately does not restate
it. What is asserted here is the part that module cannot see: that the column
the vocabulary is stored in exists and is wide enough for it, and that the
retired value is still refused where the refusal now happens.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import sqlalchemy as sa

pytestmark = pytest.mark.integration

#: The value migration 040 was written to erase. Kept as a literal so this
#: module still names the thing it was created for: nothing may reintroduce it.
_RETIRED = "closed_after_investigation"

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def migrated_columns() -> dict:
    """``cases`` as a really-migrated database has it, keyed by column name.

    Runs the shipped Alembic chain (now one baseline revision) against a scratch
    SQLite file with the running interpreter, the same way
    ``test_evidence_source_invariant_db.py`` does — the local-dev venv path does
    not exist on the CI runner.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
        db_path = handle.name
    try:
        env = os.environ.copy()
        env["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=_REPO_ROOT,
            env=env,
            check=True,
            capture_output=True,
        )
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            columns = {
                column["name"]: column
                for column in sa.inspect(engine).get_columns("cases")
            }
        finally:
            engine.dispose()
        yield columns
    finally:
        os.unlink(db_path)


def test_the_baseline_declares_a_nullable_closure_reason_column(migrated_columns):
    """The schema half of what migration 040 touched, on the real chain."""

    assert "closure_reason" in migrated_columns, (
        "cases.closure_reason is gone from the migrated schema — every terminal "
        "case's stated reason for ending has nowhere to live"
    )
    assert migrated_columns["closure_reason"]["nullable"] is True, (
        "closure_reason is a sub-categorization of CLOSED: a RESOLVED or "
        "non-terminal case must be able to carry none"
    )


def test_the_column_is_wide_enough_for_every_reason_the_model_allows(
    migrated_columns,
):
    """Schema and vocabulary have to agree, and only a test can see both.

    A reason longer than the column is a write that fails (PostgreSQL) or
    silently truncates (SQLite) at the moment a case closes — the least
    recoverable time to find out.
    """
    from faultmaven.modules.case.domain.models import VALID_CLOSURE_REASONS

    declared_length = migrated_columns["closure_reason"]["type"].length
    longest = max(VALID_CLOSURE_REASONS, key=len)

    assert len(longest) <= declared_length, (
        f"{longest!r} ({len(longest)} chars) does not fit "
        f"cases.closure_reason ({declared_length} chars)"
    )


class TestTheRetiredValueCannotComeBack:
    """The point the deleted migration was ultimately serving.

    Its final test read: "the retired value is validated on READ, so a
    surviving row fails case hydration rather than merely looking stale". The
    migration is gone; the read-side validation is what is left, so that is what
    gets pinned.
    """

    @staticmethod
    def _closed_case(closure_reason: str):
        from datetime import datetime, timedelta, timezone
        from uuid import uuid4

        from faultmaven.modules.case.domain.models import Case, CaseState

        now = datetime.now(timezone.utc)
        return Case(
            case_id=f"case_{uuid4().hex[:12]}",
            user_id="user_001",
            enterprise_id="00000000-0000-0000-0000-000000000002",
            title="Closure reason vocabulary",
            state=CaseState.CLOSED,
            created_at=now - timedelta(hours=1),
            updated_at=now,
            closed_at=now,
            closure_reason=closure_reason,
        )

    def test_a_case_carrying_the_retired_reason_is_refused(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="closure_reason"):
            self._closed_case(_RETIRED)

    @pytest.mark.parametrize(
        "reason",
        [
            # The two outcomes migration 040 mapped the retired value onto.
            # Asserted as a CONTROL: without them the refusal above would pass
            # just as well against a model that refuses every closure reason.
            "mitigation_sufficient",
            "closed_insufficient_evidence",
            # And the reason a case that never investigated ends with.
            "inquiry_only",
        ],
    )
    def test_a_live_reason_still_builds_a_case(self, reason):
        assert self._closed_case(reason).closure_reason == reason
