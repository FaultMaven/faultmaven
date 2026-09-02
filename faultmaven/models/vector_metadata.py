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
    # Owning tenant. No KB retrieval path filters on this key today: runbook
    # dedup (``RunbookKnowledgeBase``) scopes by the same
    # ``scope``/``owner_id``/``parent_document_id`` allowlist as every other KB
    # read (fm#1030), and global rows are the org-free platform tier. Declared
    # so a writer that stamps it is not silently dropped.
    organization_id: Optional[str] = None
    # There is deliberately NO ``report_type`` and no runbook-identity block
    # (``case_id``/``case_title``/``runbook_source``/``document_title``/
    # ``original_document_id``) here. #912 declared those for
    # ``RunbookKnowledgeBase.index_runbook`` — a writer with no live caller,
    # whose ``report_type == "runbook"`` stamp nothing but its own dead read
    # path filtered on. fm#1030 deleted that write half: runbooks reach
    # ChromaDB only through ``KnowledgeService._index_document_in_vector_store``,
    # and dedup reads what it writes (``document_type``, ``scope``,
    # ``owner_id``, ``title``, ``parent_document_id``). The schema earns a key
    # only when a live reader needs it, and ``reject_undeclared_keys`` makes a
    # write of an undeclared key fail loudly rather than silently — on the
    # writers that consult it: ``ChromaDBVectorStore`` and
    # ``KnowledgeVectorStore.add_documents``, which is every vector write with
    # a live production caller. It is NOT every writer in the codebase:
    # ``KnowledgeIngester._process_and_store`` writes the same collection
    # directly (stamping ``document_id``, which only its own read/delete
    # methods filter on) and bypasses this guard — that whole subsystem has no
    # production caller today (fm#1035 review; verified route→service→ingester).
    # Its former entry points ``KnowledgeService.ingest_document``/
    # ``update_document`` were DELETED in #1166; what remains is
    # ``KnowledgeIngester.ingest_document`` and ``ingest_document_object``,
    # neither of which anything calls. Reviving it without routing it through a
    # guarded writer reopens the silent-key gap this schema exists to close.
    # (Its knowledge-tier gap is closed separately: ``_process_and_store`` now
    # refuses a document that names no tier, #1166.)
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
    # A ``cause_letters`` stamp (fm#1108) rode here for the KB cause seeder's
    # join and went with it (fm#1295); chunks written before that still carry
    # the key in the store, and the allowlist below drops it on read.

    @classmethod
    def reject_undeclared_keys(cls, md: Optional[Dict[str, Any]]) -> None:
        """Raise if ``md`` carries a key this schema does not declare.

        This model is an ALLOWLIST — it copies the keys it declares and ignores
        the rest — so an undeclared key is not stored, and nothing about the
        write says so. That is how ``index_runbook`` stamped four identity keys
        on every runbook, saw the write succeed, and stored none of them: the
        loss only surfaced later, elsewhere, as a wrong value at read time
        (#912).

        Callers must invoke this BEFORE any retry/circuit-breaker wrapper. The
        failure is a deterministic programming error, so retrying it only
        wastes the budget, and each attempt is recorded as a service failure —
        enough of them open the breaker and start failing the healthy calls
        that share it.
        """
        undeclared = sorted(set(md or {}) - set(cls.model_fields))
        if undeclared:
            raise ValueError(
                f"Vector metadata carries key(s) VectorMetadata does not "
                f"declare: {undeclared}. They would be dropped silently and the "
                f"row would be stored without them — add them to "
                f"faultmaven/models/vector_metadata.py (schema field AND "
                f"to_chroma_metadata) or stop writing them."
            )

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
        # ``is not None``, not truthiness: "" means "stamped, no cause heading
        # here" and must be stored, because its ABSENCE is what tells the read
        # path a chunk predates the stamp and has to be parsed instead.
        return data
