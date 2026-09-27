"""Wire-shape builders for the tool-augmented generation loop: the schema tool definition, the assistant message that carries a tool call, and the DA system instruction."""

import inspect
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _build_schema_tool(schema_model: Any, provider: Any) -> list[dict]:
    """The structured-output tool, strict-enforced where that is available.

    Returns the plain (unenforced) tool when the provider does not report
    STRICT, or when the schema has no strict representation — the four
    ``InvestigationResponse_*`` schemas carry ``Dict[str, Any]`` fields that
    OpenAI's subset cannot express, and forcing them would guarantee empty
    milestone justifications rather than merely unenforced ones. Capability
    detection failing is treated as "not strict": the unenforced tool is the
    behaviour this path has always had, so it cannot regress a turn.
    """
    from faultmaven.infrastructure.llm.structured_output_capability import (
        StructuredOutputCapability,
    )
    from faultmaven.utils.schema_converter import (
        pydantic_to_openai_tools,
        pydantic_to_strict_openai_tools,
    )

    try:
        capability = provider.get_structured_output_capability()
    except Exception as exc:
        logger.debug(
            "Structured-output capability unavailable (%s); schema tool "
            "stays unenforced",
            exc,
        )
        return pydantic_to_openai_tools(schema_model)

    # The provider API is synchronous. A coroutine here means the provider
    # is a stand-in that answers everything asynchronously, which is not an
    # answer — close it so it does not surface as an un-awaited-coroutine
    # warning, and treat the capability as unknown.
    if inspect.iscoroutine(capability):
        capability.close()
        return pydantic_to_openai_tools(schema_model)

    if capability != StructuredOutputCapability.STRICT:
        return pydantic_to_openai_tools(schema_model)

    return pydantic_to_strict_openai_tools(schema_model)


def _build_da_system_instruction(
    tool_names: list[str],
    schema_tool_name: str,
) -> str:
    """Build the system instruction that tells the LLM how to use DA tools.

    Adapts to whichever investigation tools are actually registered.
    Without this, the LLM sees tool definitions but has no guidance on
    when or why to call them, leading to non-deterministic tool usage.
    """
    has_search = "search_file" in tool_names
    has_da = "deep_analysis" in tool_names
    has_web = "web_search" in tool_names
    has_kb = "kb_qa" in tool_names

    # Build tool guidance based on what's actually available
    search_mode_guidance = (
        "search_file modes:\n"
        "- keyword (DEFAULT): Splits query into tokens and finds lines "
        "containing all of them. Use for IPs, hostnames, error codes, "
        "service names, usernames. Just pass the raw value as query — "
        'e.g., query="173.234.31.186" or query="timeout connection".\n'
        "- regex: Only when keyword mode cannot express the pattern "
        "(e.g., timestamp ranges, capture groups). Regex is error-prone "
        "— prefer keyword mode unless you specifically need pattern matching."
    )

    # Core evidence tools
    tool_lines = []
    if has_search:
        tool_lines.append(
            "- search_file: keyword/regex search against raw evidence files. "
            "Use for exact matches — IPs, timestamps, error codes, service names."
        )
    if has_da:
        tool_lines.append(
            "- deep_analysis: LLM-interpreted analysis of specific evidence sections. "
            "Use for analytical questions keyword search cannot answer. "
            "Limited to 1 call per turn."
        )
    if has_kb:
        tool_lines.append(
            "- kb_qa: Search the knowledge base for runbooks, best practices, "
            "and documented solutions. Returns results from all accessible "
            "sources (global, personal, team) automatically."
        )
    if has_web:
        tool_lines.append(
            "- web_search: Search trusted technical websites (Stack Overflow, "
            "official docs) for error messages and solutions."
        )

    if tool_lines:
        # Build priority guidance
        priority_parts = []
        if has_search or has_da:
            evidence_tools = ", ".join(
                t for t in ["search_file", "deep_analysis"] if t in tool_names
            )
            priority_parts.append(
                f"1. Start with case evidence ({evidence_tools}) — "
                "ground your analysis in THIS case's data first."
            )
        if has_kb:
            priority_parts.append(
                "2. Check knowledge base (kb_qa) for documented solutions "
                "when evidence alone doesn't explain the issue."
            )
        if has_web:
            priority_parts.append(
                "3. Use web_search as a last resort when evidence and KB "
                "have no answers — e.g., unfamiliar error messages or "
                "technology-specific issues."
            )

        tool_guidance = (
            f"You have {len(tool_lines)} investigation tools:\n"
            + "\n".join(tool_lines)
            + "\n\nTool priority:\n"
            + "\n".join(priority_parts)
        )
        if has_search:
            tool_guidance += f"\n\n{search_mode_guidance}"
    else:
        tool_guidance = (
            "No investigation tools are available for this turn. "
            "Base your analysis on the evidence context provided."
        )

    return (
        "You have investigation tools available to search and analyze "
        "the raw evidence files attached to this case.\n\n"
        f"{tool_guidance}\n\n"
        "QUESTION ROUTING — Decide which type of question the user is asking:\n\n"
        "TYPE A — CASE QUESTION (about THIS case's evidence):\n"
        "Questions about specific data in the submitted files — IPs, errors, "
        "timestamps, patterns, configurations, or anything that requires "
        "examining the evidence. Examples: 'What IPs failed auth?', "
        "'What happened at 14:00?', 'Is there a pattern in the errors?'\n"
        f"→ You MUST search the evidence ({', '.join(t for t in ['search_file', 'deep_analysis'] if t in tool_names)}) before "
        "responding. The structural indexes are summaries — they lack the "
        "specific values needed for grounded analysis. After searching, call "
        f"{schema_tool_name} to produce your structured response.\n\n"
        "TYPE B — KNOWLEDGE QUESTION (general technical knowledge):\n"
        "Questions about technologies, concepts, best practices, or setup "
        "procedures that are NOT answerable from case evidence. Examples: "
        "'What is Opik?', 'How to set up Redis clustering?', "
        "'Common causes of OOM kills?'\n"
        "→ You MUST search kb_qa first for documented solutions, runbooks, "
        "or best practices. If kb_qa returns relevant results, ground your "
        "answer in them and cite the source. If no relevant results, answer "
        "from your own knowledge (do not mention the failed search). "
        "Optionally use web_search for supplementary detail. Connect your "
        f"answer to the case context when relevant, then call {schema_tool_name}.\n\n"
        "TYPE C — HYBRID (needs both evidence AND knowledge):\n"
        "Questions that bridge case data and external knowledge. Examples: "
        "'Is our Redis config following best practices?', "
        "'Are these SSH settings secure?'\n"
        "→ Search evidence first to understand the current state, then use "
        "your knowledge, web_search, or KB tools for the reference baseline.\n\n"
        "TYPE D — ABOUT FAULTMAVEN (the assistant itself):\n"
        "Questions about YOU — which model or provider generates these "
        "responses, how you retrieve runbooks, who built you, what you can "
        "do. Examples: 'What LLM are you running on?', 'How do you work "
        "under the hood?'\n"
        "→ For that part, do NOT search the evidence or the knowledge base: "
        "FaultMaven is not the system under investigation and nothing about "
        "it is in the case. Answer it briefly from the self-reference "
        "guidance in your instructions and never request FaultMaven's own "
        "configuration as evidence. If the same message ALSO delivers or "
        "asks about case data, that part is Type A/B/C — search it first, "
        "then answer the FaultMaven part alongside — before calling "
        f"{schema_tool_name}.\n\n"
        "DEFAULT: When uncertain between Types A–C, treat it as Type A "
        "(case question) — evidence search is always safe. Only skip "
        "evidence search when the question clearly cannot be answered from "
        "log files, configs, or other submitted data.\n\n"
        "IMPORTANT — Search for the specific entity, not the event type:\n"
        "When the user asks about a specific IP, hostname, username, error "
        "code, or timestamp, search for THAT value directly — e.g., "
        'query="173.234.31.186", not query="Failed password". Searching '
        "for event types returns results for ALL entities and buries the "
        "relevant lines.\n\n"
        "IMPORTANT — PII tokens vs raw data:\n"
        "The <evidence_collected> summaries use PII placeholders "
        "(e.g., <IP_ADDRESS_1>). The raw files contain ORIGINAL values. "
        "When calling search_file, use ORIGINAL values from the user's "
        "message, NOT PII tokens.\n\n"
        "SEARCHABLE EVIDENCE — Only use search_file on evidence with "
        'searchable="true" in <evidence_collected>. These are uploaded '
        "files with raw content on disk. Evidence WITHOUT this attribute "
        "are investigation notes — they have no file to search. If you "
        "need to search a file, take its id and its label from the "
        "searchable entries.\n\n"
        "EVIDENCE vs KNOWLEDGE — These are fundamentally different data types:\n"
        "- EVIDENCE is case-specific data submitted by the user: log files, "
        "metrics, configs, pasted text, screenshots, user statements about "
        "their environment. Only user-submitted data goes in evidence_to_add.\n"
        "- KNOWLEDGE is pre-built reference material from kb_qa, web_search, "
        "or your own training data. Knowledge informs your analysis but is "
        "NEVER recorded as evidence. Do NOT create evidence_to_add entries "
        "from kb_qa results, web_search results, or your own knowledge.\n\n"
        "RESPONSE FORMAT — Ground your response in evidence:\n"
        "- Every item in <evidence_collected> carries a label attribute. "
        "That label is its name — use it verbatim and use nothing else. "
        "Not every item is a file the user named: text they pasted is "
        'labelled like "pasted text (turn 3)", and that IS its name. '
        "Never invent a filename for one, and never reach for a "
        "file-looking name from inside a file's contents.\n"
        "- For case questions, cite the label and line numbers from "
        "search results (e.g., 'In data_6-1.log, line 42: ...' or "
        "'In pasted text (turn 3), line 42: ...') and explain the "
        "significance using causal language.\n"
        "- For knowledge questions, state the relevant facts and relate "
        "them to the user's investigation context when possible.\n"
        "- Reference evidence by its label or by description, never by "
        "ev_ IDs."
    )


def _build_assistant_message(response: Any) -> dict:
    """Convert LLMResponse to OpenAI-format assistant message.

    Round-trips two kinds of provider-specific artifacts when present:
    1. Per-tool-call `provider_metadata` (e.g. signatures bound to a
       specific functionCall).
    2. Response-level `provider_metadata` (e.g. Gemini 3.x's full
       `assistant_parts` array, which carries thoughtSignatures attached
       to text/thought/functionCall parts that must all round-trip
       together — skipping any one produces a 400 on the next turn).

    Both are absent for providers/models that don't emit reasoning
    artifacts (Gemini 2.5, OpenAI Chat Completions, etc.) — the keys
    are omitted entirely so downstream serializers see no change.
    """
    tool_calls_list = []
    for tc in response.tool_calls or []:
        entry = {
            "id": tc.id,
            "type": tc.type,
            "function": tc.function,
        }
        if getattr(tc, "provider_metadata", None):
            entry["provider_metadata"] = tc.provider_metadata
        tool_calls_list.append(entry)

    msg = {
        "role": "assistant",
        "content": response.content or "",
    }
    if tool_calls_list:
        msg["tool_calls"] = tool_calls_list
    if getattr(response, "provider_metadata", None):
        msg["provider_metadata"] = response.provider_metadata
    return msg
