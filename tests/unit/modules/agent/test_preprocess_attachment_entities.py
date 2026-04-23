"""Phase 4b — ``_preprocess_attachment`` entity-registry wiring.

Covers the investigation service's integration with Phase 4a's
``CaseRepository.upsert_case_entities`` method. Specifically:

1. When the preprocessing result carries entity observations,
   ``upsert_case_entities`` is called with the new Evidence's id and a
   correctly-stamped ``CaseEntity`` list.
2. When it carries none (feature flag off, or data type without a
   registered extractor), the upsert is not called.
3. Repositories that don't implement ``upsert_case_entities`` (legacy
   doubles / partial mocks) don't break the upload path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.schemas import Attachment, TurnPayload
from faultmaven.models.api_models import IntentType, QueryIntent
from faultmaven.modules.agent.domain.services.investigation_service import (
    InvestigationService,
)
from faultmaven.modules.case.domain.models import EntityType

from .conftest import MockCaseRepository, MockMilestoneEngine, create_sample_case


def _make_preprocessing_result(entities=None, content_hash="hash_alpha"):
    """Preprocessing-result stub carrying a Phase 4 entity payload."""
    result = MagicMock()
    result.summary = "Logs"
    result.structural_index = "error body"
    result.data_type = MagicMock(value="logs")
    result.content_hash = content_hash
    result.extraction_method = "crime_scene"
    result.extraction_metadata = {}
    result.coverage_start_ts = None
    result.coverage_end_ts = None
    result.entities = entities or []
    result.entity_overflow_types = []
    return result


class _EntityAwareRepo(MockCaseRepository):
    """MockCaseRepository with configurable ``upsert_case_entities``."""

    def __init__(self, include_upsert: bool = True):
        super().__init__()
        self.find_by_content_hash = AsyncMock(return_value=None)
        if include_upsert:
            self.upsert_case_entities = AsyncMock(return_value=None)


def _make_service(repo, preprocessing_result):
    preprocessing = AsyncMock()
    preprocessing.classify_and_extract = AsyncMock(return_value=preprocessing_result)
    return InvestigationService(
        milestone_engine=MockMilestoneEngine(),
        case_repository=repo,
        preprocessing_service=preprocessing,
        file_storage_service=None,
    )


def _make_payload():
    return TurnPayload(
        query="Analyze",
        attachments=[
            Attachment(
                content=b"logs bytes",
                filename="auth.log",
                content_type="text/plain",
            )
        ],
        intent=QueryIntent(type=IntentType.CONVERSATION),
    )


class TestPreprocessAttachmentEntities:
    @pytest.mark.asyncio
    async def test_entities_upserted_when_present(self):
        repo = _EntityAwareRepo()
        pp = _make_preprocessing_result(
            entities=[
                {
                    "entity_type": "ip",
                    "entity_value": "10.0.0.5",
                    "mention_count": 3,
                    "in_error_context": True,
                },
                {
                    "entity_type": "user",
                    "entity_value": "alice",
                    "mention_count": 1,
                    "in_error_context": False,
                },
            ],
        )
        service = _make_service(repo, pp)

        case = create_sample_case()
        case.user_id = "user_owner"
        await repo.save(case)

        await service.process_turn(case.case_id, "user_owner", _make_payload())

        repo.upsert_case_entities.assert_awaited_once()
        args = repo.upsert_case_entities.await_args.args
        case_id_arg, evidence_id_arg, entities_arg = args
        assert case_id_arg == case.case_id
        # Evidence id is generated inside the service — just confirm it
        # matches the evidence the service appended to the case.
        saved = await repo.get(case.case_id)
        new_ev = [ev for ev in saved.evidence if ev.content_hash == "hash_alpha"][0]
        assert evidence_id_arg == new_ev.evidence_id
        types = {e.entity_type for e in entities_arg}
        assert types == {EntityType.IP, EntityType.USER}
        ip = next(e for e in entities_arg if e.entity_type == EntityType.IP)
        assert ip.entity_value == "10.0.0.5"
        assert ip.mention_count == 3
        assert ip.in_error_context is True

    @pytest.mark.asyncio
    async def test_no_upsert_when_preprocessing_emits_no_entities(self):
        """Flag-off / extractor-less data types — the preprocessor
        returns an empty list, the service must not hit the repo."""
        repo = _EntityAwareRepo()
        pp = _make_preprocessing_result(entities=[])
        service = _make_service(repo, pp)

        case = create_sample_case()
        case.user_id = "user_owner"
        await repo.save(case)

        await service.process_turn(case.case_id, "user_owner", _make_payload())

        repo.upsert_case_entities.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_repo_method_is_tolerated(self):
        """Legacy repos (partial test doubles) don't expose
        ``upsert_case_entities`` — the upload must still succeed."""
        repo = _EntityAwareRepo(include_upsert=False)
        pp = _make_preprocessing_result(
            entities=[
                {
                    "entity_type": "ip",
                    "entity_value": "10.0.0.9",
                    "mention_count": 1,
                    "in_error_context": False,
                }
            ]
        )
        service = _make_service(repo, pp)

        case = create_sample_case()
        case.user_id = "user_owner"
        await repo.save(case)

        # Must not raise.
        await service.process_turn(case.case_id, "user_owner", _make_payload())

    @pytest.mark.asyncio
    async def test_malformed_entity_is_skipped_silently(self):
        """Defensive conversion: a bad dict (missing fields, unknown
        entity_type) is dropped rather than raising, matching the
        best-effort contract around the registry write path."""
        repo = _EntityAwareRepo()
        pp = _make_preprocessing_result(
            entities=[
                {"entity_type": "ip", "entity_value": "10.0.0.1"},
                {"entity_type": "not_a_real_type", "entity_value": "x"},
                {"entity_type": "ip"},  # missing value
                {"entity_type": "ip", "entity_value": ""},  # empty value
            ]
        )
        service = _make_service(repo, pp)

        case = create_sample_case()
        case.user_id = "user_owner"
        await repo.save(case)

        await service.process_turn(case.case_id, "user_owner", _make_payload())

        # One valid row survived — the IP at 10.0.0.1.
        assert repo.upsert_case_entities.await_count == 1
        _, _, entities_arg = repo.upsert_case_entities.await_args.args
        assert len(entities_arg) == 1
        assert entities_arg[0].entity_value == "10.0.0.1"
