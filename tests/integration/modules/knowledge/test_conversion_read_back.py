"""What a stored conversion job reads back as, through the real service.

Two defects with one shape: a job is written successfully and then cannot be
read faithfully. Both are invisible to the write path, because the response that
CREATES a conversion is assembled in memory and never round-trips.

1. ``get_conversion`` does ``ConversionStatus(job.status)``. The CHECK admits
   ``'cancelled'``; before this change the enum did not, so a row the database
   accepts raised ``ValueError`` — a 500 on ``GET /knowledge/conversions/{id}``
   (#520's ``ConversionStatus`` arm, second half).
2. The per-failure-mode refusals reach the user only through
   ``ConversionResponse.warnings``, which ``get_conversion`` never populated. So
   migration 047 made ``'partial'`` storable while the REASON for partiality was
   the one thing not stored: one refresh replaced the message naming both
   colliding failure modes with an unexplained PARTIAL (migration 048).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import (
    Base,
    ConversionJobModel,
    EnterpriseModel,
    OrganizationModel,
    UploadedFileModel,
)
from faultmaven.modules.knowledge.domain.models.conversion import (
    AnalysisResult,
    ConversionStatus,
    FailureModeAnalysis,
    PreprocessingResult,
    SourceAssessment,
)
from faultmaven.modules.knowledge.domain.services.conversion_service import (
    ConversionService,
)

pytestmark = pytest.mark.integration

ORG = "00000000-0000-0000-0000-000000000001"

RUNBOOK = """---
id: placeholder
title: "Generated"
domain: platform
service: redis
symptom_class: [saturation]
scope: global
tags: []
difficulty: medium
severity: high
version: "1.0.0"
last_updated: "2026-01-01"
verified_by: ""
status: draft
---

# Runbook: Generated

## Symptom Recognition
Redis begins evicting keys and clients see OOM errors on every write path.

## Applicability
Any Redis deployment running with a maxmemory bound.

## Diagnostic Steps
### Step 1. Read INFO memory

## Causes
### Cause A: maxmemory is below the working set
Statement: the instance reached its configured cap.

## Prevention
Alert on the used_memory to maxmemory ratio.
"""


@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session_factory(engine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(
            EnterpriseModel(
                enterprise_id=ORG, name="Default Enterprise", slug="default"
            )
        )
        session.add(
            OrganizationModel(
                organization_id=ORG,
                enterprise_id=ORG,
                name="Default Org",
                slug="default-org",
            )
        )
        await session.commit()
    return factory


@pytest.fixture
def service(session_factory, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = MagicMock()
    settings.llm.get_knowledge_model.return_value = "test-model"
    settings.llm.explicit_role_provider.return_value = None
    svc = ConversionService(
        llm_router=MagicMock(),
        settings=settings,
        db_session_factory=session_factory,
    )
    with patch.object(
        type(svc), "_data_dir", new=property(lambda self: tmp_path / "data/knowledge")
    ):
        yield svc


def _mode(fm_id: str, title: str, symptom_class: str) -> FailureModeAnalysis:
    return FailureModeAnalysis(
        id=fm_id,
        title=title,
        domain="platform",
        service="redis",
        symptom_class=[symptom_class],
        severity="high",
        symptoms_summary="Writes fail.",
        resolution_summary="Raise the cap.",
    )


async def _convert(service, tmp_path, titles):
    modes = [
        _mode(f"fm-{i}", t, c)
        for i, (t, c) in enumerate(zip(titles, ["saturation", "latency", "errors"]))
    ]
    source = tmp_path / "doc.md"
    source.write_text("source material", encoding="utf-8")
    service._preprocessor.preprocess = AsyncMock(
        return_value=PreprocessingResult(
            extracted_text="source material", source_metadata={}
        )
    )
    service._llm_router.route = AsyncMock(
        return_value=SimpleNamespace(content=RUNBOOK, is_truncated=False)
    )
    with patch.object(
        ConversionService,
        "_analyze_document",
        AsyncMock(
            return_value=AnalysisResult(
                is_actionable=True,
                failure_modes=modes,
                source_assessment=SourceAssessment(
                    content_type="doc",
                    actionability_rating="high",
                    missing_information=[],
                ),
            )
        ),
    ):
        return await service.convert_document(
            file_path=source,
            content_type="text/markdown",
            original_filename="doc.md",
            scope="global",
            user_id="user_x",
            organization_id=ORG,
        )


class TestACancelledJobCanBeRead:
    async def test_a_cancelled_row_reads_back_instead_of_raising(
        self, service, session_factory
    ):
        """The CHECK admits it, so the enum must parse it.

        Pre-fix this raised ``ValueError: 'cancelled' is not a valid
        ConversionStatus`` out of ``get_conversion`` — a 500 on a row the
        database itself accepted.
        """
        async with session_factory() as session:
            session.add(
                UploadedFileModel(
                    file_id="file_c",
                    enterprise_id=ORG,
                    uploaded_by="user_x",
                    filename="d.md",
                    size_bytes=1,
                    content_type="text/markdown",
                    upload_source="conversion_source",
                    uploaded_at_turn=0,
                )
            )
            await session.flush()
            session.add(
                ConversionJobModel(
                    id="conv_cancelled",
                    user_id="user_x",
                    enterprise_id=ORG,
                    scope="global",
                    status="cancelled",
                    source_file_id="file_c",
                    source_type="document",
                    failure_modes_detected=0,
                    analysis_result={},
                    created_at=datetime.now(timezone.utc),
                )
            )
            await session.commit()

        # The commit above IS the premise: the CHECK admits 'cancelled', so if
        # it did not this test would have errored on the INSERT rather than
        # asserting something unreachable.
        result = await service.get_conversion("conv_cancelled", "user_x")

        assert result is not None
        assert result.status is ConversionStatus.CANCELLED


class TestTheReasonForPartialitySurvivesAReRead:
    async def test_warnings_are_returned_by_get_conversion(self, service, tmp_path):
        """047 made 'partial' storable; 048 makes it explicable.

        The colliding-mode refusal names both failure modes. Pre-fix that string
        existed only in the response that created the conversion — every
        subsequent read returned ``warnings=[]`` while still reporting PARTIAL.
        """
        created = await _convert(service, tmp_path, ["Redis OOM", "redis oom"])
        assert created.status == ConversionStatus.PARTIAL
        assert len(created.warnings) == 1, created.warnings

        reread = await service.get_conversion(created.conversion_id, "user_x")

        assert reread is not None
        assert reread.status == ConversionStatus.PARTIAL
        assert reread.warnings == created.warnings, (
            "the reason a job is partial must survive the response that " "created it"
        )
        assert "fm-0" in reread.warnings[0] and "fm-1" in reread.warnings[0]

    async def test_a_clean_job_reads_back_with_no_warnings(self, service, tmp_path):
        """The negative control. Without it the assertion above is satisfied by
        a read path that returns some fixed non-empty list."""
        created = await _convert(service, tmp_path, ["Redis OOM", "Redis Slow"])
        assert created.status == ConversionStatus.COMPLETED
        assert created.warnings == []

        reread = await service.get_conversion(created.conversion_id, "user_x")

        assert reread is not None
        assert reread.status == ConversionStatus.COMPLETED
        assert reread.warnings == []
