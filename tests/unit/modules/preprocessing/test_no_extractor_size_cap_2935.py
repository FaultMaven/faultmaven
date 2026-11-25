"""#2935: no Tier 1 extractor bounds its input below the upload limit.

Nine extractors refused content over a fixed 50,000,000 characters with
"[File exceeds 50MB maximum size limit for extraction]", and the sanity check
passed it, so under ``MAX_UPLOAD_SIZE_MB=50`` an upload just past that count
was stored as the placeholder. The upload limit bounds the content and the
Tier 1 worker bounds the extraction (Principle 14, #2637).
"""

from __future__ import annotations

import pytest

from faultmaven.config.settings import reset_settings
from faultmaven.models.api import DataType
from faultmaven.modules.preprocessing.classifier import DataClassifier
from faultmaven.modules.preprocessing.extractors import LogsAndErrorsExtractor
from faultmaven.modules.preprocessing.preprocessing_service import (
    PreprocessingService,
)

pytestmark = pytest.mark.unit

HEAD = "2026-11-25T08:00:00Z ERROR [payments] upstream connect timeout pool=primary\n"
LINE = "2026-11-25T08:00:01Z INFO [api] GET /orders/7 status=200 duration_ms=12\n"


@pytest.fixture
def fifty_mb_upload_limit(monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "50")
    reset_settings()
    yield
    reset_settings()


async def test_a_log_past_fifty_million_characters_gets_an_extract_or_the_preview(
    fifty_mb_upload_limit,
):
    """Through the real Tier 1 worker: the extractor's own extract, or the
    preview its deadline or a limit ends in (the worker's business). Neither
    is the placeholder, and both keep the log's head."""
    content = (HEAD + LINE * (50_000_001 // len(LINE) + 1))[:50_000_001]
    service = PreprocessingService(
        classifier=DataClassifier(), logs_extractor=LogsAndErrorsExtractor()
    )
    assert service._upload_mb == 50, "premise: the upload admits this log"

    result = await service.classify_and_extract(
        content=content, filename="app.log", principal="u1"
    )

    # An extract is stored as its ExtractResult's JSON, a preview as text.
    stored = result.structural_index
    assert result.detailed_data_type == DataType.LOGS_AND_ERRORS
    assert "[File exceeds" not in stored, stored
    metadata = result.extraction_metadata or {}
    if result.extraction_method == "structure_extraction":
        assert metadata.get("timeout_fallback") or metadata.get("error_fallback")
    else:
        assert result.extraction_method == LogsAndErrorsExtractor().strategy_name
    assert HEAD.strip() in stored
