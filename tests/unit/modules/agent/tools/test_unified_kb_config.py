"""Tests for UnifiedKBConfig — staleness awareness, hybrid mode, metadata formatting."""

from datetime import datetime, timedelta, timezone

import pytest

from faultmaven.modules.agent.tools.kb_configs.unified_kb_config import (
    STALENESS_THRESHOLD_DAYS,
    UnifiedKBConfig,
)


@pytest.fixture
def config():
    return UnifiedKBConfig()


class TestSearchMode:
    """Verify hybrid search is enabled for unified KB."""

    def test_search_mode_is_hybrid(self, config):
        assert config.search_mode == "hybrid"


class TestFormatChunkMetadata:
    """Tests for staleness-aware chunk metadata formatting."""

    def test_basic_metadata(self, config):
        meta = {"scope": "global", "title": "OOM Runbook"}
        result = config.format_chunk_metadata(meta, 0.92)
        assert "Score: 0.92" in result
        assert "Scope: global" in result
        assert "Title: OOM Runbook" in result

    def test_domain_and_service_included(self, config):
        meta = {
            "scope": "global",
            "title": "K8s Pod Crash",
            "domain": "compute",
            "service": "kubernetes",
        }
        result = config.format_chunk_metadata(meta, 0.85)
        assert "Domain: compute" in result
        assert "Service: kubernetes" in result

    def test_draft_status_shown(self, config):
        meta = {"scope": "global", "title": "Draft Runbook", "status": "draft"}
        result = config.format_chunk_metadata(meta, 0.80)
        assert "Status: draft" in result

    def test_verified_status_not_shown(self, config):
        """Verified is the expected state — don't clutter output."""
        meta = {"scope": "global", "title": "Good Runbook", "status": "verified"}
        result = config.format_chunk_metadata(meta, 0.90)
        assert "Status:" not in result

    def test_deprecated_status_shown(self, config):
        meta = {"scope": "global", "title": "Old Runbook", "status": "deprecated"}
        result = config.format_chunk_metadata(meta, 0.70)
        assert "Status: deprecated" in result

    def test_stale_content_warning(self, config):
        """Content older than STALENESS_THRESHOLD_DAYS gets a warning."""
        old_date = (
            datetime.now(timezone.utc) - timedelta(days=STALENESS_THRESHOLD_DAYS + 30)
        ).strftime("%Y-%m-%d")
        meta = {"scope": "global", "title": "Ancient Runbook", "last_updated": old_date}
        result = config.format_chunk_metadata(meta, 0.75)
        assert "STALE" in result

    def test_moderately_old_content_shows_age(self, config):
        """Content 90-180 days old shows age but no STALE warning."""
        old_date = (datetime.now(timezone.utc) - timedelta(days=120)).strftime(
            "%Y-%m-%d"
        )
        meta = {"scope": "global", "title": "Aging Runbook", "last_updated": old_date}
        result = config.format_chunk_metadata(meta, 0.80)
        assert "Last updated:" in result
        assert "STALE" not in result

    def test_recent_content_no_warning(self, config):
        """Content less than 90 days old gets no staleness note."""
        recent_date = (datetime.now(timezone.utc) - timedelta(days=30)).strftime(
            "%Y-%m-%d"
        )
        meta = {
            "scope": "global",
            "title": "Fresh Runbook",
            "last_updated": recent_date,
        }
        result = config.format_chunk_metadata(meta, 0.90)
        assert "STALE" not in result
        assert "Last updated:" not in result

    def test_no_last_updated_no_warning(self, config):
        """Missing last_updated field produces no staleness note."""
        meta = {"scope": "global", "title": "Undated Runbook"}
        result = config.format_chunk_metadata(meta, 0.85)
        assert "STALE" not in result
        assert "Last updated:" not in result

    def test_iso_format_last_updated(self, config):
        """Handles ISO 8601 datetime format."""
        old_date = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        meta = {"scope": "global", "title": "ISO Runbook", "last_updated": old_date}
        result = config.format_chunk_metadata(meta, 0.75)
        assert "STALE" in result

    def test_category_included(self, config):
        meta = {"scope": "team", "title": "Team Runbook", "category": "networking"}
        result = config.format_chunk_metadata(meta, 0.88)
        assert "Category: networking" in result


class TestStalenessNote:
    """Tests for _staleness_note static method."""

    def test_returns_none_for_recent(self):
        recent = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d")
        assert UnifiedKBConfig._staleness_note(recent) is None

    def test_returns_age_for_medium(self):
        medium = (datetime.now(timezone.utc) - timedelta(days=100)).strftime("%Y-%m-%d")
        note = UnifiedKBConfig._staleness_note(medium)
        assert note is not None
        assert "Last updated:" in note

    def test_returns_stale_for_old(self):
        old = (datetime.now(timezone.utc) - timedelta(days=200)).strftime("%Y-%m-%d")
        note = UnifiedKBConfig._staleness_note(old)
        assert note is not None
        assert "STALE" in note

    def test_handles_invalid_date(self):
        assert UnifiedKBConfig._staleness_note("not-a-date") is None

    def test_handles_empty_string(self):
        assert UnifiedKBConfig._staleness_note("") is None


class TestSystemPrompt:
    """Verify synthesis prompt includes staleness guidance."""

    def test_staleness_guidance_in_prompt(self, config):
        assert "Staleness" in config.system_prompt or "STALE" in config.system_prompt
        assert "outdated" in config.system_prompt.lower()
        assert "draft" in config.system_prompt.lower()

    def test_verified_preference_in_prompt(self, config):
        assert "verified" in config.system_prompt.lower()


class TestFormatResponse:
    """Tests for response formatting."""

    def test_includes_answer(self, config):
        result = config.format_response("The answer", ["Doc A"], 1, 0.9)
        assert "The answer" in result

    def test_includes_sources(self, config):
        result = config.format_response("Answer", ["Doc A", "Doc B"], 2, 0.85)
        assert "Doc A" in result
        assert "Doc B" in result

    def test_truncates_many_sources(self, config):
        sources = [f"Doc {i}" for i in range(8)]
        result = config.format_response("Answer", sources, 8, 0.8)
        assert "+3 more" in result
