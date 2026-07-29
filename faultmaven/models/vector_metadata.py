from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, HttpUrl, field_validator

from faultmaven.utils.serialization import to_json_compatible


class VectorMetadata(BaseModel):
    """Canonical metadata schema for vector documents sent to ChromaDB.

    Ensures consistent keys, value types, and ISO-8601 timestamps. Drops None
    and coerces non-primitive values to strings.
    """

    title: Optional[str] = None
    document_type: Optional[str] = None
    tags: List[str] = []
    source_url: Optional[str] = None
    # scope carries only the immutable visibility floor: 'personal' (owner-only)
    # or 'global' (platform). Team visibility is NEVER written here — it lives in
    # the share table and is resolved into an id allowlist at query time. Writing
    # a mutable 'team'/team_id into vector metadata would orphan a chunk on
    # unshare (it would match no filter branch). ADR-013 §D4 / ADR-011 D3.
    scope: Optional[str] = None
    owner_id: Optional[str] = None
    # Owning tenant. Collections whose retrieval carries a mandatory tenant
    # predicate (the runbook collection — see ``RunbookKnowledgeBase``) filter on
    # this key, so it has to survive normalization: a stamp this schema dropped
    # would leave the row unreachable by every scoped search. Unset on
    # collections that scope by ``scope``/``owner_id`` instead.
    organization_id: Optional[str] = None
    # What kind of artifact the row is. The runbook collection is not a separate
    # collection: ``RunbookKnowledgeBase`` is injected the general KB store
    # (``faultmaven_kb``) and its ``COLLECTION_NAME`` constant is decorative, so
    # runbooks and KB documents share one collection and this key is the ONLY
    # discriminator between them. ``search_runbooks`` ANDs ``report_type ==
    # "runbook"`` into every query, so — like ``organization_id`` above — a value
    # this schema dropped would make every row the runbook path writes
    # unreachable by every runbook search. Restored for that reason (#912); the
    # remaining runbook-identity keys (``report_id``, ``case_id``, ``case_title``,
    # ``runbook_source``) are still dropped and still tracked there.
    report_type: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # RAG-enrichment fields: extracted from runbook frontmatter at ingestion
    domain: Optional[str] = None
    service: Optional[str] = None
    last_updated: Optional[str] = None
    status: Optional[str] = None
    severity: Optional[str] = None
    symptom_class: Optional[str] = None  # Comma-separated from frontmatter list
    # Chunk tracking fields: set when documents are split into multiple chunks
    chunk_index: Optional[int] = None
    total_chunks: Optional[int] = None
    parent_document_id: Optional[str] = None

    @field_validator("tags", mode="before")
    @classmethod
    def _coerce_tags(cls, v: Any) -> List[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [t.strip() for t in v.split(",") if t.strip()]
        if isinstance(v, list):
            return [str(t) for t in v]
        return [str(v)]

    @field_validator(
        "title",
        "document_type",
        "source_url",
        "scope",
        "owner_id",
        "organization_id",
        "report_type",
        "domain",
        "service",
        "last_updated",
        "status",
        "severity",
        "symptom_class",
        "parent_document_id",
        mode="before",
    )
    @classmethod
    def _coerce_str(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        return str(v)

    def to_chroma_metadata(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        if self.title:
            data["title"] = self.title
        if self.document_type:
            data["document_type"] = self.document_type
        if self.tags:
            data["tags"] = ",".join(self.tags)
        if self.source_url:
            data["source_url"] = self.source_url
        if self.scope:
            data["scope"] = self.scope
        if self.owner_id:
            data["owner_id"] = self.owner_id
        if self.organization_id:
            data["organization_id"] = self.organization_id
        if self.report_type:
            data["report_type"] = self.report_type
        if self.created_at:
            data["created_at"] = to_json_compatible(self.created_at)
        if self.updated_at:
            data["updated_at"] = to_json_compatible(self.updated_at)
        if self.domain:
            data["domain"] = self.domain
        if self.service:
            data["service"] = self.service
        if self.last_updated:
            data["last_updated"] = self.last_updated
        if self.status:
            data["status"] = self.status
        if self.severity:
            data["severity"] = self.severity
        if self.symptom_class:
            data["symptom_class"] = self.symptom_class
        if self.chunk_index is not None:
            data["chunk_index"] = self.chunk_index
        if self.total_chunks is not None:
            data["total_chunks"] = self.total_chunks
        if self.parent_document_id:
            data["parent_document_id"] = self.parent_document_id
        return data
