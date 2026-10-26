"""Tests for VectorMetadata — RAG enrichment fields in ChromaDB metadata."""

import pytest
from pydantic import ValidationError

from faultmaven.models.vector_metadata import VectorMetadata


class TestVectorMetadataRAGFields:
    """Tests for domain, service, last_updated, status fields."""

    def test_rag_fields_in_chroma_metadata(self):
        meta = VectorMetadata(
            title="OOM Runbook",
            scope="global",
            domain="application",
            service="java",
            last_updated="2026-03-26",
            status="verified",
        )
        chroma = meta.to_chroma_metadata()
        assert chroma["domain"] == "application"
        assert chroma["service"] == "java"
        assert chroma["last_updated"] == "2026-03-26"
        assert chroma["status"] == "verified"

    def test_none_rag_fields_excluded(self):
        meta = VectorMetadata(title="Simple Runbook", scope="global")
        chroma = meta.to_chroma_metadata()
        assert "domain" not in chroma
        assert "service" not in chroma
        assert "last_updated" not in chroma
        assert "status" not in chroma

    def test_partial_rag_fields(self):
        meta = VectorMetadata(
            title="Partial Runbook",
            scope="global",
            domain="networking",
        )
        chroma = meta.to_chroma_metadata()
        assert chroma["domain"] == "networking"
        assert "service" not in chroma

    def test_coercion_of_rag_fields(self):
        """Non-string values get coerced to strings by validator."""
        meta = VectorMetadata(
            title="Coerce Test",
            scope="global",
            domain=123,  # type: ignore
            service=True,  # type: ignore
        )
        chroma = meta.to_chroma_metadata()
        assert chroma["domain"] == "123"
        assert chroma["service"] == "True"


class TestTheTenantKey:
    """#1168: the schema carries ``enterprise_id`` and no ``organization_id``.

    ``organization_id`` was declared, stamped by nothing and filtered on by
    nothing — a key that looked like a tenant control without being one. Under
    ADR-017 the organization bills and is never a visibility predicate, so it
    is removed rather than wired, and the allowlist now refuses it. The tenant
    key is ``enterprise_id``: declared, emitted, and required on every KB chunk
    (the store half of that is pinned in ``test_knowledge_vector_store.py``).
    """

    def test_organization_id_is_no_longer_a_declared_key(self):
        assert "organization_id" not in VectorMetadata.model_fields

        with pytest.raises(ValueError, match="organization_id"):
            VectorMetadata.reject_undeclared_keys(
                {"scope": "personal", "organization_id": "org-1"}
            )

    def test_enterprise_id_is_declared_and_stored(self):
        VectorMetadata.reject_undeclared_keys({"enterprise_id": "ent-1"})

        chroma = VectorMetadata(
            scope="personal", enterprise_id="ent-1"
        ).to_chroma_metadata()

        assert chroma["enterprise_id"] == "ent-1"

    def test_an_absent_enterprise_id_is_not_emitted_as_a_blank(self):
        """A missing stamp stays MISSING, never ``""`` — the store guard and the
        #1775 read conjunct both key on the absence, and an empty string would
        be a value no enterprise matches that the guard still had to catch."""
        assert (
            "enterprise_id" not in VectorMetadata(scope="global").to_chroma_metadata()
        )

    @pytest.mark.parametrize(
        "value",
        [None, "", "   ", 7, True, b"ent"],
        ids=["none", "empty", "whitespace", "int", "bool", "bytes"],
    )
    def test_require_enterprise_id_value_refuses_every_shape_that_names_no_tenant(
        self, value
    ):
        """The one tenant rule, which both the indexer (on its raw argument)
        and the store (on each chunk's ``metadata.get("enterprise_id")``) call.
        A key absent from a chunk's metadata reaches it as ``None``."""
        with pytest.raises(ValueError, match="carries no enterprise_id"):
            VectorMetadata.require_enterprise_id_value(value, document_id="doc_chunk_0")

    def test_require_enterprise_id_value_admits_a_named_tenant(self):
        VectorMetadata.require_enterprise_id_value("ent-1")

    @pytest.mark.parametrize("value", [7, True], ids=["int", "bool"])
    def test_a_non_string_tenant_is_refused_by_the_model_not_stringified(self, value):
        """``enterprise_id`` is deliberately absent from ``_coerce_str``: a
        stringified ``"7"`` or ``"True"`` would pass every non-blank check
        downstream and match no enterprise, so the model refuses it instead."""
        with pytest.raises(ValidationError, match="enterprise_id"):
            VectorMetadata(scope="personal", enterprise_id=value)


class TestTheSchemaIsAnAllowlist:
    """What ``VectorMetadata`` declares is what a KB row carries, and no more.

    ``KnowledgeService`` builds every chunk's metadata with
    ``to_chroma_metadata``, and ``KnowledgeVectorStore.add_documents`` refuses
    an undeclared key through ``reject_undeclared_keys``.
    """

    def test_to_chroma_metadata_emits_every_key_the_schema_declares(self):
        """The allowlist stores everything it declares — the refusal's other half.

        ``reject_undeclared_keys`` (below) only covers keys the schema does NOT
        declare. The
        opposite defect — a field declared on the model but never emitted by
        ``to_chroma_metadata`` — passes it and still drops the value on every
        write. That is not hypothetical: it is exactly the state ``report_type``
        and the runbook identity keys were in (#912).

        Asserted over EVERY field rather than a sample, because a sample only
        protects the fields someone thought to list: with eight of twenty-four
        covered, dropping ``severity`` and ``symptom_class`` from the emitter
        left the whole suite green while every KB row silently lost them. The
        coverage assertion below makes adding a schema field force a decision
        here rather than silently widening the gap.
        """
        # Every value is UNIQUE, and that is load-bearing rather than tidy: a
        # cross-wire mutation is only detectable if the two fields it confuses
        # hold different values. An earlier version of this sample reused
        # "runbook" for document_type and report_type, and "doc-77" for
        # original_document_id and parent_document_id — so swapping either pair
        # in to_chroma_metadata passed the whole suite. The uniqueness assertion
        # below keeps that from creeping back.
        sample = {
            "title": "Runbook: Connection pool exhaustion",
            "document_type": "operational_runbook",
            "tags": "postgresql,database",
            "source_url": "https://example.invalid/runbook",
            "scope": "global",
            "owner_id": "user-1",
            "enterprise_id": "ent-a",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
            "domain": "database",
            "service": "postgresql",
            "last_updated": "2026-01-01",
            "status": "verified",
            "severity": "high",
            "symptom_class": "resource_exhaustion",
            "chunk_index": 0,
            "total_chunks": 3,
            "parent_document_id": "parentdoc-99",
        }

        values = [str(v) for v in sample.values()]
        assert len(set(values)) == len(values), (
            "two fields share a value, so a mutation swapping them would be "
            "invisible here — give every field a distinct one"
        )

        assert set(sample) == set(VectorMetadata.model_fields), (
            "every declared field needs a sample value here — a new field "
            "without one is a field this test does not protect"
        )

        stored = VectorMetadata(**sample).to_chroma_metadata()

        missing = set(sample) - set(stored)
        assert not missing, (
            f"declared but not emitted by to_chroma_metadata: {sorted(missing)} — "
            f"these are written by callers and silently dropped"
        )

        # VALUES, not just keys. A key-presence assertion cannot tell a correct
        # emitter from one that cross-wires fields (`data["severity"] =
        # self.status`), and a declared key carrying another field's value is
        # the same "wrong value stated as fact" defect this issue is about —
        # arguably worse, since the row looks complete. The uniqueness
        # assertion above is what makes this able to see a swap at all.
        expected = dict(sample)
        expected["tags"] = "postgresql,database"  # list -> comma-joined
        expected["created_at"] = "2026-01-01T00:00:00Z"  # datetime -> ISO-8601
        expected["updated_at"] = "2026-01-02T00:00:00Z"
        assert stored == expected

    @pytest.mark.parametrize(
        "retired_key",
        [
            "report_type",
            "case_id",
            "case_title",
            "runbook_source",
            "document_title",
            "original_document_id",
        ],
    )
    def test_a_retired_runbook_identity_key_is_refused_not_dropped(self, retired_key):
        """The keys the dead runbook writer stamped now fail loudly (fm#1030).

        #912 declared these for ``RunbookKnowledgeBase.index_runbook``, a
        writer with no live caller; fm#1030 deleted that write half and
        repointed dedup at the live writer's keys. Removal is fail-loud by
        construction — ``reject_undeclared_keys`` refuses them — which is the
        safe direction: a writer that still believes in these keys finds out
        at its first write, not as a silent drop read back arbitrarily later.
        """
        with pytest.raises(ValueError, match=retired_key):
            VectorMetadata.reject_undeclared_keys({retired_key: "value"})
