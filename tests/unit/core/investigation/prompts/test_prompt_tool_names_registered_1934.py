"""Every tool name an investigation prompt tells the model to call is a real one (#1934).

The prompts are plain strings; nothing checks that a tool they name exists. The
prompts named ``case_evidence_qa`` (the internal module / wrapped tool) while the
model's tool list carried ``case_evidence_search`` (``CaseEvidenceQAAdapter.name``),
so the instructions and the tool list disagreed on the name.

The registered vocabulary is derived from source (each tool's ``name`` property
returns a string literal), so a new or renamed tool moves the pin with it. The
scan covers the tool-name family that drifted (``*_qa`` / ``*_search`` tokens)
plus names known to be retired.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import faultmaven
from faultmaven.core.investigation.prompts import templates

pytestmark = pytest.mark.unit

_PKG = Path(faultmaven.__file__).parent
_TOOLS_DIR = _PKG / "modules" / "agent" / "tools"
_PROMPTS_DIR = Path(templates.__file__).parent.parent

# Retired spellings that must never reappear in model-facing text.
_RETIRED = {"case_evidence_qa", "global_kb_qa", "user_kb_qa"}
_TOOL_SHAPED = re.compile(r"\b[a-z]+(?:_[a-z]+)*_(?:qa|search)\b")


def _registered_tool_names() -> set[str]:
    names: set[str] = set()
    for path in _TOOLS_DIR.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.FunctionDef) and node.name == "name":
                for ret in ast.walk(node):
                    if isinstance(ret, ast.Return) and isinstance(
                        ret.value, ast.Constant
                    ):
                        names.add(ret.value.value)
    return names


def _prompt_mentions() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in sorted(_PROMPTS_DIR.rglob("*.py")):
        text = path.read_text()
        tokens = set(_TOOL_SHAPED.findall(text)) | {r for r in _RETIRED if r in text}
        for token in tokens:
            found.setdefault(token, set()).add(str(path.relative_to(_PKG)))
    return found


def test_registry_vocabulary_is_read_from_source() -> None:
    names = _registered_tool_names()
    # Positive control: the scan sees the tools it is meant to police.
    assert {"kb_qa", "search_file", "case_evidence_search"} <= names


def test_prompts_mention_the_scanned_family() -> None:
    # Positive control: an empty scan would pass vacuously.
    assert "kb_qa" in _prompt_mentions()


def test_every_tool_name_in_a_prompt_is_a_registered_tool() -> None:
    registered = _registered_tool_names()
    unknown = {
        token: sorted(files)
        for token, files in _prompt_mentions().items()
        if token not in registered
    }
    assert not unknown, (
        "prompt text names tools the model is not given (registered: "
        f"{sorted(registered)}): {unknown}"
    )
