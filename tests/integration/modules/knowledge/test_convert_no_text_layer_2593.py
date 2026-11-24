"""``POST /knowledge/convert`` refuses a PDF with no text layer as
``NO_TEXT_LAYER`` (#2593): through the route, with the real service, the real
preprocessor and its worker. It answered ``FILE_CORRUPT``, whose Dashboard copy
told the reader the file was corrupt."""

from __future__ import annotations

import pytest

from tests.unit.modules.knowledge.test_document_parser import _blank_page_pdf
from tests.unit.modules.knowledge.test_document_preprocessor_existing_runbook import (
    _app_with_real_service,
)


@pytest.mark.integration
def test_post_convert_answers_an_image_only_pdf_with_no_text_layer(tmp_path):
    """The detail names the route's temp file, so only its sentence is
    asserted."""
    path, content_type = _blank_page_pdf(tmp_path)

    with _app_with_real_service() as client:
        response = client.post(
            "/api/v1/knowledge/convert",
            data={"scope": "personal"},
            files={"file": ("scan.pdf", path.read_bytes(), content_type)},
        )

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error_code"] == "NO_TEXT_LAYER"
    assert body["detail"].startswith("Failed to parse ")
    assert body["detail"].endswith(
        ".pdf: No extractable text in PDF. The file may be scanned/image-only."
    )
