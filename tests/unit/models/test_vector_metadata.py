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
