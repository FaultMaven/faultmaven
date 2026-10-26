"""Tests for the shared frontmatter extraction utility."""

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
