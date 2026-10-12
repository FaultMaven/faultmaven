"""Every tool name the model reads is a tool the model is GIVEN (#1934).

The prompts are plain strings; nothing tied the tool names they use to the
registry the model's tool list is built from. They named ``case_evidence_qa``
(the internal module) while the tool list carried ``case_evidence_search``.

The given set comes from the real ``_create_investigation_tools`` fed bare
``AgentTool`` doubles (``.name`` is a constant property, so no dependencies are
needed). The vocabulary is every ``AgentTool`` subclass name plus retired
spellings. Model-facing strings are collected by AST from the prompt package,
the DA instruction / vectorization modules and the given tools' own modules
(docstrings excluded: they are for developers).

Flag-gated text is paired with its flag explicitly: the entity tools are given
only under FAULTMAVEN_ENTITY_REGISTRY, so their names may appear in text that
renders only under that flag. ``reclassify_evidence`` is never given (see
``_RECLASSIFY_HEDGE``).

Scope: names that are conditional only under non-default settings, and
tool-less turns, are #1939.
"""

from __future__ import annotations

import ast
import difflib
import importlib
import pkgutil
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import faultmaven
import faultmaven.modules.agent.tools as tools_pkg
from faultmaven.container.providers.services import _create_investigation_tools
from faultmaven.core.investigation.milestone_engine.tool_messages import (
    _build_da_system_instruction,
)
from faultmaven.modules.agent.tools.base import AgentTool
from faultmaven.modules.agent.tools.deep_analysis_tool import DeepAnalysisTool
from faultmaven.modules.agent.tools.find_entity_tool import FindEntityTool
from faultmaven.modules.agent.tools.kb_tool_adapter import (
    CaseEvidenceQAAdapter,
    KBToolAdapter,
)
from faultmaven.modules.agent.tools.list_top_entities_tool import ListTopEntitiesTool
from faultmaven.modules.agent.tools.search_file_tool import SearchFileTool
from faultmaven.modules.agent.tools.vectorize_file_tool import VectorizeFileTool
from faultmaven.modules.agent.tools.web_search import WebSearchTool

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
            "find_entity_tool.py",
            "list_top_entities_tool.py",
        )
    ],
]

_DEFAULT_TOOLS = dict(
    search_file_tool=SearchFileTool,
    deep_analysis_tool=DeepAnalysisTool,
    web_search_tool=WebSearchTool,
    kb_adapter=KBToolAdapter,
    vectorize_file_tool=VectorizeFileTool,
    case_evidence_qa_adapter=CaseEvidenceQAAdapter,
)
_ENTITY_TOOLS = dict(
    find_entity_tool=FindEntityTool, list_top_entities_tool=ListTopEntitiesTool
)
# Given only under FAULTMAVEN_ENTITY_REGISTRY; the text naming them renders
# only under that flag.
ENTITY_FLAG_NAMES = {"find_entity", "list_top_entities"}
# Never in the DA registry (its constructor needs the investigation service,
# which does not exist when the tool is built). blocks.py names it behind an
# explicit "if it is not in your available tools" hedge, and nowhere else.
_RECLASSIFY_HEDGE = {"reclassify_evidence": {"blocks.py"}}
_RETIRED = {"case_evidence_qa", "global_kb_qa", "user_kb_qa"}
_NOT_TOOLS = {"directed_analysis"}
_TOKEN = re.compile(r"(?<![\w.])[a-z]+(?:_[a-z]+)+(?![\w(]*\.)")


def _bare(cls):
    return object.__new__(cls)


def given(present: dict) -> set[str]:
    container = SimpleNamespace(**{k: _bare(c) for k, c in present.items()})
    registry = _create_investigation_tools(container)
    return {t.name for t in registry.get_all_tools()} if registry else set()


def _subclasses(cls):
    for sub in cls.__subclasses__():
        yield sub
        yield from _subclasses(sub)


def vocabulary() -> set[str]:
    for mod in pkgutil.iter_modules(tools_pkg.__path__):
        importlib.import_module(f"{tools_pkg.__name__}.{mod.name}")
    names = set()
    for cls in _subclasses(AgentTool):
        try:
            names.add(_bare(cls).name)
        except Exception:  # abstract / non-constant name
            pass
    return names | _RETIRED


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
    allowed = g | ENTITY_FLAG_NAMES  # entity text is flag-gated, see below
    not_given, near = {}, {}
    for tok, found in mentions(files).items():
        if tok in _NOT_TOOLS or tok in g:
            continue
        if tok in _RECLASSIFY_HEDGE and found <= _RECLASSIFY_HEDGE[tok]:
            continue
        if tok in ENTITY_FLAG_NAMES:
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


def test_entity_tools_are_given_only_under_their_flag() -> None:
    assert ENTITY_FLAG_NAMES <= given({**_DEFAULT_TOOLS, **_ENTITY_TOOLS})
    assert not ENTITY_FLAG_NAMES & given(_DEFAULT_TOOLS)


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
    # The only model-facing home of the entity names is text that renders when
    # FAULTMAVEN_ENTITY_REGISTRY is on (the caller of the highlights block
    # gates on it); their tools are given under exactly that flag.
    homes = set().union(
        *(found for n, found in mentions().items() if n in ENTITY_FLAG_NAMES)
    )
    assert homes <= {
        "entity_highlights.py",
        "find_entity_tool.py",
        "list_top_entities_tool.py",
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
