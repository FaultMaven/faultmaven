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
from tests.wallclock import assert_linear_growth

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
    assert flagged.kb_question == KbQuestion(
        kind=LOOKUP, topic="We're seeing 502s at 14:00", from_context=True
    )


def test_the_intent_data_form_is_plain():
    assert KbQuestion(kind=LOOKUP, topic="kafka lag").as_intent_data() == {
        "kind": "lookup",
        "topic": "kafka lag",
        "from_context": False,
    }


class TestTheOffTopicClassifierIsNotAsked:
    def test_a_flagged_question_skips_it(self):
        classification = classify_query("What runbooks do you have?")
        assert classification.kb_question is not None
        assert needs_llm_triage(classification) is False
        # Positive control: the same mode and confidence without the flag is
        # the open bucket the classifier is asked about.
        unflagged = QueryClassification(
            mode=classification.mode, confidence=classification.confidence
        )
        assert needs_llm_triage(unflagged) is True

    def test_the_same_bucket_without_the_flag_still_asks(self):
        open_bucket = QueryClassification(
            mode=ProcessingMode.DIRECTED_ANALYSIS, confidence=0.6
        )
        assert needs_llm_triage(open_bucket) is True

    def test_a_flagged_knowledge_query_skips_it(self):
        classification = classify_query("What is the runbook for kafka consumer lag?")
        assert classification.mode is ProcessingMode.KNOWLEDGE_QUERY
        assert needs_llm_triage(classification) is False


# ---------------------------------------------------------------------------
# Review round (#2797): precision, topics and cost
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        # An Ansible playbook in a debugging question.
        "what's the playbook task that failed here?",
        "show me why the playbook failed",
        "explain the playbook error: fatal: [web1]",
        "do we have any playbooks running at that time?",
        "is there a playbook run that touched the db?",
        # A runbook already in the conversation.
        "is there a runbook step I missed?",
        "what is the runbook step 3 doing?",
        "explain the runbook you gave me earlier",
        "list the runbooks you used",
        "is there a reason the runbook didn't work?",
        # The noun is the object of a preposition.
        "is there a way to fix this without the runbook?",
        # Asks which runbooks apply to the case: the investigation's own push.
        "what runbooks are relevant here?",
    ],
)
def test_a_runbook_named_in_passing_is_not_a_question_about_the_store(message):
    assert detect_kb_question(message) is None


@pytest.mark.parametrize(
    "message, topic",
    [
        (
            "We ran out of disk space, is there a runbook for that?",
            "We ran out of disk space",
        ),
        ("the jobs won't run, is there a runbook for cron failures?", "cron failures"),
        (
            "I need to update the cert, is there a runbook for cert rotation?",
            "cert rotation",
        ),
        (
            "Can't delete the namespace, is there a runbook for stuck namespaces?",
            "stuck namespaces",
        ),
    ],
)
def test_an_action_in_another_clause_does_not_veto_the_question(message, topic):
    question = detect_kb_question(message)
    assert (question.kind, question.topic) == (LOOKUP, topic)


@pytest.mark.parametrize(
    "message, topic",
    [
        (
            "Our nginx is returning 502s since 14:00. Is there a runbook for this?",
            "Our nginx is returning 502s since 14:00",
        ),
        (
            "kafka consumers are stuck rebalancing, any runbooks for this?",
            "kafka consumers are stuck rebalancing",
        ),
    ],
)
def test_this_points_back_at_the_rest_of_the_message(message, topic):
    assert detect_kb_question(message) == KbQuestion(
        kind=LOOKUP, topic=topic, from_context=True
    )


@pytest.mark.parametrize(
    "message, expected",
    [
        ("do you have any runbooks?", KbQuestion(kind=LISTING)),
        ("list all the runbooks", KbQuestion(kind=LISTING)),
        # Filler words are no topic: the engine falls back.
        ("can you show me the runbook?", KbQuestion(kind=LOOKUP, topic="")),
        ("summarize the runbook", KbQuestion(kind=LOOKUP, topic="")),
        ("what does the runbook say to do next?", KbQuestion(kind=LOOKUP, topic="")),
    ],
)
def test_the_question_frame_is_never_the_topic(message, expected):
    assert detect_kb_question(message) == expected


@pytest.mark.parametrize(
    "message",
    [
        "Is there a runbook for postgres replication lag. Also we see errors in "
        "the logs since 3pm",
        "is there a runbook for postgres replication lag, and can you check the "
        "uploaded log too?",
    ],
)
def test_a_topic_ends_at_its_clause(message):
    assert detect_kb_question(message) == KbQuestion(
        kind=LOOKUP, topic="postgres replication lag"
    )


@pytest.mark.parametrize(
    "payload_at, label",
    [
        # The shapes that were quadratic: a frame that never completes.
        (lambda n: "is there a runbook " + "runbooks do " * n, "unfinished frames"),
        (lambda n: "which runbooks do " + "runbooks do x " * n, "listing frames"),
        (lambda n: "is there a " + "word " * n + "runbook for x?", "filler words"),
    ],
    ids=["unfinished-frames", "listing-frames", "filler-words"],
)
def test_detection_cost_does_not_grow_with_the_message(payload_at, label):
    """It runs synchronously on every turn, and a turn's text can be
    megabytes. A quadratic pattern took 13.9 s on an 80k-character message."""
    assert_linear_growth(
        detect_kb_question,
        payload_at,
        small=16,
        label=f"detect_kb_question on {label}",
    )


# ---------------------------------------------------------------------------
# Review round 2 (#2797)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message, topic",
    [
        # A full stop ends the clause an action verb governs.
        (
            "We run postgres 15. Is there a runbook for replication lag?",
            "replication lag",
        ),
        # The headline phrasing.
        ("what does our runbook on postgres replication say?", "postgres replication"),
        (
            "what does our runbook on postgres replication say about failover?",
            "postgres replication failover",
        ),
        # The runbook's name stays in the search beside the subject.
        (
            "what does the nginx 502 runbook say about upstream timeouts?",
            "nginx 502 upstream timeouts",
        ),
        # An error message keeps its colon.
        (
            "is there a runbook for Error: ECONNREFUSED 10.0.0.1:5432",
            "Error: ECONNREFUSED 10.0.0.1:5432",
        ),
        # A topic may open with a word the question frame also uses.
        (
            "is there a runbook for open file descriptor exhaustion?",
            "open file descriptor exhaustion",
        ),
        ("do we have a runbook on list index out of range", "list index out of range"),
        # A second knowledge-base noun inside the topic is the topic's.
        (
            "is there a runbook for ansible playbook failed tasks",
            "ansible playbook failed tasks",
        ),
        ("is there a runbook for the v1.2 upgrade?", "v1.2 upgrade"),
        ("give me a link to the kafka runbook", "kafka"),
    ],
)
def test_round_two_topics(message, topic):
    question = detect_kb_question(message)
    assert (question.kind, question.topic) == (LOOKUP, topic)


@pytest.mark.parametrize(
    "message",
    [
        # The noun belongs to another clause's subject.
        "is there a reason the runbook restarts the broker first?",
        # A runbook already in the conversation.
        "what's in the runbook you just showed me, step 3?",
        "explain the runbook from earlier",
        "is the runbook above still right?",
    ],
)
def test_round_two_messages_that_are_not_questions_about_the_store(message):
    assert detect_kb_question(message) is None


def test_a_pasted_log_is_not_the_topic():
    """The engine searches with the problem statement instead (code review
    #6): 300 characters of log find nothing useful."""
    log = "2026-11-14 10:00:01 ERROR conn refused to db host\n" * 40
    assert detect_kb_question(log + "\nis there a runbook for this?") == KbQuestion(
        kind=LOOKUP, topic=""
    )
    # A paste in the question's own paragraph is refused whole, never cut to
    # a prefix of log.
    same_paragraph = log.rstrip("\n") + "\nis there a runbook for this?"
    assert detect_kb_question(same_paragraph) == KbQuestion(kind=LOOKUP, topic="")
    # Positive control: a short description in the question's paragraph is
    # still the topic.
    assert detect_kb_question(
        "Our nginx is returning 502s.\nIs there a runbook for this?"
    ) == KbQuestion(kind=LOOKUP, topic="Our nginx is returning 502s", from_context=True)


class TestAPureKnowledgeBaseQuestionIsNotDiagnostic:
    """F4: a question that reports nothing about the case is a knowledge
    query, so tools are not forced on a case with files to answer it."""

    @pytest.mark.parametrize(
        "message",
        [
            "Is there a runbook for redis OOM?",
            # The topic's own anchors (a status code) do not count.
            "Is there a runbook for nginx 502?",
            "What runbooks do you have?",
            "is there a runbook for this?",
        ],
    )
    def test_it_is_a_knowledge_query(self, message):
        assert classify_query(message).mode is ProcessingMode.KNOWLEDGE_QUERY

    @pytest.mark.parametrize(
        "message",
        [
            # The report is in the rest of the message.
            "We're seeing 502s at 14:00, is there a runbook for this?",
            "kafka consumers are stuck rebalancing, any runbooks for this?",
            # An anchor outside the topic.
            "the logs show errors since 14:00 — is there a runbook for redis OOM?",
        ],
    )
    def test_one_that_also_reports_the_case_stays_diagnostic(self, message):
        classification = classify_query(message)
        assert classification.kb_question is not None
        assert classification.mode is not ProcessingMode.KNOWLEDGE_QUERY
