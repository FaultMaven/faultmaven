"""
Token-Aware Context Manager for LLM Conversations

This module implements intelligent context window management using token budgets
instead of fixed message counts. It combines:
1. Running summaries of older conversation turns
2. Full text of recent messages
3. Token counting to stay within LLM limits

Design principles:
- Predictable cost and latency (token budget control)
- Maximum context utilization (pack as much relevant info as possible)
- Graceful degradation (works without summarization if needed)
"""

import logging
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timezone
from faultmaven.models import parse_utc_timestamp

logger = logging.getLogger(__name__)


@dataclass
class ConversationTurn:
    """Represents a single conversation turn"""
    timestamp: datetime
    role: str  # "user" or "assistant"
    content: str
    tokens: int  # Approximate token count


@dataclass
class ContextBudget:
    """Token budget configuration for context management (optimized for efficiency)"""
    max_total_tokens: int = 1500  # Total budget (reduced from 4000 for efficiency)
    reserved_for_recent: int = 750  # Reserve for recent messages - 50%
    max_summary_tokens: int = 600  # Maximum tokens for summary - 40%
    min_recent_messages: int = 2  # Always include at least last 2 turns (reduced from 3)

    @property
    def available_for_summary(self) -> int:
        """Tokens available for summary after reserving recent message space"""
        return min(self.max_summary_tokens, self.max_total_tokens - self.reserved_for_recent)


class TokenCounter:
    """
    Approximate token counting for context management.

    Uses simple heuristic: ~4 characters per token (GPT-style).
    This is intentionally conservative to avoid exceeding limits.

    For production, can be upgraded to use tiktoken library for exact counts.
    """

    CHARS_PER_TOKEN = 4.0  # Conservative estimate

    @classmethod
    def count_tokens(cls, text: str) -> int:
        """
        Count approximate tokens in text.

        Args:
            text: Text to count tokens for

        Returns:
            Approximate token count (conservative estimate)
        """
        if not text:
            return 0

        # Simple character-based estimation
        char_count = len(text)
        token_count = int(char_count / cls.CHARS_PER_TOKEN) + 1  # +1 for rounding up

        return token_count

    @classmethod
    def estimate_tokens_batch(cls, texts: List[str]) -> List[int]:
        """
        Count tokens for multiple texts efficiently.

        Args:
            texts: List of texts to count

        Returns:
            List of token counts corresponding to input texts
        """
        return [cls.count_tokens(text) for text in texts]


class ConversationSummarizer:
    """
    Generates concise summaries of conversation history.

    This uses LLM to create running summaries that preserve:
    - Key troubleshooting findings
    - Important context and decisions
    - Current problem status
    """

    SUMMARY_PROMPT_TEMPLATE = """Summarize the following troubleshooting conversation, preserving all critical information:

- Problem description and symptoms
- Key findings and diagnostics performed
- Important decisions made
- Current investigation status
- Any open questions or pending actions

Conversation history:
{conversation_text}

Provide a concise summary (max {max_tokens} tokens) that maintains all essential troubleshooting context:"""

    def __init__(self, llm_provider=None):
        """
        Initialize summarizer.

        Args:
            llm_provider: Optional LLM provider for generating summaries.
                         If None, falls back to extractive summary.
        """
        self.llm_provider = llm_provider
        self.logger = logging.getLogger(__name__)

    async def summarize_conversation(
        self,
        turns: List[ConversationTurn],
        max_tokens: int = 1000,
        existing_summary: Optional[str] = None
    ) -> str:
        """
        Generate summary of conversation turns.

        Args:
            turns: List of conversation turns to summarize
            max_tokens: Maximum tokens for summary
            existing_summary: Optional existing summary to build upon

        Returns:
            Concise summary of the conversation
        """
        if not turns:
            return existing_summary or ""

        # If no LLM provider, use extractive summary
        if not self.llm_provider:
            return self._extractive_summary(turns, max_tokens, existing_summary)

        try:
            # Build conversation text
            conversation_lines = []
            if existing_summary:
                conversation_lines.append(f"Previous summary: {existing_summary}\n")

            for turn in turns:
                time_str = turn.timestamp.strftime("%H:%M") if turn.timestamp else ""
                conversation_lines.append(f"[{time_str}] {turn.role.title()}: {turn.content}")

            conversation_text = "\n".join(conversation_lines)

            # Generate summary using LLM
            prompt = self.SUMMARY_PROMPT_TEMPLATE.format(
                conversation_text=conversation_text,
                max_tokens=max_tokens
            )

            response = await self.llm_provider.generate(
                prompt=prompt,
                max_tokens=max_tokens,
                temperature=0.3  # Lower temperature for consistent summaries
            )

            summary = response.get("content", "").strip()

            self.logger.info(
                f"Generated conversation summary: {len(turns)} turns → "
                f"{TokenCounter.count_tokens(summary)} tokens"
            )

            return summary

        except Exception as e:
            self.logger.warning(f"LLM summarization failed, using extractive: {e}")
            return self._extractive_summary(turns, max_tokens, existing_summary)

    def _extractive_summary(
        self,
        turns: List[ConversationTurn],
        max_tokens: int,
        existing_summary: Optional[str] = None
    ) -> str:
        """
        Fallback extractive summary (no LLM required).

        Extracts key sentences and formats as bullet points.
        """
        summary_parts = []

        if existing_summary:
            summary_parts.append(existing_summary)

        # Extract first user message (problem description)
        for turn in turns:
            if turn.role == "user":
                summary_parts.append(f"• Problem: {turn.content[:200]}...")
                break

        # Add note about additional turns
        if len(turns) > 2:
            summary_parts.append(f"• [{len(turns)} conversation turns with troubleshooting diagnostics]")

        summary = "\n".join(summary_parts)

        # Truncate if needed
        if TokenCounter.count_tokens(summary) > max_tokens:
            # Simple truncation by characters
            char_limit = int(max_tokens * TokenCounter.CHARS_PER_TOKEN * 0.9)  # 90% to be safe
            summary = summary[:char_limit] + "..."

        return summary


class TokenAwareContextManager:
    """
    Manages conversation context within token budgets.

    Core algorithm:
    1. Calculate tokens for all messages
    2. Always include minimum recent messages (full text)
    3. If budget remains, include more recent messages
    4. If older messages exist, summarize them
    5. Combine: [summary] + [recent messages]
    """

    def __init__(
        self,
        budget: Optional[ContextBudget] = None,
        summarizer: Optional[ConversationSummarizer] = None
    ):
        """
        Initialize context manager.

        Args:
            budget: Token budget configuration
            summarizer: Conversation summarizer instance
        """
        self.budget = budget or ContextBudget()
        self.summarizer = summarizer or ConversationSummarizer()
        self.logger = logging.getLogger(__name__)

    async def build_context(
        self,
        conversation_history: List[Dict],
        existing_summary: Optional[str] = None,
        case_title: Optional[str] = None
    ) -> Tuple[str, Dict[str, any]]:
        """
        Build token-aware conversation context.

        Args:
            conversation_history: List of conversation turns (dicts with role, content, timestamp)
            existing_summary: Optional existing running summary
            case_title: Optional case title for additional context

        Returns:
            Tuple of (formatted_context, metadata)

        Metadata includes:
            - total_tokens: Total tokens in returned context
            - recent_message_count: Number of full messages included
            - summary_tokens: Tokens used by summary
            - truncated: Whether older messages were summarized
        """
        if not conversation_history:
            return "", {"total_tokens": 0, "recent_message_count": 0}

        # Convert to ConversationTurn objects with token counts
        turns = self._parse_conversation_history(conversation_history)

        # Separate into recent and older turns
        recent_turns, older_turns = self._split_by_budget(turns)

        # Build context components
        context_parts = []
        metadata = {
            "total_tokens": 0,
            "recent_message_count": len(recent_turns),
            "summary_tokens": 0,
            "truncated": len(older_turns) > 0
        }

        # Add case context if available
        if case_title:
            case_context = f"Troubleshooting Case: {case_title}\n"
            context_parts.append(case_context)
            metadata["total_tokens"] += TokenCounter.count_tokens(case_context)

        # Add summary of older turns if they exist
        if older_turns:
            summary = await self._build_summary(older_turns, existing_summary)
            if summary:
                summary_section = f"Previous conversation summary:\n{summary}\n"
                context_parts.append(summary_section)
                summary_tokens = TokenCounter.count_tokens(summary_section)
                metadata["summary_tokens"] = summary_tokens
                metadata["total_tokens"] += summary_tokens

        # Add recent messages (full text)
        if recent_turns:
            recent_section = self._format_recent_messages(recent_turns)
            context_parts.append(recent_section)
            metadata["total_tokens"] += sum(turn.tokens for turn in recent_turns)

        # Combine all parts
        formatted_context = "\n".join(context_parts)

        self.logger.info(
            f"Built token-aware context: {metadata['total_tokens']} tokens "
            f"({metadata['recent_message_count']} recent messages, "
            f"{'summary' if metadata['truncated'] else 'no summary'})"
        )

        return formatted_context, metadata

    def _parse_conversation_history(self, history: List[Dict]) -> List[ConversationTurn]:
        """Parse conversation history into ConversationTurn objects"""
        turns = []

        for item in history:
            content = item.get("content", "").strip()
            if not content:
                continue

            role = item.get("role", "user")
            timestamp_str = item.get("timestamp")

            # Parse timestamp (convert to timezone-naive for consistency)
            try:
                if timestamp_str:
                    dt = parse_utc_timestamp(timestamp_str)
                    timestamp = dt.replace(tzinfo=None)
                else:
                    timestamp = datetime.now(timezone.utc)
            except:
                timestamp = datetime.now(timezone.utc)

            # Count tokens
            tokens = TokenCounter.count_tokens(content)

            turns.append(ConversationTurn(
                timestamp=timestamp,
                role=role,
                content=content,
                tokens=tokens
            ))

        return turns

    def _split_by_budget(
        self,
        turns: List[ConversationTurn]
    ) -> Tuple[List[ConversationTurn], List[ConversationTurn]]:
        """
        Split turns into recent (full text) and older (to be summarized).

        Algorithm:
        1. Always include min_recent_messages
        2. Keep adding recent messages while under reserved_for_recent budget
        3. Remaining older messages go to summary
        """
        if not turns:
            return [], []

        # Always include minimum recent messages
        min_messages = min(self.budget.min_recent_messages, len(turns))
        recent_turns = list(reversed(turns[-min_messages:]))  # Most recent first
        older_turns = turns[:-min_messages] if len(turns) > min_messages else []

        # Calculate tokens for minimum recent messages
        recent_tokens = sum(turn.tokens for turn in recent_turns)

        # Try to add more recent messages while under budget
        remaining_budget = self.budget.reserved_for_recent - recent_tokens
        idx = len(turns) - min_messages - 1

        while idx >= 0 and remaining_budget > 0:
            turn = turns[idx]
            if turn.tokens <= remaining_budget:
                recent_turns.insert(0, turn)  # Add to front (chronological order)
                remaining_budget -= turn.tokens
                idx -= 1
            else:
                break

        # Recalculate older turns
        recent_count = len(recent_turns)
        older_turns = turns[:len(turns) - recent_count] if recent_count < len(turns) else []

        return recent_turns, older_turns

    async def _build_summary(
        self,
        older_turns: List[ConversationTurn],
        existing_summary: Optional[str]
    ) -> str:
        """Build summary of older conversation turns"""
        if not older_turns:
            return existing_summary or ""

        try:
            summary = await self.summarizer.summarize_conversation(
                turns=older_turns,
                max_tokens=self.budget.available_for_summary,
                existing_summary=existing_summary
            )
            return summary
        except Exception as e:
            self.logger.error(f"Failed to build summary: {e}")
            return existing_summary or ""

    def _format_recent_messages(self, recent_turns: List[ConversationTurn]) -> str:
        """Format recent messages with full text"""
        lines = ["Recent conversation:"]

        for i, turn in enumerate(recent_turns, 1):
            time_str = turn.timestamp.strftime("%H:%M")
            role_label = "User" if turn.role == "user" else "Assistant"
            lines.append(f"{i}. [{time_str}] {role_label}: {turn.content}")

        lines.append("")  # Blank line for separation
        return "\n".join(lines)
