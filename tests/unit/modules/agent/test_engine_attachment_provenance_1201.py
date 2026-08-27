"""Regression set for #1201 — page captures reached the engine as file uploads.

``investigation_service`` built the per-attachment dict handed to
``engine.process_turn`` like this::

    is_paste = att.filename.startswith("pasted-content-")
    source = "paste" if is_paste else "file_upload"

The input origin was inferred from the SHAPE OF A SYNTHETIC FILENAME and
nothing else. A page capture is minted as ``page-capture-<ts>.txt``, which does
not match that prefix, so it reached the engine tagged ``file_upload`` —
indistinguishable from a file the user deliberately chose, when nobody chose a
file at all. Page capture is the primary Copilot channel.

Nothing failed: the turn proceeds and the evidence is usable. The casualty is
the engine's view of HOW the data arrived, which surfaces as subtly wrong
reasoning about user intent rather than as an error.

The same file already had the precedence right sixty lines earlier
(``_is_paste_upload`` → ``UploadedFile.is_pasted``): provenance tag first, the
minted-filename shape as the fallback for rows whose tag predates the current
values. Two derivations of one fact, and the engine-dispatch path used the
wrong one. This pins the single shared answer.
"""

from datetime import datetime, timezone

import pytest

from faultmaven.modules.agent.domain.services.investigation_service import (
    _engine_attachment_metadata,
)
from faultmaven.modules.case.contracts import UploadedFile

pytestmark = pytest.mark.unit

MINTED_PASTE = "pasted-content-20260709T105531.txt"
MINTED_CAPTURE = "page-capture-20260709T105531.txt"


def _row(filename: str, upload_source: str, **over) -> UploadedFile:
    return UploadedFile(
        file_id="file_0e0e0e0e0e05",
        filename=filename,
        size_bytes=512,
        content_type="text/plain",
        uploaded_at_turn=1,
        uploaded_at=datetime.now(timezone.utc),
        uploaded_by="user_123",
        upload_source=upload_source,
        storage_ref="evidence/case_x/blob.txt",
        data_type="logs",
        summary="Pod restart loop.",
        **over,
    )


class TestInputOrigin:
    """One derivation, on the row, reconciling the tag with the minted name."""

    def test_a_minted_capture_name_is_a_page_capture(self):
        assert _row(MINTED_CAPTURE, "page_capture").input_origin == "page_capture"

    def test_a_minted_paste_name_is_a_paste(self):
        assert _row(MINTED_PASTE, "text_paste").input_origin == "text_paste"

    def test_a_chosen_file_is_a_file_upload(self):
        assert _row("nginx-error.log", "file_upload").input_origin == "file_upload"

    def test_the_legacy_paste_spelling_normalises(self):
        """``upload_source`` carries both ``paste`` and ``text_paste`` in the
        wild. Callers get ONE answer rather than having to know that."""
        assert _row(MINTED_PASTE, "paste").input_origin == "text_paste"

    def test_the_tag_wins_over_a_name_that_carries_no_prefix(self):
        """A row whose minted name was replaced still reports its true origin."""
        assert _row("notes.txt", "page_capture").input_origin == "page_capture"

    def test_a_users_own_prefix_colliding_filename_is_not_a_capture(self):
        """The user genuinely named a file ``page-capture-notes.txt``. Rule 2:
        the tag on such a row was computed FROM that prefix, so it carries no
        independent information and the row is treated as a chosen file."""
        row = _row("page-capture-notes.txt", "file_upload")

        assert row.input_origin == "file_upload"


class TestTheEngineDispatchMetadata:
    def test_a_page_capture_is_not_reported_as_a_file_upload(self):
        """The defect, stated directly. The primary Copilot channel."""
        meta = _engine_attachment_metadata(_row(MINTED_CAPTURE, "page_capture"))

        assert meta["source_type"] == "page_capture", (
            "a captured page reaches the engine indistinguishable from a file "
            "the user chose to upload"
        )

    def test_a_paste_is_reported_as_a_paste(self):
        meta = _engine_attachment_metadata(_row(MINTED_PASTE, "text_paste"))

        assert meta["source_type"] == "text_paste"

    def test_a_chosen_file_is_still_a_file_upload(self):
        meta = _engine_attachment_metadata(_row("nginx-error.log", "file_upload"))

        assert meta["source_type"] == "file_upload"

    def test_the_rest_of_the_dict_is_unchanged(self):
        """The engine reads these keys by name; only source_type is in scope."""
        row = _row("nginx-error.log", "file_upload")

        meta = _engine_attachment_metadata(row)

        assert meta["file_id"] == row.file_id
        assert meta["filename"] == row.filename
        assert meta["data_type"] == "logs"
        assert meta["size"] == row.size_bytes
        assert meta["summary"] == "Pod restart loop."
        assert meta["storage_ref"] == row.storage_ref
        assert set(meta) == {
            "file_id",
            "filename",
            "data_type",
            "size",
            "source_type",
            "summary",
            "storage_ref",
        }

    def test_absent_optional_fields_still_render_as_empty_strings(self):
        row = _row("x.log", "file_upload").model_copy(
            update={"data_type": None, "summary": None}
        )

        meta = _engine_attachment_metadata(row)

        assert meta["data_type"] == ""
        assert meta["summary"] == ""


class TestItAgreesWithTheSiblingDerivation:
    """``_is_paste_upload`` had the precedence right all along. The two must not
    drift — that divergence IS #1201."""

    @pytest.mark.parametrize(
        "filename,tag",
        [
            (MINTED_PASTE, "text_paste"),
            (MINTED_PASTE, "paste"),
            (MINTED_CAPTURE, "page_capture"),
            ("nginx-error.log", "file_upload"),
            ("page-capture-notes.txt", "file_upload"),
        ],
    )
    def test_is_pasted_and_input_origin_agree(self, filename, tag):
        row = _row(filename, tag)

        assert row.is_pasted == (row.input_origin == "text_paste")

    @pytest.mark.parametrize(
        "filename,tag",
        [
            (MINTED_CAPTURE, "page_capture"),
            (MINTED_PASTE, "text_paste"),
            ("nginx-error.log", "file_upload"),
        ],
    )
    def test_is_page_capture_and_input_origin_agree(self, filename, tag):
        row = _row(filename, tag)

        assert row.is_page_capture == (row.input_origin == "page_capture")
