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

    # --- Regression: short commands and substring false positives ---

    def test_short_commands_accepted(self, extractor):
        """Regression: a ``len(text) <= 5`` gate used to run *before* the
        indicator check, silently rejecting ``top``, ``ps``, ``ping`` —
        all already in the indicator list, all with length ≤ 5 and all
        therefore false-negatives under the old predicate."""
        for cmd in ("top", "ps", "ping"):
            assert extractor._looks_like_command(cmd) is True, cmd

    def test_substring_false_positives_rejected(self, extractor):
        """Regression: the old ``cmd in text.lower()`` check matched
        indicator names *inside* unrelated words (``go`` inside
        ``google.com``, ``pip`` inside ``zipper``, ``ssh`` inside
        ``smashhit``). Token-based matching rejects them."""
        for not_a_command in ("google.com", "zipper", "ssh-keygen-wrapper"):
            # ``ssh-keygen-wrapper`` has ``ssh`` only as a hyphen-joined
            # substring — its single whitespace token is the whole string
            # which is not an indicator.
            assert extractor._looks_like_command(not_a_command) is False, not_a_command

    def test_command_detected_inside_multi_word_inline(self, extractor):
        """A backticked fragment like ``sudo kubectl get pods`` should
        still be recognised — the token ``kubectl`` is an indicator even
        when not the first token."""
        assert extractor._looks_like_command("sudo kubectl get pods") is True

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
        assert "Troubleshooting" in result.file_extract
        assert (
            "Code blocks" in result.file_extract
            or "Commands" in result.file_extract
            or "kubectl" in result.file_extract
        )


class TestSetextH1AndLeadParagraph:
    """README-style markdown: Setext H1 (title underlined with `===` or
    `---`) and a lead paragraph that contains identifying metadata
    (version label, project description). The earlier title fallback
    used `[:100]` truncation which split a long title with badge links
    mid-URL, and never surfaced the lead paragraph at all — so v1.1-style
    version labels in README abstracts were invisible to the agent
    (text-nab-readme-01 q1, ISS investigation 2026-04-29).
    """

    @pytest.fixture
    def extractor(self):
        from faultmaven.modules.preprocessing.extractors.documentation_extractor import (
            DocumentationExtractor,
        )

        return DocumentationExtractor()

    def test_setext_h1_with_dashes_detected(self, extractor):
        content = (
            "The Numenta Anomaly Benchmark (NAB) v1.1\n"
            "----------------------------------------\n"
            "\n"
            "Welcome. This is the abstract paragraph.\n"
        )
        result = extractor.extract(content)
        # Title must be preserved verbatim (not the underline line).
        assert "The Numenta Anomaly Benchmark (NAB) v1.1" in result.file_extract
        # Underline must NOT appear as the title.
        assert (
            "Documentation: -----" not in result.file_extract
            and "Documentation: ====" not in result.file_extract
        )

    def test_setext_h1_with_equals_detected(self, extractor):
        content = (
            "Project Title\n" "=============\n" "\n" "Lead paragraph content here.\n"
        )
        result = extractor.extract(content)
        assert "Documentation: Project Title" in result.file_extract

    def test_lead_paragraph_surfaced_after_title(self, extractor):
        """Lead paragraph after the title must appear as 'Abstract: ...'
        so version labels and project metadata are visible in the
        first-line orientation."""
        content = (
            "# My Project\n"
            "\n"
            "My Project v2.4.0 is a tool for doing the thing. It supports\n"
            "multiple platforms and ships under Apache 2.0.\n"
            "\n"
            "## Installation\n"
            "Run `pip install myproject`.\n"
        )
        result = extractor.extract(content)
        assert "Abstract:" in result.file_extract
        assert "v2.4.0" in result.file_extract

    def test_long_title_with_badges_not_truncated_at_100(self, extractor):
        """Titles up to ~250 chars must round-trip fully — README badge
        links commonly push titles past 100 chars and the prior cap was
        cutting them mid-URL."""
        long_title = (
            "The Project (NAME) "
            "[![Build Status](https://travis-ci.org/org/repo.svg?branch=main)](https://travis-ci.org/org/repo)"
        )
        content = f"{long_title}\n{'-' * 30}\n\nAbstract paragraph.\n"
        result = extractor.extract(content)
        # Full title should make it into output (length ~140 chars).
        assert long_title in result.file_extract

    def test_lead_paragraph_skipped_when_only_subheaders_follow(self, extractor):
        """If no prose immediately follows the title (just subheaders),
        no Abstract section is emitted — avoids surfacing arbitrary
        section content as the abstract."""
        content = "# Bare Title\n" "\n" "## First Section\n" "Content here.\n"
        result = extractor.extract(content)
        # No prose paragraph between title and first H2 → no Abstract.
        assert "Abstract:" not in result.file_extract
