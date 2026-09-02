"""050_strip_cause_record_metadata

Drop the two keys the retired cause-record pipeline wrote into
``knowledge_items.metadata``: ``causes`` (the per-Cause graph record the KB
cause seeder instantiated as candidate hypotheses) and ``chunk_stamp`` (the
identity of the ``cause_letters`` stamp written on each chunk). fm#1295 removed
the seeder (#1307) and then the record, the stamp and every reader of both
(#1309); ``ingest_runbook`` writes ``metadata={}`` since then.

Left in place the old values were not merely inert, they were paid for: the
repository decodes and deep-copies the metadata blob on every read, and the 91
built-in runbooks carried ~1.66 MB of ``causes`` JSON between them, so every KB
listing parsed it for nothing. Rows are rewritten only when one of the two keys
is present; any other key a row carries is preserved.

Downgrade is a no-op by design: the values were derived from the runbook
markdown and the (now removed) grammar, and the code that would read them back
no longer exists. Re-creating them would re-create dead data.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-09-02 12:00:00
"""

import json
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DEAD_KEYS = ("causes", "chunk_stamp")


def _decode(value):
    """The stored blob as a dict, or None when it is not an object.

    SQLite stores the column as TEXT (JSON string); PostgreSQL as JSONB, which
    the driver hands back already decoded.
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text('SELECT item_id, "metadata" FROM knowledge_items')
    ).fetchall()
    if bind.dialect.name == "postgresql":
        stmt = sa.text(
            'UPDATE knowledge_items SET "metadata" = CAST(:m AS JSONB) '
            "WHERE item_id = :i"
        )
    else:
        stmt = sa.text('UPDATE knowledge_items SET "metadata" = :m WHERE item_id = :i')
    for item_id, raw in rows:
        decoded = _decode(raw)
        if decoded is None or not any(k in decoded for k in _DEAD_KEYS):
            continue
        cleaned = {k: v for k, v in decoded.items() if k not in _DEAD_KEYS}
        bind.execute(stmt, {"m": json.dumps(cleaned, ensure_ascii=False), "i": item_id})


def downgrade() -> None:
    # Derived data with no remaining reader; nothing to restore.
    pass
