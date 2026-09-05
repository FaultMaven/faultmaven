"""Tests for the query classifier — scenario-driven processing mode routing.

Validates that classify_query() correctly routes user messages to
TRIAGE, KNOWLEDGE_QUERY, AGENT_META, or DIRECTED_ANALYSIS based on heuristic
entity detection and phrasing analysis. No LLM involved — this is deterministic.
"""

import pytest

from faultmaven.modules.agent.domain.services.query_classifier import (
    ProcessingMode,
    QueryClassification,
    _extract_entities,
    _has_case_reference,
    _has_interrogative_structure,
    _is_agent_self_reference,
    _is_generic_request,
    _is_knowledge_question,
    classify_query,
)

# ============================================================
# Entity Extraction
# ============================================================


class TestExtractEntities:
    """Tests for _extract_entities() regex-based entity detection."""

    def test_extracts_hh_mm_timestamps(self):
        entities = _extract_entities("what happened at 14:00?")
        assert "timestamps" in entities
        assert "14:00" in entities["timestamps"]

    def test_extracts_hh_mm_ss_timestamps(self):
        entities = _extract_entities("error at 14:30:15")
        assert "14:30:15" in entities["timestamps"]

    def test_extracts_iso_dates(self):
        entities = _extract_entities("check logs for 2024-01-15")
        assert "2024-01-15" in entities["timestamps"]

    def test_extracts_month_day_dates(self):
        entities = _extract_entities("crash on Jan 15 was bad")
        assert "Jan 15" in entities["timestamps"]

    def test_extracts_http_status_codes(self):
        entities = _extract_entities("why are we getting 503 and 404 errors?")
        assert "status_codes" in entities
        assert "503" in entities["status_codes"]
        assert "404" in entities["status_codes"]

    def test_ignores_non_error_status_codes(self):
        """Only 4xx and 5xx should be captured."""
        entities = _extract_entities("we got 200 OK and 301 redirect")
        assert "status_codes" not in entities

    def test_extracts_error_keywords(self):
        entities = _extract_entities("the process was killed due to OOM")
        assert "error_keywords" in entities
        assert "oom" in entities["error_keywords"]
        assert "killed" in entities["error_keywords"]

    def test_extracts_multi_word_error_keywords(self):
        entities = _extract_entities("seeing connection refused from the backend")
        assert "connection refused" in entities["error_keywords"]

    def test_extracts_service_names(self):
        entities = _extract_entities("check logs from nginx and errors from redis")
        assert "services" in entities
        assert "nginx" in entities["services"]
        assert "redis" in entities["services"]

    def test_extracts_ip_addresses(self):
        entities = _extract_entities("connection from 10.0.0.5 is blocked")
        assert "ip_addresses" in entities
        assert "10.0.0.5" in entities["ip_addresses"]

    def test_no_entities_from_plain_text(self):
        entities = _extract_entities("what is the root cause?")
        assert not any(entities.values())

    def test_multiple_entity_types(self):
        entities = _extract_entities("why did nginx return 502 at 14:00 from 10.0.0.1?")
        assert "timestamps" in entities
        assert "status_codes" in entities
        assert "services" in entities
        assert "ip_addresses" in entities


# ============================================================
# Generic Request Detection
# ============================================================


class TestIsGenericRequest:
    """Tests for _is_generic_request() phrasing detection."""

    @pytest.mark.parametrize(
        "message",
        [
            "analyze this log file",
            "check this for me",
            "look at the errors",
            "what's in here",
            "what is going on",
            "anything wrong with this?",
            "see any issues?",
            "can you look at this",
            "here's the log file",
            "tell me what's happening",
            "what do you see",
            "review my application logs",
            "examine this config",
        ],
    )
    def test_detects_generic_phrases(self, message):
        assert _is_generic_request(message) is True

    @pytest.mark.parametrize(
        "message",
        [
            "why did nginx return 502 at 14:00?",
            "what caused the OOM kill?",
            "show me errors from redis between 13:00 and 14:00",
            "is the connection timeout related to DNS?",
        ],
    )
    def test_specific_questions_are_not_generic(self, message):
        assert _is_generic_request(message) is False


# ============================================================
# Interrogative Structure Detection
# ============================================================


class TestHasInterrogativeStructure:
    """Tests for _has_interrogative_structure()."""

    def test_detects_question_with_interrogative(self):
        assert _has_interrogative_structure("what caused the error?") is True

    def test_detects_why_question(self):
        assert _has_interrogative_structure("why is the app slow?") is True

    def test_no_question_mark_returns_false(self):
        assert _has_interrogative_structure("check the logs") is False

    def test_question_mark_without_interrogative(self):
        """A lone '?' without interrogative words is not detected."""
        assert _has_interrogative_structure("hmm?") is False


# ============================================================
# classify_query — Core Routing Logic
# ============================================================


class TestClassifyQuery:
    """Tests for classify_query() end-to-end routing decisions."""

    # --- TRIAGE cases ---

    def test_no_message_with_attachments_is_triage(self):
        """User dropped a file with no question → Triage."""
        result = classify_query("", has_attachments=True)
        assert result.mode == ProcessingMode.TRIAGE
        assert result.confidence >= 0.9

    def test_no_message_no_attachments_is_triage(self):
        """Empty turn → Triage (low confidence)."""
        result = classify_query("")
        assert result.mode == ProcessingMode.TRIAGE

    def test_generic_phrasing_no_entities_is_triage(self):
        """Generic request without technical entities → Triage."""
        result = classify_query("analyze this log file")
        assert result.mode == ProcessingMode.TRIAGE
        assert result.confidence >= 0.8

    def test_whitespace_only_is_triage(self):
        result = classify_query("   ")
        assert result.mode == ProcessingMode.TRIAGE

    # --- DIRECTED_ANALYSIS cases ---

    def test_entities_plus_question_is_da(self):
        """Specific entities + interrogative structure → DA (high confidence)."""
        result = classify_query("what caused the 502 errors at 14:00?")
        assert result.mode == ProcessingMode.DIRECTED_ANALYSIS
        assert result.confidence >= 0.9
        assert "status_codes" in result.detected_entities
        assert "timestamps" in result.detected_entities

    def test_entities_without_question_not_generic_is_da(self):
        """Entities + non-generic phrasing → DA."""
        result = classify_query("investigate 502s at 14:00")
        assert result.mode == ProcessingMode.DIRECTED_ANALYSIS
        assert result.confidence >= 0.7

    def test_question_without_entities_is_da(self):
        """Question with interrogative but no technical entities → DA (lower confidence)."""
        result = classify_query("why is the app slow?")
        assert result.mode == ProcessingMode.DIRECTED_ANALYSIS
        assert result.confidence >= 0.5

    def test_ambiguous_defaults_to_da(self):
        """Ambiguous input (no entities, no generic phrasing, no question) → DA."""
        result = classify_query("performance degradation")
        assert result.mode == ProcessingMode.DIRECTED_ANALYSIS
        assert result.confidence == 0.5

    def test_generic_with_entities_routes_to_da(self):
        """Generic phrasing BUT with entities → entities win, route to DA."""
        result = classify_query("check this for OOM errors")
        assert result.mode == ProcessingMode.DIRECTED_ANALYSIS
        assert "error_keywords" in result.detected_entities

    def test_service_name_triggers_da(self):
        """Mentioning a service name with a question → DA."""
        result = classify_query("what errors is nginx throwing?")
        assert result.mode == ProcessingMode.DIRECTED_ANALYSIS
        assert "services" in result.detected_entities

    def test_error_keyword_triggers_da(self):
        result = classify_query("investigate the connection timeout")
        assert result.mode == ProcessingMode.DIRECTED_ANALYSIS
        assert "error_keywords" in result.detected_entities

    # --- Classification dataclass ---

    def test_classification_has_correct_fields(self):
        result = classify_query("what happened at 14:00?")
        assert isinstance(result, QueryClassification)
        assert isinstance(result.mode, ProcessingMode)
        assert isinstance(result.detected_entities, dict)
        assert 0.0 <= result.confidence <= 1.0

    def test_processing_mode_enum_values(self):
        assert ProcessingMode.TRIAGE.value == "triage"
        assert ProcessingMode.DIRECTED_ANALYSIS.value == "directed_analysis"
        assert ProcessingMode.KNOWLEDGE_QUERY.value == "knowledge_query"
        assert ProcessingMode.SEMANTIC_SEARCH.value == "semantic_search"


# ============================================================
# Knowledge Question Detection
# ============================================================


class TestIsKnowledgeQuestion:
    """Tests for _is_knowledge_question() detection."""

    @pytest.mark.parametrize(
        "message",
        [
            "What is Opik?",
            "What is a load balancer?",
            "How does Redis clustering work?",
            "How to set up a reverse proxy?",
            "Explain the CAP theorem",
            "What's the difference between TCP and UDP?",
            "Can you explain what Kubernetes does?",
            "What does etcd do?",
            "What is gRPC used for?",
            "How are microservices different from monoliths?",
            "What's the purpose of a service mesh?",
            "What are common causes of memory leaks?",
            "Best practice for database indexing?",
            "How does connection pooling work?",
            "Can you describe what Prometheus does?",
        ],
    )
    def test_detects_knowledge_questions(self, message):
        # Service names/error keywords are allowed in knowledge questions
        entities = _extract_entities(message)
        assert _is_knowledge_question(message, entities) is True

    @pytest.mark.parametrize(
        "message",
        [
            "What is the error in the logs?",
            "How does this service crash?",
            "Explain the errors we're seeing",
            "What is the issue with our system?",
        ],
    )
    def test_case_references_override_knowledge(self, message):
        """Questions with case-specific references are NOT knowledge queries."""
        assert _is_knowledge_question(message, {}) is False

    def test_timestamps_override_knowledge(self):
        """Questions with timestamps are NOT knowledge queries."""
        entities = {"timestamps": ["14:00"]}
        assert _is_knowledge_question("What happened at 14:00?", entities) is False

    def test_status_codes_override_knowledge(self):
        """Questions with HTTP status codes are NOT knowledge queries."""
        entities = {"status_codes": ["502"]}
        assert _is_knowledge_question("What is a 502 error?", entities) is False

    def test_ip_addresses_override_knowledge(self):
        """Questions with IP addresses are NOT knowledge queries."""
        entities = {"ip_addresses": ["10.0.0.1"]}
        assert _is_knowledge_question("What is 10.0.0.1?", entities) is False

    def test_service_names_do_not_block_knowledge(self):
        """Service names are allowed — they can be the subject of knowledge questions."""
        entities = {"services": ["redis"]}
        assert _is_knowledge_question("How does Redis work?", entities) is True

    def test_error_keywords_do_not_block_knowledge(self):
        """Error keywords are allowed — concepts like 'connection pool' can be knowledge subjects."""
        entities = {"error_keywords": ["connection pool"]}
        assert (
            _is_knowledge_question("How does connection pooling work?", entities)
            is True
        )


class TestHasCaseReference:
    """Tests for _has_case_reference() detection."""

    @pytest.mark.parametrize(
        "message",
        [
            "check the log file",
            "what's in the logs?",
            "look at this error",
            "we're seeing high latency",
            "I'm getting timeout errors",
            "what happened to our service?",
            "what caused the crash?",
            "the errors in the dump",
            "from the trace output",
        ],
    )
    def test_detects_case_references(self, message):
        assert _has_case_reference(message) is True

    @pytest.mark.parametrize(
        "message",
        [
            "What is Opik?",
            "How does Redis clustering work?",
            "Explain the CAP theorem",
            # Grey-zone: bare nouns that SHOULD NOT trigger case reference
            "What is a null pointer exception?",
            "How to handle errors in async Python?",
            "What is a service mesh?",
            "Explain log rotation strategies",
            "What is data replication?",
            "How does file system journaling work?",
            "What is a stack trace?",
            "What is a system call?",
            "Best practices for error handling?",
        ],
    )
    def test_no_case_reference_in_knowledge_questions(self, message):
        assert _has_case_reference(message) is False


# ============================================================
# classify_query — Knowledge Query Routing
# ============================================================


class TestClassifyQueryKnowledgeMode:
    """Tests for KNOWLEDGE_QUERY routing in classify_query()."""

    @pytest.mark.parametrize(
        "message",
        [
            "What is Opik?",
            "What is a circuit breaker pattern?",
            "How does connection pooling work?",
            "Explain the difference between TCP and UDP",
            "How to configure Redis sentinel?",
            "What's the purpose of a WAF?",
            "Can you describe what Prometheus does?",
            "What are best practices for logging?",
        ],
    )
    def test_knowledge_questions_route_to_knowledge_query(self, message):
        """Pure knowledge questions → KNOWLEDGE_QUERY."""
        result = classify_query(message)
        assert result.mode == ProcessingMode.KNOWLEDGE_QUERY
        assert result.confidence >= 0.8

    @pytest.mark.parametrize(
        "message",
        [
            # Grey-zone: bare nouns that previously triggered false-positive
            # case reference detection. These are knowledge questions about
            # concepts that happen to use words like "error", "service", "file".
            "What is a null pointer exception?",
            "How to handle errors in async Python?",
            "What is a service mesh?",
            "Explain log rotation strategies",
            "What is data replication?",
            "How does file system journaling work?",
            "What is a stack trace?",
            "What is a system call?",
            "Best practices for error handling?",
            "How to configure a node pool?",
        ],
    )
    def test_grey_zone_knowledge_questions_not_blocked(self, message):
        """Knowledge questions with bare technical nouns should NOT be
        blocked by case reference detection."""
        result = classify_query(message)
        assert result.mode == ProcessingMode.KNOWLEDGE_QUERY

    def test_knowledge_with_hard_entities_routes_to_da(self):
        """Knowledge phrasing + hard case entities (timestamps, status codes) → DA."""
        result = classify_query("What is the 502 error at 14:00?")
        assert result.mode == ProcessingMode.DIRECTED_ANALYSIS

    def test_knowledge_with_case_reference_routes_to_da(self):
        """Knowledge phrasing + possessive case reference → not KNOWLEDGE_QUERY."""
        # "the error in the logs" has possessive prefix → case reference
        result = classify_query("What is the error in the logs?")
        assert result.mode != ProcessingMode.KNOWLEDGE_QUERY

    def test_knowledge_with_timestamp_entity_routes_to_da(self):
        """Knowledge phrasing + timestamp entity → DA (timestamp overrides)."""
        # "14:00" is a timestamp entity, regardless of service mention
        result = classify_query("What is nginx returning at 14:00?")
        assert result.mode == ProcessingMode.DIRECTED_ANALYSIS

    def test_knowledge_query_no_entities(self):
        """Knowledge query classification has no entities."""
        result = classify_query("What is Opik?")
        assert result.detected_entities == {}


# ============================================================
# Agent self-reference (#1328)
# ============================================================


class TestAgentSelfReference:
    """#1328: a question about FaultMaven ITSELF routes to AGENT_META.

    Before this mode existed the reported message classified as
    DIRECTED_ANALYSIS (question words, no knowledge phrasing, no entities),
    so in INVESTIGATING it was sent with ``tool_choice=required`` under the
    EVIDENCE GROUNDING block — and the agent asked the user for FaultMaven's
    own deployment manifests as case evidence.
    """

    REPORTED = (
        "Curious about how you work under the hood though: what LLM model and "
        "provider are currently generating these responses? Are you using a "
        "single model or routing tasks across different models, and what "
        "embedding model and vector database power your runbook retrieval?"
    )

    def test_reported_message_routes_to_agent_meta(self):
        result = classify_query(self.REPORTED)
        assert result.mode == ProcessingMode.AGENT_META

    @pytest.mark.parametrize(
        "message",
        [
            "What model are you?",
            "Which LLM provider is generating these responses?",
            "Who built you?",
            "How do you work?",
            "how were you built",
            "What is your architecture?",
            "Are you open source?",
            "are you an AI?",
            "Can you explain how you retrieve runbooks?",
            "what vector db do you use",
            "Which embedding model powers your runbook retrieval?",
            "are you using a single model or routing across several?",
            "what is your context window",
            "Do you use ChromaDB or Postgres for retrieval?",
            "What is FaultMaven?",
            "tell me about faultmaven",
        ],
    )
    def test_self_referential_phrasings(self, message):
        assert classify_query(message).mode == ProcessingMode.AGENT_META

    def test_service_names_do_not_block(self):
        """The subject can name a service and still be about the assistant."""
        result = classify_query("Do you use ChromaDB or Postgres for retrieval?")
        assert result.mode == ProcessingMode.AGENT_META

    @pytest.mark.parametrize(
        "message",
        [
            # Addressing the advisor about the CASE is not self-reference.
            "Could you check the logs for me?",
            "what do you see in this file?",
            "do you think the deploy caused this?",
            "Are you sure the OOM killer killed postgres?",
            "what is your recommendation for the next step?",
            # "model" / "you" / "your" in a diagnostic sense.
            "What model of NIC is in the server?",
            "Which model is the load balancer using for health checks?",
            "Which model do you recommend for the replacement GPU node?",
            "Explain your reasoning for that hypothesis.",
            "What is your version of the timeline?",
            # General knowledge stays KNOWLEDGE_QUERY.
            "How does the OOM killer choose a victim?",
            "What is Redis used for?",
        ],
    )
    def test_case_and_knowledge_questions_are_not_self_reference(self, message):
        assert classify_query(message).mode != ProcessingMode.AGENT_META

    def test_case_reference_blocks_self_reference(self):
        """A question anchored to case data is about the case however phrased."""
        result = classify_query("What does the log say about you?")
        assert result.mode != ProcessingMode.AGENT_META

    def test_hard_entities_block_self_reference(self):
        result = classify_query("What model are you, and what happened at 14:00?")
        assert result.mode == ProcessingMode.DIRECTED_ANALYSIS

    def test_precedes_knowledge_classification(self):
        """'How do you work?' also matches knowledge phrasing; self-reference wins."""
        assert _is_knowledge_question("How do you work?", {})
        assert classify_query("How do you work?").mode == ProcessingMode.AGENT_META

    def test_predicate_is_exposed(self):
        assert _is_agent_self_reference("who are you", {}) is True
        assert (
            _is_agent_self_reference("who are you", {"timestamps": ["14:00"]}) is False
        )
