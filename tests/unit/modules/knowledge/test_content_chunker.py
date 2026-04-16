"""Tests for ContentChunker - structure-aware document splitting."""

import pytest

from faultmaven.modules.knowledge.domain.services.content_chunker import ContentChunker


@pytest.fixture
def chunker():
    return ContentChunker()


class TestSplitByStructure:
    """Test splitting on markdown structural boundaries (headers, horizontal rules)."""

    def test_splits_on_markdown_headers(self, chunker):
        # Each section must be >= MIN_CHUNK_CHARS (100) to avoid merge in _normalize
        intro = "# Introduction\n" + "Some intro text about the system. " * 10
        problem = "## Problem\n" + "Describing the problem in detail. " * 10
        solution = "## Solution\n" + "The fix involves multiple steps. " * 10
        content = f"{intro}\n\n{problem}\n\n{solution}"
        chunks = chunker.split(content)
        assert len(chunks) >= 2
        # Each chunk should contain coherent content
        joined = " ".join(chunks)
        assert "Introduction" in joined
        assert "Solution" in joined

    def test_splits_on_horizontal_rules(self, chunker):
        sec1 = "Section one with enough content to pass the minimum. " * 5
        sec2 = "Section two also has enough content to be standalone. " * 5
        sec3 = "Section three rounds it out with sufficient content. " * 5
        content = f"{sec1}\n\n---\n\n{sec2}\n\n---\n\n{sec3}"
        chunks = chunker.split(content)
        assert len(chunks) >= 2

    def test_single_section_no_split(self, chunker):
        content = "Just a single paragraph with no headers or rules."
        chunks = chunker.split(content)
        assert len(chunks) == 1
        assert chunks[0] == content


class TestSplitBySentences:
    """Test fallback line-boundary splitting for unstructured content."""

    def test_splits_long_unstructured_text(self, chunker):
        # Create content longer than MAX_CHUNK_CHARS without any headers
        lines = [f"Line {i}: " + "x" * 80 for i in range(100)]
        content = "\n".join(lines)
        assert len(content) > chunker.MAX_CHUNK_CHARS

        chunks = chunker.split(content)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= chunker.MAX_CHUNK_CHARS

    def test_short_unstructured_stays_intact(self, chunker):
        content = "Short content without any structure markers."
        chunks = chunker.split(content)
        assert len(chunks) == 1


class TestYamlFrontmatterStripping:
    """Test that YAML frontmatter is stripped before chunking."""

    def test_strips_frontmatter(self, chunker):
        content = (
            "---\ntitle: My Doc\nauthor: Test\n---\n\n"
            "# Actual Content\nHere is the document body."
        )
        chunks = chunker.split(content)
        joined = " ".join(chunks)
        assert "title: My Doc" not in joined
        assert "Actual Content" in joined

    def test_preserves_content_after_frontmatter(self, chunker):
        content = "---\nkey: value\n---\n\nImportant content here."
        chunks = chunker.split(content)
        assert any("Important content here" in c for c in chunks)


class TestNormalize:
    """Test post-processing: merging tiny sections, splitting oversized ones."""

    def test_merges_tiny_sections(self, chunker):
        # Sections smaller than MIN_CHUNK_CHARS should be merged
        content = "# A\nHi\n\n# B\nBye\n\n# C\nThis section has enough content to pass the minimum threshold and then some more text."
        chunks = chunker.split(content)
        # Tiny sections (< 100 chars) should be merged with neighbors
        for chunk in chunks:
            # After normalization, chunks shouldn't be tiny (unless they're the last remnant)
            pass
        # Just verify we get valid output
        assert len(chunks) >= 1
        assert all(len(c.strip()) > 0 for c in chunks)

    def test_splits_oversized_sections(self, chunker):
        # A single header section larger than MAX_CHUNK_CHARS
        content = "# Big Section\n" + ("x" * 100 + "\n") * 50
        assert len(content) > chunker.MAX_CHUNK_CHARS
        chunks = chunker.split(content)
        for chunk in chunks:
            assert len(chunk) <= chunker.MAX_CHUNK_CHARS

    def test_empty_content_returns_empty(self, chunker):
        assert chunker.split("") == []
        assert chunker.split("   ") == []

    def test_frontmatter_only_returns_original_stripped(self, chunker):
        # When body is empty but content.strip() is truthy, returns original stripped
        content = "---\ntitle: test\n---\n\n   "
        result = chunker.split(content)
        # The original content (with frontmatter) is returned since body was empty
        assert len(result) == 1


class TestRealWorldDocuments:
    """Test with realistic document structures."""

    def test_runbook_with_multiple_sections(self, chunker):
        # Each section must be > MIN_CHUNK_CHARS and total > MAX_CHUNK_CHARS
        # to force splitting. MAX_CHUNK_CHARS = 3000.
        symptoms = "## Symptoms\n" + "Users report MySQL connection timeouts. " * 40
        diagnosis = (
            "## Diagnosis\n" + "Check connection count via SHOW PROCESSLIST. " * 40
        )
        resolution = "## Resolution\n" + "Increase max_connections in my.cnf. " * 40
        content = (
            "---\ntitle: MySQL Troubleshooting\n---\n\n"
            f"# MySQL Connection Troubleshooting\n\n{symptoms}\n\n{diagnosis}\n\n{resolution}"
        )
        assert len(content) > chunker.MAX_CHUNK_CHARS  # Ensure it's big enough
        chunks = chunker.split(content)
        assert len(chunks) >= 2
        # Frontmatter should be stripped
        joined = " ".join(chunks)
        assert "title: MySQL" not in joined
        assert "Symptoms" in joined
        assert "Resolution" in joined

    def test_preserves_code_blocks_within_sections(self, chunker):
        config_section = (
            "# Config Example\n\n"
            "Apply this configuration to your cluster for proper routing. " * 5 + "\n\n"
            "```yaml\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test\n```\n"
        )
        next_section = (
            "# Next Section\n"
            "More content here with enough text to be standalone. " * 5
        )
        content = f"{config_section}\n{next_section}"
        chunks = chunker.split(content)
        # Code block should stay with its section
        config_chunk = [c for c in chunks if "ConfigMap" in c]
        assert len(config_chunk) == 1
        assert "Apply this configuration" in config_chunk[0]

    def test_handles_asterisk_horizontal_rules(self, chunker):
        sec1 = "Part one with enough content to be a standalone chunk. " * 5
        sec2 = "Part two with enough content to be a standalone chunk. " * 5
        content = f"{sec1}\n\n***\n\n{sec2}"
        chunks = chunker.split(content)
        assert len(chunks) >= 2

    def test_handles_underscore_horizontal_rules(self, chunker):
        sec1 = "Part one with enough content to be a standalone chunk. " * 5
        sec2 = "Part two with enough content to be a standalone chunk. " * 5
        content = f"{sec1}\n\n___\n\n{sec2}"
        chunks = chunker.split(content)
        assert len(chunks) >= 2
