"""Evidence Module - Domain Service.

Case-tied evidence is created by the milestone engine via the case
turn endpoint (POST /api/v1/cases/{case_id}/turns) and persisted into
the `evidence` table directly. The previous standalone-evidence path
(EvidenceService, APIEvidenceArtifactService, evidence_artifacts /
standalone_evidence tables, POST /api/v1/evidence and related endpoints)
was deleted in storage redesign 2026-04 phase 2 — see
deployment-schema-strategy.md §7.2.

Surviving public API:
    From domain.services.file_storage_service:
        - FileStorageService (used by the case-tied turn flow)
"""

from faultmaven.modules.evidence.domain.services.file_storage_service import (
    FileStorageService,
)

__all__ = [
    "FileStorageService",
]
