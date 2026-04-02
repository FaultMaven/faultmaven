"""Tests for VectorMetadata — RAG enrichment fields in ChromaDB metadata."""

import pytest

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
