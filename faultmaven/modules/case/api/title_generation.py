"""Case-title generation helpers (extracted from ``case/api/routes``, fm#1707).

Constants, validators and generation helpers for the auto-titling of cases
still holding the ``Case-YYMMDD-N`` placeholder, plus the LLM/extractive title
generation pipeline behind ``POST /cases/{case_id}/title``. Route handlers
stay in ``case.api.routes``; this module holds only the top-level helpers a
prior decomposition wave (fm#1707) moved out of that file, byte-for-byte.
"""

import asyncio
import logging
import re
from typing import List, Optional

from fastapi import HTTPException, status

from faultmaven.exceptions import ServiceException, ValidationException
from faultmaven.models.interfaces_case import ICaseService

# ``_is_default_case_title``: the alias and why it bounds auto-titling are
# documented where the route module binds it, in ``routes.py``.
from faultmaven.modules.case.domain.models.evidence import (
    is_default_case_title as _is_default_case_title,
)

logger = logging.getLogger(__name__)


# Configurable banned words list - minimal but extensible
BANNED_GENERIC_WORDS = [
    "new case",
    "untitled",
    "troubleshooting",
    "conversation",
    "discussion",
    "issue",
    "problem",
    "help",
    "assistance",
    "user query",
    "support request",
    "technical issue",
]

# =============================================================================
# Title Generation Constants
# =============================================================================

# Incomplete ending detection - words that indicate mid-sentence title cuts
# These words should NEVER be the last word in a title as they indicate truncation
INCOMPLETE_ENDINGS = {
    # Auxiliary verbs
    "have",
    "has",
    "is",
    "are",
    "was",
    "were",
    "been",
    # Modal verbs
    "will",
    "would",
    "should",
    "could",
    "can",
    "may",
    "might",
    # Articles
    "the",
    "a",
    "an",
    # Possessive adjectives
    "my",
    "our",
    "their",
    "your",
    "his",
    "her",
    "its",
    # Demonstratives
    "this",
    "that",
    "these",
    "those",
    # Personal pronouns (subject)
    "i",
    "you",
    "he",
    "she",
    "it",
    "we",
    "they",
    # Prepositions
    "with",
    "about",
    "from",
    "into",
    "to",
    "for",
    "of",
    "in",
    "on",
    "at",
    "after",
    "before",
    "during",
    "between",
    "through",
    "without",
    "within",
    "upon",
    "under",
    "over",
    "across",
    "against",
    # Conjunctions
    "and",
    "or",
    "but",
    "by",
    "so",
    "if",
    "when",
    "while",
    # Subordinating conjunctions and the remaining common prepositions. The set
    # above was already reaching for the whole closed class; these are the
    # members it had not needed until the cut started being MOVED to a boundary
    # rather than merely tested at one (#1098) — "…OOM Killed Since" and
    # "…Degraded Because" read exactly as broken as "…Has Been".
    "since",
    "until",
    "unless",
    "although",
    "though",
    "because",
    "than",
    "as",
    "whether",
    "where",
    "which",
    "who",
    "whom",
    "whose",
    "nor",
    "per",
    "via",
    "onto",
    "toward",
    "towards",
    "beyond",
    "behind",
    "below",
    "above",
    "among",
    "around",
    "along",
    "beside",
    "besides",
    "despite",
    "except",
    "inside",
    "outside",
    "near",
    "throughout",
    "versus",
}

# Words ending in "ly" that are NOT manner adverbs, so they may legitimately end
# a title ("Latency Anomaly", "Log Supply"). Everything else ending in "ly" is
# treated as an adverb modifying a verb that the cut removed — "…Has Been
# Repeatedly" (#1098). The allowlist is the safe direction to be wrong in: a
# noun wrongly classed as an adverb costs one word off the title, because the
# walk-back simply continues to the next boundary; an adverb wrongly kept
# persists a title that reads broken in the report's most prominent line.
LY_NOT_ADVERB = {
    "anomaly",
    "assembly",
    "apply",
    "ally",
    "supply",
    "reply",
    "family",
    "only",
    "early",
    "daily",
    "hourly",
    "weekly",
    "monthly",
    "quarterly",
    "yearly",
    "friendly",
    "likely",
    "costly",
    "deadly",
    "timely",
    "orderly",
    "ugly",
    "holy",
    "poly",
    "rally",
    "tally",
    "policy",
    # Proper nouns that reach incident titles as service or month names.
    "july",
    "italy",
    "fastly",
}


# Punctuation that clings to a token without being part of the word. Stripped
# from BOTH ends before judging a word, and from the RIGHT end of the word the
# title actually keeps. The two sets differ deliberately: ``is_title_valid``
# accepts ")]}" as a final character, so those are judged-through but never
# stripped off the kept word — trimming them would leave an unbalanced bracket
# ("Timeout (Staging" from "Timeout (Staging)").
_TITLE_EDGE_PUNCT = ".,!?;:\"'()[]“”‘’"
_TITLE_TRAILING_REPAIR = ".,!?;:\"'“”‘’"


def _is_manner_adverb(bare: str) -> bool:
    """Is this an "-ly" adverb rather than an "-ly" noun or adjective?

    The allowlist is matched against the FINAL hyphen-separated element, so
    derived compounds inherit their base's classification ("bi-weekly",
    "grafana-daily"). Matched EXACTLY, never by suffix: ``"totally".endswith(
    "tally")`` would turn a real adverb into an allowed ender, which is the
    direction this whole rule exists to prevent.
    """
    if not bare.endswith("ly"):
        return False
    return bare.rsplit("-", 1)[-1] not in LY_NOT_ADVERB


def _word_can_end_title(word: str, *, was_cut: bool) -> bool:
    """Can this word be the LAST word of a title without reading mid-phrase?

    Three classes cannot: tokens carrying no letter or digit (a dash or an
    arrow is punctuation, not a word, and stopping the walk-back on one leaves
    a candidate ``is_title_valid`` then rejects); the closed-class connectives
    enumerated in ``INCOMPLETE_ENDINGS`` (articles, prepositions, conjunctions,
    auxiliaries, modals, determiners, pronouns); and — only when the title was
    actually CUT — manner adverbs. Adverbs are detected morphologically rather
    than listed, so one the list never anticipated is still caught (#1098).

    ``was_cut`` is not a refinement, it is the adverb rule's precondition. The
    rule exists because the adverb modifies a verb the cut removed; on input
    that fit under the cap the verb is still there, and "Payments Api
    Restarting Repeatedly" is an idiomatic incident title being shortened for a
    reason that does not apply to it. The connective rule needs no such gate: a
    title ending "…during the" reads broken however it got there, and the code
    this replaced rejected such candidates outright, so trimming is strictly
    better.

    The vocabulary is ENGLISH-ONLY. A German or French problem statement cut
    mid-phrase gets none of this protection — pre-existing scope rather than a
    regression (nothing here ever handled it), and worth knowing before someone
    reads the guard as language-neutral.
    """
    bare = word.lower().strip(_TITLE_EDGE_PUNCT)
    if not bare:
        return False
    if not any(c.isalnum() for c in bare):
        return False
    if bare in INCOMPLETE_ENDINGS:
        return False
    if was_cut and _is_manner_adverb(bare):
        return False
    return True


def truncate_title_at_phrase_boundary(
    words: List[str], max_words: int, min_words: int
) -> Optional[List[str]]:
    """Cut a title to ``max_words``, then back up to a phrase boundary (#1098).

    A fixed word cap lands wherever the count runs out, which is routinely
    mid-phrase — the reported titles ended "…Has Been Repeatedly" and
    "…Degraded After Release", cut at exactly eight words. The cap stays (a
    title is meant to be short); what changes is that the cut is moved back to
    the last word that can actually END a phrase, so the result reads as a
    title rather than as a sentence someone stopped typing.

    Returns ``None`` when backing up leaves fewer than ``min_words`` — the
    caller then falls through to its next title source, which is the behavior
    the single-word ``INCOMPLETE_ENDINGS`` check already had; this generalizes
    it from "reject the whole candidate" to "use the good prefix of it".

    No ellipsis is appended: after the walk-back the title reads as a complete
    phrase, and marking it as truncated would advertise a cut that is no longer
    visible. Titles are understood to be short — the summary's own body carries
    the full problem statement.
    """
    was_cut = len(words) > max_words
    kept = list(words[:max_words])
    while kept and not _word_can_end_title(kept[-1], was_cut=was_cut):
        kept.pop()
    if len(kept) < min_words:
        return None
    # Repair the boundary word: ``_word_can_end_title`` judges it with clinging
    # punctuation stripped, but the caller keeps it verbatim — so a kept
    # "Cluster," passed the boundary check and was then rejected by
    # ``is_title_valid``, which requires an alphanumeric final character. Doing
    # it here fixes all three call sites at once (only the two extractive ones
    # stripped the joined string afterwards; the LLM path never re-stripped
    # after its clip). The alphanumeric guard above means this cannot empty the
    # word.
    kept[-1] = kept[-1].rstrip(_TITLE_TRAILING_REPAIR)
    return kept


# Conversational filler patterns (ordered longest-first for greedy matching)
# Only strip COMPLETE conversational phrases, not single words that might be part of content
CONVERSATIONAL_FILLER = [
    "i was wondering if you could help me with",
    "could you assist me with",
    "can you help me with",
    "i need help with",
    "i have a question about",
    "could you assist with",
    "i'm having trouble with",
    "i'm experiencing",
    "i am experiencing",
    "i am having",
    "i noticed",  # "I noticed our API..."
    "by the way,",  # "By the way, can..."
    "hello,",  # Only strip if followed by comma
    "hi,",  # Only strip if followed by comma
    "hey,",  # Only strip if followed by comma
]

# Title casing exceptions - keep these words lowercase in the middle of titles
TITLE_CASE_LOWERCASE_WORDS = {
    "a",
    "an",
    "the",
    "in",
    "on",
    "at",
    "to",
    "for",
    "of",
    "with",
}

# =============================================================================
# Title Generation Thresholds and Settings
# =============================================================================

# Titleability is decided by ONE gate — substance (see _titleable_substance and
# the content gate in generate_case_title). A second, turn-count gate used to sit
# in front of it and is deliberately gone. The history: the original gate was this
# 200-char content check; fa04f440 *replaced* it with a 5-turn threshold; #477 then
# re-introduced a (richer) substance gate to unblock upload-driven cases without
# removing the threshold that had replaced its ancestor. The two ANDed gates were
# residue of that incomplete replacement, not a policy — and the AND made #477
# unreachable for precisely the cases it was written for: an investigation driven
# by a log dump or a page capture carries kilobytes of evidence and a confirmed
# problem statement after ONE user turn. Turn count is not a proxy for substance;
# the substance measure rejects everything the turn count rejected (a 0-turn case
# contributes no problem statement, no evidence, no files and no chat, so it lands
# at 0 chars) without rejecting the content-rich ones.

# Content length thresholds
MIN_CONTENT_LENGTH_FOR_TITLE = 200  # Minimum chars of user content after extraction
EXTRACTIVE_MAX_CONTENT_LENGTH = (
    300  # Use fast extractive for simple, short conversations
)

# Title validation constraints
MIN_TITLE_WORDS = 2  # Minimum words in valid title ("API Error" is valid)
MIN_TITLE_LENGTH = 5  # Minimum characters in valid title
MAX_TITLE_WORDS_DEFAULT = 8  # Default maximum words in generated title
MIN_EXTRACTIVE_WORDS = (
    3  # Extractive path requires more words than validation (more conservative)
)

# LLM generation settings (optimized for title quality)
LLM_TITLE_MAX_TOKENS = (
    128  # Prevent truncation (Gemini may emit reasoning tokens before the title)
)
LLM_TITLE_TEMPERATURE = 0.2  # More deterministic generation
LLM_TITLE_TOP_P = 0.9  # Focused sampling

# Context extraction settings
MAX_USER_MESSAGES_FOR_CONTEXT = 12  # Cap message count to reduce noise
MIN_MESSAGE_WORD_COUNT = 3  # Filter out very short messages like "ok", "thanks"
CONTEXT_MESSAGE_LIMIT = 10  # Number of recent messages to fetch for context

# =============================================================================
# Helper Functions for Title Generation
# =============================================================================


def is_title_valid(title: str, check_banned_words: bool = True) -> bool:
    """Validate generated title meets quality standards.

    Args:
        title: Generated title string
        check_banned_words: Whether to check against banned generic words

    Returns:
        True if title passes all validation gates
    """
    if not title:
        return False

    words = title.split()
    # Length/word-count guards (language-agnostic)
    # Reduced from 3 to 2 words - many valid titles are 2 words:
    # "Database Timeout", "API Slowness", "Memory Leak", "Redis Error"
    if len(words) < MIN_TITLE_WORDS or len(title.strip()) < MIN_TITLE_LENGTH:
        return False

    # Check for incomplete endings (titles ending mid-sentence)
    # These indicate truncated or low-quality titles
    last_word = words[-1].lower().strip(".,!?;:")
    if last_word in INCOMPLETE_ENDINGS:
        return False

    # Catch titles truncated mid-token (e.g., "Database I/" from "I/O")
    # A valid title should end with an alphanumeric character
    last_char = title.rstrip()[-1]
    if not last_char.isalnum() and last_char not in ")]}":
        return False

    # Optional banned words check (English-centric, configurable)
    if check_banned_words:
        title_lower = title.lower().strip()
        return not (
            title_lower in BANNED_GENERIC_WORDS
            or any(generic in title_lower for generic in BANNED_GENERIC_WORDS)
        )

    return True


def apply_title_case(title: str) -> str:
    """Apply title case formatting to generated title.

    Capitalizes first letter of each word except common articles/prepositions
    in the middle of the title.

    Args:
        title: Raw title string

    Returns:
        Title-cased string (e.g., "Database Connection Timeout")
    """
    words = title.split()
    title_cased = []
    for i, word in enumerate(words):
        # Always capitalize first word, otherwise check exceptions
        if i == 0 or word.lower() not in TITLE_CASE_LOWERCASE_WORDS:
            title_cased.append(word.capitalize())
        else:
            title_cased.append(word.lower())
    return " ".join(title_cased)


def get_extractive_fallback_title(
    user_signals: Optional[str],
    context_text: str,
    case,
    max_words: int = MAX_TITLE_WORDS_DEFAULT,
) -> Optional[str]:
    """Generate fallback title using extractive logic.

    Tries multiple sources in order of reliability:
    1. Pre-extracted user signals
    2. Re-extract from context
    3. Case description

    Args:
        user_signals: Pre-extracted user content
        context_text: Full conversation context
        case: Case object
        max_words: Maximum words in title

    Returns:
        Extracted title or None if insufficient content
    """

    def _clean_fallback_candidate(text: str) -> Optional[str]:
        """Clean and validate a fallback title candidate."""
        # Cut to the cap, then back up to a phrase boundary (#1098) — the
        # shared rule, so this path cannot drift from the smart-extractive one
        # beside it or from the LLM over-length clip.
        words = truncate_title_at_phrase_boundary(
            text.strip().split(), max_words, MIN_TITLE_WORDS
        )
        if not words:
            return None
        candidate = " ".join(words)
        # Strip trailing punctuation (consistent with smart extractive path)
        candidate = candidate.strip(".,!?;:")
        if is_title_valid(candidate, check_banned_words=False):
            return apply_title_case(candidate)
        return None

    # First try the pre-extracted user signals (most reliable)
    if user_signals and user_signals.strip():
        result = _clean_fallback_candidate(user_signals)
        if result:
            return result

    # Fallback to re-extracting from context if user_signals not provided
    extracted_signals = _extract_user_signals_from_context(context_text)
    if extracted_signals:
        result = _clean_fallback_candidate(extracted_signals)
        if result:
            return result

    # Final fallback: try case description if available and meaningful
    if (
        hasattr(case, "description")
        and case.description
        and case.description.strip()
        and case.description != "No description"
    ):
        result = _clean_fallback_candidate(case.description)
        if result:
            return result

    # Skip case title fallback entirely - it's likely to be generic
    # If no meaningful content found, this should trigger 422 instead
    return None


def _sanitize_title_content(content: str) -> str:
    """Sanitize content for title generation - remove PII, profanity, etc."""
    if not content:
        return ""

    # Basic content hygiene - remove common PII patterns
    # Remove email addresses
    content = re.sub(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", "[email]", content
    )

    # Remove phone numbers (basic patterns)
    content = re.sub(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b", "[phone]", content)
    content = re.sub(r"\b\(\d{3}\)\s*\d{3}[-.]?\d{4}\b", "[phone]", content)

    # Remove IP addresses
    content = re.sub(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "[ip]", content)

    # Remove URLs
    content = re.sub(r"https?://[^\s]+", "[url]", content)

    # Remove file paths (basic patterns)
    content = re.sub(r"[A-Za-z]:\\[^\s]+", "[path]", content)
    content = re.sub(r"/[^\s]+/", "[path]", content)

    return content.strip()


def _extract_user_signals_from_context(context_text: str) -> str:
    """Extract meaningful user content from conversation context for title generation.

    Focuses only on user messages, filtering out system/agent responses.
    Dedupes near-identical lines and caps to last 8-12 meaningful user messages.
    Returns the most relevant user content for title generation.
    """
    if not context_text or not context_text.strip():
        return ""

    lines = context_text.strip().split("\n")
    user_messages = []
    seen_messages = set()  # For deduplication

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Skip system headers and metadata
        skip_patterns = [
            "Previous conversation",
            "Case status:",
            "Created:",
            "Last updated:",
            "Message count:",
            "Current query:",
            "Description: No description",
            "Case: New Case",
            "Case: Untitled",
            "] Assistant:",  # Skip assistant responses
            "] System:",  # Skip system messages
        ]

        if any(pattern in line for pattern in skip_patterns):
            continue

        # Extract user messages specifically (only user lines)
        user_content = None
        if "] User:" in line:
            # Extract content after "User:"
            user_content = line.split("] User:", 1)[-1].strip()
        elif "User:" in line and not line.startswith("["):
            # Handle simpler "User:" format
            user_content = line.split("User:", 1)[-1].strip()
        elif line.startswith("Description:") and "No description" not in line:
            # Extract meaningful description as user content
            user_content = line.split("Description:", 1)[-1].strip()

        # Validate and dedupe user content
        if (
            user_content
            and len(user_content.split())
            >= MIN_MESSAGE_WORD_COUNT  # Filter short messages
            and user_content.lower() not in seen_messages
        ):  # Dedupe

            seen_messages.add(user_content.lower())
            user_messages.append(user_content)

            # Cap to last N meaningful user messages to reduce noise
            if len(user_messages) > MAX_USER_MESSAGES_FOR_CONTEXT:
                user_messages = user_messages[-MAX_USER_MESSAGES_FOR_CONTEXT:]

    # Return ALL user messages concatenated for accurate length measurement
    # This ensures the threshold check considers total conversation depth,
    # not just the last message (which could be short like "thanks")
    if user_messages:
        # Join all user messages with space separator
        all_content = " ".join(user_messages)
        return _sanitize_title_content(all_content)

    return ""


def _case_problem_statement(case) -> str:
    """Confirmed (problem_verification) or proposed (inquiry) problem statement."""
    pv = getattr(case, "problem_verification", None)
    statement = getattr(pv, "symptom_statement", None) if pv else None
    if not statement:
        inquiry = getattr(case, "inquiry", None)
        statement = (
            getattr(inquiry, "proposed_problem_statement", None) if inquiry else None
        )
    return (statement or "").strip()


# A statement shorter than this is too thin to be a meaningful title on its own.
_MIN_PROBLEM_STATEMENT_LEN_FOR_TITLE = 20


def _has_problem_statement(case) -> bool:
    """True when the case carries a non-trivial problem statement (title-grade)."""
    return len(_case_problem_statement(case)) >= _MIN_PROBLEM_STATEMENT_LEN_FOR_TITLE


def _titleable_substance(case, user_signals: str) -> str:
    """Richest titleable content for a case, for the title gate + prompt.

    The previous gate measured only sanitized user chat from the last few
    messages, so upload/capture-driven investigations — where the human types
    little but the case carries a confirmed problem statement, evidence, and
    file summaries — were permanently blocked from titling. This measures the
    real substance in priority order:

    1. Confirmed/proposed problem statement (the ideal title source).
    2. Evidence summaries (what the investigation established).
    3. Uploaded-file summaries (what data the user provided).
    4. User chat (their own framing) — always appended as fallback.

    The guard still fires on a genuinely empty case (no problem statement, no
    evidence, no files, a one-line "hi") because none of 1–3 contribute and the
    chat is below threshold — preserving the gate's original intent.
    """
    parts: list[str] = []

    # 1. Problem statement — confirmed (problem_verification) or proposed (inquiry).
    statement = _case_problem_statement(case)
    if statement:
        parts.append(statement)

    # 2. Evidence summaries (cap to a few — enough to establish substance).
    for ev in (getattr(case, "evidence", None) or [])[:5]:
        summary = getattr(ev, "summary", None)
        if summary and summary.strip():
            parts.append(summary.strip())

    # 3. Uploaded-file summaries.
    for uf in (getattr(case, "uploaded_files", None) or [])[:5]:
        summary = getattr(uf, "summary", None)
        if summary and summary.strip():
            parts.append(summary.strip())

    # 4. User chat framing (fallback / always included).
    if user_signals and user_signals.strip():
        parts.append(user_signals.strip())

    # Dedupe case-insensitively, preserve order.
    seen: set[str] = set()
    unique: list[str] = []
    for part in parts:
        key = part.lower()
        if key not in seen:
            seen.add(key)
            unique.append(part)

    return _sanitize_title_content(" ".join(unique))


# The placeholder every case is born with when the caller supplies no title:
# ``Case-{YYMMDD}-{sequence}`` (see CaseService.create_case). Anchored on both
# ends so a user-chosen title that merely *starts* "Case-..." is never mistaken
# for a placeholder and silently overwritten.
#
class _TitleSubstanceTooThin(ValidationException):
    """The case does not carry enough substance to name yet.

    A distinct type, not a distinct message. Both this and "the LLM and its
    fallback both failed" are refusals to produce a title, and to a client both
    are the same 422 — but to an operator they are opposite conditions: the first
    is the gate working, the second is the titler broken. Auto-titling runs
    unattended on every turn, so telling them apart by exception type is what
    keeps a systematically failing titler from being indistinguishable from a
    stream of ordinary thin cases. Matching on the message text would have tied
    that distinction to user-facing copy.
    """


async def _generate_and_persist_title(
    *,
    case,
    case_id: str,
    user_id: str,
    case_service: ICaseService,
    llm_provider,
    max_words: int,
    hint: Optional[str],
    correlation_id: str,
) -> tuple[str, str, int]:
    """Gate on substance, generate a title, persist it, and verify the write.

    Shared by the ``POST /cases/{case_id}/title`` endpoint and the post-turn
    background auto-titling task, so both apply the *same* policy. Splitting the
    policy across a route and a task is how the two drift; there is one copy.

    Returns:
        ``(title, source, substance_length)`` — ``source`` is ``llm`` /
        ``extractive`` / ``fallback`` (telemetry), ``substance_length`` the number
        of characters the gate measured.

    Raises:
        ValidationException: the case carries too little substance to name, or
            neither the LLM nor the extractive fallback produced a title.
        HTTPException 500: the title was generated but could not be persisted, or
            did not read back as written.
    """
    # Get conversation context for LLM prompt
    context_text = ""
    try:
        context_text = await case_service.get_case_conversation_context(
            case_id, limit=CONTEXT_MESSAGE_LIMIT
        )
    except Exception as e:
        logger.warning(
            f"Failed to get conversation context for case {case_id}, using fallback: {str(e)}",
            extra={
                "case_id": case_id,
                "error": str(e),
                "error_type": type(e).__name__,
                "correlation_id": correlation_id,
            },
        )
        context_text = (
            f"Case: {case.title}\nDescription: {case.description or 'No description'}"
        )

    # Extract meaningful content for the title prompt + gate. The gate's
    # purpose is a quality guard — don't ask the LLM to title a case with
    # too little substance. Substance is NOT just user chat: an upload- or
    # capture-driven investigation can have a confirmed problem statement,
    # evidence, and file summaries while the user typed almost nothing. We
    # measure the richest available signal (problem statement → evidence/
    # file summaries → user chat) so the guard fires on genuinely empty
    # cases without blocking content-rich ones. See _titleable_substance.
    user_signals = _extract_user_signals_from_context(context_text)
    user_message_content = _titleable_substance(case, user_signals)

    # Extraction diagnostics. Lengths only, at DEBUG. This used to log 300 chars
    # of raw conversation and 200 of extracted substance at INFO — verbatim user
    # content, pre-redaction, straight into the log pipeline. That was already the
    # wrong level for it, and auto-titling turned it from "whenever a client asks"
    # into "every turn of every case that is not yet named". The lengths are what
    # actually diagnose the failure this logging was added for (an empty
    # extraction); the text was never needed to tell 0 from 200.
    logger.debug(
        "Title generation: extracted user signals",
        extra={
            "case_id": case_id,
            "context_length": len(context_text) if context_text else 0,
            "user_signals_length": len(user_signals or ""),
            "substance_length": len(user_message_content or ""),
        },
    )

    # The gate. A confirmed/proposed problem statement is, by itself, a
    # title-grade summary of the case — when one exists the case is titleable
    # regardless of how little the user typed. Otherwise require
    # MIN_CONTENT_LENGTH_FOR_TITLE chars of substance (evidence/file summaries +
    # chat) so we don't ask the LLM to title an empty case. This is the *only*
    # gate; see the constants block for why turn count is no longer one.
    has_problem_statement = _has_problem_statement(case)
    if (
        not has_problem_statement
        and len(user_message_content) < MIN_CONTENT_LENGTH_FOR_TITLE
    ):
        logger.info(
            f"Skipping title generation: insufficient content (case_id={case_id}, length={len(user_message_content)})",
            extra={
                "case_id": case_id,
                "content_length": len(user_message_content),
                "threshold": MIN_CONTENT_LENGTH_FOR_TITLE,
                "has_problem_statement": has_problem_statement,
            },
        )
        raise _TitleSubstanceTooThin(
            f"Need at least {MIN_CONTENT_LENGTH_FOR_TITLE} characters of "
            f"conversation content to generate a meaningful title "
            f"(currently {len(user_message_content)} characters). "
            f"Continue discussing your issue, then try again."
        )

    # Generate title using LLM with fallback logic
    try:
        generated_title, title_source = await _generate_title_with_llm(
            context_text,
            case,
            max_words,
            hint,
            user_message_content,
            llm_provider,
            # Cost routing keys off the user's CHAT length (the original
            # "simple single-issue conversation" signal), not the larger
            # substance blob — otherwise every substance-rich case would
            # always take the LLM path. Content/extractive still use the
            # richer substance (user_message_content).
            routing_signal=user_signals,
        )
    except ValueError:
        raise ValidationException(
            "Cannot generate meaningful title from available context"
        )

    # Persist the generated title to database (Approach 1: Generate AND persist)
    try:
        success = await case_service.update_case(
            case_id, {"title": generated_title}, user_id
        )
        if not success:
            logger.error(
                f"Failed to persist generated title for case {case_id}",
                extra={"case_id": case_id, "generated_title": generated_title},
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to persist generated title",
                headers={"x-correlation-id": correlation_id},
            )

        # Verify persistence by re-fetching the case from database
        verification_case = await case_service.get_case(case_id, user_id)
        if verification_case and verification_case.title != generated_title:
            logger.error(
                f"Title persistence verification failed for case {case_id}: expected '{generated_title}', got '{verification_case.title}'",
                extra={
                    "case_id": case_id,
                    "expected_title": generated_title,
                    "actual_title": verification_case.title,
                },
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Title saved but verification failed - possible database issue",
                headers={"x-correlation-id": correlation_id},
            )

        logger.info(
            f"Title persistence verified for case {case_id}",
            extra={"case_id": case_id, "title": generated_title},
        )
    except HTTPException:
        # Re-raise HTTPException without modification to preserve original error
        raise
    except ServiceException as e:
        # Handle service-level exceptions with proper error detail
        logger.error(
            f"Service error persisting generated title: {e}",
            extra={"case_id": case_id, "correlation_id": correlation_id},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to persist generated title",
            headers={"x-correlation-id": correlation_id},
        )
    except Exception as e:
        # Handle unexpected exceptions
        logger.error(
            f"Unexpected error persisting generated title: {e}",
            extra={"case_id": case_id, "correlation_id": correlation_id},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to persist generated title",
            headers={"x-correlation-id": correlation_id},
        )

    return generated_title, title_source, len(user_message_content or "")


# Ceiling on the auto-titling attempt. It sits on the turn's critical path (see
# _auto_title_case_if_default for why), so it must never be able to hold a turn's
# answer open: the extractive path is ~1ms and the LLM path ~0.5-1.2s, and a
# titler that has stopped answering has to lose rather than delay the reply.
AUTO_TITLE_TIMEOUT_SECONDS = 15.0


async def _auto_title_case_if_default(
    case_id: str,
    user_id: str,
    case_service: ICaseService,
    llm_provider,
) -> None:
    """Name a case that is still carrying its ``Case-YYMMDD-N`` placeholder.

    Titling policy lives on the server but the *trigger* used to live in each
    client: the Slack agent and the Dashboard never called
    ``POST /cases/{case_id}/title`` at all, so their cases kept the placeholder
    forever, and the Copilot re-implemented the gate in TypeScript. One
    server-side trigger is what makes the policy apply everywhere (fm#1069).

    **This runs inline, before ``POST /cases/{case_id}/turns`` answers, and that
    placement is load-bearing — do not move it to a BackgroundTasks task.** Two
    separate hazards close because of it:

    * **The next turn cannot clobber this write.** A title-only update takes
      ``CaseService.update_case``'s metadata channel — a scoped UPDATE with no
      version bump — deliberately, so it cannot stale-conflict with an in-flight
      turn save. But that cuts both ways: the versioned full-row ``save`` the
      engine performs writes ``title`` from its own in-memory snapshot, and OCC
      cannot see a write that never bumped the version. A turn that had loaded the
      case *before* titling landed would therefore save the placeholder straight
      back over the generated title. Finishing before the response is what orders
      the two: the client cannot submit turn N+1 until turn N has answered, so
      every subsequent load already carries the new title. (A concurrent writer on
      the *same* case from a different client can still race it — that is the
      pre-existing property of the metadata channel, which user renames share.)
    * **Two attempts cannot overlap.** For one sequential client there is never a
      second in-flight attempt to duplicate the LLM call or lose a verification
      read against.

    The tenant needs no explicit re-binding here *because* of that placement: the
    global ``bind_request_enterprise_context`` dependency has already bound this
    request's enterprise in this task. Moving this off the request would silently
    break that — ``get_current_enterprise_id`` is total, answering the Standalone
    enterprise for an unbound context rather than failing, so a detached task
    would not raise, it would quietly address the wrong tenant.

    Cost is bounded by construction: it returns immediately unless the title is
    still the placeholder, so a case is named at most once, and a case too thin to
    name is refused by the substance gate *before* any LLM call.

    Every failure is swallowed: this is best-effort naming, and nothing about a
    turn should be reported as failed because its title could not be made.
    """
    try:
        case = await case_service.get_case(case_id, user_id)
        if not case or not _is_default_case_title(case.title):
            return

        title, source, _ = await asyncio.wait_for(
            _generate_and_persist_title(
                case=case,
                case_id=case_id,
                user_id=user_id,
                case_service=case_service,
                llm_provider=llm_provider,
                max_words=MAX_TITLE_WORDS_DEFAULT,
                hint=None,
                correlation_id=f"auto-title:{case_id}",
            ),
            timeout=AUTO_TITLE_TIMEOUT_SECONDS,
        )
        logger.info(
            "Auto-titled case on turn completion",
            extra={"case_id": case_id, "title": title, "title_source": source},
        )
    except _TitleSubstanceTooThin as e:
        # The gate working, not a fault: the case is genuinely too thin to name
        # yet. Expected on early turns; the next turn tries again.
        logger.debug(
            f"Auto-titling skipped for case {case_id}: {e}",
            extra={"case_id": case_id},
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"Auto-titling timed out for case {case_id} after "
            f"{AUTO_TITLE_TIMEOUT_SECONDS}s",
            extra={"case_id": case_id},
        )
    except Exception as e:
        # Everything else is a fault worth seeing at default level — including a
        # plain ValidationException, which at this point means the LLM *and* the
        # extractive fallback both failed to produce a title. Logging that at
        # DEBUG alongside the gate would make a systematically broken titler
        # indistinguishable from a stream of ordinary thin cases.
        logger.warning(
            f"Auto-titling failed for case {case_id}: {e}",
            extra={"case_id": case_id, "error_type": type(e).__name__},
        )


def _generate_smart_extractive_title(
    user_signals: str, max_words: int = MAX_TITLE_WORDS_DEFAULT
) -> Optional[str]:
    """Generate title using smart extractive logic (no LLM).

    Strips conversational filler and extracts meaningful technical content.

    Args:
        user_signals: Pre-extracted user content from conversation
        max_words: Maximum words in generated title

    Returns:
        Extracted title or None if insufficient content
    """
    if not user_signals or not user_signals.strip():
        return None

    # Clean and tokenize
    content = user_signals.strip()
    content_lower = content.lower()

    # Strip filler from beginning (try longest patterns first, repeat until no match)
    stripped_any = True
    while stripped_any:
        stripped_any = False
        for filler in CONVERSATIONAL_FILLER:
            if content_lower.startswith(filler):
                # Remove the filler and any trailing whitespace/punctuation
                content = content[len(filler) :].strip().strip(",").strip()
                content_lower = content.lower()
                stripped_any = True
                break  # Start over with longest patterns first

    # Tokenize the cleaned content
    words = content.split()

    # Cut to the cap, then back up to a phrase boundary (#1098). The extractive
    # path requires more words (3) than general validation (2) — intentional:
    # extractive titles from longer content are more reliable. Falling under
    # that bar returns None and falls through to the LLM, which is what the
    # narrower single-word check did too; the difference is that a good prefix
    # is now kept instead of the whole candidate being thrown away.
    meaningful_words = truncate_title_at_phrase_boundary(
        words, max_words, MIN_EXTRACTIVE_WORDS
    )
    if not meaningful_words:
        return None

    # Join and clean up
    title = " ".join(meaningful_words)
    title = title.strip(".,!?;:")

    # Apply title casing and return
    return apply_title_case(title)


async def _generate_title_with_llm(
    context_text: str,
    case,
    max_words: int = 8,
    hint: Optional[str] = None,
    user_signals: Optional[str] = None,
    llm_provider=None,
    routing_signal: Optional[str] = None,
) -> tuple[str, str]:
    """Generate title using hybrid approach: smart extractive for simple cases, LLM for complex.

    Strategy:
    - Simple cases (< 300 chars): Fast smart extractive (1ms, $0, no API)
    - Complex cases (>= 300 chars): LLM synthesis (500-1200ms, ~$0.0002, API call)

    Smart extractive strips conversational filler and extracts meaningful terms.
    LLM path is used for multi-topic or long conversations requiring synthesis.

    Args:
        context_text: Conversation context text
        case: Case object
        max_words: Maximum words in generated title
        hint: Optional hint to guide title generation
        user_signals: Pre-extracted user signals from conversation
        llm_provider: LLM provider from app.state (Composition Root)

    Returns:
        Tuple of (title, source) where source is "extractive", "llm", or "fallback"
    """
    try:
        # LLM provider passed from app.state (Composition Root)
        if not llm_provider:
            fallback = get_extractive_fallback_title(
                user_signals, context_text, case, max_words
            )
            if not fallback:
                raise ValueError("Insufficient context for title generation")
            return fallback, "extractive"

        # HYBRID APPROACH: Use smart extractive for simple cases, LLM for complex ones
        # Complexity heuristics:
        # 1. Content length: < EXTRACTIVE_MAX_CONTENT_LENGTH chars = simple, single-issue conversation
        # 2. No user_signals means insufficient extraction (rare edge case)

        # Route on the user's chat length (routing_signal) when provided, so a
        # large synthesized substance blob doesn't force every case onto the LLM
        # path; fall back to user_signals when no separate routing signal given.
        routing_text = routing_signal if routing_signal is not None else user_signals
        use_smart_extractive = False
        if user_signals and user_signals.strip():
            content_length = len(routing_text or "")
            # Simple conversation: short content that likely describes a single issue
            if content_length < EXTRACTIVE_MAX_CONTENT_LENGTH:
                use_smart_extractive = True
                logger.info(
                    f"Title generation: Using smart extractive (content_length={content_length})",
                    extra={"content_length": content_length, "decision": "extractive"},
                )

        if use_smart_extractive:
            # Fast path: Smart extractive title generation (1ms, $0, no API call)
            extractive_title = _generate_smart_extractive_title(user_signals, max_words)
            if extractive_title and is_title_valid(extractive_title):
                logger.info(
                    "Title generation: Smart extractive success",
                    extra={"extractive_title": extractive_title},
                )
                return extractive_title, "extractive"
            else:
                # Extractive failed (rare), fall through to LLM
                logger.info(
                    "Title generation: Smart extractive insufficient, using LLM",
                    extra={"extractive_attempt": extractive_title},
                )

        # Slow path: LLM-based title generation (500-1200ms, $0.0001-0.0003, API call)
        # Used for: complex conversations, long content, multi-topic discussions
        logger.info(
            f"Title generation: Using LLM (content_length={len(user_signals) if user_signals else 0})",
            extra={
                "content_length": len(user_signals) if user_signals else 0,
                "decision": "llm",
            },
        )

        # Use extracted user signals if available, otherwise fall back to full context
        # User signals are already cleaned, deduplicated, and focused on user content
        prompt_content = (
            user_signals if user_signals and user_signals.strip() else context_text
        )

        # Simple, clear prompt focused on the task
        hint_text = f" {hint}" if hint else ""
        prompt = (
            f"Generate a concise, descriptive title (maximum {max_words} words) for this technical support conversation.\n\n"
            f"User's messages:\n{prompt_content}\n\n"
            f"Requirements:\n"
            f"- Maximum {max_words} words\n"
            f"- Use specific technical terms from the conversation\n"
            f"- Title Case format (e.g., 'PostgreSQL Connection Timeout')\n"
            f"- Avoid generic words: Issue, Problem, Troubleshooting, Conversation\n"
            f"- Return ONLY the title, no quotes or explanations{hint_text}\n\n"
            f"Title:"
        )

        # Generate title using LLM with optimized settings
        response = await llm_provider.generate(
            prompt=prompt,
            max_tokens=LLM_TITLE_MAX_TOKENS,
            temperature=LLM_TITLE_TEMPERATURE,
            top_p=LLM_TITLE_TOP_P,
        )

        # A title cut at the output cap is a mid-word title, and it is
        # persisted. Fall back to the placeholder rather than retrying: the
        # budget here is a handful of tokens for a handful of words, so a cut
        # means the model ignored the length instruction, not that it needed
        # more room — a bigger cap would buy a longer wrong answer (#1094).
        if response is not None and response.is_truncated:
            logger.warning(
                "Title generation: response truncated at the output cap",
                extra={"stop_reason": response.stop_reason.value},
            )
            raise ValueError("LLM response was truncated at the output limit")

        if response and response.content and response.content.strip():
            # Strip quotes/punctuation; collapse whitespace
            generated_title = response.content.strip().strip('"').strip("'").strip()

            # The sentinel-string blacklist that used to sit here is gone.
            #
            # It existed only because a provider wrote placeholder strings
            # ("[Response truncated due to token limit]",
            # "[Content blocked by safety filters]") into `content` when it had
            # nothing real to return, and this was the far end of the system
            # string-matching them back out. Both halves are retired: the
            # provider now reports `stop_reason` and leaves content empty, the
            # cut is caught above, and a blocked response falls through the
            # empty-content guard on this very line into the placeholder title
            # (#1094).

            generated_title = re.sub(
                r"\s+", " ", generated_title
            )  # Collapse whitespace
            generated_title = generated_title.rstrip(
                ".,!?;:"
            )  # Remove trailing punctuation

            # Remove common LLM prefixes/suffixes
            prefixes_to_remove = [
                "Title:",
                "title:",
                "Here is a title:",
                "Here's a title:",
            ]
            for prefix in prefixes_to_remove:
                if generated_title.lower().startswith(prefix.lower()):
                    generated_title = generated_title[len(prefix) :].strip()

            # Check if LLM returned NONE token (deterministic escape hatch)
            if generated_title.upper() == "NONE":
                logger.info("Title generation: LLM returned NONE token")
                raise ValueError("LLM determined no compliant title possible")

            # Lightweight guards: length ≤ max_words, ≥3 words, no banned generics, basic validation
            words = generated_title.split()
            if len(words) > max_words:
                # The model ignored the length instruction. Clipping to the cap
                # silently persisted a mid-phrase title — this path had no
                # completeness check at all, unlike the two extractive ones
                # (#1098). Back up to a phrase boundary instead; if nothing
                # usable survives, treat it like the other constraint
                # violations here and fall through rather than persist a
                # broken title (the same call the truncated-at-cap branch above
                # makes, for the same reason).
                trimmed = truncate_title_at_phrase_boundary(
                    words, max_words, MIN_TITLE_WORDS
                )
                if not trimmed:
                    # Logged, not silent: without this the branch is
                    # indistinguishable from the TypeError that removing it
                    # would raise (both land in the broad except below and
                    # yield the same fallback), so the refusal has no
                    # observable of its own to pin. Same shape as the sibling
                    # guards in this block.
                    logger.info(
                        "Title generation: LLM output over the word cap with "
                        "no usable phrase boundary under it",
                        extra={"over_cap_title": generated_title},
                    )
                    raise ValueError(
                        "LLM title exceeded the word cap with no usable "
                        "phrase boundary under it"
                    )
                words = trimmed
                generated_title = " ".join(words)

            # Run lightweight validation guards
            if not is_title_valid(generated_title):
                logger.info(
                    "Title generation: LLM output failed validation guards",
                    extra={"invalid_title": generated_title},
                )

                # Minimal deterministic fallback behind flag for resiliency (optional but prudent)
                from faultmaven.config.settings import get_settings

                use_fallback = get_settings().case.title_generation_use_fallback
                if use_fallback:
                    fallback = get_extractive_fallback_title(
                        user_signals, context_text, case, max_words
                    )
                    if fallback and is_title_valid(
                        fallback, check_banned_words=False
                    ):  # Don't block non-English fallbacks
                        logger.info(
                            "Title generation: Using extractive fallback for resiliency",
                            extra={"fallback_title": fallback},
                        )
                        return fallback, "fallback"

                # If no fallback or fallback fails, return 422
                raise ValueError(
                    "Generated title failed validation guards and fallback insufficient"
                )

            logger.info(
                "Title generation: LLM success",
                extra={"generated_title": generated_title},
            )
            return generated_title, "llm"
        else:
            fallback = get_extractive_fallback_title(
                user_signals, context_text, case, max_words
            )
            if not fallback:
                raise ValueError("LLM failed and insufficient fallback context")
            logger.info(
                f"Title generation: LLM empty response, using fallback",
                extra={"fallback_title": fallback},
            )
            return fallback, "fallback"

    except Exception as e:
        logger.warning(f"LLM title generation failed, trying fallback: {e}")
        fallback = get_extractive_fallback_title(
            user_signals, context_text, case, max_words
        )
        if not fallback:
            raise ValueError("Both LLM and fallback title generation failed")
        logger.info(
            f"Title generation: LLM exception, using fallback",
            extra={"error": str(e), "fallback_title": fallback},
        )
        return fallback, "fallback"
