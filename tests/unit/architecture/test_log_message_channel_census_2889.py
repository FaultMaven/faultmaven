"""No log line or print carries an exception's text or a subprocess's output (#2889).

Principles §7, "What a log line may hold", rule 1
(``docs/architecture/core-architecture/architectural-design-principles.md``):
"A log line holds operational metadata ... A message that interpolates an
exception's text, a payload, or a user-supplied value is not metadata. A
first-party exception contributes its class and its ``error_code`` ..., not its
message." Stage S3 of #2886. The rule was stated (#2893) and a new site still
landed a day later (``fa8fc53c0``, #2919), so the census is a guard.

**A site** is any of these, anywhere under ``faultmaven/``:

* **A log call carrying an exception's text.** A log call is an attribute call
  whose method is in :data:`LOG_METHODS` or starts with ``log_``, on any
  receiver but those :func:`_not_a_log_call` names (``math.log``, an argparse
  parser's ``error``, a task's or future's zero-argument ``.exception()``). It
  carries the text when a positional argument or a keyword value reads an
  exception name other than through a safe form.
* **A ``print``**, by the same test.
* **Subprocess output** (``.stderr`` or ``.stdout`` on anything but ``sys``) in a
  log call's or a print's arguments, other than through a safe form.
* **A call** to ``format_exc``, ``format_exception`` or ``print_exc``.
* **``SystemExit(<non-literal>)``**: the interpreter prints it (§7's outputs
  table, the ``SystemExit`` row).

**Exception names** are the names bound by ``except ... as N``, a function's
parameters annotated ``...Error``/``...Exception`` or unannotated and named
``e``/``err``/``error``/``exc``/``exception``, and, one level deep, the names
assigned in the same function from an expression reading one of those
(``msg = str(e)``, ``cause = e.__cause__``) or from a zero-argument
``.exception()`` (``failure = task.exception()``).

**Safe forms**: a call in :data:`SAFE_CALLS` (``exception_log_name(e)``,
``type(e)``, ``len(result.stderr)``, ...); an attribute in :data:`SAFE_ATTRS`
(``e.status_code``, ``e.returncode``, ...); an ``e is None`` test. An
``error_code`` is not one: a third party's can be any text, and only
``exception_log_name`` renders a code, after checking it. ``exc_info=``, and an
exception (not an alias holding its text) passed bare as a keyword field
(``error=e``, an ``extra`` value), are not sites: rule 4's processor renders
those as class chains (``infrastructure/logging/config.py``).

``exit_refusing(str(exc))`` (``cli/_database_gate.py``) is not a site: §7 names
its shape, a refusal on stderr whose text our code chose, as the compliant one,
and the census counts no call to it (:data:`NOT_SITES`).

**Blind spots**, stated so nobody mistakes this for more: text laundered
through two names (``a = str(e); b = a``), a container, or a parameter typed
``str`` (``error: str`` holding a caller's ``str(e)``); the worker child's
``failure`` frame, whose text arrives as data; a payload that is not an
exception or a subprocess's output (a response body). **Over-counts**: an alias
is any name assigned from an exception, so one holding a flag or a count
computed from it (``cancelled = _arrived_cancelled(failure)``) is read as text.

:data:`ALLOWED` lists, file by file, the sites the later sub-stages of S3 switch.
A new site, a file not listed, or a count that moved either way fails: a
switched site shrinks its row, and the last sub-stage deletes the table. No
file under ``faultmaven/infrastructure/llm/`` is listed (S3a, #2889).
"""

from __future__ import annotations

import ast
import re
import textwrap
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import FrozenSet, List, Tuple

import pytest

from tests.source_tree import parse_file

pytestmark = [pytest.mark.unit, pytest.mark.architecture]

PACKAGE = Path(__file__).resolve().parents[3] / "faultmaven"

LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
    | {"fatal"}
)

#: Calls whose result names an exception, or measures a text, without its text.
SAFE_CALLS = frozenset(
    {
        "type",
        "isinstance",
        "id",
        "len",
        "class_name",
        "exception_class_name",
        "exception_class_chain",
        "exception_log_name",
        "exception_frames",
        "logged_exception",
        "declared_llm_category",
    }
)

#: Attributes of an exception or a finished process that hold a number, a flag
#: or a fixed code, never text a third party chose.
SAFE_ATTRS = frozenset(
    {
        "status_code",
        "errno",
        "returncode",
        "retryable",
        "pgcode",
        "sqlstate",
        "__class__",
        "__name__",
    }
)

#: Callees whose text §7 names as compliant.
NOT_SITES = frozenset({"exit_refusing"})

#: The names an unannotated parameter holding an exception goes by.
EXCEPTION_PARAM_NAMES = frozenset({"e", "err", "error", "exc", "exception"})

_EXCEPTION_ANNOTATION = re.compile(r"(Error|Exception|BaseException)\b")
_LOGGING_KEYWORDS = frozenset({"exc_info", "stack_info", "stacklevel"})
_PRINT_KEYWORDS = frozenset({"file", "end", "sep", "flush"})
_TRACEBACK_TEXT = frozenset({"format_exc", "format_exception", "print_exc"})
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _callee(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


def _receiver_name(func: ast.Attribute) -> str:
    receiver = func.value
    if isinstance(receiver, ast.Attribute):
        return receiver.attr
    return receiver.id if isinstance(receiver, ast.Name) else ""


def _is_task_exception(call: ast.Call) -> bool:
    """``task.exception()``: a task's or future's exception, not a log call."""
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "exception"
        and not call.args
        and not call.keywords
    )


def _not_a_log_call(call: ast.Call) -> bool:
    receiver = _receiver_name(call.func)
    return (
        receiver == "math"
        or receiver == "parser"
        or receiver.endswith("_parser")
        or _is_task_exception(call)
    )


def _is_log_call(call: ast.Call) -> bool:
    func = call.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr not in LOG_METHODS and not func.attr.startswith("log_"):
        return False
    return not _not_a_log_call(call)


def _is_print(call: ast.Call) -> bool:
    return isinstance(call.func, ast.Name) and call.func.id == "print"


class _Reads(ast.NodeVisitor):
    """Whether an expression reads a name in ``names``, or subprocess output,
    other than through a safe form."""

    def __init__(self, names: FrozenSet[str]):
        self.names = names
        self.name_hit = False
        self.output_hit = False

    def visit_Call(self, node: ast.Call) -> None:
        if _callee(node) not in SAFE_CALLS:
            self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        """``exc is not None``: a test, not text on the line."""

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in SAFE_ATTRS and isinstance(node.value, ast.Name):
            return
        if node.attr in ("stderr", "stdout") and not (
            isinstance(node.value, ast.Name) and node.value.id == "sys"
        ):
            self.output_hit = True
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in self.names:
            self.name_hit = True


def _reads(node: ast.AST, names: FrozenSet[str]) -> _Reads:
    reads = _Reads(names)
    reads.visit(node)
    return reads


def _argument_values(call: ast.Call, printing: bool, exceptions: FrozenSet[str]):
    """The argument expressions a line's text is built from."""
    yield from call.args
    for keyword in call.keywords:
        if keyword.arg in _LOGGING_KEYWORDS:
            continue
        if printing and keyword.arg in _PRINT_KEYWORDS:
            continue
        value = keyword.value
        if isinstance(value, ast.Name) and value.id in exceptions:
            continue  # a field: rendered as its class chain (rule 4)
        if keyword.arg == "extra" and isinstance(value, ast.Dict):
            for item in value.values:
                if not (isinstance(item, ast.Name) and item.id in exceptions):
                    yield item
            continue
        yield value


class _Names:
    """The exception names in force: the exceptions themselves, and the
    one-level aliases that may hold their text."""

    def __init__(self, exceptions: FrozenSet[str] = frozenset(), texts=frozenset()):
        self.exceptions = frozenset(exceptions)
        self.texts = frozenset(texts)

    @property
    def all(self) -> FrozenSet[str]:
        return self.exceptions | self.texts

    def adding(self, exceptions=(), texts=()) -> "_Names":
        return _Names(self.exceptions | set(exceptions), self.texts | set(texts))


def _exception_params(scope) -> set:
    found = set()
    arguments = scope.args
    for arg in arguments.posonlyargs + arguments.args + arguments.kwonlyargs:
        annotation = arg.annotation
        if annotation is not None:
            if _EXCEPTION_ANNOTATION.search(ast.unparse(annotation)):
                found.add(arg.arg)
        elif arg.arg in EXCEPTION_PARAM_NAMES:
            found.add(arg.arg)
    return found


def _is_exception_valued(value: ast.AST, exceptions: FrozenSet[str]) -> bool:
    """``x = e``, ``x = e.__cause__``, ``x = task.exception()``: an exception,
    rendered as its class chain when passed as a field."""
    if isinstance(value, ast.Name):
        return value.id in exceptions
    if isinstance(value, ast.Attribute) and value.attr in ("__cause__", "__context__"):
        return isinstance(value.value, ast.Name) and value.value.id in exceptions
    return isinstance(value, ast.Call) and _is_task_exception(value)


def _scope_aliases(scope: ast.AST, names: _Names) -> _Names:
    """The names assigned, anywhere in ``scope`` outside a nested function,
    from an exception name in force where the assignment stands: one level,
    flow-insensitive."""
    exceptions, texts = set(), set()

    def visit(node: ast.AST, in_force: _Names) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPES):
                continue
            here = in_force
            if isinstance(child, ast.ExceptHandler) and child.name:
                here = in_force.adding(exceptions={child.name})
            if isinstance(child, (ast.Assign, ast.AnnAssign)) and child.value:
                targets = (
                    child.targets if isinstance(child, ast.Assign) else [child.target]
                )
                bound = {
                    leaf.id
                    for target in targets
                    for leaf in ast.walk(target)
                    if isinstance(leaf, ast.Name) and isinstance(leaf.ctx, ast.Store)
                }
                if _is_exception_valued(child.value, here.exceptions):
                    exceptions.update(bound)
                elif _reads(child.value, here.all).name_hit or any(
                    isinstance(sub, ast.Call) and _is_task_exception(sub)
                    for sub in ast.walk(child.value)
                ):
                    texts.update(bound)
            visit(child, here)

    visit(scope, names)
    return names.adding(exceptions=exceptions, texts=texts)


Site = Tuple[int, str]


def _call_site(call: ast.Call, names: _Names) -> str:
    """The kind of site ``call`` is, or ``""``."""
    callee = _callee(call)
    if callee in NOT_SITES:
        return ""
    if callee in _TRACEBACK_TEXT:
        return "traceback-text"
    if (
        callee == "SystemExit"
        and call.args
        and not isinstance(call.args[0], ast.Constant)
    ):
        return "systemexit-text"
    printing = _is_print(call)
    if not (printing or _is_log_call(call)):
        return ""
    name_hit = output_hit = False
    for value in _argument_values(call, printing, names.exceptions):
        reads = _reads(value, names.all)
        name_hit = name_hit or reads.name_hit
        output_hit = output_hit or reads.output_hit
    if name_hit:
        return "print" if printing else "log"
    return "subprocess-text" if output_hit else ""


def scan_tree(tree: ast.Module) -> List[Site]:
    """Every census site in ``tree``, as ``(line, kind)``."""
    sites: List[Site] = []

    def walk(node: ast.AST, names: _Names) -> None:
        for child in ast.iter_child_nodes(node):
            here = names
            if isinstance(child, ast.ExceptHandler) and child.name:
                here = names.adding(exceptions={child.name})
            if isinstance(child, _SCOPES):
                here = _scope_aliases(
                    child, names.adding(exceptions=_exception_params(child))
                )
            if isinstance(child, ast.Call):
                kind = _call_site(child, here)
                if kind:
                    sites.append((child.lineno, kind))
            walk(child, here)

    walk(tree, _scope_aliases(tree, _Names()))
    return sites


def scan_source(source: str) -> List[Site]:
    return scan_tree(ast.parse(source))


@lru_cache(maxsize=1)
def census() -> Tuple[Tuple[str, int, str], ...]:
    """Every site under ``faultmaven/``, as ``(path, line, kind)``, the path
    relative to the package. A file that does not parse fails the scan
    (``SyntaxError``), never counts as clean."""
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE).as_posix()
        for line, kind in scan_tree(parse_file(path)):
            found.append((relative, line, kind))
    return tuple(found)


#: Sites per file, pending S3's later sub-stages (#2889). Exact: a switched
#: site shrinks its row; a row at zero is deleted.
ALLOWED = {
    "_container_impl.py": 4,
    "api/exception_handlers.py": 9,
    "api/middleware/auth.py": 6,
    "api/middleware/client_ip.py": 1,
    "api/middleware/contract_probe.py": 2,
    "api/middleware/deduplication.py": 4,
    "api/middleware/idempotency.py": 6,
    "api/middleware/logging.py": 3,
    "api/middleware/performance.py": 3,
    "api/middleware/rate_limiting.py": 4,
    "api/middleware/tenant_scope.py": 1,
    "api/operator_audit.py": 1,
    "api/protection.py": 3,
    "api/routes/admin.py": 7,
    "api/routes/admin_cases.py": 1,
    "api/routes/admin_config.py": 7,
    "api/v1/auth_dependencies.py": 8,
    "bootstrap/composition.py": 4,
    "bootstrap/data_init.py": 8,
    "bootstrap/kb_generation_sweep.py": 3,
    "bootstrap/kb_incomplete_sweep.py": 3,
    "bootstrap/kb_init.py": 12,
    "bootstrap/kb_orphan_sweep.py": 5,
    "bootstrap/kb_pack.py": 2,
    "bootstrap/lifespan.py": 11,
    "bootstrap/middleware.py": 5,
    "bootstrap/startup.py": 2,
    "cli/backfill_case_outcome.py": 1,
    "cli/demote_platform_admin.py": 4,
    "cli/personal_tenant.py": 6,
    "cli/promote_platform_admin.py": 2,
    "cli/provision_sso_org.py": 10,
    "cli/purge_traces.py": 2,
    "cli/reassign_cases.py": 4,
    "cli/remove_org_member.py": 5,
    "cli/reset_kb.py": 4,
    "cli/set_turn_cap.py": 1,
    "cli/wipe_deployment.py": 3,
    "config/investigation_capability.py": 2,
    "config/llm_config_overrides.py": 6,
    "config/revocation_storage.py": 1,
    "config/secret_file.py": 4,
    "config/settings.py": 1,
    "config/tls_root_set.py": 9,
    "container/providers/infrastructure.py": 5,
    "container/providers/services.py": 12,
    "container/providers/tools.py": 6,
    "container/utils.py": 2,
    "core/investigation/case_telemetry.py": 3,
    "core/investigation/llm_error_handler.py": 7,
    "core/investigation/milestone_engine/engine.py": 2,
    "core/investigation/milestone_engine/generation.py": 9,
    "core/investigation/milestone_engine/runbook_creation.py": 4,
    "core/investigation/milestone_engine/statement_revision.py": 1,
    "core/investigation/milestone_engine/structured_output.py": 1,
    "core/investigation/milestone_engine/terminal_turns.py": 2,
    "core/investigation/milestone_engine/tool_messages.py": 1,
    "core/investigation/milestone_engine/turn_application.py": 2,
    "core/investigation/milestone_engine/turn_generation.py": 1,
    "core/investigation/milestone_engine/vectorization.py": 3,
    "core/investigation/prompts/context_builder/entity_highlights.py": 1,
    "core/investigation/terminal_transitions.py": 1,
    "core/preprocessing/tier2/local_service.py": 1,
    "core/preprocessing/vector_storage.py": 1,
    "core/processing/log_analyzer.py": 4,
    "core/processing/pattern_learner.py": 3,
    "infrastructure/auth/database_user_store.py": 8,
    "infrastructure/auth/user_store.py": 15,
    "infrastructure/base_client.py": 4,
    "infrastructure/caching/intelligent_cache.py": 13,
    "infrastructure/concurrency/teardown.py": 1,
    "infrastructure/embedding_guard.py": 1,
    "infrastructure/error_recovery.py": 4,
    "infrastructure/health/component_monitor.py": 4,
    "infrastructure/index_slot.py": 1,
    "infrastructure/jobs/job_service.py": 7,
    "infrastructure/knowledge/knowledge_vector_store.py": 4,
    "infrastructure/knowledge/runbook_kb.py": 2,
    "infrastructure/logging/helpers.py": 1,
    "infrastructure/model_cache.py": 2,
    "infrastructure/observability/alerting.py": 4,
    "infrastructure/observability/apm_integration.py": 3,
    "infrastructure/observability/apm_metrics.py": 1,
    "infrastructure/observability/confidence_service.py": 6,
    "infrastructure/observability/dashboard_service.py": 8,
    "infrastructure/observability/funnel_metrics.py": 1,
    "infrastructure/observability/metrics_collector.py": 15,
    "infrastructure/observability/metrics_exporters/prometheus.py": 1,
    "infrastructure/observability/opik_sdk.py": 5,
    "infrastructure/observability/performance_monitoring.py": 8,
    "infrastructure/observability/sla_monitor.py": 19,
    "infrastructure/observability/startup.py": 14,
    "infrastructure/observability/tracing.py": 2,
    "infrastructure/persistence/case_vector_store.py": 4,
    "infrastructure/persistence/chromadb_store.py": 1,
    "infrastructure/persistence/pg_connection.py": 1,
    "infrastructure/protection/rate_limiter.py": 8,
    "infrastructure/protection/tenant_turn_cap.py": 1,
    "infrastructure/redis_client.py": 1,
    "infrastructure/security/case_redaction.py": 3,
    "infrastructure/security/pseudonym_key.py": 1,
    "infrastructure/security/redaction.py": 3,
    "infrastructure/shims/security.py": 2,
    "infrastructure/storage/filesystem.py": 1,
    "infrastructure/tasks/case_cleanup.py": 6,
    "infrastructure/tasks/job_runners.py": 4,
    "infrastructure/tasks/llm_usage_retention.py": 1,
    "infrastructure/worker_process.py": 2,
    "jobs/case_cleanup.py": 1,
    "jobs/kb_seed.py": 1,
    "jobs/kb_vector_enterprise_backfill.py": 2,
    "jobs/llm_usage_retention.py": 1,
    "jobs/run.py": 4,
    "main.py": 27,
    "modules/agent/domain/services/investigation_service/attachments.py": 1,
    "modules/agent/domain/services/investigation_service/service.py": 2,
    "modules/agent/domain/services/investigation_service/turn_settlement.py": 1,
    "modules/agent/jobs/storage_cleanup.py": 3,
    "modules/agent/tools/base.py": 1,
    "modules/agent/tools/deep_analysis_tool.py": 1,
    "modules/agent/tools/find_entity_tool.py": 1,
    "modules/agent/tools/kb_tool_adapter.py": 2,
    "modules/agent/tools/list_top_entities_tool.py": 1,
    "modules/agent/tools/reclassify_evidence_tool.py": 2,
    "modules/agent/tools/search_file_tool.py": 2,
    "modules/agent/tools/vectorize_file_tool.py": 1,
    "modules/auth/api/auth.py": 14,
    "modules/auth/api/oauth.py": 8,
    "modules/auth/api/session.py": 15,
    "modules/auth/contracts.py": 1,
    "modules/auth/domain/services/auth_service.py": 3,
    "modules/auth/domain/services/jwt_token_generator.py": 9,
    "modules/auth/domain/services/user_service.py": 2,
    "modules/auth/infrastructure/metrics/oauth_metrics.py": 9,
    "modules/auth/infrastructure/stores/redis_session_store.py": 10,
    "modules/case/api/routes/cases.py": 17,
    "modules/case/api/routes/conversation.py": 12,
    "modules/case/api/routes/data.py": 2,
    "modules/case/api/routes/evidence.py": 4,
    "modules/case/api/routes/reports.py": 6,
    "modules/case/api/routes/sharing.py": 2,
    "modules/case/api/title_generation.py": 7,
    "modules/case/api/turn_idempotency.py": 1,
    "modules/case/domain/services/case_data_ingestion_service.py": 11,
    "modules/case/domain/services/case_driver.py": 1,
    "modules/case/domain/services/case_service.py": 27,
    "modules/case/infrastructure/case_metadata_reader.py": 1,
    "modules/case/infrastructure/postgresql_hybrid_case_repository/loading.py": 2,
    "modules/case/infrastructure/postgresql_hybrid_case_repository/rows.py": 2,
    "modules/case/infrastructure/sessionless_case_repository.py": 4,
    "modules/case/infrastructure/sqlite_case_repository/loading.py": 3,
    "modules/case/infrastructure/sqlite_case_repository/repository.py": 1,
    "modules/case/infrastructure/sqlite_case_repository/rows.py": 2,
    "modules/evidence/domain/services/file_storage_service.py": 7,
    "modules/knowledge/api/conversion_routes.py": 4,
    "modules/knowledge/api/routes.py": 13,
    "modules/knowledge/domain/services/conversion_service/runbook_scan.py": 3,
    "modules/knowledge/domain/services/conversion_service/service.py": 5,
    "modules/knowledge/domain/services/document_parser.py": 3,
    "modules/knowledge/domain/services/document_preprocessor.py": 3,
    "modules/knowledge/domain/services/knowledge_service.py": 20,
    "modules/knowledge/domain/services/runbook_gate.py": 1,
    "modules/knowledge/infrastructure/persistence/knowledge_item_repository.py": 13,
    "modules/preprocessing/preprocessing_service.py": 2,
    "modules/report/api/routes.py": 7,
    "modules/report/domain/services/report_generation_service.py": 1,
    "modules/report/domain/services/report_recommendation_service.py": 1,
    "server.py": 1,
    "services/base.py": 4,
    "utils/frontmatter.py": 1,
    "utils/model_context.py": 1,
    "utils/schema_converter.py": 1,
    "utils/token_estimation.py": 3,
}


RULE = (
    "Principles §7 rule 1: a log line or print names an exception by "
    "exception_log_name(e) (faultmaven.utils.exception_name), never its text, "
    "and subprocess output by exit code and length (#2889)."
)


def test_the_scan_reads_the_package():
    assert len(list(PACKAGE.rglob("*.py"))) > 500
    assert census(), "the census found no site at all: the scan is not reading"


def test_each_file_holds_exactly_its_allowed_sites():
    counts = Counter(path for path, _, _ in census())
    problems = []
    for path in sorted(set(counts) | set(ALLOWED)):
        found, allowed = counts.get(path, 0), ALLOWED.get(path, 0)
        if found == allowed:
            continue
        lines = ", ".join(f"{n} ({kind})" for p, n, kind in census() if p == path)
        row = f'"{path}": {found},' if found else "delete its row"
        problems.append(
            f"{path}: {found} sites, {allowed} allowed (lines {lines or 'none'}). "
            + ("Name the exception by its class instead. " if found > allowed else "")
            + f"Row: {row}"
        )
    assert not problems, RULE + "\n" + "\n".join(problems)


def test_the_switched_area_is_not_on_the_list():
    """S3a switched every site under ``infrastructure/llm/``: a site there is
    never allowed, so reverting one fails the census as an unlisted file."""
    assert not [path for path in ALLOWED if path.startswith("infrastructure/llm/")]
    assert all(count > 0 for count in ALLOWED.values())


# ── the census definition, on source the test controls ────────────────────


def _found(source: str) -> List[Site]:
    return scan_source(textwrap.dedent(source))


@pytest.mark.parametrize(
    "source",
    [
        "try:\n    pass\nexcept Exception as e:\n    logger.error(f'x {e}')",
        "try:\n    pass\nexcept Exception as e:\n    logger.warning('x %s', e)",
        "try:\n    pass\nexcept Exception as e:\n    log.log_once('k', error=str(e))",
        "def f(exc: ServiceError):\n    logger.info('x', extra={'d': str(exc)})",
        "try:\n    pass\nexcept Exception as exc:\n    print(f'{exc}')",
        "try:\n    pass\nexcept Exception as e:\n    logger.info(f'{type(e).__name__}: {e}')",
        "try:\n    pass\nexcept Exception as e:\n    logger.info(f'{e.error_code}')",
        "try:\n    pass\nexcept Exception as e:\n    _diag.warning(f'x {e}')",
        "try:\n    pass\nexcept Exception as e:\n    msg = str(e)\nlogger.error(msg)",
        "try:\n    pass\nexcept Exception as e:\n    msg = f'x {e}'\n    log.warning('x', error=msg)",
        "def f(e):\n    cause = e.__cause__\n    logger.info(f'{cause}')",
        "def f(task):\n    failure = task.exception()\n    logger.warning('%s', failure)",
        "def f(e):\n    logger.error(f'failed: {e.stderr}')",
        "logger.info(f'output: {result.stdout}')",
        "raise SystemExit(f'refusing: {path}')",
        "traceback.print_exc()",
    ],
)
def test_a_site_is_found(source):
    assert _found(source), source


@pytest.mark.parametrize(
    "source",
    [
        "try:\n    pass\nexcept Exception as e:\n    logger.error(f'{exception_log_name(e)}')",
        "try:\n    pass\nexcept Exception as e:\n    log.warning('x', error=e)",
        "try:\n    pass\nexcept Exception as e:\n    logger.info('x', extra={'e': e})",
        "try:\n    pass\nexcept Exception as e:\n    logger.info(type(e).__name__)",
        "try:\n    pass\nexcept Exception as e:\n    logger.info('x', exc_info=e)",
        "def f(e):\n    logger.error(f'exit={e.returncode}, stderr_len={len(e.stderr)}')",
        "logger.error(f'stderr_len={len(result.stderr)}')",
        "def f(e):\n    logger.info(f'len={len(str(e))}')",
        "def f(exc):\n    logger.info('x' + (' y' if exc is not None else ''))",
        "def f(exc):\n    exit_refusing(str(exc))",
        "print('refusing to run', file=sys.stderr)",
        "def f(task):\n    task.exception()",
        "def f(e):\n    math.log(e)",
        "def f(e):\n    parser.error(f'{e}')",
        "raise SystemExit('refusing to run')",
    ],
)
def test_a_safe_form_is_not_a_site(source):
    assert not _found(source), source


def test_a_file_that_does_not_parse_fails_the_scan(tmp_path):
    broken = tmp_path / "broken.py"
    broken.write_text("def f(:\n")
    with pytest.raises(SyntaxError):
        scan_tree(parse_file(broken))
