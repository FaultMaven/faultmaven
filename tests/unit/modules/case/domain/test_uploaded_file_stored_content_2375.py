"""``UploadedFile.has_stored_content``: the one "can the raw file be read" test.

The readers, the prompt's ``file_backed`` marker and the engine's forced-tool
gate all ask it (#2375), so it holds exactly when a storage backend key is set.
"""

import pytest

from faultmaven.modules.case.contracts import UploadedFile

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "storage_ref,expected",
    [("org/case/2026-10-28/app.log", True), (None, False), ("", False)],
    ids=["stored", "never_stored", "empty_key"],
)
def test_it_holds_exactly_when_a_storage_key_is_set(storage_ref, expected):
    upload = UploadedFile(
        filename="app.log",
        size_bytes=100,
        uploaded_at_turn=1,
        storage_ref=storage_ref,
    )

    assert upload.has_stored_content is expected
