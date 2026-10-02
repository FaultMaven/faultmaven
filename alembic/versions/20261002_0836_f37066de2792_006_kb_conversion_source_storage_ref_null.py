"""006_kb_conversion_source_storage_ref_null

KB conversion-source rows carry no ``storage_ref`` (#836).

``uploaded_files.storage_ref`` is a storage-backend key (#689), or NULL. Two KB
writers stored something else in it: a filesystem path. ``upload_document``
stored the runbook's path, and the disk scan stored the scanned file's path.
Both write NULL now. This revision brings the rows they already wrote into
line, so the column holds one kind of value in every row.

Which rows
----------

Exactly the rows those writers create: ``upload_source = 'conversion_source'``
with no ``case_id``. Both writers set exactly that pair, and the evidence
writer always sets a ``case_id``, so an evidence row's backend key is never
touched. Rows already NULL are left out, so the count logged is the number of
paths removed.

Nothing reads the old value. Nothing ever opened a file by it; its one reader
echoed it to the client as ``source_file.retained_path``, and that field is
gone. What locates a runbook is ``conversion_drafts.file_path``, which this
revision does not touch.

Row-level security
------------------

``uploaded_files`` is tenant-scoped. On PostgreSQL the UPDATE runs under
``SET LOCAL row_security = off``. The role that runs the migrations owns the
table, and the baseline ``ENABLE``s its policy without ``FORCE``-ing it, so the
owner is exempt and the setting changes nothing for it: every enterprise's rows
are updated. A role the policy would filter is not exempt, and under
``row_security = off`` its UPDATE RAISES instead of updating only the rows one
enterprise can see and reporting success. Revision 003 makes the same choice
for the same reason.

``env.py`` runs every revision in one transaction, so ``SET LOCAL`` would last
for any revision after this one in the same run. The setting goes back to
``DEFAULT`` once the UPDATE has run. That is the value it had before, because
neither ``env.py`` nor any revision sets ``row_security`` for the session.

SQLite has no row-level security; the UPDATE runs as it is.

``downgrade()`` changes nothing, because nothing at the parent revision needs
the paths back. Its code only echoed the value, as ``retained_path``, and read
NULL as an empty string. It already stored NULL for every other conversion
source: document conversions, case conversions and manually created runbooks.

Revision ID: f37066de2792
Revises: 1c5a2ad13a65
Create Date: 2026-10-02 08:36:02
"""

import logging
from typing import Sequence, Union

from sqlalchemy import text

from alembic import op

revision: str = "f37066de2792"
down_revision: Union[str, Sequence[str], None] = "1c5a2ad13a65"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Exactly the rows the two KB writers create, and only those still holding a
#: value. Frozen here rather than built from the ORM, because migrations are
#: history.
NULL_CONVERSION_SOURCE_REFS = (
    "UPDATE uploaded_files SET storage_ref = NULL "
    "WHERE upload_source = 'conversion_source' "
    "AND case_id IS NULL "
    "AND storage_ref IS NOT NULL"
)

#: A role the policy would filter raises instead of updating a subset.
ROW_SECURITY_OFF = "SET LOCAL row_security = off"
#: Back to the value before, for whatever runs later in the same transaction.
ROW_SECURITY_RESTORED = "SET LOCAL row_security TO DEFAULT"


def upgrade() -> None:
    """NULL every conversion-source ``storage_ref``, and log how many."""
    context = op.get_context()
    postgresql = context.dialect.name == "postgresql"
    if postgresql:
        op.execute(ROW_SECURITY_OFF)
    if context.as_sql:
        # Offline (``--sql``): the statement only, with no count to log.
        op.execute(NULL_CONVERSION_SOURCE_REFS)
    else:
        nulled = op.get_bind().execute(text(NULL_CONVERSION_SOURCE_REFS)).rowcount
        logging.getLogger("alembic.runtime.migration").info(
            "006: cleared storage_ref on %d KB conversion-source row(s)", nulled
        )
    if postgresql:
        op.execute(ROW_SECURITY_RESTORED)


def downgrade() -> None:
    """Nothing to restore: the parent revision's code reads a NULL
    ``storage_ref`` on these rows as it always has, so no path is needed back."""
