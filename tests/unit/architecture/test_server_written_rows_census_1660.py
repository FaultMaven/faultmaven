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
(``agent_response_summary``) — by name, or by DUMPING the whole record:
``model_dump``/``model_dump_json``/``dict``/``json`` on a case or a turn record
whose include set, or implicit full dump, carries those fields, and
``dict(...)``, ``vars(...)``, ``.__dict__`` and ``json.dumps(...)`` over one.
Each site is declared below, with a count, as either

* a PROMPT reader, naming the function that guards it and what that guard must
  do — call an ``is_server_written_*`` predicate on the row it iterates, as a
  condition, or test ``agent_response_synthesized`` on the same record whose
  summary it reads; or
* another reader, with the reason it puts nothing in front of a model.

A new read site, a moved one or a changed count fails until it is declared.
Appends and ``len()`` are writes and counts, not reads of what was said, and
are not collected.

What this does NOT see, stated so nobody mistakes it for more:

* a dump whose receiver it cannot type. It knows a case by its name (``case``,
  ``*_case``, ``case_updated``) and a turn record by its name (``turn``,
  ``*_turn``, ``*record``), by indexing ``turn_history``, or by being the loop
  variable over ``turn_history``. ``c.model_dump()`` is invisible; an
  ``include`` that names a field is caught whatever the receiver;
* a renderer handed a list by a declared reader (each surface's behavioural
  tests cover the existing ones), and a declared non-prompt reader whose RESULT
  a caller later hands to a model;
* raw SQL against ``case_messages`` outside the repositories, and a field
  reached through a variable (``getattr(case, name)``);
* whether a guard's branch actually skips or marks the row. The guard check
  proves the predicate is applied, as a condition, to the row being iterated;
  what the branch then does is what each surface's behavioural tests prove.
"""

from __future__ import annotations

import ast
import re
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

#: What a whole-record dump carries: a case's rows, and its turn records (whose
#: summaries are reply text); a turn record's summary.
_CASE_CARRIES = frozenset({"messages", "turn_history"})
_TURN_CARRIES = frozenset({"agent_response_summary"})
_DUMP_METHODS = frozenset({"model_dump", "model_dump_json", "dict", "json"})
_CASE_NAME = re.compile(r"^(?:.*_)?case(?:_updated|_obj|_copy)?$")
_TURN_NAME = re.compile(r"^(?:.*_)?(?:turn|record)$")
_TURN_LISTS = frozenset({"turn_history", "turn_records"})

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
    # The rows it writes, and the whole-case re-validation before the write.
    (
        "faultmaven/modules/case/infrastructure/sqlite_case_repository.py",
        "SQLiteCaseRepository.save",
    ): (2, _PERSISTENCE),
    # Whole-record dumps into storage: the turn records a case row carries,
    # and the checkpoint snapshot (a hash and a stored copy, never a prompt).
    (
        "faultmaven/modules/case/infrastructure/sqlite_case_repository.py",
        "SQLiteCaseRepository._case_record_params",
    ): (1, _PERSISTENCE),
    (
        "faultmaven/modules/case/infrastructure/postgresql_hybrid_case_repository.py",
        "PostgreSQLHybridCaseRepository._case_record_params",
    ): (1, _PERSISTENCE),
    (
        "faultmaven/core/investigation/checkpoint_service.py",
        "CheckpointService.create_checkpoint",
    ): (
        3,
        _PERSISTENCE,
    ),
    (
        "faultmaven/infrastructure/llm/providers/anthropic.py",
        "AnthropicProvider.generate",
    ): (1, _WIRE),
    ("faultmaven/infrastructure/llm/router.py", "LLMRouter.generate"): (1, _WIRE),
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


def _ident(expr: ast.AST) -> str | None:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return None


def _turn_loop_names(tree: ast.AST) -> set[str]:
    """Names bound by ``for t in <x>.turn_history`` — loops and comprehensions."""
    return {
        node.target.id
        for node in ast.walk(tree)
        if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension))
        and isinstance(node.target, ast.Name)
        and _ident(node.iter) in _TURN_LISTS
    }


def _kind_of(expr: ast.AST, turn_names: set[str]) -> str | None:
    """``"case"``, ``"turn"`` or ``None`` for what a dump is taken of."""
    if isinstance(expr, ast.Subscript):
        return "turn" if _ident(expr.value) in _TURN_LISTS else None
    name = _ident(expr)
    if name is None:
        return None
    if isinstance(expr, ast.Name) and name in turn_names:
        return "turn"
    if _CASE_NAME.match(name):
        return "case"
    if _TURN_NAME.match(name):
        return "turn"
    return None


def _literal_keys(node: ast.AST | None) -> set | None:
    """The string keys of a literal ``include``/``exclude``; None if not literal."""
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        return {e.value for e in node.elts if isinstance(e, ast.Constant)}
    if isinstance(node, ast.Dict):
        return {k.value for k in node.keys if isinstance(k, ast.Constant)}
    return None


def _dump_carries(node: ast.Call, turn_names: set[str]) -> bool:
    """A dump of a case or a turn record that carries rows or reply summaries."""
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in _DUMP_METHODS:
        kind = _kind_of(func.value, turn_names)
        keywords = {k.arg: k.value for k in node.keywords if k.arg}
        if "include" in keywords:
            named = _literal_keys(keywords["include"])
            if named is None:  # an include it cannot read: trust the receiver
                return kind is not None
            return bool(named & (_CASE_CARRIES | _TURN_CARRIES))
        if kind is None:
            return False
        carried = _CASE_CARRIES if kind == "case" else _TURN_CARRIES
        return bool(carried - (_literal_keys(keywords.get("exclude")) or set()))
    if isinstance(func, ast.Name) and func.id in {"dict", "vars"} and node.args:
        return _kind_of(node.args[0], turn_names) is not None
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "dumps"
        and _ident(func.value) == "json"
        and node.args
    ):
        return _kind_of(node.args[0], turn_names) is not None
    return False


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

    turn_names = _turn_loop_names(tree)
    for node in ast.walk(tree):
        hit = False
        if isinstance(node, ast.Attribute):
            if node.attr in _FIELDS:
                hit = not _is_write_or_count(node, parents)
            elif node.attr == "__dict__":
                hit = _kind_of(node.value, turn_names) is not None
        elif isinstance(node, ast.Call):
            func = node.func
            key = None
            if (
                isinstance(func, ast.Name)
                and func.id == "getattr"
                and len(node.args) >= 2
            ):
                key = node.args[1]
            elif isinstance(func, ast.Attribute) and func.attr == "get" and node.args:
                key = node.args[0]
            hit = (isinstance(key, ast.Constant) and key.value in _FIELDS) or (
                _dump_carries(node, turn_names)
            )
        elif isinstance(node, ast.Subscript):
            # A subscript ASSIGNED to (``call_kwargs["messages"] = ...``) or
            # deleted builds a payload; it reads nothing. The attribute arm
            # already excludes writes, and #1667's tool loop tripped this one.
            key = node.slice
            hit = (
                isinstance(key, ast.Constant)
                and key.value in _FIELDS
                and not isinstance(node.ctx, (ast.Store, ast.Del))
            )
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


def _in_condition(node: ast.AST, parents: dict) -> bool:
    """Whether *node* is (part of) an ``if``/``while``/ternary test or a
    comprehension filter — through ``not``, ``and``/``or`` and comparisons."""
    child, parent = node, parents.get(node)
    while parent is not None:
        if isinstance(parent, (ast.If, ast.IfExp, ast.While)) and child is parent.test:
            return True
        if isinstance(parent, ast.comprehension) and child in parent.ifs:
            return True
        if not isinstance(parent, (ast.BoolOp, ast.UnaryOp, ast.Compare)):
            return False
        child, parent = parent, parents.get(parent)
    return False


def _applied_guards(fn: ast.AST) -> set[str]:
    """What *fn* actually applies, not merely mentions.

    * a predicate counts when it is CALLED on a variable the function iterates
      (a ``for``/comprehension target) and the call is a condition;
    * the flag counts when every ``X.agent_response_summary`` it reads has
      ``X.agent_response_synthesized`` tested as a condition on the same ``X``.
    """
    parents = {c: n for n in ast.walk(fn) for c in ast.iter_child_nodes(n)}
    rows = {
        t.id
        for n in ast.walk(fn)
        if isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension))
        for t in ast.walk(n.target)
        if isinstance(t, ast.Name)
    }
    applied = {
        n.func.id
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id in _BOTH
        and n.args
        and isinstance(n.args[0], ast.Name)
        and n.args[0].id in rows
        and _in_condition(n, parents)
    }
    summaries = {
        ast.dump(n.value)
        for n in ast.walk(fn)
        if isinstance(n, ast.Attribute) and n.attr == "agent_response_summary"
    }
    tested = {
        ast.dump(n.value)
        for n in ast.walk(fn)
        if isinstance(n, ast.Attribute)
        and n.attr == "agent_response_synthesized"
        and _in_condition(n, parents)
    }
    if summaries and summaries <= tested:
        applied |= _FLAG
    return applied


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
            "def f(row):\n    return row.model_dump()['agent_response_summary']",
            "def f(case):\n    return list(case.messages)",
            # Whole-record dumps. The first two are the shapes review probed
            # into the aside and triage prompts; both passed the old scan.
            "def f(case):\n    return json.dumps(case.model_dump(include={'messages'}))",
            "def f(case):\n    return case.turn_history[-1].model_dump("
            "include={'turn_number', 'agent_response_summary'})",
            "def f(c):\n    return c.model_dump(include={'messages': True})",
            "def f(c):\n    return c.model_dump(include={'turn_history'})",
            "def f(case, keys):\n    return case.model_dump(include=keys)",
            "def f(case):\n    return case.model_dump()",
            "def f(updated_case):\n    return updated_case.model_dump_json()",
            "def f(case):\n    return case.model_dump(exclude={'messages'})",
            "def f(case):\n    return case.turn_history[-1].model_dump()",
            "def f(case):\n    return [t.model_dump() for t in case.turn_history]",
            "def f(case):\n    for t in case.turn_history:\n        yield t.dict()",
            "def f(last_turn):\n    return last_turn.model_dump(mode='json')",
            "def f(case):\n    return dict(case)",
            "def f(turn):\n    return vars(turn)",
            "def f(case):\n    return case.__dict__",
            "def f(case):\n    return json.dumps(case, default=str)",
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
            # Building an LLM request payload assigns a "messages" KEY; it
            # reads no case row. #1667's tool loop tripped the census here.
            "def f(kwargs, msgs):\n    kwargs['messages'] = msgs",
            "def f(payload):\n    del payload['messages']",
        ],
    )
    def test_writes_and_counts_are_not_reads(self, snippet):
        assert not read_sites(snippet), snippet

    @pytest.mark.parametrize(
        "snippet",
        [
            "def f(case):\n    return case.model_dump(include={'title', 'state'})",
            "def f(case):\n    return case.model_dump(exclude={'messages', 'turn_history'})",
            "def f(turn):\n    return turn.model_dump(exclude={'agent_response_summary'})",
            "def f(case):\n    return case.inquiry.model_dump()",
            "def f(evidence):\n    return evidence.model_dump()",
            "def f(response):\n    return response.json()",
            "def f(payload):\n    return json.dumps(payload)",
        ],
    )
    def test_a_dump_that_carries_neither_field_is_not_a_read(self, snippet):
        assert not read_sites(snippet), snippet

    def test_a_read_is_attributed_to_its_enclosing_function(self):
        src = "class C:\n    def m(self, case):\n        return case.messages[0]"
        assert read_sites(src) == Counter({"C.m": 1})


class TestTheGuardCheck:
    """The check a prompt reader must pass is "applied", not "mentioned": the
    old form passed a function that named the predicate and ignored it."""

    @staticmethod
    def _applied(src: str) -> set[str]:
        return _applied_guards(_parse(src).body[0])

    @pytest.mark.parametrize(
        "src",
        [
            "def f(rows):\n    for m in rows:\n"
            "        if is_server_written_user_row(m):\n            continue",
            "def f(rows):\n    return [m for m in rows"
            " if not is_server_written_user_row(m)]",
            "def f(rows):\n    for i, m in enumerate(rows):\n"
            "        if x and is_server_written_user_row(m):\n            pass",
        ],
    )
    def test_a_predicate_applied_to_the_iterated_row_counts(self, src):
        assert self._applied(src) == {_USER}

    @pytest.mark.parametrize(
        "src",
        [
            # called, result discarded
            "def f(rows):\n    for m in rows:\n        is_server_written_user_row(m)",
            # a condition, but not on the row being iterated
            "def f(rows, meta):\n    for m in rows:\n"
            "        if is_server_written_user_row(meta):\n            continue",
            # only mentioned
            "def f(rows):\n    guard = is_server_written_user_row\n"
            "    return [m for m in rows]",
        ],
    )
    def test_a_predicate_merely_present_does_not(self, src):
        assert self._applied(src) == set()

    def test_the_flag_counts_when_it_gates_the_same_record(self):
        src = (
            "def f(turn):\n    if turn.agent_response_synthesized:\n        return ''\n"
            "    return turn.agent_response_summary"
        )
        assert self._applied(src) == _FLAG

    @pytest.mark.parametrize(
        "src",
        [
            "def f(turn, other):\n    if other.agent_response_synthesized:\n"
            "        return ''\n    return turn.agent_response_summary",
            "def f(turn):\n    seen = turn.agent_response_synthesized\n"
            "    return turn.agent_response_summary",
        ],
    )
    def test_the_flag_does_not_when_it_gates_something_else(self, src):
        assert self._applied(src) == set()


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
    """Applied, not mentioned: see ``_applied_guards``. What the guarded branch
    then does with the row is not checked here — the behavioural tests of each
    surface are what pin that it skips or marks it."""
    path, _ = site
    _, guards = PROMPT_READERS[site]
    for qualname, required in guards.items():
        missing = required - _applied_guards(_function(path, qualname))
        assert not missing, f"{path}::{qualname} does not apply {sorted(missing)}"
