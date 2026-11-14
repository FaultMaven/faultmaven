"""Recognising a question about the knowledge base itself (§5.6 of
``orchestration-capabilities.md``).

The flag sits beside the processing mode, never instead of it: a message that
also reports a symptom keeps its diagnostic handling. Precision-first — a miss
falls back to the message's ordinary handling, while a false positive tells the
user something about their knowledge base they never asked.
"""

import pytest

from faultmaven.modules.agent.domain.services.out_of_band import needs_llm_triage
from faultmaven.modules.agent.domain.services.query_classifier import (
    KbQuestion,
    KbQuestionKind,
    ProcessingMode,
    QueryClassification,
    classify_query,
    detect_kb_question,
)

pytestmark = pytest.mark.unit

LOOKUP = KbQuestionKind.LOOKUP
LISTING = KbQuestionKind.LISTING


@pytest.mark.parametrize(
    "message, kind, topic",
    [
        ("Is there a runbook for redis OOM?", LOOKUP, "redis OOM"),
        ("Do we have a runbook for kafka consumer lag?", LOOKUP, "kafka consumer lag"),
        ("What is the runbook for kafka consumer lag?", LOOKUP, "kafka consumer lag"),
        (
            "What does our runbook say about certificate expiry?",
            LOOKUP,
            "certificate expiry",
        ),
        ("kafka consumer lag runbook?", LOOKUP, "kafka consumer lag"),
        ("List the runbooks about kafka", LOOKUP, "kafka"),
        ("which runbooks do we have for kafka?", LOOKUP, "kafka"),
        (
            "Is there a runbook titled 'PostgreSQL Replication Lag'?",
            LOOKUP,
            "PostgreSQL Replication Lag",
        ),
        (
            "Show me the PostgreSQL Replication Lag runbook",
            LOOKUP,
            "PostgreSQL Replication Lag",
        ),
        (
            "What does the runbook 'Redis Memory Pressure' say?",
            LOOKUP,
            "Redis Memory Pressure",
        ),
        ("Open our team's DNS failover runbook", LOOKUP, "DNS failover"),
        (
            "Does the knowledge base cover certificate rotation?",
            LOOKUP,
            "certificate rotation",
        ),
        ("any runbooks on etcd quorum loss?", LOOKUP, "etcd quorum loss"),
        (
            "Explain the Postgres replication lag runbook",
            LOOKUP,
            "Postgres replication lag",
        ),
        ("what's in the nginx 502 runbook", LOOKUP, "nginx 502"),
        (
            "is there anything in the knowledge base on TLS rotation?",
            LOOKUP,
            "TLS rotation",
        ),
        ("do we have a playbook for DNS outages", LOOKUP, "DNS outages"),
        (
            "Do you have a runbook to recover from a split-brain etcd cluster?",
            LOOKUP,
            "split-brain etcd cluster",
        ),
        ("What runbooks do you have?", LISTING, ""),
    ],
)
def test_a_question_about_the_knowledge_base_is_recognised(message, kind, topic):
    assert detect_kb_question(message) == KbQuestion(kind=kind, topic=topic)


@pytest.mark.parametrize(
    "message",
    [
        "Is there a runbook for this?",
        "We're seeing 502s at 14:00, is there a runbook for this?",
        "do we have a runbook for this error?",
    ],
)
def test_a_question_that_points_back_names_no_topic(message):
    """The engine then searches with the case's problem statement, or asks."""
    assert detect_kb_question(message) == KbQuestion(kind=LOOKUP, topic="")


@pytest.mark.parametrize(
    "message",
    [
        # Doing something to a runbook is not asking what the store holds.
        "Create a runbook from this case",
        "convert this case into a runbook",
        "update the kafka runbook with this fix",
        "I followed the runbook but it didn't work",
        "follow step 3 of the runbook",
        "we ran the runbook and the pod still crashes",
        "the runbook didn't work",
        # The noun is the object of another phrase.
        "find the root cause in the runbook",
        "What does the log say about the runbook job?",
        # No knowledge-base noun: deliberately not caught.
        "Do you have anything on etcd quorum loss?",
        "How do I fix postgres replication lag?",
        "how does the knowledge base work?",
        # "KB" is kilobytes, not the knowledge base.
        "the disk is 90% full, only 3 KB left",
    ],
)
def test_a_message_that_is_not_one_is_left_alone(message):
    assert classify_query(message).kb_question is None


def test_a_question_about_faultmaven_is_not_also_a_knowledge_base_question():
    classification = classify_query("How do you retrieve runbooks?")
    assert classification.mode is ProcessingMode.AGENT_META
    assert classification.kb_question is None


def test_the_flag_leaves_the_mode_alone():
    """A symptom plus a runbook question stays a diagnostic turn."""
    message = "We're seeing 502s at 14:00, is there a runbook for this?"
    flagged = classify_query(message)
    assert flagged.mode is ProcessingMode.DIRECTED_ANALYSIS
    assert flagged.kb_question == KbQuestion(kind=LOOKUP, topic="")


def test_the_intent_data_form_is_plain():
    assert KbQuestion(kind=LOOKUP, topic="kafka lag").as_intent_data() == {
        "kind": "lookup",
        "topic": "kafka lag",
    }


class TestTheOffTopicClassifierIsNotAsked:
    def test_a_flagged_question_skips_it(self):
        classification = classify_query("What runbooks do you have?")
        # Positive control: this is the open bucket the classifier would
        # otherwise be asked about.
        assert classification.mode is ProcessingMode.DIRECTED_ANALYSIS
        assert classification.confidence <= 0.6
        assert classification.kb_question is not None
        assert needs_llm_triage(classification) is False

    def test_the_same_bucket_without_the_flag_still_asks(self):
        open_bucket = QueryClassification(
            mode=ProcessingMode.DIRECTED_ANALYSIS, confidence=0.6
        )
        assert needs_llm_triage(open_bucket) is True

    def test_a_flagged_knowledge_query_skips_it(self):
        classification = classify_query("What is the runbook for kafka consumer lag?")
        assert classification.mode is ProcessingMode.KNOWLEDGE_QUERY
        assert needs_llm_triage(classification) is False
