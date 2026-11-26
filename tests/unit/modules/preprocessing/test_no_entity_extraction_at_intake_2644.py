"""Intake runs no entity extraction, with the registry flag on (#2644).

Phase 4 ran the entity patterns over the whole upload on the event loop,
behind ``FAULTMAVEN_ENTITY_REGISTRY``, and nothing read what it produced.
It is removed (#1708's ruled retirement), so the result carries no entity
fields, and a log naming more distinct IPs than the old per-type cap (500)
writes no ``entities`` block into the evidence metadata.
"""

from unittest.mock import MagicMock

import pytest

from faultmaven.config.settings import get_settings, reset_settings
from faultmaven.core.preprocessing.models import PreprocessingResult
from faultmaven.models.api import DataType
from faultmaven.modules.preprocessing.preprocessing_service import PreprocessingService
from tests.unit.modules.preprocessing.extraction_stubs import CrimeScene


@pytest.fixture
def registry_flag_on(monkeypatch):
    monkeypatch.setenv("FAULTMAVEN_ENTITY_REGISTRY", "true")
    reset_settings()
    yield
    reset_settings()


@pytest.mark.unit
async def test_a_log_upload_extracts_no_entities(registry_flag_on):
    assert get_settings().preprocessing.entity_registry_enabled is True
    classifier = MagicMock()
    verdict = classifier.classify.return_value
    verdict.data_type = DataType.LOGS_AND_ERRORS
    verdict.confidence = 0.9
    verdict.source = "rule_based"
    verdict.source_type = None
    verdict.classification_failed = False
    verdict.suggested_types = None
    service = PreprocessingService(classifier=classifier, logs_extractor=CrimeScene())
    content = "\n".join(
        f"2024-01-01T00:00:00Z ERROR refused from 10.0.{i // 256}.{i % 256} port 22"
        for i in range(501)
    )

    result = await service.classify_and_extract(content=content, principal="u1")

    assert result.detailed_data_type == DataType.LOGS_AND_ERRORS
    assert {"entities", "entity_overflow_types"}.isdisjoint(
        PreprocessingResult.model_fields
    )
    assert "entities" not in result.extraction_metadata["evidence_metadata"]
