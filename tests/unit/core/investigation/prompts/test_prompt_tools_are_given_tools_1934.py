"""Every tool name the model reads is a tool the model is GIVEN (#1934).

The prompts are plain strings; nothing tied the tool names they use to the
registry the model's tool list is built from. They named ``case_evidence_qa``
(the internal module) while the tool list carried ``case_evidence_search``.

The given set comes from the real ``_create_investigation_tools`` fed bare
``AgentTool`` doubles (``.name`` is a constant property, so no dependencies are
needed). The vocabulary is the name of every ``BaseTool`` subclass the tools
package defines, plus retired spellings. Model-facing strings are collected by
AST from the prompt package, the DA instruction / vectorization modules and the
given tools' own modules (docstrings excluded: they are for developers).

Flag-gated text is paired with its flag explicitly: the entity tools are given
only under FAULTMAVEN_ENTITY_REGISTRY and ``reclassify_evidence`` only under
FAULTMAVEN_RECLASSIFY_ENABLED (#1941), so their names may appear in source that
renders only when they are given.

The source scan above answers "is every name a real, possibly-given tool". The
render matrix below (#1939, #1955) answers the per-turn question: every tool
the prompts of a turn name is one THAT turn gives. Rows are given sets derived
from the real registry under each condition that removes or adds a tool, plus
the tool-less turn; columns are the real renders a turn sends. A terminal turn
is given the row's tools in ``TERMINAL_QA_TOOLS`` (#1964), so its cells — the
prompt, and the terminal DA message — are fed that intersection; the terminal
prompt also points at suggestions, and those cells pair the prompt with the
suggestion list the reply is given.
"""

from __future__ import annotations

import ast
import asyncio
import difflib
import hashlib
import importlib
import inspect
import pkgutil
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import chromadb
import pytest
from chromadb.config import Settings as ChromaSettings

import faultmaven
import faultmaven.modules.agent.tools as tools_pkg
from faultmaven.config.settings import get_settings
from faultmaven.container.providers.services import _create_investigation_tools
from faultmaven.core.investigation import terminal_transitions
from faultmaven.core.investigation.milestone_engine import terminal_replies
from faultmaven.core.investigation.milestone_engine.terminal_replies import (
    GENERATE_RUNBOOK_PAYLOAD,
    REGENERATE_CLOSURE_SUMMARY_PAYLOAD,
    REGENERATE_RESOLUTION_SUMMARY_PAYLOAD,
    _closed_suggestions,
    _resolved_suggestions,
    terminal_affordances,
)
from faultmaven.core.investigation.milestone_engine.terminal_turns import (
    TERMINAL_QA_TOOLS,
)
from faultmaven.core.investigation.milestone_engine.tool_messages import (
    _build_da_system_instruction,
)
from faultmaven.core.investigation.milestone_engine.vectorization import (
    EvidenceVectorizer,
)
from faultmaven.core.investigation.prompts.context_builder.entity_highlights import (
    EntityHighlightGroup,
    EntityHighlightRow,
)
from faultmaven.core.investigation.prompts.context_builder.evidence import (
    _build_evidence_context,
)
from faultmaven.core.investigation.prompts.templates.assembly import (
    get_prompt_for_case,
)
from faultmaven.core.investigation.prompts.templates.fallback import (
    DEGRADED_NO_TOOLS_NOTICE,
    get_fallback_prompt_for_case,
)
from faultmaven.core.investigation.prompts.templates.tool_text import tools_line
from faultmaven.infrastructure.persistence.case_vector_store import CaseVectorStore
from faultmaven.models.interfaces import BaseTool, ToolResult
from faultmaven.modules.agent.tools.base import AgentTool, ToolContext
from faultmaven.modules.agent.tools.case_evidence_qa import AnswerFromCaseEvidence
from faultmaven.modules.agent.tools.deep_analysis_tool import DeepAnalysisTool
from faultmaven.modules.agent.tools.find_entity_tool import FindEntityTool
from faultmaven.modules.agent.tools.kb_qa import AnswerFromKB
from faultmaven.modules.agent.tools.kb_tool_adapter import CaseEvidenceQAAdapter
from faultmaven.modules.agent.tools.list_top_entities_tool import ListTopEntitiesTool
from faultmaven.modules.agent.tools.reclassify_evidence_tool import (
    ReclassifyEvidenceTool,
)
from faultmaven.modules.agent.tools.search_file_tool import (
    SearchFileTool,
    format_searchable_alternatives,
)
from faultmaven.modules.agent.tools.vectorize_file_tool import VectorizeFileTool
from faultmaven.modules.agent.tools.web_search import WebSearchTool
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InquiryData,
    UploadedFile,
)
from faultmaven.modules.case.domain.models.progress import MitigationRecord
from tests.model_content import disabled_redaction
from tests.utils import admit_every_parent

pytestmark = pytest.mark.unit

PKG = Path(faultmaven.__file__).parent
_TOOLS = PKG / "modules/agent/tools"

MODEL_FACING = [
    *sorted((PKG / "core/investigation/prompts").rglob("*.py")),
    PKG / "core/investigation/milestone_engine/tool_messages.py",
    PKG / "core/investigation/milestone_engine/vectorization.py",
    *[
        _TOOLS / f
        for f in (
            "search_file_tool.py",
            "deep_analysis_tool.py",
            "web_search.py",
            "kb_tool_adapter.py",
            "vectorize_file_tool.py",
            "kb_qa.py",
            "case_evidence_qa.py",
            "kb_configs/case_evidence_config.py",
            "find_entity_tool.py",
            "list_top_entities_tool.py",
            "reclassify_evidence_tool.py",
        )
    ],
]

_DEFAULT_TOOLS = dict(
    search_file_tool=SearchFileTool,
    deep_analysis_tool=DeepAnalysisTool,
    web_search_tool=WebSearchTool,
    # The wrapped tool: the registry builds the ``kb_qa`` adapter around it,
    # bound to the knowledge service's admission check (#2397).
    kb_qa_tool=AnswerFromKB,
    vectorize_file_tool=VectorizeFileTool,
    case_evidence_qa_adapter=CaseEvidenceQAAdapter,
)
_ENTITY_TOOLS = dict(
    find_entity_tool=FindEntityTool, list_top_entities_tool=ListTopEntitiesTool
)
# Given only under FAULTMAVEN_ENTITY_REGISTRY; the text naming them renders
# only under that flag.
ENTITY_FLAG_NAMES = {"find_entity", "list_top_entities"}
_RECLASSIFY_TOOLS = dict(reclassify_evidence_tool=ReclassifyEvidenceTool)
# Given only under FAULTMAVEN_RECLASSIFY_ENABLED (#1941). Its own module renders
# only when it is given, and blocks.py names it only when it is given (#1939).
RECLASSIFY_FLAG_NAMES = {"reclassify_evidence"}
_RECLASSIFY_HOMES = {"blocks.py", "reclassify_evidence_tool.py"}
# Spellings of tools that no longer exist: a prompt naming one is ``not_given``.
_RETIRED = {
    "case_evidence_qa",
    "global_kb_qa",
    "user_kb_qa",
    # Deleted by #2376: built, or only registered into a registry nothing read,
    # and never given to the model.
    "list_evidence",
    "list_evidence_by_time",
    "read_file",
    "search_knowledge",
}
_NOT_TOOLS = {"directed_analysis"}
_TOKEN = re.compile(r"(?<![\w.])[a-z]+(?:_[a-z]+)+(?![\w(]*\.)")


def _bare(cls):
    return object.__new__(cls)


def given(present: dict) -> set[str]:
    container = SimpleNamespace(**{k: _bare(c) for k, c in present.items()})
    registry = _create_investigation_tools(
        container,
        knowledge_service=SimpleNamespace(admitted_generations=admit_every_parent),
    )
    return {t.name for t in registry.get_all_tools()} if registry else set()


def _subclasses(cls):
    for sub in cls.__subclasses__():
        yield sub
        yield from _subclasses(sub)


def tool_class_names() -> set[str]:
    """The name of every ``BaseTool`` subclass the tools package, subpackages
    included, defines. #943's ``KnowledgeBaseTool`` was a ``BaseTool``, not an
    ``AgentTool``. A concrete class whose name is not a constant reports a
    sentinel, which no registry gives, so it fails the comparison."""
    for mod in pkgutil.walk_packages(tools_pkg.__path__, f"{tools_pkg.__name__}."):
        importlib.import_module(mod.name)
    names = set()
    for cls in _subclasses(BaseTool):
        # The package's classes, not a test's: a double such as
        # ``test_tool_registry``'s ``FailingTool`` stays a subclass for as long
        # as it is alive in this process.
        if not cls.__module__.startswith(f"{tools_pkg.__name__}."):
            continue
        if inspect.isabstract(cls):
            continue
        try:
            names.add(_bare(cls).name)
        except Exception:
            names.add(f"<{cls.__qualname__}: no constant name>")
    return names


def vocabulary() -> set[str]:
    return tool_class_names() | _RETIRED


def strings(path: Path):
    tree = ast.parse(path.read_text())
    docstrings = {
        id(n.body[0].value)
        for n in ast.walk(tree)
        if isinstance(
            n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        )
        and n.body
        and isinstance(n.body[0], ast.Expr)
        and isinstance(n.body[0].value, ast.Constant)
    }
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            if id(n) not in docstrings:
                yield n.value


def mentions(files=MODEL_FACING) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for path in files:
        for text in strings(path):
            for tok in _TOKEN.findall(text):
                out.setdefault(tok, set()).add(path.name)
    return out


def violations(files=MODEL_FACING, given_names=None, vocab=None):
    g = given(_DEFAULT_TOOLS) if given_names is None else given_names
    vocab = vocabulary() if vocab is None else vocab
    allowed = g | ENTITY_FLAG_NAMES | RECLASSIFY_FLAG_NAMES  # flag-gated, see below
    not_given, near = {}, {}
    for tok, found in mentions(files).items():
        if tok in _NOT_TOOLS or tok in g:
            continue
        if tok in ENTITY_FLAG_NAMES or tok in RECLASSIFY_FLAG_NAMES:
            continue
        if tok in vocab:
            not_given[tok] = sorted(found)
        elif difflib.get_close_matches(tok, sorted(allowed), 1, 0.75):
            near[tok] = sorted(found)
    return not_given, near


def test_default_registry_is_the_six() -> None:
    assert given(_DEFAULT_TOOLS) == {
        "search_file",
        "vectorize_file",
        "deep_analysis",
        "kb_qa",
        "case_evidence_search",
        "web_search",
    }


def test_every_tool_class_is_given_with_every_flag_on() -> None:
    """A tool class nothing gives the model is dead code that reads as live:
    ``KnowledgeBaseTool`` (#943), ``reclassify_evidence`` built and never
    registered (#1941), and four tools #2376 deleted. With every flag on, the
    registry ``_create_investigation_tools`` builds holds every tool class the
    tools package defines. Compared without ``_RETIRED``, so a class revived
    under a retired name is held to the same rule."""
    assert tool_class_names() == given(
        {**_DEFAULT_TOOLS, **_ENTITY_TOOLS, **_RECLASSIFY_TOOLS}
    )


def test_entity_tools_are_given_only_under_their_flag() -> None:
    assert ENTITY_FLAG_NAMES <= given({**_DEFAULT_TOOLS, **_ENTITY_TOOLS})
    assert not ENTITY_FLAG_NAMES & given(_DEFAULT_TOOLS)


def test_reclassify_evidence_is_given_only_under_its_flag() -> None:
    # #1941: the flag used to build a tool nothing registered, so the model
    # was never given it and the hedge told the user the feature was off.
    assert given({**_DEFAULT_TOOLS, **_RECLASSIFY_TOOLS}) == given(_DEFAULT_TOOLS) | (
        RECLASSIFY_FLAG_NAMES
    )
    assert not RECLASSIFY_FLAG_NAMES & given(_DEFAULT_TOOLS)


def test_reclassify_text_pairs_with_its_flag() -> None:
    # Outside its own tool module, the only model-facing home of the name is
    # the evidence-grounding block, which renders it only when it is given —
    # the render matrix below pins that per turn.
    homes = set().union(
        *(found for n, found in mentions().items() if n in RECLASSIFY_FLAG_NAMES)
    )
    assert homes <= _RECLASSIFY_HOMES, homes


def test_every_tool_name_the_model_reads_is_given() -> None:
    not_given, near = violations()
    assert not not_given, f"names a tool the model is not given: {not_given}"
    assert not near, f"near-miss of a given tool name: {near}"


def _scan_text(tmp_path: Path, text: str) -> list[Path]:
    path = tmp_path / "probe.py"
    path.write_text(f"X = {text!r}\n")
    return [path]


@pytest.mark.parametrize(
    "text, bucket",
    [
        ("call case_evidence_qa on it", "not_given"),
        ("call global_kb_qa on it", "not_given"),
        ("call read_file on it", "not_given"),
        ("call list_evidence on it", "not_given"),
        ("call list_evidence_by_time on it", "not_given"),
        ("call search_knowledge on it", "not_given"),
        ("call search_files on it", "near"),
        ("call deep_analyze on it", "near"),
    ],
)
def test_the_scan_bites(tmp_path: Path, text: str, bucket: str) -> None:
    not_given, near = violations(_scan_text(tmp_path, text))
    assert not_given if bucket == "not_given" else near, (text, not_given, near)


def test_the_scan_passes_a_given_name(tmp_path: Path) -> None:
    assert violations(_scan_text(tmp_path, "call case_evidence_search")) == ({}, {})


def test_entity_text_pairs_with_its_flag() -> None:
    # The only model-facing homes of the entity names are text that renders
    # when FAULTMAVEN_ENTITY_REGISTRY is on (the caller of the highlights block
    # gates on it; their tools are given under exactly that flag), and the
    # terminal DA message's per-tool lines, each rendered only when its tool is
    # given (#1964; the terminal column of the render matrix pins it per row).
    homes = set().union(
        *(found for n, found in mentions().items() if n in ENTITY_FLAG_NAMES)
    )
    assert homes <= {
        "entity_highlights.py",
        "find_entity_tool.py",
        "list_top_entities_tool.py",
        "tool_messages.py",
    }, homes


@pytest.mark.parametrize(
    "absent, others", [("web_search", ["kb_qa"]), ("kb_qa", ["web_search"])]
)
def test_da_instruction_names_only_present_knowledge_tools(absent, others) -> None:
    present = ["search_file", "deep_analysis", *others]
    text = _build_da_system_instruction(present, "Schema")
    assert absent not in text
    assert others[0] in text


def test_da_instruction_without_either_names_neither() -> None:
    text = _build_da_system_instruction(["search_file"], "Schema")
    assert "web_search" not in text and "kb_qa" not in text


def test_da_instruction_with_both_names_both() -> None:
    text = _build_da_system_instruction(
        ["search_file", "deep_analysis", "kb_qa", "web_search"], "Schema"
    )
    assert "kb_qa" in text and "web_search" in text


# ===========================================================================
# #1939 / #1955: the render matrix — what ONE turn's prompts name
# ===========================================================================

#: A tool name as rendered text carries it: snake_case, not a dotted path's
#: part, and — unlike ``_TOKEN`` — still caught at the end of a sentence.
_RENDERED_TOKEN = re.compile(r"(?<![\w.])[a-z]+(?:_[a-z]+)+(?!\w|\.\w)")
VOCAB = vocabulary()


def rendered_mentions(text: str) -> set[str]:
    """The tool names ``text`` mentions."""
    return {t for t in _RENDERED_TOKEN.findall(text) if t in VOCAB}


def _minus(*attrs: str) -> dict:
    return {k: v for k, v in _DEFAULT_TOOLS.items() if k not in attrs}


#: Given sets, each derived from the real ``_create_investigation_tools`` under
#: a condition that removes or adds tools, plus the tool-less turn (the
#: single-shot route, a DA provider without tool calling, the
#: ``ToolCallingUnsupportedError`` fallback, the #662 retry).
ROWS: dict[str, frozenset[str]] = {
    "six": frozenset(given(_DEFAULT_TOOLS)),
    "no_kb": frozenset(given(_minus("kb_qa_tool"))),
    "no_deep_analysis": frozenset(given(_minus("deep_analysis_tool"))),
    "no_web_search": frozenset(given(_minus("web_search_tool"))),
    # SKIP_SERVICE_CHECKS, or no vector stores: neither QA adapter is built,
    # while the container still builds vectorize_file_tool — the registry is
    # what keeps it from being given alone.
    "no_vector_stores": frozenset(
        given(_minus("kb_qa_tool", "case_evidence_qa_adapter"))
    ),
    "entity": frozenset(given({**_DEFAULT_TOOLS, **_ENTITY_TOOLS})),
    "reclassify": frozenset(given({**_DEFAULT_TOOLS, **_RECLASSIFY_TOOLS})),
    "none": frozenset(),
}

_MSG = "what does the log show?"
_FILE_ID = "file_1939aaaaaaaa"
#: Longer than the per-item extract cap, so the truncation markers render.
_BIG_INDEX = "\n".join(
    f"2026-10-12T10:{i % 60:02d}:00Z ERROR upstream timeout req={i}" for i in range(400)
)
_GROUPS = [
    EntityHighlightGroup(
        entity_type="ip", rows=(EntityHighlightRow("10.61.3.99", 214, True),)
    )
]


def _file(file_id: str = _FILE_ID, turn: int = 1) -> UploadedFile:
    return UploadedFile(
        file_id=file_id,
        filename=f"{file_id}.log",
        size_bytes=900_000,
        content_type="text/plain",
        uploaded_at_turn=turn,
        upload_source="file_upload",
        storage_ref=f"evidence/case_1939/{file_id}.txt",
        data_type="logs",
        summary="App log.",
        structural_index=_BIG_INDEX,
    )


def _evidence() -> Evidence:
    return Evidence(
        evidence_id="ev_193900000001",
        source_file_id=_FILE_ID,
        summary="upstream timeouts",
        extract="ERROR upstream timeout",
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        source_type=EvidenceSourceType.LOGS,
        primary_purpose="Test",
        collected_by="user_1939",
        collected_at_turn=1,
    )


def _case(state: CaseState = CaseState.INVESTIGATING, stage: str = "diagnosis"):
    case = Case(
        case_id="case_1939aaaaaaaa",
        title="API timeouts",
        description="Upstream timeouts since 10:00",
        user_id="user_1939",
        enterprise_id="org_1939",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            proposed_problem_statement="API timeouts",
        ),
        uploaded_files=[_file(), _file("file_1939bbbbbbbb", turn=3)],
        evidence=[_evidence()],
        current_turn=3,
    )
    if stage == "mitigation":
        case.progress.mitigation = MitigationRecord(proposed_at_turn=2, accepted=True)
    elif stage == "treatment":
        case.progress.solution_accepted = True
    if state == CaseState.INQUIRY:
        case = case.model_copy(
            update={"state": state, "evidence": [], "inquiry": InquiryData()}
        )
    elif state != CaseState.INVESTIGATING:
        stamp = "resolved_at" if state == CaseState.RESOLVED else "closed_at"
        case = case.model_copy(
            update={"state": state, stamp: datetime.now(timezone.utc)}
        )
    return case


def _crowded_case():
    """More files than the evidence budget holds: the degraded renders."""
    case = _case()
    case.uploaded_files += [_file(f"file_1939c{i:011d}", turn=1) for i in range(6)]
    case.uploaded_files += [_file(f"file_1939d{i:011d}", turn=3) for i in range(3)]
    return case


def _advisory(given_tools: frozenset[str]) -> str:
    """The engine's advisory after three empty searches of one file."""
    vectorizer = EvidenceVectorizer(deps=SimpleNamespace(investigation_tools=None))
    return asyncio.run(
        vectorizer.track_da_result(
            func_name="search_file",
            evidence_id="ev_1",
            tool_result=ToolResult(success=True, data={"results_count": 0}),
            result_text="",
            case=None,
            tool_context=None,
            da_empty_search_counts={"ev_1": 2},
            proactive_tasks={},
            given_tools=given_tools,
        )
    )


#: The stage/mode columns of an investigating turn.
_INVESTIGATING = {
    "diagnosis": ("diagnosis", "triage"),
    "diagnosis_directed": ("diagnosis", "directed_analysis"),
    "mitigation": ("mitigation", "triage"),
    "treatment": ("treatment", "triage"),
    "knowledge_query": ("diagnosis", "knowledge_query"),
    "agent_meta": ("diagnosis", "agent_meta"),
}


def renders(g: frozenset[str]) -> dict[str, str]:
    """Every model-facing text a turn given ``g`` can send, by column."""
    out = {
        "inquiry": get_prompt_for_case(_case(CaseState.INQUIRY), _MSG, given_tools=g),
    }
    for column, (stage, mode) in _INVESTIGATING.items():
        out[column] = get_prompt_for_case(
            _case(stage=stage),
            _MSG,
            processing_mode=mode,
            given_tools=g,
            entity_highlight_groups=_GROUPS,
        )
    out["evidence_over_budget"] = _build_evidence_context(
        _crowded_case(), char_budget_override=12_000, given_tools=g
    )
    for state in (CaseState.INQUIRY, CaseState.INVESTIGATING, CaseState.RESOLVED):
        out[f"fallback_{state.value}"] = get_fallback_prompt_for_case(
            _case(state), _MSG, given_tools=g
        )
    out["da_instruction"] = _build_da_system_instruction(sorted(g), "Schema")
    out["empty_search_advisory"] = _advisory(g)
    return out


#: Columns that are whole prompts, so they state the turn's tools.
_PROMPTS = {
    "inquiry",
    *_INVESTIGATING,
    "fallback_inquiry",
    "fallback_investigating",
    "fallback_resolved",
}


def _without_tools_line(text: str) -> str:
    return "\n".join(
        line
        for line in text.split("\n")
        if not line.startswith("Tools available on this turn:")
    )


def test_the_rows_are_what_the_registry_gives() -> None:
    """Guard the matrix: each condition really removes or adds the tool."""
    six = ROWS["six"]
    assert ROWS["no_kb"] == six - {"kb_qa"}
    assert ROWS["no_deep_analysis"] == six - {"deep_analysis"}
    assert ROWS["no_web_search"] == six - {"web_search"}
    # vectorize_file goes with case_evidence_search: its results send the
    # model to that tool.
    assert ROWS["no_vector_stores"] == six - {
        "kb_qa",
        "case_evidence_search",
        "vectorize_file",
    }
    assert ROWS["entity"] == six | ENTITY_FLAG_NAMES
    assert ROWS["reclassify"] == six | RECLASSIFY_FLAG_NAMES


def test_vectorize_file_is_given_only_beside_case_evidence_search() -> None:
    assert "vectorize_file" not in given(_minus("case_evidence_qa_adapter"))
    assert "vectorize_file" in given(_DEFAULT_TOOLS)


@pytest.mark.parametrize("row", sorted(ROWS))
def test_every_tool_a_turn_names_is_one_it_gives(row: str) -> None:
    g = ROWS[row]
    not_given = {
        column: sorted(rendered_mentions(text) - g)
        for column, text in renders(g).items()
        if rendered_mentions(text) - g
    }
    assert not not_given, f"given {sorted(g)}, the turn names: {not_given}"


@pytest.mark.parametrize("row", sorted(ROWS))
def test_each_given_tool_is_named_where_it_is_used(row: str) -> None:
    """Non-vacuity: a gate that never renders its tool would pass the cell
    above. Each given tool with prompt text of its own is named outside the
    tools line (``vectorize_file`` has none: the engine runs it)."""
    g = ROWS[row]
    named = set().union(
        *(rendered_mentions(_without_tools_line(t)) for t in renders(g).values())
    )
    assert g - {"vectorize_file"} <= named, sorted(g - {"vectorize_file"} - named)


@pytest.mark.parametrize("row", sorted(ROWS))
def test_every_prompt_states_the_turns_tools(row: str) -> None:
    g = ROWS[row]
    line = tools_line(g)
    missing = [c for c, t in renders(g).items() if c in _PROMPTS and line not in t]
    assert not missing, missing
    if not g:
        assert line.startswith("No investigation tools are available on this turn")


#: Phrases that point at a tool without naming it. Text must not instruct a
#: tool the turn lacks under a synonym either (#1939 review), so a tool-less
#: turn renders none of them, and a turn missing one tool renders none of that
#: tool's.
TOOL_IMPLYING = (
    "search for",
    "search strategy",
    "zone 2 change event search",
    "tools for",
    "tool call",
    "stay searchable",
)
MISSING_TOOL_IMPLYING = {
    "no_kb": ("search kb", "search the knowledge base", "knowledge base search"),
    "no_vector_stores": ("semantic query", "semantic search", "vectoriz"),
    "no_deep_analysis": ("deep analysis", "llm-interpreted"),
}

#: ``<evidence_collected>`` is case data: its bodies quote file content, which
#: may say anything. The probes read the prompt's own text, so they skip the
#: bodies. The opening tags the renderer wrote are its own words (their
#: attributes mark provenance, ``file_backed="true"``, whatever the turn's
#: tools — #1969), so those are still read. What marks a tag as the
#: renderer's is the block's genuine fence token, which quoted content cannot
#: contain: a tag-shaped string in a body has none and stays skipped.
_EVIDENCE_BLOCK = re.compile(
    r'<evidence_collected fence="([0-9a-f]+)">.*?</evidence_collected fence="\1">',
    re.S,
)
_OPENING_TAG = re.compile(r"<[a-z_]+(?:\s[^<>]*)?>")


def _renderers_own_tags(block: re.Match) -> list[str]:
    """The opening tags in one evidence block that carry its genuine token."""
    live = f'fence="{block.group(1)}"'
    return [tag for tag in _OPENING_TAG.findall(block.group(0)) if live in tag]


def synonym_hits(text: str, phrases) -> list[str]:
    tags = " ".join(
        tag
        for block in _EVIDENCE_BLOCK.finditer(text)
        for tag in _renderers_own_tags(block)
    )
    own = (_EVIDENCE_BLOCK.sub("", text) + "\n" + tags).lower()
    return [p for p in phrases if p in own]


def test_the_evidence_exemption_reads_only_the_renderers_own_tags() -> None:
    """A tag-shaped string quoted in a body is case data, not prompt text: it
    carries no genuine token, so it is not read. The renderer's own opening
    tags are."""
    quoted = (
        '<evidence_collected fence="abc123">\n'
        '  <evidence id="ev_1" label="app.log" fence="abc123">'
        '<summary fence="abc123"><a title="search for it">quoted</a>'
        '</summary fence="abc123"></evidence fence="abc123">\n'
        '</evidence_collected fence="abc123">'
    )
    assert synonym_hits(quoted, ["search for"]) == []
    # The same words in a tag the renderer wrote are read.
    own = quoted.replace('label="app.log"', 'label="search for it"')
    assert synonym_hits(own, ["search for"]) == ["search for"]


def test_a_toolless_turn_implies_no_tool_by_any_name() -> None:
    hits = {
        column: synonym_hits(text, TOOL_IMPLYING)
        for column, text in renders(ROWS["none"]).items()
        if synonym_hits(text, TOOL_IMPLYING)
    }
    assert not hits, hits


@pytest.mark.parametrize("row", sorted(MISSING_TOOL_IMPLYING))
def test_a_missing_tool_is_not_implied_by_a_synonym(row: str) -> None:
    phrases = MISSING_TOOL_IMPLYING[row]
    hits = {
        column: synonym_hits(text, phrases)
        for column, text in renders(ROWS[row]).items()
        if synonym_hits(text, phrases)
    }
    assert not hits, hits


@pytest.mark.parametrize("row", sorted(ROWS))
def test_no_render_calls_a_row_searchable(row: str) -> None:
    """The row attribute marks provenance, never a capability (#1969): no
    column of any turn renders ``searchable="`` — in the evidence block or out
    of it, whatever the tools — while the file-backed rows it replaced still
    carry ``file_backed="true"``."""
    rendered = renders(ROWS[row])
    hits = [column for column, text in rendered.items() if 'searchable="' in text]
    assert not hits, hits
    assert any(
        'file_backed="true"' in text for text in rendered.values()
    ), "no column rendered a file-backed row — the probe is vacuous"


#: What a tool-less turn may say with ``searchab`` in it: the fallback upload
#: heading, which says the file is NOT searchable on this turn.
_TOOLLESS_TRUE_NEGATIVES = ("not searchable this turn",)


def _searchability_claims(text: str) -> list[str]:
    """Every line of ``text`` that says ``searchab``, less the true negatives."""
    lines = []
    for line in text.split("\n"):
        scrubbed = line.lower()
        for ok in _TOOLLESS_TRUE_NEGATIVES:
            scrubbed = scrubbed.replace(ok, "")
        if "searchab" in scrubbed:
            lines.append(line.strip()[:160])
    return lines


def _toolless_renders() -> dict[str, str]:
    """Every column of the tool-less row, plus the #662 retry's fallback with
    its degraded-turn notice appended (the engine sends both as one prompt)."""
    out = renders(ROWS["none"])
    for column in [c for c in out if c.startswith("fallback_")]:
        out[f"{column}+notice"] = out[column] + DEGRADED_NO_TOOLS_NOTICE
    return out


def test_a_toolless_turn_makes_no_claim_about_searchability() -> None:
    """The fence declaration and the fallback fence rule are rendered on every
    fenced prompt, so a stale ``searchability`` there reaches every tool-less
    turn (#1969). Nothing a tool-less turn renders says ``searchab``, in the
    evidence block or out of it, but the fallback heading's true negative."""
    claims = {
        column: _searchability_claims(text)
        for column, text in _toolless_renders().items()
        if _searchability_claims(text)
    }
    assert not claims, claims


def test_the_searchability_probe_bites() -> None:
    """Non-vacuity: the true negative is really rendered (and really scrubbed),
    and a stale claim in the fence texts would be caught."""
    heading = "CURRENT-TURN UPLOAD (not searchable this turn):"
    assert any(heading in t for t in _toolless_renders().values())
    assert _searchability_claims(heading) == []
    assert _searchability_claims("never a claim about an item's searchability.")
    assert _searchability_claims("Files are fully SEARCHABLE")


def test_the_synonym_probes_bite() -> None:
    """Each phrase is found where it is used: in the default six's renders,
    outside the evidence block, so the tool-less assertion is not vacuous."""
    six = "\n".join(renders(ROWS["six"]).values())
    missing = [p for p in TOOL_IMPLYING if not synonym_hits(six, [p])]
    assert not missing, missing


def test_the_degraded_evidence_renders_were_reached() -> None:
    """Non-vacuity for the stub cells: the over-budget column really renders
    the omitted-to-fit stub, the omission marker and a truncation marker."""
    text = renders(ROWS["six"])["evidence_over_budget"]
    assert "[Full content omitted to fit budget" in text
    assert "evidence_omitted" in text
    assert "[TRUNCATED:" in text


def _tool_class_strings(cls) -> list[str]:
    """Model-facing strings of one tool: its class body and its module's
    top-level constants (results it builds), docstrings excluded."""
    path = Path(importlib.import_module(cls.__module__).__file__)
    tree = ast.parse(path.read_text())
    out = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name != cls.__name__:
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        wrapper = ast.Module(body=[node], type_ignores=[])
        docstrings = {
            id(n.body[0].value)
            for n in ast.walk(wrapper)
            if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and n.body
            and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant)
        }
        out += [
            n.value
            for n in ast.walk(wrapper)
            if isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and id(n) not in docstrings
            and not (isinstance(node, ast.Expr) and n is node.value)
        ]
    return out


@pytest.mark.parametrize("row", sorted(ROWS))
def test_a_given_tools_own_text_names_only_given_tools(row: str) -> None:
    """A tool's description and results reach the model on the turns it is
    given; what they point at must be given on those turns too."""
    g = ROWS[row]
    by_name = {}
    for cls in _subclasses(AgentTool):
        try:
            by_name[_bare(cls).name] = cls
        except Exception:
            continue
    bad = {
        name: sorted(
            set().union(*map(rendered_mentions, _tool_class_strings(by_name[name]))) - g
        )
        for name in g
    }
    assert not {k: v for k, v in bad.items() if v}, bad


# ---------------------------------------------------------------------------
# #1955: the terminal prompt points at a suggestion only when it is offered
# ---------------------------------------------------------------------------

_REGENERATE_REDIRECT = "use the **Regenerate** suggestion"
_RUNBOOK_REDIRECT = "use the **Generate a runbook from this\nresolved case** suggestion"
_DRAFT_POINTER = "A runbook for this case already exists in the case creator's"

#: (state, ready, draft exists, regenerations left) per terminal condition.
_TERMINAL = {
    "resolved_offered": (CaseState.RESOLVED, True, False, 2),
    "resolved_draft_exists": (CaseState.RESOLVED, True, True, 2),
    "resolved_not_ready": (CaseState.RESOLVED, False, False, 2),
    "resolved_regen_exhausted": (CaseState.RESOLVED, True, False, 0),
    "closed": (CaseState.CLOSED, False, False, 2),
    "closed_regen_exhausted": (CaseState.CLOSED, False, False, 0),
}


def _terminal_turn(condition: str, g: frozenset[str], monkeypatch):
    """The suggestion list a terminal Q&A reply is given, and the prompt
    built from it — through the same builders the engine uses."""
    state, ready, draft, remaining = _TERMINAL[condition]
    monkeypatch.setattr(terminal_replies, "runbook_conversion_ready", lambda c: ready)
    monkeypatch.setattr(
        terminal_transitions, "should_generate_terminal_summary", lambda c: True
    )
    case = _case(state)
    offered = (
        _resolved_suggestions(case, remaining, runbook_already_exists=draft)
        if state == CaseState.RESOLVED
        else _closed_suggestions(case, remaining)
    )
    prompt = get_prompt_for_case(
        case,
        _MSG,
        given_tools=g,
        terminal_affordances=terminal_affordances(offered, runbook_draft_exists=draft),
    )
    return {s["payload"] for s in offered}, prompt


def terminal_given(row: str) -> frozenset[str]:
    """What a terminal Q&A turn is given on ``row``: its tools in the terminal
    set (#1964)."""
    return ROWS[row] & TERMINAL_QA_TOOLS


@pytest.mark.parametrize("condition", sorted(_TERMINAL))
@pytest.mark.parametrize("row", sorted(ROWS))
def test_the_terminal_prompt_points_only_at_offered_suggestions(
    condition: str, row: str, monkeypatch
) -> None:
    g = terminal_given(row)
    payloads, prompt = _terminal_turn(condition, g, monkeypatch)
    regen = bool(
        payloads
        & {REGENERATE_RESOLUTION_SUMMARY_PAYLOAD, REGENERATE_CLOSURE_SUMMARY_PAYLOAD}
    )
    assert (_REGENERATE_REDIRECT in prompt) is regen
    assert (_RUNBOOK_REDIRECT in prompt) is (GENERATE_RUNBOOK_PAYLOAD in payloads)
    assert (_DRAFT_POINTER in prompt) is (condition == "resolved_draft_exists")
    assert rendered_mentions(prompt) <= g
    assert tools_line(g) in prompt


def test_each_terminal_redirect_renders_somewhere(monkeypatch) -> None:
    """Non-vacuity: each redirect is reachable, so the pairing above is not
    passing on prompts that never name any suggestion."""
    seen = {
        c: _terminal_turn(c, terminal_given("six"), monkeypatch)[1]
        for c in sorted(_TERMINAL)
    }
    assert _RUNBOOK_REDIRECT in seen["resolved_offered"]
    assert _REGENERATE_REDIRECT in seen["closed"]
    assert _DRAFT_POINTER in seen["resolved_draft_exists"]
    assert "No runbook can be generated from this case here" in seen["closed"]


# ---------------------------------------------------------------------------
# #1964: the terminal DA message, and the investigating one unchanged
# ---------------------------------------------------------------------------

#: What a terminal turn's system message must not carry: the investigating
#: message's routing, search mandates and evidence-row rules, all of which
#: imply new investigation or point at blocks the terminal prompt does not
#: render.
TERMINAL_FORBIDDEN = (
    "investigation tools",
    "MUST",
    "Type A",
    "TYPE B",
    "TYPE C",
    "DEFAULT:",
    "Tool priority",
    "before responding",
    "evidence_collected",
    "evidence_to_add",
)

#: The synonyms of a tool, by the tool; a terminal message renders none of a
#: tool it does not give.
_IMPLIED_BY = {
    "kb_qa": MISSING_TOOL_IMPLYING["no_kb"],
    "case_evidence_search": MISSING_TOOL_IMPLYING["no_vector_stores"],
    "deep_analysis": MISSING_TOOL_IMPLYING["no_deep_analysis"],
}


@pytest.mark.parametrize("row", sorted(ROWS))
def test_the_terminal_da_message_names_only_its_offered_tools(row: str) -> None:
    g = terminal_given(row)
    if not g:
        # Nothing offered: the turn sends no tools, so no tool-loop system
        # message exists, and the builder refuses to make one.
        with pytest.raises(ValueError):
            _build_da_system_instruction([], "Schema", terminal=True)
        return
    text = _build_da_system_instruction(sorted(g), "Schema", terminal=True)
    assert tools_line(g) in text
    assert rendered_mentions(text) == g
    # Each offered tool has its own line, beyond the tools line.
    assert rendered_mentions(_without_tools_line(text)) == g
    assert [p for p in TERMINAL_FORBIDDEN if p in text] == []
    absent = [p for t, ps in _IMPLIED_BY.items() if t not in g for p in ps]
    assert synonym_hits(text, absent) == []


def test_the_terminal_da_message_column_is_not_vacuous() -> None:
    """The forbidden phrases are the investigating message's own: each one is
    in the default six's investigating render, so their absence above is the
    terminal branch, not a phrase that never renders anywhere."""
    six = _build_da_system_instruction(sorted(ROWS["six"]), "Schema")
    assert [p for p in TERMINAL_FORBIDDEN if p not in six] == []


#: sha256 of ``_build_da_system_instruction(sorted(row), "Schema")`` per row:
#: the investigating message as main renders it. #1964 gave the builder a
#: terminal branch and left this text byte-identical (first captured from
#: origin/main 069b9906e); recaptured at the merge of #1979 (6eb207216), whose
#: file_backed rename changed the FILE-BACKED EVIDENCE paragraph and nothing
#: else in seven rows (``none`` renders no search_file text). A change that
#: edits this text on purpose recaptures these (the failure prints the digest).
_MAIN_DA_INSTRUCTION_SHA256 = {
    "entity": "42fec33c62f252b06d3c57925288fde9828696be3be11a0ee2f44330156c90e8",
    "no_deep_analysis": (
        "802d56b602e77b7717017d974f23f7ff6272124b0dec5e1d6617b84735b5b12f"
    ),
    "no_kb": "23cc6cda1aba9e568d0b4f6041f1b4634571d7d5079134b562b5efe02c113873",
    "no_vector_stores": (
        "cac8b431002efe00985e81932ee62b60711c7cd584497ecb681e0fc2b2a8a465"
    ),
    "no_web_search": (
        "edf4d970c838a74544e2f6061c308cea1828328602ca994058f0a7370c5f0808"
    ),
    "none": "d936321c73db0ef7610e8e6d7f4709540d1dcace70f7f0f60e4a2d268386ed5d",
    "reclassify": "55f3bd558ca54615da65120881259cd406b2c4adb4cee16cf66946a2ca81a0b0",
    "six": "18c4a262faddb9e1e9741c082d1a148814692239cdcb60a979703efd450af66d",
}


def test_the_pinned_rows_are_the_matrix_rows() -> None:
    assert set(_MAIN_DA_INSTRUCTION_SHA256) == set(ROWS)


@pytest.mark.parametrize("row", sorted(ROWS))
def test_the_investigating_da_message_is_unchanged(row: str) -> None:
    text = _build_da_system_instruction(sorted(ROWS[row]), "Schema")
    assert text == _build_da_system_instruction(
        sorted(ROWS[row]), "Schema", terminal=False
    )
    digest = hashlib.sha256(text.encode()).hexdigest()
    assert digest == _MAIN_DA_INSTRUCTION_SHA256[row], digest


@pytest.mark.parametrize("condition", sorted(_TERMINAL))
def test_a_toolless_terminal_prompt_makes_no_claim_about_searchability(
    condition: str, monkeypatch
) -> None:
    _, prompt = _terminal_turn(condition, ROWS["none"], monkeypatch)
    assert not _searchability_claims(prompt), _searchability_claims(prompt)


# ---------------------------------------------------------------------------
# #1977 / #1994: what an EMPTY case_evidence_search tells the model
# ---------------------------------------------------------------------------
#
# The text is a tool result, not a prompt, so it is a column of its own: the
# real adapter over a real ``CaseVectorStore`` on an in-memory ChromaDB client
# (only the query embedding is stubbed), fed the tools one turn gives it and
# one case. The invariant: what it says is true for that case and names only
# tools the turn gives, and the search leaves no collection behind.

_EMBEDDER = "faultmaven.infrastructure.model_cache.model_cache.aembed_query"

#: What the text said before #1977. Each is false of a case whose uploads are
#: all under ``vectorization_min_size_bytes``: those files are never indexed,
#: so "nothing uploaded" and "still being indexed" are both wrong, and the
#: model could repeat either to the user.
_OLD_EMPTY_CLAIMS = (
    "no vectorized evidence",
    "nothing has been uploaded",
    "still being indexed",
    "5-15 seconds",
    "file summaries received",
)


def _with_case_evidence(g: frozenset[str]) -> bool:
    return "case_evidence_search" in g


#: The turns that can run the tool: every row giving it, and the terminal
#: turn's share of each (#1964). ``search_file`` is in every one of them (the
#: pin below), so the empty text always has a tool to point at; the text that
#: names none is the one a context with no turn gets.
_EMPTY_SEARCH_GIVEN: dict[str, frozenset[str]] = {
    **{row: ROWS[row] for row in ROWS if _with_case_evidence(ROWS[row])},
    **{
        f"{row}_terminal": terminal_given(row)
        for row in ROWS
        if _with_case_evidence(ROWS[row])
    },
}

_SMALL_UPLOAD_BYTES = 2_000


def _small_files_case():
    """Every upload is far under the vectorization floor: none is ever
    indexed, though one is recorded as evidence and one is not."""
    case = _case()
    case.uploaded_files = [
        f.model_copy(update={"size_bytes": _SMALL_UPLOAD_BYTES})
        for f in case.uploaded_files
    ]
    return case


def _no_upload_case():
    case = _case()
    case.uploaded_files = []
    case.evidence = []
    return case


def _unstored_row_case():
    """The only file is recorded as evidence but has no stored file
    (``storage_ref`` is None when the deployment has no file storage): there
    is nothing for ``search_file`` to read."""
    case = _small_files_case()
    case.uploaded_files = [
        case.uploaded_files[0].model_copy(update={"storage_ref": None})
    ]
    return case


def _unstored_row_beside_a_stored_one_case():
    """Two recorded rows: one with no stored file, one with. Only the stored
    one is offered."""
    case = _small_files_case()
    unstored, stored = case.uploaded_files
    case.uploaded_files = [unstored.model_copy(update={"storage_ref": None}), stored]
    case.evidence = [
        case.evidence[0],
        _evidence().model_copy(
            update={"evidence_id": "ev_193900000002", "source_file_id": stored.file_id}
        ),
    ]
    return case


#: case -> (builder, the ids a tool that reads its files can be pointed at, the
#: ids it must not be: they name a file with nothing stored behind it)
_EMPTY_SEARCH_CASES = {
    "small_files": (
        _small_files_case,
        {"ev_193900000001", "file_1939bbbbbbbb"},
        set(),
    ),
    "no_upload": (_no_upload_case, set(), set()),
    "unstored_row_only": (
        _unstored_row_case,
        set(),
        {"ev_193900000001", "file_1939aaaaaaaa"},
    ),
    "unstored_row_beside_stored": (
        _unstored_row_beside_a_stored_one_case,
        {"ev_193900000002"},
        {"ev_193900000001", "file_1939aaaaaaaa"},
    ),
}


def _ephemeral_client():
    """Pinned settings: chromadb refuses a second client for one cached System
    whose ``Settings`` differ (#823)."""
    return chromadb.EphemeralClient(
        settings=ChromaSettings(
            anonymized_telemetry=False,
            allow_reset=False,
            environment="",
            is_persistent=False,
        )
    )


def _empty_search(g: frozenset[str], case):
    """The real ``case_evidence_search`` over a case with no vectors: its
    result, the collections left behind, and the case it searched.

    The case id is unique, so a collection a regression leaves behind in this
    shared in-process store cannot be read as another test's."""
    case = case.model_copy(update={"case_id": f"case_1977{uuid.uuid4().hex[:12]}"})
    client = _ephemeral_client()
    adapter = CaseEvidenceQAAdapter(
        AnswerFromCaseEvidence(
            vector_store=CaseVectorStore(client=client), llm_router=MagicMock()
        )
    )
    context = ToolContext(
        session_id=case.case_id,
        case_id=case.case_id,
        enterprise_id=case.enterprise_id,
        user_id=case.user_id,
        in_memory_case=case,
        given_tools=g,
        redaction_ctx=disabled_redaction(),
    )
    try:
        with patch(_EMBEDDER, new=AsyncMock(return_value=[0.1] * 8)):
            result = asyncio.run(
                adapter.execute_with_context(
                    {"question": "what errors are in the log?"}, context
                )
            )
        return result, [c.name for c in client.list_collections()], case
    finally:
        try:
            client.delete_collection(f"case_{case.case_id}")
        except Exception:  # noqa: BLE001 - nothing was created: nothing to remove
            pass


def test_search_file_is_given_on_every_turn_that_can_run_a_tool() -> None:
    """The impossibility the empty-search columns rest on, asserted rather than
    assumed (#2004 review): ``search_file`` registers unconditionally, so no
    turn that gives any tool lacks it, terminal share included, and the only
    set without it is the tool-less one, on which no tool runs. A row that
    gives tools but not ``search_file`` fails here, and that row needs its own
    empty-search column and conditional tool text (``case_evidence_search``'s
    description and ``vectorize_file``'s results name ``search_file``)."""
    assert ROWS["none"] == frozenset()
    for row, g in ROWS.items():
        if g:
            assert "search_file" in g, row
            assert "search_file" in terminal_given(row), f"{row} (terminal)"
    # Non-vacuity: the columns below exist, and the terminal share is among them.
    assert _EMPTY_SEARCH_GIVEN
    assert {r for r in _EMPTY_SEARCH_GIVEN if r.endswith("_terminal")}


@pytest.mark.parametrize("case_name", sorted(_EMPTY_SEARCH_CASES))
@pytest.mark.parametrize("row", sorted(_EMPTY_SEARCH_GIVEN))
def test_an_empty_case_evidence_search_is_true_for_the_case_and_names_only_given_tools(
    row: str, case_name: str
) -> None:
    g = _EMPTY_SEARCH_GIVEN[row]
    build, readable, unreadable = _EMPTY_SEARCH_CASES[case_name]
    result, collections, case = _empty_search(g, build())
    text = result.data
    min_size = get_settings().agent.vectorization_min_size_bytes

    assert result.success is True
    # True for the case: its files are all under the floor, so none is ever
    # indexed, and the text says what the index holds and where the floor is,
    # read from settings. It claims nothing about uploads being absent or
    # about a file "still being indexed".
    assert all(f.size_bytes < min_size for f in case.uploaded_files)
    assert "No file of this case has been indexed for semantic search" in text
    assert f"files of at least {min_size:,} bytes that have been indexed" in text
    assert "a smaller file is never indexed" in text
    for claim in _OLD_EMPTY_CLAIMS:
        assert claim not in text.lower(), claim
    assert "searchab" not in text.lower()

    # Only a tool the turn gives is named: ``search_file`` (given on every
    # such turn), and only with a file it can read, listed as its own miss
    # lists them. A file with nothing stored behind it is never offered.
    named = rendered_mentions(text)
    assert named <= g and "search_file" in g
    assert not any(i in text for i in unreadable)
    if readable:
        assert named == {"search_file"}
        assert format_searchable_alternatives(case.evidence, "", case) in text
        assert all(i in text for i in readable)
    else:
        assert named == set()
        assert not any(f.storage_ref for f in case.uploaded_files)
        assert "This case has no uploaded file to read." in text

    # Searching created nothing (#1994).
    assert f"case_{case.case_id}" not in collections


def test_the_empty_search_floor_is_read_from_settings_not_written_in_the_text() -> None:
    stub = SimpleNamespace(agent=SimpleNamespace(vectorization_min_size_bytes=12_345))
    with patch(
        "faultmaven.modules.agent.tools.kb_configs.case_evidence_config.get_settings",
        return_value=stub,
    ):
        result, _, _ = _empty_search(ROWS["six"], _small_files_case())

    assert "files of at least 12,345 bytes that have been indexed" in result.data
    assert "50,000" not in result.data


@pytest.mark.parametrize("case_name", sorted(_EMPTY_SEARCH_CASES))
def test_a_context_built_without_a_turns_tools_names_none(case_name: str) -> None:
    """The default of ``ToolContext.given_tools`` is the tool-less set, so a
    context nobody stated a turn for cannot point the model at a tool or at a
    file: it says what the index holds and that the result shows nothing about
    uploads."""
    build, readable, unreadable = _EMPTY_SEARCH_CASES[case_name]
    result, collections, case = _empty_search(frozenset(), build())
    text = result.data

    assert rendered_mentions(text) == set()
    assert not any(i in text for i in readable | unreadable)
    assert "No file of this case has been indexed for semantic search" in text
    assert "does not show whether the case has uploaded files" in text
    assert f"case_{case.case_id}" not in collections
