"""Evidence Domain Services.

The evidence module's domain services are case-tied. The previous
EvidenceService and APIEvidenceArtifactService classes (and their
standalone evidence path) were deleted in storage redesign 2026-04
phase 2 — see deployment-schema-strategy.md §7.2.

Evidence creation now flows through the case-tied turn endpoint
(POST /api/v1/cases/{case_id}/turns) and is persisted via the
milestone engine into the `evidence` table directly.
"""

__all__: list[str] = []
