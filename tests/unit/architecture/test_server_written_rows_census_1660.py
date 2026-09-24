"""Every read of a case's message rows, or of a turn record's reply summary, is
accounted for (#1660).

#1451's rule — no row the server wrote is rendered to a model as something a
party SAID — lives at each place that puts those rows in front of a model. PR
#1658 enforced it on the five surfaces the ruling named. Two more had no check
(the out-of-band triage prompt and the runbook-extraction prompt), and they
were found by reading, not by anything that would have failed. This is the
scan that found them, shipped so a seventh cannot arrive silently.

The unit is a READ SITE: a function that takes rows out of a case
(``case.messages``, ``get_messages``) or a reply summary out of a turn record
(``agent_response_summary``). Each one is declared below, with a count, as
either

* a PROMPT reader, naming the function that guards it and the names that guard
  must use — the two ``is_server_written_*`` predicates for rows, the
  ``agent_response_synthesized`` flag for a turn record; or
* another reader, with the reason it puts nothing in front of a model.

A new read site, a moved one or a changed count fails until it is declared.
Appends and ``len()`` are writes and counts, not reads of what was said, and
are not collected.

What this does NOT see, stated so nobody mistakes it for more: a renderer that
receives a list from a declared reader and quotes it (the behavioural tests of
each surface cover the existing ones); a declared non-prompt reader whose
RESULT a caller later hands to a model; raw SQL against ``case_messages``
outside the repositories; and a field reached through a variable
(``getattr(case, name)``).
"""

from __future__ import annotations

import ast
import warnings
from collections import Counter
from functools import lru_cache
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.architecture]

_ROOT = Path(__file__).resolve().parents[3]
_PACKAGE = _ROOT / "faultmaven"

#: What a read site reads.
_FIELDS = frozenset({"messages", "get_messages", "agent_response_summary"})

_USER = "is_server_written_user_row"
_ASSISTANT = "is_server_written_assistant_row"
_BOTH = frozenset({_USER, _ASSISTANT})
_FLAG = frozenset({"agent_response_synthesized"})

_CTX = "faultmaven/core/investigation/prompts/context_builder.py"
_CASE_SERVICE = "faultmaven/modules/case/domain/services/case_service.py"
_INVESTIGATION = "faultmaven/modules/agent/domain/services/investigation_service.py"

#: (file, function) -> (read count, {guarding function: names it must use}).
PROMPT_READERS: dict[tuple[str, str], tuple[int, dict[str, frozenset[str]]]] = {
    # The investigation prompt's RECENT window, the verbatim history it falls
    # back to, and the EARLIER TURNS preview it takes from a user row.
    (_CTX, "_build_graduated_history"): (
        1,
        {
            "_build_graduated_history": _BOTH,
            "_build_verbatim_history": _BOTH,
            "_preview_turn_from_messages": frozenset({_USER}),
        },
    ),
    (_CTX, "_build_turn_summary"): (2, {"_build_turn_summary": _FLAG}),
    (_CTX, "_build_compact_history"): (2, {"_build_compact_history": _FLAG}),
    # The auto-titler's context.
    (_CASE_SERVICE, "CaseService.get_case_conversation_context"): (
        1,
        {"CaseService.get_case_conversation_context": _BOTH},
    ),
    # The out-of-band triage prompt, and the greeting's "Where we left off".
    # It reads assistant rows only — it filters on role first.
    (
        "faultmaven/modules/agent/domain/services/orientation.py",
        "last_investigation_message",
    ): (
        1,
        {"last_investigation_message": frozenset({_ASSISTANT})},
    ),
    # The runbook-extraction prompt.
    (
        "faultmaven/modules/knowledge/domain/services/suggestion_service.py",
        "SuggestionService.extract_knowledge_from_case",
    ): (1, {"_extraction_transcript": _BOTH}),
}

_PERSISTENCE = "persistence: stores or loads the rows, renders nothing"
_WIRE = "the LLM request payload's own `messages`, not a case's rows"

#: (file, function) -> (read count, why it puts nothing in front of a model).
OTHER_READERS: dict[tuple[str, str], tuple[int, str]] = {
    (_INVESTIGATION, "InvestigationService._handle_file_reclassification"): (
        1,
        "copies the list onto a shallow case copy",
    ),
    (_CASE_SERVICE, "CaseService.get_case_messages_enhanced"): (
        1,
        "the transcript API: a page of rows for a client to render",
    ),
    (
        "faultmaven/modules/case/infrastructure/case_repository.py",
        "InMemoryCaseRepository.get_messages",
    ): (1, _PERSISTENCE),
    (
        "faultmaven/modules/case/infrastructure/case_repository.py",
        "InMemoryCaseRepository.save",
    ): (1, _PERSISTENCE),
    (
        "faultmaven/modules/case/infrastructure/postgresql_hybrid_case_repository.py",
        "PostgreSQLHybridCaseRepository.save",
    ): (1, _PERSISTENCE),
    (
        "faultmaven/modules/case/infrastructure/sessionless_case_repository.py",
        "SessionlessCaseRepository.get_messages",
    ): (1, _PERSISTENCE),
    (
        "faultmaven/modules/case/infrastructure/sqlite_case_repository.py",
        "SQLiteCaseRepository.save",
    ): (1, _PERSISTENCE),
    (
        "faultmaven/infrastructure/llm/providers/anthropic.py",
        "AnthropicProvider.generate",
    ): (
        4,
        _WIRE,
    ),
    (
        "faultmaven/infrastructure/llm/providers/local_provider.py",
        "LocalProvider._call_openai_compatible_api",
    ): (1, _WIRE),
    ("faultmaven/infrastructure/llm/router.py", "LLMRouter.generate"): (1, _WIRE),
    ("faultmaven/infrastructure/llm/router.py", "LLMRouter._update_opik_span"): (
        1,
        _WIRE,
    ),
}


def _is_write_or_count(node: ast.Attribute, parents: dict) -> bool:
    """``x.messages.append(...)`` / ``.extend`` / ``.insert``, or ``len(x.messages)``."""
    parent = parents.get(node)
    if (
        isinstance(parent, ast.Attribute)
        and parent.attr in {"append", "extend", "insert"}
        and isinstance(parents.get(parent), ast.Call)
    ):
        return True
    return (
        isinstance(parent, ast.Call)
        and isinstance(parent.func, ast.Name)
        and parent.func.id == "len"
    )


def _parse(source: str) -> ast.Module:
    # A module with an invalid escape in a non-raw string warns on every
    # parse; that is the module's business, not this census's.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", (DeprecationWarning, SyntaxWarning))
        return ast.parse(source)


def read_sites(source: str) -> Counter:
    """``{function qualname: reads}`` for one module's source."""
    tree = _parse(source)
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    found: Counter = Counter()

    def scope_of(node: ast.AST) -> str:
        names = []
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(node.name)
        return ".".join(reversed(names)) or "<module>"

    for node in ast.walk(tree):
        hit = False
        if isinstance(node, ast.Attribute) and node.attr in _FIELDS:
            hit = not _is_write_or_count(node, parents)
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            key = None
            if (
                isinstance(func, ast.Name)
                and func.id == "getattr"
                and len(node.args) >= 2
            ):
                key = node.args[1]
            elif isinstance(func, ast.Attribute) and func.attr == "get":
                key = node.args[0]
            hit = isinstance(key, ast.Constant) and key.value in _FIELDS
        elif isinstance(node, ast.Subscript):
            key = node.slice
            hit = isinstance(key, ast.Constant) and key.value in _FIELDS
        if hit:
            found[scope_of(node)] += 1
    return found


@lru_cache(maxsize=1)
def _scan() -> tuple[Counter, int]:
    sites: Counter = Counter()
    files = 0
    for path in sorted(_PACKAGE.rglob("*.py")):
        files += 1
        rel = path.relative_to(_ROOT).as_posix()
        for scope, count in read_sites(path.read_text(encoding="utf-8")).items():
            sites[(rel, scope)] = count
    return sites, files


def _function(path: str, qualname: str) -> ast.AST:
    node: ast.AST = _parse((_ROOT / path).read_text(encoding="utf-8"))
    for part in qualname.split("."):
        node = next(
            child
            for child in ast.iter_child_nodes(node)
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and child.name == part
        )
    return node


def _names_used(node: ast.AST) -> set[str]:
    return {
        n.id if isinstance(n, ast.Name) else n.attr
        for n in ast.walk(node)
        if isinstance(n, (ast.Name, ast.Attribute))
    }


class TestTheDetector:
    """Reach first: a scan that misses a shape the codebase writes reports a
    clean census over sites it never saw."""

    @pytest.mark.parametrize(
        "snippet",
        [
            "def f(case):\n    return [m for m in case.messages]",
            "def f(case):\n    return getattr(case, 'messages', None) or []",
            "async def f(repo):\n    return await repo.get_messages('c')",
            "def f(data):\n    return data['messages']",
            "def f(data):\n    return data.get('messages')",
            "def f(turn):\n    return turn.agent_response_summary",
            "def f(turn):\n    return turn.model_dump()['agent_response_summary']",
            "def f(case):\n    return list(case.messages)",
        ],
    )
    def test_every_read_shape_is_collected(self, snippet):
        assert sum(read_sites(snippet).values()) == 1, snippet

    @pytest.mark.parametrize(
        "snippet",
        [
            "def f(case, row):\n    case.messages.append(row)",
            "def f(case, rows):\n    case.messages.extend(rows)",
            "def f(case):\n    return len(case.messages)",
            "def f(case):\n    return case.message_count",
        ],
    )
    def test_writes_and_counts_are_not_reads(self, snippet):
        assert not read_sites(snippet), snippet

    def test_a_read_is_attributed_to_its_enclosing_function(self):
        src = "class C:\n    def m(self, case):\n        return case.messages[0]"
        assert read_sites(src) == Counter({"C.m": 1})


def test_the_scan_covers_the_package():
    """A positive control: a walk that stopped matching would report nothing
    to declare, which reads as a clean census."""
    sites, files = _scan()
    assert files > 400, f"scanned {files} files under {_PACKAGE}"
    assert set(PROMPT_READERS) <= set(sites), "a declared prompt reader was not found"


def test_every_read_site_is_declared():
    sites, _ = _scan()
    declared = {k: v[0] for k, v in {**PROMPT_READERS, **OTHER_READERS}.items()}

    undeclared = {k: n for k, n in sites.items() if k not in declared}
    stale = sorted(k for k in declared if k not in sites)
    recounted = {
        k: (declared[k], n)
        for k, n in sites.items()
        if k in declared and declared[k] != n
    }

    assert not undeclared, (
        "new reads of case message rows or a turn record's reply summary:\n  "
        + "\n  ".join(f"{f}::{s} ({n})" for (f, s), n in sorted(undeclared.items()))
        + "\nIf one puts text in front of a model, it must not quote a row the "
        "server wrote (#1434, #1451): apply is_server_written_user_row / "
        "is_server_written_assistant_row, or read agent_response_synthesized. "
        "Then declare it in PROMPT_READERS; otherwise in OTHER_READERS, with why."
    )
    assert not stale, f"declared but no longer read (moved or renamed?): {stale}"
    assert not recounted, (
        "the number of reads changed (declared, found) — look at the new one "
        f"before updating the count: {recounted}"
    )


@pytest.mark.parametrize("site", sorted(PROMPT_READERS), ids=lambda s: s[1])
def test_every_prompt_reader_is_guarded(site):
    path, _ = site
    _, guards = PROMPT_READERS[site]
    for qualname, required in guards.items():
        missing = required - _names_used(_function(path, qualname))
        assert not missing, f"{path}::{qualname} does not use {sorted(missing)}"
