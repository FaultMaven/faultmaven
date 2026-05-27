"""Unit tests for the case-to-runbook fixes (PR following case_f3c931fb5b9c report):

1. Domain resolution: `from_case()` no longer hardcodes the invalid `general`
   value; it picks one of the 7 taxonomy domains by keyword.
2. Frontmatter id rewriting: `_force_frontmatter_id` overrides whatever the
   LLM emitted with the canonical kebab-case runbook_id.
3. Scope default: case-generated runbooks default to `personal`, not `global`.
"""

from __future__ import annotations

import pytest

from faultmaven.modules.knowledge.domain.models.conversion import (
    CaseConversionRequest,
    _resolve_domain,
)
from faultmaven.modules.knowledge.domain.services.conversion_service import (
    _force_frontmatter_id,
)

# =============================================================================
# Domain resolution
# =============================================================================


class TestResolveDomain:
    """Pin the 7-domain taxonomy mapping. Falls back to `application` when no
    keyword fires — `general` is no longer used."""

    def test_istio_keywords_resolve_to_networking(self):
        assert _resolve_domain("istio destinationrule envoy 503") == "networking"

    def test_postgres_keywords_resolve_to_database(self):
        assert _resolve_domain("postgres connection pool deadlock") == "database"

    def test_k8s_keywords_resolve_to_compute(self):
        assert _resolve_domain("kubernetes pod crashloop oom") == "compute"

    def test_oauth_keywords_resolve_to_security(self):
        assert _resolve_domain("oauth jwt token forbidden 403") == "security"

    def test_s3_keywords_resolve_to_storage(self):
        assert _resolve_domain("s3 bucket disk full pvc") == "storage"

    def test_kafka_keywords_resolve_to_messaging(self):
        assert _resolve_domain("kafka consumer lag broker partition") == "messaging"

    def test_unknown_text_falls_back_to_application(self):
        # No taxonomy keyword fires — fall through to the catch-all bucket.
        assert _resolve_domain("the system is misbehaving for unclear reasons") == (
            "application"
        )

    def test_empty_text_falls_back_to_application(self):
        assert _resolve_domain("") == "application"

    def test_never_returns_invalid_domain(self):
        # Regression guard: `general` (the pre-fix default) is not a valid
        # runbook domain. Ensure no input path yields it.
        for text in ["", "totally unknown", "istio", "kafka", "postgres"]:
            assert _resolve_domain(text) != "general"


# =============================================================================
# Frontmatter id rewriting
# =============================================================================


class TestForceFrontmatterId:
    """LLMs sometimes ignore the prompt-injected runbook_id and use the case
    title verbatim as the frontmatter `id`. The force-rewrite step must
    produce a frontmatter whose `id` is the canonical kebab-case value."""

    def test_overrides_bad_id_with_canonical_kebab_case(self):
        content = (
            "---\n"
            "id: Case-260526-4\n"
            'title: "Some Runbook"\n'
            "domain: networking\n"
            "---\n"
            "\n# Body content here\n"
        )
        result = _force_frontmatter_id(content, "istio-503-errors-abc1")
        assert "id: istio-503-errors-abc1" in result
        assert "id: Case-260526-4" not in result
        # Body content untouched
        assert "# Body content here" in result

    def test_inserts_id_when_missing_entirely(self):
        content = (
            "---\n"
            'title: "Some Runbook"\n'
            "domain: networking\n"
            "---\n"
            "\n# Body\n"
        )
        result = _force_frontmatter_id(content, "my-runbook-id")
        # The new id is the first frontmatter field
        assert result.startswith("---\nid: my-runbook-id\ntitle:")

    def test_no_op_when_no_frontmatter(self):
        # Without a frontmatter block, the caller's downstream validator
        # rejects the content anyway — don't synthesize one here.
        content = "# Just markdown, no frontmatter\n\nBody."
        result = _force_frontmatter_id(content, "x")
        assert result == content

    def test_preserves_other_frontmatter_fields(self):
        content = (
            "---\n"
            "id: WRONG\n"
            'title: "T"\n'
            "domain: networking\n"
            "tags: [a, b, c]\n"
            "---\n"
            "Body."
        )
        result = _force_frontmatter_id(content, "right-id")
        assert "id: right-id" in result
        assert 'title: "T"' in result
        assert "domain: networking" in result
        assert "tags: [a, b, c]" in result


# =============================================================================
# Scope default
# =============================================================================


class _StubCase:
    """Minimal duck-typed stand-in for the Case domain model. Just enough
    attributes to satisfy `CaseConversionRequest.from_case`'s getattr chain."""

    def __init__(self, case_id: str, title: str):
        self.case_id = case_id
        self.title = title
        # Optional attrs left absent on purpose — `from_case` uses getattr
        # with defaults for each.


class TestCaseConversionRequestDefaults:
    def test_default_scope_is_personal(self):
        req = CaseConversionRequest.from_case(_StubCase("c1", "Title"))
        assert req.scope == "personal"

    def test_explicit_scope_overrides_default(self):
        req = CaseConversionRequest.from_case(_StubCase("c1", "Title"), scope="team")
        assert req.scope == "team"

    def test_domain_is_application_when_no_signal(self):
        # No tags / affected services / root cause → no keyword match →
        # fallback domain `application` (not `general`).
        req = CaseConversionRequest.from_case(_StubCase("c1", "Title"))
        assert req.domain == "application"

    def test_domain_resolves_from_case_title_signals(self):
        # When the title carries domain signal, that flows through the
        # _resolve_domain heuristic.
        case = _StubCase("c1", "Title")
        case.tags = ["istio", "envoy", "destinationrule"]
        req = CaseConversionRequest.from_case(case)
        assert req.domain == "networking"
