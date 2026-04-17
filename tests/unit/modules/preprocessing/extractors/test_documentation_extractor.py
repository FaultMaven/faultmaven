"""
Tests for DocumentationExtractor.

Covers:
- R7.2: Command detection expansion (position-independent matching, short text skip)
"""

import pytest

from faultmaven.modules.preprocessing.extractors.documentation_extractor import (
    DocumentationExtractor,
)


class TestDocumentationExtractor:
    @pytest.fixture
    def extractor(self):
        return DocumentationExtractor()

    def test_properties(self, extractor):
        assert extractor.strategy_name == "documentation_structure"
        assert extractor.llm_calls_used == 0

    # --- R7.2: Command detection fixes ---

    def test_kubectl_detected_anywhere(self, extractor):
        """'kubectl get pods' should match regardless of position in backtick."""
        assert extractor._looks_like_command("kubectl get pods") is True
        assert extractor._looks_like_command("sudo kubectl get pods") is True

    def test_docker_detected(self, extractor):
        """Docker commands detected."""
        assert extractor._looks_like_command("docker ps -a") is True
        assert extractor._looks_like_command("sudo docker logs mycontainer") is True

    def test_true_too_short(self, extractor):
        """'true' is too short (<=5 chars) — should NOT match."""
        assert extractor._looks_like_command("true") is False

    def test_null_too_short(self, extractor):
        """'null' is too short (<=5 chars) — should NOT match."""
        assert extractor._looks_like_command("null") is False

    def test_force_flag_no_command(self, extractor):
        """'--force' has no command keyword — should NOT match."""
        assert extractor._looks_like_command("--force") is False

    def test_basic_document_extraction(self, extractor):
        """Basic markdown document extraction works."""
        content = """# Troubleshooting Guide

## Check Pod Status

Run `kubectl get pods` to see all running pods.

## View Logs

Use `docker logs myservice` to view service logs.

## Configuration

Set `LOG_LEVEL=debug` in the `.env` file.
"""
        result = extractor.extract(content)
        assert "Troubleshooting" in result
        assert "Code blocks" in result or "Commands" in result or "kubectl" in result
