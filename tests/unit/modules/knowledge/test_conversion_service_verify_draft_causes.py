"""Flywheel-closure tests for ConversionService.verify_draft.

On human verification of a case→runbook draft, the service extracts the v4
``## Causes`` graph record and passes it to ``ingest_runbook(causes=...)`` so the
runbook re-enters diagnosis as structured candidates the Phase-4 seeder consumes
(produce/consume symmetry). Extraction lives HERE — the human-verification gate —
and deliberately NOT inside ``ingest_runbook``, so the anonymous/experimental
``upload_document`` path never becomes a seeder feeder (entry criterion b).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.modules.knowledge.domain.models.conversion import DraftStatus
from faultmaven.modules.knowledge.domain.services.conversion_service import (
    ConversionService,
)
from faultmaven.modules.knowledge.domain.services.runbook_cause_extractor import (
    extract_causes,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


_RUNBOOK_WITH_CAUSES = """---
id: draft-runbook
title: Example Runbook
status: draft
verified_by: ""
tags: [db]
---

## Causes

### Cause A: Connection pool exhausted
**Statement:** All DB connections are checked out and none are returned.
**Chain:**
- root: Connection leak in request handler
- D: Requests block waiting for a connection
**Indicators:**
- root: [Symptom] open connections climb monotonically
**Interventions:**
- **remediation** (root): Fix the leak. **Verification:** pool stabilizes.

### Cause Z: Unidentified
**Statement:** Root cause not yet determined.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): Consult an SME. **Risk:** none. **Duration:** review. **Verification:** N/A.
"""

_RUNBOOK_NO_CAUSES = """---
id: draft-runbook
title: No Causes
status: draft
verified_by: ""
---

## Overview
This runbook has no Causes section.

## Diagnostic Steps
### Step 1: look
```
echo hi
```
"""


def _session_factory(job, draft):
    execute_calls = {"n": 0}

    async def _execute(_stmt):
        execute_calls["n"] += 1
        result = MagicMock()
        result.scalar_one_or_none.return_value = (
            job if execute_calls["n"] == 1 else draft
        )
        return result

    session = AsyncMock()
    session.execute = _execute

    class _Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return session

        async def __aexit__(self, *_):
            return None

    return _Factory()


def _service(tmp_file: Path, knowledge_service, monkeypatch) -> ConversionService:
    # Pin the knowledge root at the directory the fixture writes into.
    # ``verify_draft`` containment-checks ``conversion_drafts.file_path``
    # against this root (#1213 follow-up); in production the draft always
    # lives under it, so a fixture writing outside it is not a shape the
    # service can be handed.
    monkeypatch.setattr(
        ConversionService, "_data_dir", property(lambda self: tmp_file.parent)
    )

    job = MagicMock()
    job.user_id = "user_x"
    job.scope = "personal"
    job.organization_id = "org_x"
    job.team_id = None

    draft = MagicMock()
    draft.status = DraftStatus.DRAFT.value
    draft.validation_passed = True
    draft.file_path = str(tmp_file)
    draft.title = "Example Runbook"
    draft.id = "d_x"
    draft.runbook_id = "draft-runbook"

    return ConversionService(
        llm_router=MagicMock(),
        settings=MagicMock(),
        db_session_factory=_session_factory(job, draft),
        knowledge_service=knowledge_service,
    )


async def _run_verify(service):
    return await service.verify_draft(
        conversion_id="conv_x",
        draft_id="d_x",
        user_id="user_x",
        username="alice",
    )


async def test_verify_draft_passes_extracted_causes_to_ingest(tmp_path, monkeypatch):
    rb = tmp_path / "runbook.md"
    rb.write_text(_RUNBOOK_WITH_CAUSES, encoding="utf-8")
    ks = MagicMock()
    ks.ingest_runbook = AsyncMock(return_value=3)

    service = _service(rb, ks, monkeypatch)
    resp = await _run_verify(service)

    assert resp.status == "verified"
    ks.ingest_runbook.assert_awaited_once()
    passed = ks.ingest_runbook.await_args.kwargs["causes"]
    # The wire re-runs extraction on the (frontmatter-updated) file content; the
    # Causes section is unchanged, so it equals a fresh extraction — and it is
    # non-empty (the flywheel actually carries structure through).
    expected = extract_causes(rb.read_text(encoding="utf-8"))
    assert passed == expected
    assert len(passed) == 2  # Cause A + fallback Cause Z
    assert passed[0]["cause_letter"] == "A" and passed[0]["chain_nodes"]


async def test_verify_draft_passes_none_when_no_causes_section(tmp_path, monkeypatch):
    # `causes or None` — a runbook without a Causes section carries no record,
    # so nothing is persisted for the seeder (metadata stays None).
    rb = tmp_path / "runbook.md"
    rb.write_text(_RUNBOOK_NO_CAUSES, encoding="utf-8")
    ks = MagicMock()
    ks.ingest_runbook = AsyncMock(return_value=2)

    service = _service(rb, ks, monkeypatch)
    await _run_verify(service)

    ks.ingest_runbook.assert_awaited_once()
    assert ks.ingest_runbook.await_args.kwargs["causes"] is None
