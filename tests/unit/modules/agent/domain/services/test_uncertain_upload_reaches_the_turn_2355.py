"""#2355: what the investigation turn sees of an uncertain upload.

The preprocessing tests pin the extraction method; this pins its effect on
the turn. ``_preprocess_attachment`` derives ``classification_failed`` from
``extraction_method == "classification_failed"``, and every card effect reads
that one flag: the type choices and the "I haven't analyzed it yet" note
(``_build_classification_clarification``), and the exclusion from forced
directed analysis (``_turn_delivers_evidence_bearing_attachment``, #708). A
short uncertain upload must carry none of them; a long one still carries all.
"""

from __future__ import annotations

import pytest

from faultmaven.core.investigation.schemas import Attachment
from faultmaven.models.api import DataType
from faultmaven.modules.agent.domain.services.investigation_service.attachments import (
    _preprocess_attachment,
    _turn_delivers_evidence_bearing_attachment,
)
from faultmaven.modules.agent.domain.services.investigation_service.clarification import (
    _build_classification_clarification,
)
from faultmaven.modules.preprocessing.classifier import DataClassifier
from faultmaven.modules.preprocessing.preprocessing_service import (
    MIN_EXTRACTION_LINES,
    PreprocessingService,
)
from tests.case_builders import inquiry_case
from tests.unit.modules.preprocessing.extraction_stubs import CrimeScene

pytestmark = pytest.mark.unit


class _Storage:
    """Stores nothing; answers the key a real store would."""

    async def store_file(self, **kwargs):
        return {"storage_key": f"org/case/{kwargs['original_filename']}"}


def _uncertain_service() -> PreprocessingService:
    real = DataClassifier()

    class _Uncertain(DataClassifier):
        def classify(self, *args, **kwargs):
            result = real.classify(*args, **kwargs)
            result.classification_failed = True
            result.confidence = 0.40
            result.suggested_types = [
                DataType.LOGS_AND_ERRORS,
                DataType.UNSTRUCTURED_TEXT,
            ]
            return result

    return PreprocessingService(classifier=_Uncertain(), logs_extractor=CrimeScene())


async def _preprocess(line_count: int):
    content = "\n".join(f"svc-a pod restarted, attempt {i}" for i in range(line_count))
    return await _preprocess_attachment(
        _Storage(),
        _uncertain_service(),
        inquiry_case(case_id="case_aabb11223344", enterprise_id="org_123"),
        Attachment(
            content=content.encode(),
            filename="pasted-content-20261113T081500.txt",
            content_type="text/plain",
            source_metadata={"source_type": "text_paste"},
        ),
        user_id="user_123",
        turn_number=2,
    )


@pytest.mark.asyncio
async def test_a_short_uncertain_paste_is_evidence_with_no_card():
    result = await _preprocess(MIN_EXTRACTION_LINES - 1)

    assert result.classification_failed is False
    assert result.uploaded_file.data_type == DataType.UNSTRUCTURED_TEXT.value
    assert _build_classification_clarification([result]) == ([], None)
    assert _turn_delivers_evidence_bearing_attachment([result]) is True


@pytest.mark.asyncio
async def test_a_long_uncertain_paste_is_still_carded():
    """The control: at the cutoff the type picks the extractor, and all three
    card effects are still there."""
    result = await _preprocess(MIN_EXTRACTION_LINES)

    assert result.classification_failed is True
    suggestions, note = _build_classification_clarification([result])
    assert suggestions
    assert note is not None and "haven't analyzed" in note
    assert _turn_delivers_evidence_bearing_attachment([result]) is False
