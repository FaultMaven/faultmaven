"""The investigation-sessions subsystem is retired, not deprecated (#1935).

Owner ruling 2026-10-12. The subsystem was the top of a Case →
InvestigationSession → AgentExecution → AgentToolCall hierarchy for an agent
orchestrator that no longer exists; the levels below it went in #982 and #1001,
and what it was meant to hold is kept where it is read: ``turn_history`` /
``case_messages`` / ``turn_receipts`` per period, the usage ledger for tokens,
the case state for pause and resume, the case's single driver (ADR-020) for "one
active session". Revision 017 drops the table, and the routes, service,
repository, models and ``ServiceFactory`` went with it.

This pins that no code path in ``faultmaven/`` can bring it back by halves: no
name, attribute, parameter, import or string that names one of the retired
symbols (``RETIRED``), the four ``sessions:*`` RBAC permissions included. The
match is on those names, not on the word "session":
the AUTH session — ``/api/v1/sessions``, the resume route
``/api/v1/cases/sessions/{session_id}/resume/{case_id}``,
``AuthSessionCreateRequest``, ``AuthSessionStatus`` — is a different thing and
must stay writable.

Read by AST, so comments (the contract changelog in ``api/contract_version.py``
records the removal, as history must) are not symbols. The alembic history is
outside the scanned tree: the baseline creates the table and 017 drops it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "faultmaven"

#: The retired subsystem's names, as they would reappear in code, SQL, a route
#: path or an RBAC permission. ``\b`` keeps ``AuthSessionCreateRequest`` (the auth mint's body)
#: out of the request-model arm.
RETIRED = re.compile(
    r"investigation_?session|session_goal|findings_summary"
    r"|total_agent_executions|total_token_usage|token_budget_limit"
    r"|service_?factory|\bSession(Create|Update)Request\b|SessionListResponse"
    r"|api\.routes\.sessions|cases/\{case_id\}/sessions"
    # The RBAC permissions seeded for it (``Permission.SESSIONS_*``).
    r"|\bsessions[:_](read|create|execute|manage)\b",
    re.IGNORECASE,
)


def _symbols(tree: ast.AST):
    """Every identifier and string literal in ``tree``, with its line."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            yield node.lineno, node.id
        elif isinstance(node, ast.Attribute):
            yield node.lineno, node.attr
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.lineno, node.name
        elif isinstance(node, ast.arg):
            yield node.lineno, node.arg
        elif isinstance(node, ast.keyword) and node.arg:
            yield node.value.lineno, node.arg
        elif isinstance(node, ast.alias):
            yield node.lineno, f"{node.name} {node.asname or ''}"
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.lineno, node.module
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


def _is_docstring(node: ast.AST) -> bool:
    return isinstance(node, ast.Expr) and isinstance(
        getattr(node, "value", None), ast.Constant
    )


def _retired_hits(tree: ast.AST) -> list[tuple[int, str]]:
    return [(line, text) for line, text in _symbols(tree) if RETIRED.search(text)]


def test_no_investigation_session_symbol_remains_in_faultmaven():
    files = sorted(SOURCE.rglob("*.py"))
    assert len(files) > 500, f"scanned the wrong tree: {SOURCE}"
    found = []
    for path in files:
        tree = ast.parse(path.read_text())
        # Docstrings are prose, like comments: drop them before scanning.
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body and _is_docstring(body[0]):
                body.pop(0)
        for lineno, text in _retired_hits(tree):
            found.append(f"{path.relative_to(ROOT)}:{lineno}: {text[:80]!r}")
    assert found == [], "investigation sessions are retired (#1935):\n" + "\n".join(
        found
    )


def test_the_scan_sees_a_planted_symbol():
    """Positive control: the walk reports a name, an import, a parameter, a
    table name and a route path."""
    tree = ast.parse(
        "from faultmaven.services.service_factory import ServiceFactory\n"
        "def f(session_goal):\n"
        "    return 'investigation_sessions'\n"
        "@router.get('/api/v1/cases/{case_id}/sessions/active')\n"
        "def g(body: SessionCreateRequest): ...\n"
        "SESSIONS_EXECUTE = 'sessions:execute'\n"
        "grant(Permission.SESSIONS_MANAGE)\n"
    )
    texts = [text for _, text in _retired_hits(tree)]
    assert len(texts) >= 9, texts


def test_the_auth_session_surface_is_not_flagged():
    """Negative control: the AUTH session's names are not the retired
    subsystem."""
    tree = ast.parse(
        "router.post('/api/v1/sessions')\n"
        "router.post('/api/v1/cases/sessions/{session_id}/resume/{case_id}')\n"
        "def create(request: AuthSessionCreateRequest, session_service): ...\n"
        "status = AuthSessionStatus.ACTIVE\n"
        "key = f'session:{session_id}:current_case_id'\n"
        'metric = \'active_sessions:{"service": "faultmaven"}\'\n'
        "perm = Permission.CASES_READ\n"
    )
    assert _retired_hits(tree) == []
