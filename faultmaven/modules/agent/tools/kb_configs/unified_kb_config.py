"""
Unified Knowledge Base Configuration

KB-specific configuration for the unified KB tool that searches all scopes.
Combines the best of global (educational) and user (procedural) approaches.

Features:
- Hybrid search mode (vector + keyword with RRF merge)
- Staleness-aware metadata: surfaces last_updated and status to synthesis LLM
- Scope-aware tiebreaking: personal > team > global for close scores
"""

from datetime import datetime, timezone
from typing import Optional

from faultmaven.modules.agent.tools.kb_config import KBConfig

# Chunks older than this many days get a staleness warning in context
STALENESS_THRESHOLD_DAYS = 180


class UnifiedKBConfig(KBConfig):
    """
    Configuration for the unified Knowledge Base search.

    Searches faultmaven_kb collection with metadata-based scope filtering.
    Results may come from global, personal, or team runbooks.
    Uses hybrid search (vector + keyword) with staleness-aware synthesis.
    """

    def get_collection_name(self, scope_id: Optional[str]) -> str:
        """Single KB collection for all scopes."""
        return "faultmaven_kb"

    def format_chunk_metadata(self, metadata: dict, score: float) -> str:
        """Format with source scope, document info, and staleness warning."""
        parts = [f"Score: {score:.2f}"]

        scope = metadata.get("scope", "global")
        parts.append(f"Scope: {scope}")

        if "title" in metadata:
            parts.append(f"Title: {metadata['title']}")
        elif "document_title" in metadata:
            parts.append(f"Doc: {metadata['document_title']}")
        if "category" in metadata:
            parts.append(f"Category: {metadata['category']}")

        # Domain/service context for filtering relevance
        if "domain" in metadata:
            parts.append(f"Domain: {metadata['domain']}")
        if "service" in metadata:
            parts.append(f"Service: {metadata['service']}")

        # Staleness-aware: surface status and last_updated
        status = metadata.get("status")
        if status and status != "verified":
            parts.append(f"Status: {status}")

        last_updated = metadata.get("last_updated")
        if last_updated:
            staleness_note = self._staleness_note(last_updated)
            if staleness_note:
                parts.append(staleness_note)

        return ", ".join(parts)

    @staticmethod
    def _staleness_note(last_updated: str) -> Optional[str]:
        """Generate staleness warning if content is older than threshold."""
        try:
            # Handle both ISO 8601 and date-only formats
            if "T" in last_updated:
                updated_dt = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
            else:
                updated_dt = datetime.strptime(last_updated, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )

            age_days = (datetime.now(timezone.utc) - updated_dt).days

            if age_days > STALENESS_THRESHOLD_DAYS:
                return f"⚠ STALE ({age_days} days old)"
            elif age_days > 90:
                return f"Last updated: {age_days} days ago"
        except (ValueError, TypeError):
            pass
        return None

    def extract_source_name(self, metadata: dict) -> str:
        """Extract document title or article ID as source."""
        if "title" in metadata:
            return metadata["title"]
        if "document_title" in metadata:
            return metadata["document_title"]
        if "kb_article_id" in metadata:
            return metadata["kb_article_id"]
        return "Unknown document"

    def get_citation_format(self) -> str:
        return "document titles"

    def format_response(
        self, answer: str, sources: list, chunk_count: int, confidence: float
    ) -> str:
        """Format with unified citations."""
        response = f"{answer}\n\n"

        if sources:
            response += f"Sources: {', '.join(sources[:5])}"
            if len(sources) > 5:
                response += f" (+{len(sources) - 5} more)"

        return response

    @property
    def requires_scope_id(self) -> bool:
        """No explicit scope_id needed — filtering is automatic."""
        return False

    @property
    def search_mode(self) -> str:
        """Use hybrid search (vector + keyword with RRF merge)."""
        return "hybrid"

    @property
    def relevance_threshold(self) -> Optional[float]:
        """Refuse synthesis when no chunk clears this cosine-similarity floor.

        Derived from a measured distribution rather than assumed, because the
        assumed one was wrong twice over (#1072). The old value, 0.3, was read
        as a cosine floor but compared against ``1 - chroma_distance``, which
        on ChromaDB's default ``l2`` space is ``2*cos - 1`` — so it was really
        a cosine floor of 0.65, sitting *inside* the on-topic population. It
        refused correctly-retrieved, on-topic queries and told the model the KB
        did not cover topics it holds dedicated runbooks for.
        ``cosine_from_chroma_distance`` fixed the scale; this value re-derives
        the number on it.

        Measured against the shipped 91-runbook KB with BGE-M3, in true cosine:

        - on-topic, correct runbook at rank 1 ... 0.591 - 0.750
        - off-topic, unrelated domain .......... 0.358 - 0.413
        - off-topic, adjacent vocabulary ....... 0.477
          (ZooKeeper query -> Kafka chunks via "leader election" — the exact
          failure this guard exists for, and the tightest real constraint)

        0.5 is the lowest value that still rejects the adjacent-vocabulary case
        it must reject, and it clears the worst on-topic observation by 0.09.
        Biased toward the low end deliberately: a false refusal is a positive
        false claim about KB coverage, while a false accept only puts loosely
        related runbooks in front of a synthesizer already instructed to say
        when information is missing.

        Re-derive this — do not nudge it — if the embedding model or the corpus
        changes. A relative test (top-1 vs top-k spread) was considered and does
        not work here: off-topic spread measured *wider* than on-topic (0.061 vs
        0.023), so it inverts the signal.
        """
        return 0.5

    @property
    def empty_result_message(self) -> str:
        """The KB was searched and holds nothing matching — no cause asserted.

        Deliberately says only what is known: the search ran and matched
        nothing. It does NOT explain why (no "nothing has been uploaded",
        no "indexing may still be in progress") — this tool searches
        runbooks and documentation, and has no visibility into any
        ingestion state that would justify such a claim (#943).
        """
        return (
            "No matching runbooks or documentation were found in the "
            "knowledge base for this query. The knowledge base was searched "
            "successfully; it simply contains nothing relevant. Answer from "
            "other available context instead."
        )

    @property
    def cache_ttl(self) -> int:
        """24-hour cache (KB content is relatively stable)."""
        return 86400

    @property
    def system_prompt(self) -> str:
        return """You are retrieving from the knowledge base, which contains:
- System-wide best practices and documentation
- The user's personal runbooks and procedures
- Team-shared troubleshooting guides

Answer with clarity and precision:
- Provide step-by-step instructions when procedures are available
- Include best practices and common pitfalls
- Reference documents by title for attribution
- Use the appropriate level of detail based on the content source

IMPORTANT — Staleness awareness:
- If a chunk is marked as STALE or was last updated a long time ago, warn the user that the procedure may be outdated
- If a chunk has status "draft" or "deprecated", note this in your answer
- Prefer verified and recently-updated content over stale content when both are relevant
- If only stale content is available, still provide the answer but include a staleness caveat

Be helpful and actionable. Combine general guidance with specific procedures when both are available."""
