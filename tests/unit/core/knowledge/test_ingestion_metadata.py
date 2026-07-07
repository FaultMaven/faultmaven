"""Tests for ingestion — frontmatter extraction and structure-aware chunking."""

import pytest

from faultmaven.utils.frontmatter import extract_frontmatter_metadata


class TestExtractFrontmatterMetadata:
    """Tests for shared frontmatter extraction utility."""

    def test_extracts_all_rag_fields(self):
        content = """---
id: java-jvm-oom
title: "Java JVM OutOfMemoryError"
domain: application
service: java
last_updated: "2026-03-26"
status: verified
severity: critical
symptom_class:
  - oom
  - crash
tags:
  - java
  - oom
---

# Java JVM OutOfMemoryError
Content here.
"""
        result = extract_frontmatter_metadata(content)
        assert result["domain"] == "application"
        assert result["service"] == "java"
        assert result["last_updated"] == "2026-03-26"
        assert result["status"] == "verified"
        assert result["severity"] == "critical"
        assert result["symptom_class"] == "oom,crash"

    def test_symptom_class_single_string(self):
        content = """---
domain: compute
symptom_class: oom
---

Content.
"""
        result = extract_frontmatter_metadata(content)
        assert result["symptom_class"] == "oom"

    def test_symptom_class_empty_list(self):
        content = """---
domain: compute
symptom_class: []
---

Content.
"""
        result = extract_frontmatter_metadata(content)
        assert "symptom_class" not in result

    def test_partial_frontmatter(self):
        content = """---
title: "Simple Runbook"
domain: networking
---

# Simple Runbook
"""
        result = extract_frontmatter_metadata(content)
        assert result["domain"] == "networking"
        assert "service" not in result

    def test_no_frontmatter(self):
        assert extract_frontmatter_metadata("# Just a heading\n\nNo frontmatter.") == {}

    def test_invalid_yaml(self):
        content = """---
invalid: yaml: : broken
---

Content.
"""
        assert extract_frontmatter_metadata(content) == {}

    def test_null_values_excluded(self):
        content = """---
domain: compute
service: null
last_updated:
status: draft
---

Content.
"""
        result = extract_frontmatter_metadata(content)
        assert result["domain"] == "compute"
        assert "service" not in result
        assert "last_updated" not in result
        assert result["status"] == "draft"

    def test_non_string_values_coerced(self):
        content = """---
domain: compute
service: 42
status: draft
---

Content.
"""
        result = extract_frontmatter_metadata(content)
        assert result["service"] == "42"

    def test_ignores_non_rag_fields(self):
        content = """---
id: test
title: "Test"
domain: application
tags:
  - foo
severity: critical
---

Content.
"""
        result = extract_frontmatter_metadata(content)
        assert "id" not in result
        assert "title" not in result
        assert result["domain"] == "application"


class TestStructureAwareChunking:
    """Tests for the structure-aware content splitting.

    KnowledgeIngester is mocked in conftest.py, so we re-implement
    the chunking logic here for testability.
    """

    @pytest.fixture
    def chunker(self):
        """Chunking logic extracted from KnowledgeIngester for testing."""
        import re

        class Chunker:
            MAX_CHUNK_CHARS = 3000
            MIN_CHUNK_CHARS = 100

            @staticmethod
            def _split_by_structure(content):
                header_pattern = re.compile(r"\n(?=#{1,4}\s+\S)")
                parts = header_pattern.split(content)
                sections = [p.strip() for p in parts if p.strip()]
                if len(sections) > 1:
                    return sections
                hr_pattern = re.compile(r"\n\s*(?:---+|\*\*\*+|___+)\s*\n")
                parts = hr_pattern.split(content)
                sections = [p.strip() for p in parts if p.strip()]
                if len(sections) > 1:
                    return sections
                return [content.strip()]

            @staticmethod
            def _split_by_sentences(content, max_size=3000):
                if len(content) <= max_size:
                    return [content]
                chunks = []
                current = []
                current_len = 0
                for line in content.split("\n"):
                    line_len = len(line) + 1
                    if current_len + line_len > max_size and current:
                        chunks.append("\n".join(current))
                        current = [line]
                        current_len = line_len
                    else:
                        current.append(line)
                        current_len += line_len
                if current:
                    chunks.append("\n".join(current))
                return chunks

            def _normalize_chunks(self, sections):
                normalized = []
                pending = ""
                for section in sections:
                    if pending:
                        combined = pending + "\n\n" + section
                        if len(combined) <= self.MAX_CHUNK_CHARS:
                            pending = combined
                            continue
                        else:
                            normalized.append(pending.strip())
                            pending = ""
                    if len(section) < self.MIN_CHUNK_CHARS:
                        pending = section
                    elif len(section) > self.MAX_CHUNK_CHARS:
                        sub_chunks = self._split_by_sentences(
                            section, self.MAX_CHUNK_CHARS
                        )
                        normalized.extend(sub_chunks)
                    else:
                        normalized.append(section)
                if pending:
                    if normalized:
                        last = normalized[-1]
                        if len(last) + len(pending) + 2 <= self.MAX_CHUNK_CHARS:
                            normalized[-1] = last + "\n\n" + pending
                        else:
                            normalized.append(pending.strip())
                    else:
                        normalized.append(pending.strip())
                return [c for c in normalized if c.strip()]

        return Chunker()

    def test_splits_on_markdown_headers(self, chunker):
        content = """# Introduction

Some intro text here.

## Problem Definition

The problem is defined here.

## Diagnosis Steps

1. Check logs
2. Check metrics

## Resolution

Restart the service.
"""
        sections = chunker._split_by_structure(content)
        assert len(sections) >= 3

    def test_preserves_procedure_integrity(self, chunker):
        """A diagnostic step should NOT be split from its conditional."""
        content = """## Diagnosis

If the OOMKilled count exceeds 3:
1. Verify the memory limits in the deployment manifest
2. Check for known memory leaks in the JIRA board
3. Restart the pod with increased memory

## Resolution

Apply the fix.
"""
        sections = chunker._split_by_structure(content)
        # The diagnosis section should be one chunk, not split mid-procedure
        diagnosis = [s for s in sections if "OOMKilled" in s]
        assert len(diagnosis) == 1
        assert "Restart the pod" in diagnosis[0]

    def test_horizontal_rule_splitting(self, chunker):
        content = """Section one content.

---

Section two content.

---

Section three content.
"""
        sections = chunker._split_by_structure(content)
        assert len(sections) == 3

    def test_single_section_no_split(self, chunker):
        content = "Just a simple paragraph with no headers or rules."
        sections = chunker._split_by_structure(content)
        assert len(sections) == 1

    def test_normalize_merges_tiny_chunks(self, chunker):
        sections = [
            "Tiny",
            "Also tiny",
            "A substantive section with enough content to be meaningful on its own and worth keeping as a separate chunk.",
        ]
        normalized = chunker._normalize_chunks(sections)
        # The two tiny sections should be merged
        assert len(normalized) < len(sections)

    def test_normalize_splits_oversized_chunks(self, chunker):
        huge = "Line of content.\n" * 500  # Way over MAX_CHUNK_CHARS
        normalized = chunker._normalize_chunks([huge])
        assert len(normalized) > 1
        for chunk in normalized:
            assert len(chunk) <= chunker.MAX_CHUNK_CHARS


class TestEmptyDocumentGuard:
    """A document that produces zero chunks (empty or whitespace-only content)
    must be skipped before embedding — otherwise aembed_texts([]) returns []
    (not None) and collection.add(embeddings=[], ...) errors on an empty add.

    (Frontmatter-only content is NOT a zero-chunk case: `_split_content` keeps
    the raw content as a single chunk when frontmatter-stripping empties it.)"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("content", ["", "   \n\t  \n"])
    async def test_empty_document_skips_indexing(self, content):
        import logging
        from unittest.mock import AsyncMock, MagicMock, patch

        from faultmaven.models import KnowledgeBaseDocument
        from faultmaven.modules.knowledge.domain.services.ingestion import (
            KnowledgeIngester,
        )

        # Bypass the heavy __init__ (ChromaDB connect + model load); we only
        # exercise _process_and_store's chunk-guard branch.
        ingester = KnowledgeIngester.__new__(KnowledgeIngester)
        ingester.logger = logging.getLogger("test.ingestion")
        ingester._collection = MagicMock()  # `collection` is a read-only property

        now = "2026-07-07T00:00:00Z"
        document = KnowledgeBaseDocument(
            document_id="doc_empty",
            title="Empty",
            content=content,
            document_type="runbook",
            created_at=now,
            updated_at=now,
        )

        with patch(
            "faultmaven.modules.knowledge.domain.services.ingestion.model_cache"
        ) as mock_cache:
            mock_cache.aembed_texts = AsyncMock()
            await ingester._process_and_store(document)

        # Never embedded, never wrote an empty add.
        mock_cache.aembed_texts.assert_not_called()
        ingester._collection.add.assert_not_called()
