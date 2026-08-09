"""In-memory `add_uploaded_file` must not certify a wrong case_id as success."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
    RepositoryException,
)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_unknown_case_id_raises_rather_than_no_op():
    """The SQL implementations fail on the FK and the contract documents
    RepositoryException. A silent return here would let a caller passing the
    wrong case id pass every in-memory-backed test while writing nothing.
    """
    from faultmaven.modules.case.domain.models import UploadedFile

    repo = InMemoryCaseRepository()
    uploaded = UploadedFile(
        file_id=f"file_{uuid4().hex[:12]}",
        filename="app.log",
        size_bytes=10,
        upload_source="file_upload",
        uploaded_at_turn=1,
        uploaded_at=datetime.now(timezone.utc),
    )

    with pytest.raises(RepositoryException):
        await repo.add_uploaded_file("case_does_not_exist", uploaded, "org_1")
