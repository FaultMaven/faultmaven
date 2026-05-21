"""Evidence Grounding Validator

Detects hallucinated evidence references in agent_response prose. Item 5
in the 2026-05-20 investigation-pipeline-followups handoff: prompt-side
discipline alone (the ``_EVIDENCE_GROUNDING_BLOCK`` in templates.py)
relies on LLM compliance. This validator is the code-side safety net.

Scope (intentionally narrow, per handoff): scan agent_response for
``ev_*`` evidence-ID tokens and verify each is in ``case.evidence``.
The prompt explicitly tells the LLM NOT to cite ``ev_*`` IDs in
agent_response at all (use the evidence label instead), so any match
is by definition a compliance break. Ungrounded matches additionally
signal fabrication — the LLM emitted an ID that doesn't exist on the
case.

This is a telemetry signal, NOT a hard gate. Matches the pacing of
``_log_dropped_fields`` and ``engine_owned_affordance_served_total``:
instrument the silent failure so it stops being silent. The validator
does not modify the response.

Broader hallucination shapes (fabricated filenames, invented log
contents, made-up pod names) are not detectable without semantic
context and stay covered by the prompt-side block. If those shapes
need a code-side check too, that's a follow-up that requires named-
entity extraction against the case's known files/services — out of
scope here.
"""

from __future__ import annotations

import logging
import re

from faultmaven.modules.case.contracts import Case

logger = logging.getLogger(__name__)


# Matches the canonical evidence-ID shape from
# modules.case.domain.models.Evidence.evidence_id (pattern: ev_[a-f0-9]{12}).
# Anchored with word boundaries so longer hex strings or substrings of
# similar shape (e.g., a checksum) don't match.
_EV_ID_PATTERN = re.compile(r"\bev_[a-f0-9]{12}\b")


def find_evidence_id_references(agent_response: str) -> list[str]:
    """Return all ``ev_*`` evidence-ID tokens in ``agent_response``.

    Pure function — no case state required. Used by the validator
    below and exposed for tests + future callers that want to scan
    arbitrary text (e.g., turn-history audits).
    """
    if not agent_response:
        return []
    return _EV_ID_PATTERN.findall(agent_response)


def validate_evidence_grounding(
    case: Case, agent_response: str
) -> tuple[bool, list[str], list[str]]:
    """Check that any ``ev_*`` IDs in ``agent_response`` exist on the case.

    Returns a triple:
      - ``is_clean``: True when no ``ev_*`` tokens appear in prose.
        Compliance with the prompt's "never cite IDs in agent_response"
        rule. False when ANY ID appears, regardless of grounding.
      - ``ungrounded_ids``: ``ev_*`` tokens that appear in prose but
        are NOT in ``case.evidence``. These are the fabricated-ID
        shape — the LLM emitted an ID that doesn't exist.
      - ``cited_ids``: ``ev_*`` tokens that appear in prose AND exist
        on the case. Prompt-compliance break, but not hallucination
        per se (the ID is real, just shouldn't have been cited to
        the user).

    The split matters for diagnosis: ungrounded IDs are a stronger
    failure signal (fabrication) than cited-but-real IDs (style
    violation).
    """
    refs = find_evidence_id_references(agent_response)
    if not refs:
        return True, [], []

    known_ids = {ev.evidence_id for ev in case.evidence}
    ungrounded = sorted({r for r in refs if r not in known_ids})
    cited = sorted({r for r in refs if r in known_ids})
    return False, ungrounded, cited
