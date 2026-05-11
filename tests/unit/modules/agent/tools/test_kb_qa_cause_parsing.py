"""Tests for v3 CauseChunk parsing in AnswerFromKB."""

from faultmaven.modules.agent.tools.kb_qa import AnswerFromKB


def _chunk(metadata: dict, content: str = "...") -> dict:
    return {"content": content, "metadata": metadata, "score": 0.9}


class TestCauseChunkParsing:
    def test_returns_none_for_non_cause_chunk(self):
        chunk = _chunk({"section": "Symptom Recognition"})
        assert AnswerFromKB._parse_cause_chunk(chunk) is None

    def test_parses_full_cause_chunk(self):
        chunk = _chunk(
            {
                "id": "pg-pool",
                "cause_letter": "A",
                "cause_name": "Idle transactions",
                "cause_statement": "Sessions hold connection slots.",
                "cause_mechanism": "Idle sessions retain slots indefinitely.",
                "cause_indicator": "- [Step 1] active > 80%\n- [Step 2] idle in transaction",
                "cause_mitigation": "Terminate sessions.",
                "cause_resolution": "Set timeout.",
                "cause_verification": "Re-run Step 2.",
                "is_fallback_cause": False,
                "match_predicates": '[{"step": 2, "predicate": "contains", "target": "idle"}]',
            }
        )
        cause = AnswerFromKB._parse_cause_chunk(chunk)
        assert cause is not None
        assert cause.runbook_id == "pg-pool"
        assert cause.cause_letter == "A"
        assert cause.cause_name == "Idle transactions"
        assert cause.statement == "Sessions hold connection slots."
        assert cause.mechanism == "Idle sessions retain slots indefinitely."
        assert cause.indicators == [
            "[Step 1] active > 80%",
            "[Step 2] idle in transaction",
        ]
        assert cause.match_predicates == [
            {"step": 2, "predicate": "contains", "target": "idle"}
        ]
        assert cause.mitigation == "Terminate sessions."
        assert cause.resolution == "Set timeout."
        assert cause.verification == "Re-run Step 2."
        assert cause.is_fallback is False

    def test_fallback_flag_propagates(self):
        chunk = _chunk(
            {
                "id": "rb",
                "cause_letter": "Z",
                "cause_name": "Unidentified",
                "cause_statement": "None match.",
                "cause_mechanism": "Out of scope.",
                "cause_indicator": "- [Default]",
                "cause_mitigation": "Escalate.",
                "cause_resolution": "Out of scope.",
                "cause_verification": "N/A.",
                "is_fallback_cause": True,
            }
        )
        cause = AnswerFromKB._parse_cause_chunk(chunk)
        assert cause is not None
        assert cause.is_fallback is True

    def test_malformed_match_predicates_json_is_dropped(self):
        chunk = _chunk(
            {
                "id": "rb",
                "cause_letter": "A",
                "cause_name": "n",
                "cause_statement": "s",
                "cause_mechanism": "m",
                "cause_indicator": "- [Step 1] x",
                "cause_mitigation": "x",
                "cause_resolution": "x",
                "cause_verification": "x",
                "match_predicates": "{not valid json",  # malformed
            }
        )
        cause = AnswerFromKB._parse_cause_chunk(chunk)
        assert cause is not None
        # Malformed JSON should NOT crash parsing; predicates list stays empty.
        assert cause.match_predicates == []

    def test_indicator_lines_strip_bullet_markers(self):
        chunk = _chunk(
            {
                "id": "rb",
                "cause_letter": "A",
                "cause_name": "n",
                "cause_statement": "s",
                "cause_mechanism": "m",
                "cause_indicator": "* [Step 1] one\n+ [Step 2] two\n- [Step 3] three",
                "cause_mitigation": "x",
                "cause_resolution": "x",
                "cause_verification": "x",
            }
        )
        cause = AnswerFromKB._parse_cause_chunk(chunk)
        assert cause is not None
        assert cause.indicators == [
            "[Step 1] one",
            "[Step 2] two",
            "[Step 3] three",
        ]
