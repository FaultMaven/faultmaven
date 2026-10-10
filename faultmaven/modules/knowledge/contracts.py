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
    Optional,
    Protocol,
)

if TYPE_CHECKING:
    from faultmaven.modules.knowledge.domain.models.knowledge_item import KnowledgeItem


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


# ============================================================
# DTOs for cross-module communication
# ============================================================


# Re-export enums for external use
from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    KnowledgeItemType,
    VerificationLevel,
)
from faultmaven.modules.knowledge.taxonomy import RunbookDomain, render_vocabulary

# ============================================================
# Note: Knowledge module uses infrastructure/vector/ for vector store
# The actual KnowledgeService implementation uses IVectorStore interface
# from infrastructure layer, which is correct for vertical modules.
# ============================================================


# ============================================================
# Domain Taxonomy — FaultMaven's territory
# ============================================================

#: The engineering domains FaultMaven troubleshoots, and the single definition
#: of its territory: ``taxonomy.RunbookDomain``, the runbook ``domain``
#: vocabulary.
#:
#: That vocabulary gates KB ingestion: a runbook declaring a domain outside it
#: is rejected. The prose helpers below publish it because the *agent* side needs the same answer
#: to "what is FaultMaven for?" and was improvising its own prose versions
#: instead — "engineering work" in the out-of-band classifier, "technical
#: questions" in INQUIRY triage — while the case model carried no domain at all.
#:
#: Converted: the self-knowledge profile, both aside lanes, the every-turn
#: self-reference rule, the orientation reply, and the out-of-band router — the
#: site that decides which LANE a message enters, and therefore the one whose
#: definition of scope actually binds.
#:
#: NOT converted, on purpose: INQUIRY triage still says "use kb_qa for technical
#: questions" and leaves "technical" to the model. It names this vocabulary only
#: to say what the knowledge base HOLDS, never to decide what may be searched. A
#: draft of that change did gate the search on these seven names and was
#: reverted: it is the admission gate the paragraph above forbids, it is
#: redundant with the retrieval relevance floor, which already declines to
#: answer from off-topic chunks, and it justified itself with a claim about KB
#: contents that a prompt cannot make (#943). Do not "finish the migration" by
#: reinstating it.
#:
#: Read it as a SHARED VOCABULARY, never as an admission gate. FaultMaven
#: answers questions outside these domains — the taxonomy tells the agent when
#: it is speaking outside its expertise, not when to refuse. Being lenient about
#: what gets answered while being precise about what gets investigated is the
#: point; a topic gate built on this constant would defeat it.
#:
#: Grow it in the spec and ``RunbookDomain`` together (and kb-toolkit with
#: them), never by loosening the ingestion gate.
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
_DOMAIN_GLOSSES: Dict[RunbookDomain, str] = {
    RunbookDomain.DATABASE: (
        "relational and non-relational data stores — queries, connections, "
        "replication, locking, indexes, capacity"
    ),
    RunbookDomain.NETWORKING: (
        "how traffic reaches a service — DNS, routing, load balancers, "
        "proxies, service mesh, TLS, reachability"
    ),
    RunbookDomain.COMPUTE: (
        "the machines and what runs on them — hosts, VMs and containers, the "
        "operating system and firmware beneath them (Linux, Windows, BIOS), "
        "and the schedulers that place workloads"
    ),
    RunbookDomain.APPLICATION: (
        "code and the runtimes it runs in — memory, concurrency, framework "
        "behaviour, build and deploy pipelines, infrastructure-as-code"
    ),
    RunbookDomain.SECURITY: (
        "identity, authorization, secrets and certificates — who may do what, "
        "and the credentials that prove it"
    ),
    RunbookDomain.STORAGE: (
        "persistence beneath a workload — volumes, filesystems, object "
        "stores, attachment, capacity, durability"
    ),
    RunbookDomain.MESSAGING: (
        "asynchronous transport between services — queues, topics, brokers, "
        "consumers, backlog and delivery"
    ),
}


def describe_troubleshooting_domains() -> str:
    """The territory as one short clause, where only the names are needed.

    Used where the text is read on every turn and length is a real cost, or
    where the point is what may be CLAIMED rather than how to classify.
    """
    return render_vocabulary(RunbookDomain)


def describe_troubleshooting_scope() -> str:
    """The territory with its glosses, for prompts that must MAP onto it.

    The longer form belongs wherever the agent decides whether a question is
    its kind of work. That decision is a mapping from the user's words to a
    vertical, and a list of seven bare nouns does not support it.
    """
    lines = [f"- {domain.value}: {_DOMAIN_GLOSSES[domain]}" for domain in RunbookDomain]
    return "\n".join(lines)
