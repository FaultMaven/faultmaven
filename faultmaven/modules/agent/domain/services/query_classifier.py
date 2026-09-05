"""Query classifier for scenario-driven data processing.

Classifies user messages into processing modes based on entity detection
heuristics. No LLM call — this is a fast, deterministic classifier.

Modes:
- TRIAGE: generic request, no specific inquiry, file-only uploads
- DIRECTED_ANALYSIS: specific inquiry with entities, timestamps, error codes
- KNOWLEDGE_QUERY: general knowledge question not answerable from case evidence
- AGENT_META: a question about FaultMaven itself (what model it runs on, how it
  retrieves runbooks, who built it) — about the assistant, not the system under
  investigation (#1328)

Design Reference: docs/architecture/data-processing/README.md
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ProcessingMode(str, Enum):
    """How evidence should be processed based on the user's query."""

    TRIAGE = "triage"
    DIRECTED_ANALYSIS = "directed_analysis"
    KNOWLEDGE_QUERY = "knowledge_query"
    SEMANTIC_SEARCH = "semantic_search"
    AGENT_META = "agent_meta"


@dataclass
class QueryClassification:
    """Result of classifying a user message for processing mode routing."""

    mode: ProcessingMode
    detected_entities: dict[str, list[str]] = field(default_factory=dict)
    confidence: float = 0.0


# --- Entity extraction patterns ---

# Timestamps: HH:MM, HH:MM:SS, ISO dates, Unix-style dates
_TIMESTAMP_PATTERNS = [
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b"),  # HH:MM or HH:MM:SS
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),  # YYYY-MM-DD
    re.compile(
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\b", re.I
    ),  # Mon DD
]

# HTTP status codes: 4xx, 5xx
_STATUS_CODE_PATTERN = re.compile(r"\b[45]\d{2}\b")

# Error keywords that indicate specific technical conditions
_ERROR_KEYWORDS = frozenset(
    {
        "oom",
        "segfault",
        "sigsegv",
        "sigkill",
        "sigterm",
        "killed",
        "connection refused",
        "connection timeout",
        "connection reset",
        "deadlock",
        "out of memory",
        "heap",
        "stack overflow",
        "null pointer",
        "nullpointerexception",
        "core dump",
        "kernel panic",
        "disk full",
        "no space left",
        "permission denied",
        "authentication failed",
        "certificate expired",
        "dns resolution",
        "socket timeout",
        "broken pipe",
        "connection pool",
        "thread pool",
        "gc pause",
        "full gc",
        "slow query",
        "lock wait",
        "replication lag",
    }
)

# Generic phrases that signal triage mode (no specific inquiry)
_GENERIC_PHRASES = [
    re.compile(r"\b(?:analyze|check|look at|review|examine)\s+(?:this|the|my)\b", re.I),
    re.compile(r"\bwhat(?:'s| is) (?:in (?:here|this|the)|going on)\b", re.I),
    re.compile(r"\banything (?:wrong|unusual|interesting|notable)\b", re.I),
    re.compile(r"\bsee any (?:issues?|problems?|errors?|trouble)\b", re.I),
    re.compile(r"\bcan you (?:look|check|take a look)\b", re.I),
    re.compile(r"\bhere(?:'s| is) (?:the|my|a)\b", re.I),
    re.compile(r"\btell me what(?:'s| is)\b", re.I),
    re.compile(r"\bwhat do you (?:see|think|find)\b", re.I),
]

# Service/host name pattern: common infra names
_SERVICE_PATTERN = re.compile(
    r"\b(?:nginx|apache|postgres(?:ql)?|mysql|redis|kafka|rabbit(?:mq)?|"
    r"elasticsearch|kibana|grafana|prometheus|docker|kubernetes|k8s|"
    r"etcd|consul|vault|haproxy|envoy|istio|mongo(?:db)?|"
    r"memcache[d]?|zookeeper|cassandra|dynamo(?:db)?|"
    r"node|java|python|go|rust|tomcat|jetty|gunicorn|uvicorn|"
    r"systemd|journald|syslog|cron)\b",
    re.I,
)

# IP addresses and ports
_IP_PORT_PATTERN = re.compile(r"\b(?:\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?::\d+)?)\b")

# Knowledge question patterns — phrasing that seeks general knowledge,
# not case-specific evidence analysis
_KNOWLEDGE_PHRASES = [
    re.compile(r"\bwhat (?:is|are) (?:an? |the )?(\w+)", re.I),  # "what is X?"
    re.compile(r"\bhow (?:does|do|can|should|to)\b", re.I),  # "how does X work?"
    re.compile(r"\bexplain\b", re.I),  # "explain X"
    re.compile(r"\bwhat(?:'s| is) the (?:difference|purpose|role|benefit)\b", re.I),
    re.compile(r"\bcan you (?:explain|describe|tell me about)\b", re.I),
    re.compile(r"\bwhat does .+ (?:do|mean)\b", re.I),  # "what does X do?"
    re.compile(r"\bwhat(?:'s| is) .+ used for\b", re.I),  # "what is X used for?"
    re.compile(r"\bhow (?:is|are) .+ (?:different|related|used|configured)\b", re.I),
    re.compile(
        r"\b(?:best practices?|common causes?|typical|standard approach)\b", re.I
    ),
]

# Agent self-reference patterns — the user is asking about FaultMaven ITSELF
# (the assistant answering), not about the system under investigation (#1328).
#
# Every pattern binds the question to the ASSISTANT explicitly: a second-person
# pronoun adjacent to an agent-shaped noun ("what model are you", "your
# architecture"), a clause that ENDS on the assistant ("how do you work?",
# "who are you?"), the assistant's own output ("generating these responses"),
# or an interrogative with FaultMaven as subject ("what is FaultMaven?").
# What does NOT qualify, each a shape the first cut got wrong (PR #1334 review):
#   - bare "you"/"your" addressing the advisor about the case ("could you check
#     the logs", "your stack trace shows", "what are you doing right now?");
#   - "how do you work AROUND the ulimit" — the verb must end the clause;
#   - a model/provider/vector-DB noun with no assistant binding ("which model
#     is serving the /predict endpoint", "the vector database behind our
#     search keeps timing out") — that is the user's own system;
#   - FaultMaven named as the agent in a case question ("what does FaultMaven
#     think caused the outage") or as the SUBJECT OF A CASE (a self-hosting
#     operator: "faultmaven's embedding model fails to load ... permission
#     denied") — the latter is also caught by the error-keyword gate.
# Precision-first: a miss falls through to the prompt-level rule in
# ``_ACTIVE_ADVISOR_ROLE_BLOCK`` (the backstop), while a false positive pulls a
# real diagnostic question off the evidence path.
_CLAUSE_END = r"\s*(?:[?.!,;:]|$)"
_AGENT_SELF_PATTERNS = [
    # "how do you work?", "how you work under the hood though: ..."
    re.compile(
        r"\bhow (?:do |does |are |were |was )?you (?:work|reason|built|made|trained|"
        r"implemented|designed|architected)"
        r"(?: (?:under the hood|internally|exactly|really|actually))?"
        r"(?:" + _CLAUSE_END + r"|\s+though\b)",
        re.I,
    ),
    # "how do you retrieve runbooks?" — generic plural, clause-final; NOT
    # "how do you retrieve the runbook for postgres OOM" (a kb_qa question).
    re.compile(
        r"\bhow (?:do |does )?you (?:retrieve|search|find|rank|select|choose) "
        r"(?:runbooks|knowledge|documentation|past (?:cases|fixes))" + _CLAUSE_END,
        re.I,
    ),
    # identity — the clause ends on "you": NOT "what are you doing right now?"
    re.compile(
        r"\b(?:who|what) are you(?: (?:exactly|really|actually))?" + _CLAUSE_END,
        re.I,
    ),
    re.compile(
        r"\bwho (?:built|made|created|developed|trained|designed|owns|runs|"
        r"maintains) you\b",
        re.I,
    ),
    # "are you an AI / a bot / open source / GPT / Claude", "are you powered by"
    re.compile(
        r"\bare you (?:an? |the )?(?:ai|bot|llm|chatbot|open[- ]source|human|"
        r"chatgpt|gpt(?:-?\d)?|claude|gemini|llama|mistral)\b",
        re.I,
    ),
    re.compile(r"\bare you (?:powered|built|based|running) (?:by|on)\b", re.I),
    # "what model are you (using)?", "which provider do you use?"
    re.compile(
        r"\b(?:what|which) (?:llm|language model|ai model|model|provider|"
        r"llm provider)s?(?: and (?:model|provider)s?)? "
        r"(?:are you(?: (?:using|running|on))?|do you (?:use|run|rely on|route)|"
        r"is running you|powers? you|is behind you|serves? you)\b",
        re.I,
    ),
    # "what LLM model and provider are currently generating these responses?"
    # — bound to the assistant's OWN output, not to the user's chat service.
    re.compile(
        r"\b(?:what|which) (?:llm|language model|model|provider)s?[^?.!]{0,40}?"
        r"\b(?:is|are) (?:currently |actually |really )?"
        r"(?:generating|producing|writing|answering|behind|powering|serving) "
        r"(?:these|this|your|the) (?:responses?|answers?|replies|messages?|"
        r"conversation)\b",
        re.I,
    ),
    # "are you using a single model or routing across several?"
    re.compile(
        r"\bare you (?:using|running|routing) (?:a |an |the )?(?:single |one |"
        r"multiple |different |several )?(?:model|llm|provider)s?\b",
        re.I,
    ),
    # "your model / architecture / stack / embeddings / prompt / context window".
    # NOT "your stack trace", "your reasoning", "your memory", "your version",
    # "your recommendation" — those are about the case analysis.
    re.compile(
        r"\byour (?:own )?(?:model|llm|architecture|stack(?! trace)|prompt|"
        r"system prompt|context window|training(?: data)?|knowledge cutoff|"
        r"embeddings?|embedding model|vector (?:database|db|store)|"
        r"runbook retrieval|source code|codebase|internals)\b",
        re.I,
    ),
    # "what are your capabilities?" / "your limitations?" — clause-final, so
    # "your capabilities in terms of parsing this dump" stays a case question.
    re.compile(
        r"\byour (?:capabilities|limitations)(?: (?:as an? (?:ai|assistant|tool)|"
        r"in general|overall))?" + _CLAUSE_END,
        re.I,
    ),
    # component bound to the assistant: "what vector db do you use",
    # "which embedding model powers you" — NOT "the vector database behind our
    # search" (no "you").
    re.compile(
        r"\b(?:embedding model|vector (?:database|db|store)|llm|language model)s? "
        r"(?:do you use|are you using|do you run|are you running|powers? you|"
        r"behind you|you use|you run|you rely on)\b",
        re.I,
    ),
    # "do you use ChromaDB or Postgres for retrieval?"
    re.compile(
        r"\bdo you (?:use|run|rely on) [^?.!]{1,60}? for (?:retrieval|embeddings?|"
        r"vector search|runbook (?:search|retrieval)|routing|classification|"
        r"your (?:answers|responses|reasoning))\b",
        re.I,
    ),
    # FaultMaven as the subject — interrogative forms only. NOT "what does
    # FaultMaven think caused the outage" (a case question addressed by name).
    re.compile(
        r"\b(?:what|who) (?:is|are) faultmaven(?: (?:exactly|really|actually))?"
        + _CLAUSE_END,
        re.I,
    ),
    re.compile(
        r"\bhow does faultmaven (?:work|reason|route|retrieve|investigate|decide|"
        r"learn)\b(?! (?:around|with|on))",
        re.I,
    ),
    re.compile(r"\b(?:tell me )?about faultmaven(?: itself)?" + _CLAUSE_END, re.I),
    re.compile(
        r"\b(?:what|which) (?:llm|model|provider|embedding model|vector "
        r"(?:database|db|store))s? does faultmaven (?:use|run|rely on)\b",
        re.I,
    ),
]

# Case reference patterns — phrases that anchor a question to case data,
# overriding knowledge classification even if phrasing looks knowledge-seeking.
#
# IMPORTANT: All patterns require a possessive/locational prefix (the, this,
# my, our, in, from) to avoid matching bare nouns. Without this, "What is a
# null pointer exception?" would match on "exception" and block KNOWLEDGE_QUERY.
_CASE_REFERENCE_PHRASES = [
    # "in the logs", "from my file", "the data" — require at least one prefix
    re.compile(
        r"\b(?:in |from )(?:the |this |my |our )?(?:log|logs|file|files|evidence|data|dump|trace|output)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:the |this |my |our )(?:log|logs|file|files|evidence|data|dump|trace|output)\b",
        re.I,
    ),
    # "the error", "this exception", "our incident" — require possessive prefix
    re.compile(
        r"\b(?:the|this|our|that) (?:error|errors|exception|exceptions|crash|incident|issue|alert)\b",
        re.I,
    ),
    # "we're seeing", "I'm getting" — experiential phrasing (always case-specific)
    re.compile(
        r"\b(?:we(?:'re| are)|I(?:'m| am)) (?:seeing|getting|experiencing|having)\b",
        re.I,
    ),
    # "this service", "the server", "our cluster" — require possessive prefix
    re.compile(
        r"\b(?:this|the|our|that) (?:service|server|pod|container|cluster|instance|node|system)\b",
        re.I,
    ),
    # "what happened", "what went wrong" — inherently case-referencing
    re.compile(r"\b(?:what happened|what went wrong|what caused)\b", re.I),
]


def _extract_entities(message: str) -> dict[str, list[str]]:
    """Extract technical entities from a user message."""
    entities: dict[str, list[str]] = {}

    # Timestamps
    timestamps = []
    for pattern in _TIMESTAMP_PATTERNS:
        timestamps.extend(pattern.findall(message))
    if timestamps:
        entities["timestamps"] = timestamps

    # HTTP status codes
    status_codes = _STATUS_CODE_PATTERN.findall(message)
    if status_codes:
        entities["status_codes"] = status_codes

    # Error keywords
    message_lower = message.lower()
    found_keywords = [kw for kw in _ERROR_KEYWORDS if kw in message_lower]
    if found_keywords:
        entities["error_keywords"] = found_keywords

    # Service names
    services = _SERVICE_PATTERN.findall(message)
    if services:
        entities["services"] = list(set(s.lower() for s in services))

    # IP addresses
    ips = _IP_PORT_PATTERN.findall(message)
    if ips:
        entities["ip_addresses"] = ips

    return entities


def _is_generic_request(message: str) -> bool:
    """Check if the message matches generic/triage-like phrasing."""
    return any(pattern.search(message) for pattern in _GENERIC_PHRASES)


def _has_case_reference(message: str) -> bool:
    """Check if the message references case-specific data (logs, errors, etc.)."""
    return any(pattern.search(message) for pattern in _CASE_REFERENCE_PHRASES)


# Entity types that pin a message to THIS case whatever its phrasing. Shared by
# the two "not about the case" predicates below so they cannot drift apart.
_HARD_CASE_ENTITY_TYPES = frozenset({"timestamps", "status_codes", "ip_addresses"})


def _anchored_to_case(
    message: str, entities: dict[str, list[str]], *, error_keywords_anchor: bool
) -> bool:
    """True when the message is pinned to the case regardless of phrasing.

    Hard entities (timestamps, status codes, IPs) and case-reference phrases
    always anchor. Error keywords anchor only when the caller says so:
    a knowledge question may take one as its SUBJECT ("common causes of OOM
    kills?"), but a question about the assistant never carries one — and a
    self-hosting operator reporting "faultmaven's embedding model fails with
    permission denied" is opening a case, not asking about the agent.
    """
    if any(etype in entities for etype in _HARD_CASE_ENTITY_TYPES):
        return True
    if error_keywords_anchor and "error_keywords" in entities:
        return True
    return _has_case_reference(message)


def _is_knowledge_question(message: str, entities: dict[str, list[str]]) -> bool:
    """Check if the message is a general knowledge question.

    A knowledge question has knowledge-seeking phrasing AND does NOT
    reference case data or contain hard case-specific entities (timestamps,
    status codes, IPs). Service names and error keywords are allowed because
    they can appear as the SUBJECT of a knowledge question (e.g., "What is
    Redis?", "How does connection pooling work?").

    This prevents "what happened at 14:00?" from being classified as knowledge
    while allowing "How to configure Redis sentinel?" through.
    """
    if _anchored_to_case(message, entities, error_keywords_anchor=False):
        return False
    return any(pattern.search(message) for pattern in _KNOWLEDGE_PHRASES)


def _is_agent_self_reference(message: str, entities: dict[str, list[str]]) -> bool:
    """Check if the message asks about FaultMaven itself (#1328).

    Anchored to the case (hard entity, error keyword, or case reference) means
    "about the case" however it is phrased: "what does the log say about you",
    "your stack trace shows a null pointer". Service names do NOT anchor —
    "do you use ChromaDB or Postgres for retrieval?" names two services and is
    still about the assistant.
    """
    if _anchored_to_case(message, entities, error_keywords_anchor=True):
        return False
    return any(pattern.search(message) for pattern in _AGENT_SELF_PATTERNS)


def _has_interrogative_structure(message: str) -> bool:
    """Check if the message contains a specific question structure."""
    # Question mark with content beyond just generic phrasing
    if "?" not in message:
        return False
    # Check for interrogative words followed by specific content
    interrogative = re.search(
        r"\b(?:what|why|when|where|how|which|who|did|does|is|are|was|were|has|have|can|could)\b",
        message,
        re.I,
    )
    return interrogative is not None


def classify_query(
    user_message: str, has_attachments: bool = False
) -> QueryClassification:
    """Classify a user message for processing mode routing.

    Routes to:
    - TRIAGE: generic requests, no specific inquiry, file-only uploads
    - KNOWLEDGE_QUERY: general knowledge questions not answerable from
      case evidence (e.g., "What is Opik?", "How does Redis clustering work?")
    - AGENT_META: questions about FaultMaven itself ("what model are you?",
      "how do you retrieve runbooks?") — the assistant, not the target (#1328)
    - DIRECTED_ANALYSIS: specific questions with entities, timestamps,
      error codes, or technical conditions to investigate

    Ambiguous cases default to DIRECTED_ANALYSIS since DA subsumes
    Triage via cold-start orientation (structural index is always available).

    Args:
        user_message: The user's message text
        has_attachments: Whether the message includes file attachments

    Returns:
        QueryClassification with mode, detected entities, and confidence
    """
    message = (user_message or "").strip()

    # No message + attachments → Triage (user just dropped a file)
    if not message and has_attachments:
        return QueryClassification(
            mode=ProcessingMode.TRIAGE,
            confidence=0.95,
        )

    # No message, no attachments → Triage (nothing to direct)
    if not message:
        return QueryClassification(
            mode=ProcessingMode.TRIAGE,
            confidence=0.5,
        )

    entities = _extract_entities(message)
    is_generic = _is_generic_request(message)
    has_question = _has_interrogative_structure(message)
    has_entities = bool(entities)

    # Self-reference — the user is asking about FaultMaven, not about the
    # system under investigation (#1328). Checked first: "how do you work"
    # also satisfies the knowledge phrasing below, and "what model are you"
    # satisfies none of it and would fall through to DIRECTED_ANALYSIS,
    # where forced tools + evidence grounding turn the question into a
    # request for the user's deployment manifests.
    if _is_agent_self_reference(message, entities):
        return QueryClassification(
            mode=ProcessingMode.AGENT_META,
            confidence=0.85,
        )

    # Knowledge question — general knowledge-seeking phrasing WITHOUT
    # case references or hard case-specific entities. Must be checked
    # before entity-based routing to prevent knowledge questions from
    # falling through to DIRECTED_ANALYSIS.
    if _is_knowledge_question(message, entities):
        return QueryClassification(
            mode=ProcessingMode.KNOWLEDGE_QUERY,
            confidence=0.85,
        )

    # Specific entities + question → Directed Analysis (high confidence)
    if has_entities and has_question:
        return QueryClassification(
            mode=ProcessingMode.DIRECTED_ANALYSIS,
            detected_entities=entities,
            confidence=0.9,
        )

    # Specific entities without question mark → still DA (e.g., "check the 502s at 14:00")
    if has_entities and not is_generic:
        return QueryClassification(
            mode=ProcessingMode.DIRECTED_ANALYSIS,
            detected_entities=entities,
            confidence=0.75,
        )

    # Generic phrasing without entities → Triage
    if is_generic and not has_entities:
        return QueryClassification(
            mode=ProcessingMode.TRIAGE,
            confidence=0.85,
        )

    # Question mark but no entities and not generic → could be a specific
    # question using non-technical language. Default to DA since it subsumes Triage.
    if has_question and not is_generic:
        return QueryClassification(
            mode=ProcessingMode.DIRECTED_ANALYSIS,
            confidence=0.6,
        )

    # Generic with entities → entities win, route to DA
    if is_generic and has_entities:
        return QueryClassification(
            mode=ProcessingMode.DIRECTED_ANALYSIS,
            detected_entities=entities,
            confidence=0.65,
        )

    # Ambiguous: default to DA (it subsumes Triage via cold-start orientation)
    return QueryClassification(
        mode=ProcessingMode.DIRECTED_ANALYSIS,
        detected_entities=entities,
        confidence=0.5,
    )
