"""Knowledge Module Contracts

This module defines the public interfaces (contracts) for the Knowledge vertical module.
Other modules should import from here, not from infrastructure or domain directly.

Following the design in module-organization-design.md:
- Vertical modules expose contracts through contracts.py
- Domain services use these contracts for cross-module communication
"""

from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)

if TYPE_CHECKING:
    from faultmaven.modules.knowledge.domain.models.knowledge_item import KnowledgeItem
    from faultmaven.modules.knowledge.domain.models.suggestion import (
        KnowledgeSuggestion,
    )


# ============================================================
# Service Contracts
# ============================================================


class IKnowledgeService(Protocol):
    """Service interface for Knowledge business logic.

    KB retrieval during investigation is handled by the kb_qa tool
    (via the tool-augmented generation loop), not by this interface.
    The tool path provides proper scope filtering via ToolContext.
    See: modules/agent/tools/kb_qa.py, kb_tool_adapter.py.
    """

    async def delete_document(self, document_id: str) -> bool:
        """Delete a document from the knowledge base."""
        ...

    async def get_document(self, document_id: str) -> Optional[Dict[str, Any]]:
        """Get a specific document by ID — the TRUSTED, unscoped load.

        Has no actor and applies no visibility rule. It backs the write-policy
        check and internal ingestion, so it must keep reaching rows the caller
        could not list. Never answer an actor-facing read from it.
        """
        ...

    async def get_document_visible(
        self,
        document_id: str,
        user: Optional[Any] = None,
        team_ids: Optional[list] = None,
    ) -> Optional[Dict[str, Any]]:
        """Get a document by ID scoped to what the requester may see (#867).

        The ACTOR-FACING counterpart of :meth:`get_document`: global ∪ own ∪
        shared-to-my-teams, published-or-mine. Returns None both for an absent
        id and for one the requester cannot see, so the two are
        indistinguishable. Implementations that cannot evaluate the rule must
        return None (fail closed), never fall back to the unscoped load.
        """
        ...

    async def get_relevant_snippet(
        self, document_id: str, query: str, max_lines: int = 5
    ) -> Optional[Dict[str, Any]]:
        """Pick the document window that best matches a query, lexically.

        Named for what it does. It was ``get_semantic_snippet``, which no
        implementation ever was: the ranking is word overlap over the live
        document text, with no embedding and no vector search (#1288). The old
        name is what a reader — and the rate limiter's cost classification —
        took as evidence that this endpoint embeds.
        """
        ...


class IConversionService(Protocol):
    """Service interface for document-to-runbook conversion."""

    async def convert_document(
        self,
        file_path: Any,
        content_type: str,
        original_filename: str,
        scope: str,
        user_id: str,
        enterprise_id: Optional[str],
        team_id: Optional[str] = None,
    ) -> Any:
        """Convert a document to one or more runbook drafts.

        ``enterprise_id`` is **required and has no default**, deliberately. It
        may be ``None`` — that states "no explicit enterprise; stamp the one
        this request is bound to", which the implementation resolves through
        ``writable_enterprise_id`` — but a caller has to say so. A defaulted
        tenancy parameter is the #1143 trap: a call site that simply forgets it
        writes under whatever the ambient context happens to hold, which on
        SQLite is silent and under multi-tenant PostgreSQL is an opaque
        row-level-security refusal several frames later.
        """
        ...

    async def get_conversion(self, conversion_id: str, user_id: str) -> Optional[Any]:
        """Get conversion job details."""
        ...

    async def verify_draft(
        self, conversion_id: str, draft_id: str, user_id: str, username: str
    ) -> Optional[Any]:
        """Promote draft to verified status."""
        ...


class ISuggestionService(Protocol):
    """Service interface for Knowledge Suggestion management."""

    async def extract_knowledge_from_case(
        self,
        case_id: str,
        enterprise_id: str,
        extracted_by: str,
        include_messages: bool = True,
        include_evidence: bool = True,
        title_suggestion: Optional[str] = None,
    ) -> "KnowledgeSuggestion":
        """Extract knowledge from a case into a suggestion."""
        ...

    async def get_suggestion(
        self, suggestion_id: str
    ) -> Optional["KnowledgeSuggestion"]:
        """Get a suggestion by ID — UNSCOPED trusted load (no actor)."""
        ...

    async def get_suggestion_visible(
        self, suggestion_id: str, *, enterprise_id: str
    ) -> Optional["KnowledgeSuggestion"]:
        """Get a suggestion by ID, scoped to the actor's tenant (None if out of scope)."""
        ...

    async def list_suggestions(
        self,
        enterprise_id: str,
        status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """List one enterprise's suggestions (enterprise REQUIRED, fail-closed)."""
        ...

    async def approve_suggestion(
        self,
        suggestion_id: str,
        reviewed_by: str,
        review_notes: Optional[str] = None,
        *,
        enterprise_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Approve a suggestion and create a knowledge item."""
        ...

    async def reject_suggestion(
        self,
        suggestion_id: str,
        reviewed_by: str,
        rejection_reason: str,
        review_notes: Optional[str] = None,
        *,
        enterprise_id: str,
    ) -> bool:
        """Reject a suggestion."""
        ...


# ============================================================
# Repository Contracts
# ============================================================


# ``SuggestionConcurrencyError`` — raised by every implementation of the
# interface below — lives in ``modules/knowledge/exceptions``, NOT here.
#
# Measured, not preferred: ``contracts`` re-exports the domain models, so any
# module importing from it acquires a path to ``domain``. import-linter's layers
# contract follows indirect chains, so an infrastructure module importing this
# exception from here would report ``infrastructure -> contracts -> domain`` and
# need contract 4 to exempt the whole contracts hop — a wider hole than the
# single model import ``knowledge_item_repository`` already has exempted.
# ``exceptions`` imports nothing from ``domain``, so the exception reaches every
# layer with no chain at all.
from faultmaven.modules.knowledge.exceptions import (  # noqa: E402,F401
    SuggestionConcurrencyError,
)


class ISuggestionRepository(Protocol):
    """Persistence interface for knowledge suggestions (#1227).

    Declared here rather than in ``infrastructure`` for the reason
    ``ICaseRepository`` is: it is what ``SuggestionService`` depends on, and a
    domain service that imported a concrete repository would pull the whole ORM
    graph in at module-import time and pin itself to one implementation.

    **Every read returns a DETACHED COPY.** Mutating what a read handed you
    changes nothing until you ``save`` it. That is not a choice an
    implementation may make differently — it is what a sessionless,
    session-per-operation database repository does, and an implementation that
    returned its own live object would let a caller forget a ``save`` and still
    appear correct.

    **Every write is optimistically locked** on ``KnowledgeSuggestion.version``.
    Because reads are detached, two callers can hold the same row at version N;
    without the check the second write replays its stale snapshot over the
    first and silently reverts a concurrent decision.
    """

    async def save(self, suggestion: "KnowledgeSuggestion") -> "KnowledgeSuggestion":
        """Insert a new suggestion, or update an existing one in place.

        The update is conditional on ``suggestion.version`` still matching what
        is stored; on success the stored version is bumped and the returned
        copy carries the new value.

        Raises:
            SuggestionConcurrencyError: the row moved since it was loaded.
        """
        ...

    async def get(self, suggestion_id: str) -> Optional["KnowledgeSuggestion"]:
        """Load one suggestion by id — UNSCOPED (the trusted internal load)."""
        ...

    async def get_for_enterprise(
        self, suggestion_id: str, enterprise_id: str
    ) -> Optional["KnowledgeSuggestion"]:
        """Load one suggestion by id, scoped to ``enterprise_id``.

        ``None`` both for an absent id and for one owned by another tenant, so
        the two are indistinguishable to the caller.
        """
        ...

    async def list_for_enterprise(
        self,
        enterprise_id: str,
        *,
        status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> "Tuple[List[KnowledgeSuggestion], int]":
        """Return one page of an enterprise's suggestions and the total count.

        Newest first, by ``created_at``.
        """
        ...

    async def count_for_enterprise(
        self,
        enterprise_id: str,
        *,
        statuses: Optional["Sequence[SuggestionStatus]"] = None,
    ) -> int:
        """Count an enterprise's suggestions, optionally in given statuses."""
        ...

    @property
    def is_durable(self) -> bool:
        """Does this store survive a restart and share rows across processes?

        Reported on ``GET /admin/config/status``. An implementation states it
        about itself; the composition root is responsible for not building a
        durable-claiming store over an ephemeral database.
        """
        ...


# ============================================================
# DTOs for cross-module communication
# ============================================================


# Re-export enums for external use
from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    KnowledgeItemType,
    VerificationLevel,
)
from faultmaven.modules.knowledge.domain.models.suggestion import (
    KnowledgeSuggestion,
    PIIScanStatus,
    SuggestionStatus,
)

# ============================================================
# Note: Knowledge module uses infrastructure/vector/ for vector store
# The actual KnowledgeService implementation uses IVectorStore interface
# from infrastructure layer, which is correct for vertical modules.
# ============================================================


# ============================================================
# Domain Taxonomy — FaultMaven's territory
# ============================================================

#: The engineering domains FaultMaven troubleshoots, and the single definition
#: of its territory.
#:
#: This vocabulary already existed as ``runbook_validator.VALID_DOMAINS``, where
#: it gates KB ingestion: a runbook declaring a domain outside this set is
#: rejected. It is published here because the *agent* side needs the same answer
#: to "what is FaultMaven for?" and was improvising its own prose versions
#: instead — "engineering work" in the out-of-band classifier, "technical
#: questions" in INQUIRY triage — while the case model carried no domain at all.
#:
#: What is converted so far: the self-knowledge profile, both aside lanes, the
#: every-turn self-reference rule, and the orientation reply. What is NOT: the
#: two classifier prose versions named above, which decide which LANE a message
#: enters and therefore run BEFORE any of the converted sites. Until those are
#: converted the router and the answer prompts still judge scope by different
#: words — say so rather than reading this constant as proof they agree.
#:
#: Read it as a SHARED VOCABULARY, never as an admission gate. FaultMaven
#: answers questions outside these domains — the taxonomy tells the agent when
#: it is speaking outside its expertise, not when to refuse. Being lenient about
#: what gets answered while being precise about what gets investigated is the
#: point; a topic gate built on this constant would defeat it.
#:
#: Hand-maintained in lock-step with the kb-toolkit producer side; grow it here
#: and there together, never by loosening the ingestion gate.
#: Each domain with what it covers. The gloss is load-bearing, not decoration:
#: a bare noun leaves the agent to infer for itself whether a question about a
#: BIOS setting, a Windows service or a Kubernetes scheduler belongs to any of
#: these, and an agent that guesses "no" refuses work it should do — the
#: expensive direction.
#:
#: Glosses name LAYERS and RESPONSIBILITIES, never technologies, because a
#: technology is not a domain. ``kubernetes`` appears in the shipped corpus
#: under compute, security, storage AND networking, and ``aws-ec2`` under two;
#: which vertical a runbook belongs to is decided by what failed, not by what
#: it failed in. The technology is the separate ``service`` field.
_DOMAIN_GLOSSES: Dict[str, str] = {
    "database": (
        "relational and non-relational data stores — queries, connections, "
        "replication, locking, indexes, capacity"
    ),
    "networking": (
        "how traffic reaches a service — DNS, routing, load balancers, "
        "proxies, service mesh, TLS, reachability"
    ),
    "compute": (
        "the machines and what runs on them — hosts, VMs and containers, the "
        "operating system and firmware beneath them (Linux, Windows, BIOS), "
        "and the schedulers that place workloads"
    ),
    "application": (
        "code and the runtimes it runs in — memory, concurrency, framework "
        "behaviour, build and deploy pipelines, infrastructure-as-code"
    ),
    "security": (
        "identity, authorization, secrets and certificates — who may do what, "
        "and the credentials that prove it"
    ),
    "storage": (
        "persistence beneath a workload — volumes, filesystems, object "
        "stores, attachment, capacity, durability"
    ),
    "messaging": (
        "asynchronous transport between services — queues, topics, brokers, "
        "consumers, backlog and delivery"
    ),
}

#: The vocabulary itself. Derived from the glosses so a domain cannot exist
#: without one, and ordered by them — the cross-repo parity gate compares this
#: sequence element by element, so insertion order is part of the contract.
TROUBLESHOOTING_DOMAINS: Tuple[str, ...] = tuple(_DOMAIN_GLOSSES)


def describe_troubleshooting_domains() -> str:
    """The territory as one short clause, where only the names are needed.

    Used where the text is read on every turn and length is a real cost, or
    where the point is what may be CLAIMED rather than how to classify.
    """
    return ", ".join(TROUBLESHOOTING_DOMAINS)


def describe_troubleshooting_scope() -> str:
    """The territory with its glosses, for prompts that must MAP onto it.

    The longer form belongs wherever the agent decides whether a question is
    its kind of work. That decision is a mapping from the user's words to a
    vertical, and a list of seven bare nouns does not support it.
    """
    lines = [f"- {name}: {gloss}" for name, gloss in _DOMAIN_GLOSSES.items()]
    return "\n".join(lines)
