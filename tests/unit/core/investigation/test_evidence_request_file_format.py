"""Evidence-request file-format guardrail.

Interim behavior (until multimodal evidence ingestion lands): when the agent
asks the user to provide a *file*, it must ask only for a text-readable file —
never a screenshot/image/other non-text file, which the pipeline cannot read
yet. The guardrail is deliberately CONDITIONAL on a file request: it does NOT
push the agent to ask for plain-text files in general, it only constrains the
file-request path. When the data is on a page the user is viewing, the agent
may point them to the "Analyze current page" capture instead of a screenshot.

These pin the directive in ``_FOLLOW_UP_SUGGESTIONS_BLOCK`` (the EVIDENCE
suggestion definition) and that it reaches both the INQUIRY and INVESTIGATING
prompts, where evidence requests are generated.

When multimodal ingestion ships, relax this to "prefer text, accept images"
and update these tests accordingly.

Run:
    pytest tests/unit/core/investigation/test_evidence_request_file_format.py -v
"""

from __future__ import annotations

import pytest

from faultmaven.core.investigation.prompts.templates import (
    _FOLLOW_UP_SUGGESTIONS_BLOCK,
    INQUIRY_TEMPLATE,
    INVESTIGATION_BASE,
    _page_capture_hint,
)


@pytest.mark.unit
class TestEvidenceFileRequestGuardrail:
    def test_non_text_file_request_is_prohibited(self):
        """The agent must not ask the user to upload a screenshot/image/other
        non-text file — the evidence pipeline can't read those yet."""
        block = _FOLLOW_UP_SUGGESTIONS_BLOCK
        assert "Do NOT ask for a screenshot" in block
        assert "non-text" in block
        assert "cannot read" in block

    def test_constraint_is_conditional_on_a_file_request(self):
        """Scope guard: the directive only bites when the agent is asking for a
        FILE. It must NOT read as a blanket 'always ask the user for plain text'
        instruction — that would bias the agent toward file requests."""
        assert "IF the data you need is a FILE" in _FOLLOW_UP_SUGGESTIONS_BLOCK

    def test_page_capture_guidance_is_engine_resolved(self):
        """The block carries a placeholder, not a client roster: which
        page-capture guidance renders is resolved by the engine from
        Case.source (the LLM cannot decide client identity)."""
        assert "{page_capture_hint}" in _FOLLOW_UP_SUGGESTIONS_BLOCK

    def test_copilot_cases_get_the_capture_pointer(self):
        """The capture affordance exists only in the browser extension."""
        hint = _page_capture_hint("copilot")
        assert "Analyze current page" in hint

    @pytest.mark.parametrize("source", ["slack", "api", None, "unknown-client"])
    def test_non_copilot_cases_never_mention_the_capture(self, source):
        """A Slack/API user pointed at 'Analyze current page' is directed to
        a feature their client doesn't have. Unknown sources fail safe to
        copy/paste."""
        hint = _page_capture_hint(source)
        assert "Analyze current page" not in hint
        assert "copy/paste" in hint

    @pytest.mark.parametrize(
        "template,name",
        [
            (INQUIRY_TEMPLATE, "INQUIRY_TEMPLATE"),
            (INVESTIGATION_BASE, "INVESTIGATION_BASE"),
        ],
    )
    def test_guardrail_reaches_request_generating_prompts(self, template, name):
        """Evidence requests are generated in both the INQUIRY and INVESTIGATING
        prompts (both compose _FOLLOW_UP_SUGGESTIONS_BLOCK), so the guardrail
        must be present in each assembled template."""
        assert (
            "Do NOT ask for a screenshot" in template
        ), f"{name} lost the non-text-file evidence-request guardrail."
        assert "{page_capture_hint}" in template
