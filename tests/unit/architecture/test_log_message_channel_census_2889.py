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
  parser's ``error``, a task's or future's zero-argument ``.exception()``), or a
  call to a module function in :data:`LOG_HELPERS`, which forwards its
  arguments to a logger (``_report_once(key, message, *args)``). It
  carries the text when a positional argument or a keyword value reads an
  exception name, or calls a zero-argument ``.exception()``, other than through
  a safe form.
* **A ``print``** or an argparse parser's ``error`` (``parser``, ``*_parser``),
  which prints its message, by the same test; ``print``'s ``end=`` and ``sep=``
  are read too.
* **Subprocess output** (``.stderr`` or ``.stdout`` on anything but ``sys``) in a
  log call's or a print's arguments, or in an exit's code (below), other than
  through a safe form.
* **A call** to ``format_exc``, ``format_exception``, ``format_exception_only``,
  ``print_exc`` or ``print_exception``, or ``.format()`` on a
  ``TracebackException(...)`` or ``TracebackException.from_exception(...)``.
* **``SystemExit(<code>)``** and **``sys.exit(<code>)``** whose first positional
  argument may be text (:func:`_may_be_text`): an f-string, a ``+`` or ``%``
  operation, a ``str``/``format``/``repr`` call, or an expression that reads an
  exception name or subprocess output other than through a safe form; an
  ``if``-expression counts when either branch does, and its test is not read.
  The interpreter prints a non-integer code (§7's outputs table, the
  ``SystemExit`` row). A literal is our text, and is not a site.

**Exception names** are the names bound by ``except ... as N``, a function's
parameters annotated ``...Error``/``...Exception``/``...ExceptionGroup`` or
unannotated and named ``e``/``err``/``error``/``exc``/``exception``, and, one
level deep, the names
assigned in the same function (``=``, an annotated ``=``, ``+=`` or ``:=``)
from an expression reading one of those (``msg = str(e)``,
``cause = e.__cause__``), a zero-argument ``.exception()``
(``failure = task.exception()``) or a ``gather(..., return_exceptions=True)``.
A ``for`` loop's targets are exceptions when the iterable itself (past
``await``, ``enumerate`` and ``zip``) is a tracked exception's ``.exceptions``
or such a ``gather``, and texts over any other read of those names
(``e.args``, ``str(e).splitlines()``, ``str(results[0]).splitlines()``).

**Safe forms**: a call in :data:`SAFE_CALLS` (``exception_log_name(e)``,
``type(e)``, ``len(result.stderr)``, ``_arrived_cancelled(e)``, which returns a
``bool``, ``describe_request_body(exc.body)``, a body's type and size, ...); an
attribute in :data:`SAFE_ATTRS` (``e.status_code``,
``e.returncode``, ``e.item_id``, the knowledge item id our code sets on
``IngestOutcomeUnknown``, ``e.limit_type``, ``e.current_count``, ``e.limit`` and
``e.retry_after``, the limit and counts our code sets on ``RateLimitError``,
``e.lookup``, the fixed label (``user_id``, ``username`` or ``email``) our code
sets on ``UserLookupFailed``, whose ``identifier`` stays unsafe, ...), or read
by name
(``getattr(e, "status_code", None)``); an ``e is None`` test. An
``error_code`` is not one: a third party's can be any text, and only
``exception_log_name`` renders a code, after checking it. ``exc_info=``, and an
exception (not a name that also holds its text) passed bare as a keyword field
(``error=e``, an ``extra`` value), are not sites: rule 4's processor renders
those as class chains (``infrastructure/logging/config.py``). An exception
passed bare as ``self.log_error``'s ``error`` argument is not a site either:
``BaseService.log_error`` renders it as a class chain and frames. The pruned
paths ``_prune_invalid_sub_records`` returns (position 1 of its tuple) are
schema locations our code built, not text: :data:`NOT_EXCEPTION_RESULTS`.

``exit_refusing(str(exc))`` (``cli/_database_gate.py``) is not a site: §7 names
its shape, a refusal on stderr whose text our code chose, as the compliant one,
and the census counts no call to it (:data:`NOT_SITES`).

**Blind spots**, stated as what the scanner reads, so nobody mistakes this for
more. A log call's or a print's arguments are read for the exception names the
scanner tracks and a zero-argument ``.exception()`` (above), and for subprocess
output; text that reaches them through an untracked name is not counted. For an
exit (``SystemExit(...)``, ``sys.exit(...)``), only the first positional
argument is read, and the exit is a site only in the forms listed above;
nothing else about an exit makes it a site (its other arguments, a first
argument in any other form, or an exit spelled any other way). The allowlist
counts sites per file, so switching one site and adding another in the same
file leaves the count, and the guard, unchanged. A name filled in place, by a
method call (``logged.append(str(e))``) or a subscript store
(``d[k] = str(e)``), is not tracked, so a projection built that way and then
logged is not counted.

:data:`ALLOWED` lists, file by file, the sites the later sub-stages of S3 switch.
A new site, a file not listed, or a count that moved either way fails: a
switched site shrinks its row, and the last sub-stage deletes the table. No
file under a prefix in :data:`SWITCHED_PREFIXES` is listed.
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

#: Module functions that forward their arguments to a logger call
#: (``api/middleware/route_policy.py``, ``infrastructure/concurrency/owned_claim.py``).
LOG_HELPERS = frozenset({"_report_once"})

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
        "classify_token_limit_reason",
        "_arrived_cancelled",
        "describe_request_body",
    }
)

#: Attributes of an exception or a finished process that hold a number, a flag,
#: a fixed code or an identifier our code set, never text a third party chose.
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
        "item_id",
        "limit_type",
        "current_count",
        "limit",
        "retry_after",
        "lookup",
    }
)

#: Callees whose text §7 names as compliant.
NOT_SITES = frozenset({"exit_refusing"})

#: Calls whose tuple result holds, at the listed positions, no exception text:
#: a name unpacked there is not tracked; every other position is.
NOT_EXCEPTION_RESULTS = {"_prune_invalid_sub_records": frozenset({1})}

#: The names an unannotated parameter holding an exception goes by.
EXCEPTION_PARAM_NAMES = frozenset({"e", "err", "error", "exc", "exception"})

_EXCEPTION_ANNOTATION = re.compile(r"(Error|Exception|ExceptionGroup)\b")
_LOGGING_KEYWORDS = frozenset({"exc_info", "stack_info", "stacklevel"})
_PRINT_KEYWORDS = frozenset({"file", "flush"})
_TRACEBACK_TEXT = frozenset(
    {"format_exc", "format_exception", "format_exception_only", "print_exc"}
    | {"print_exception"}
)
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


def _is_parser(func: ast.Attribute) -> bool:
    receiver = _receiver_name(func)
    return receiver == "parser" or receiver.endswith("_parser")


def _not_a_log_call(call: ast.Call) -> bool:
    return (
        _receiver_name(call.func) == "math"
        or _is_parser(call.func)
        or _is_task_exception(call)
    )


def _is_log_call(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in LOG_HELPERS
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr not in LOG_METHODS and not func.attr.startswith("log_"):
        return False
    return not _not_a_log_call(call)


def _is_print(call: ast.Call) -> bool:
    """``print``, or an argparse parser's ``error``, which prints its message."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "print"
    return isinstance(func, ast.Attribute) and func.attr == "error" and _is_parser(func)


def _is_safe_getattr(call: ast.Call) -> bool:
    """``getattr(e, "status_code", None)``: a safe attribute, read by name."""
    args = call.args
    return (
        isinstance(call.func, ast.Name)
        and call.func.id == "getattr"
        and len(args) >= 2
        and isinstance(args[0], ast.Name)
        and isinstance(args[1], ast.Constant)
        and args[1].value in SAFE_ATTRS
    )


def _is_gather_of_exceptions(node: ast.AST) -> bool:
    """``gather(..., return_exceptions=True)``: its result holds exceptions."""
    return (
        isinstance(node, ast.Call)
        and _callee(node) == "gather"
        and any(
            k.arg == "return_exceptions"
            and isinstance(k.value, ast.Constant)
            and k.value.value is True
            for k in node.keywords
        )
    )


class _Reads(ast.NodeVisitor):
    """Whether an expression reads a name in ``names``, or subprocess output,
    other than through a safe form."""

    def __init__(self, names: FrozenSet[str]):
        self.names = names
        self.name_hit = False
        self.output_hit = False

    def visit_Call(self, node: ast.Call) -> None:
        if _is_task_exception(node):
            self.name_hit = True  # ``t.exception()`` read inline
        elif _is_safe_getattr(node):
            for default in node.args[2:]:
                self.visit(default)
        elif _callee(node) not in SAFE_CALLS:
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


def _on_self(call: ast.Call) -> bool:
    """``self.method(...)``: the receiver is the bare name ``self``."""
    receiver = call.func.value if isinstance(call.func, ast.Attribute) else None
    return isinstance(receiver, ast.Name) and receiver.id == "self"


def _argument_values(call: ast.Call, printing: bool, names: "_Names"):
    """The argument expressions a line's text is built from. A name passed bare
    as a field is skipped only when it is an exception and never its text."""
    exceptions = names.exceptions - names.texts
    for position, arg in enumerate(call.args):
        if position == 1 and _callee(call) == "log_error" and _on_self(call):
            if isinstance(arg, ast.Name) and arg.id in exceptions:
                continue  # ``BaseService.log_error``: a class chain and frames
        yield arg
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


def _bound(targets) -> set:
    return {
        leaf.id
        for target in targets
        for leaf in ast.walk(target)
        if isinstance(leaf, ast.Name) and isinstance(leaf.ctx, ast.Store)
    }


def _iterates_exceptions(it: ast.AST, exceptions: FrozenSet[str], gathered: set):
    """The iterable itself, past ``await`` and ``enumerate``/``zip``, is a tracked
    exception's ``.exceptions``, a ``gather(..., return_exceptions=True)``, or a
    name assigned one: its loop binds exceptions."""
    if isinstance(it, ast.Await):
        return _iterates_exceptions(it.value, exceptions, gathered)
    if isinstance(it, ast.Call) and _callee(it) in ("enumerate", "zip"):
        return any(_iterates_exceptions(a, exceptions, gathered) for a in it.args)
    if isinstance(it, ast.Attribute) and it.attr == "exceptions":
        return isinstance(it.value, ast.Name) and it.value.id in exceptions
    return _is_gather_of_exceptions(it) or (
        isinstance(it, ast.Name) and it.id in gathered
    )


def _not_exception_results(targets, value: ast.AST) -> set:
    """The names a tuple unpacks, from a call in :data:`NOT_EXCEPTION_RESULTS`,
    at the positions it lists."""
    if not (isinstance(value, ast.Call) and len(targets) == 1):
        return set()
    if not isinstance(targets[0], ast.Tuple):
        return set()
    if any(isinstance(elt, ast.Starred) for elt in targets[0].elts):
        return set()  # a starred target shifts the positions: exempt nothing
    positions = NOT_EXCEPTION_RESULTS.get(_callee(value), frozenset())
    return {
        elt.id
        for i, elt in enumerate(targets[0].elts)
        if i in positions and isinstance(elt, ast.Name)
    }


def _scope_aliases(scope: ast.AST, names: _Names) -> _Names:
    """The names assigned, anywhere in ``scope`` outside a nested function,
    from an exception name in force where the assignment stands, then the
    targets of a loop: exceptions over exceptions (:func:`_iterates_exceptions`),
    texts over any other read of a tracked name (``e.args``,
    ``str(e).splitlines()``). One level, flow-insensitive."""
    exceptions, texts, gathered = set(), set(), set()

    def visit(node: ast.AST, in_force: _Names, loops: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPES):
                continue
            here = in_force
            if isinstance(child, ast.ExceptHandler) and child.name:
                here = in_force.adding(exceptions={child.name})
            assigned = (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)
            if not loops and isinstance(child, assigned) and child.value:
                targets = getattr(child, "targets", None) or [child.target]
                bound = _bound(targets) - _not_exception_results(targets, child.value)
                if _is_exception_valued(child.value, here.exceptions):
                    exceptions.update(bound)
                elif any(_is_gather_of_exceptions(n) for n in ast.walk(child.value)):
                    texts.update(bound)
                    gathered.update(bound)
                elif _reads(child.value, here.all).name_hit:
                    texts.update(bound)
            if loops and isinstance(child, (ast.For, ast.AsyncFor)):
                if _iterates_exceptions(child.iter, here.exceptions, gathered):
                    exceptions.update(_bound([child.target]))
                elif _reads(child.iter, here.all).name_hit:
                    texts.update(_bound([child.target]))
            visit(child, here, loops)

    visit(scope, names, loops=False)
    visit(scope, names.adding(exceptions=exceptions, texts=texts), loops=True)
    return names.adding(exceptions=exceptions, texts=texts)


def _may_be_text(code: ast.AST, names: _Names) -> bool:
    """An exit code the interpreter would print: an f-string, a ``+`` or ``%``,
    a ``str``/``format``/``repr`` call, or a read of an exception name or of
    subprocess output, in either branch of an ``if``-expression. ``main()``,
    ``0 if ok else 1``, ``base | 1`` and a constant are not."""
    if isinstance(code, ast.IfExp):
        return _may_be_text(code.body, names) or _may_be_text(code.orelse, names)
    reads = _reads(code, names.all)
    return (
        isinstance(code, ast.JoinedStr)
        or (isinstance(code, ast.BinOp) and isinstance(code.op, (ast.Add, ast.Mod)))
        or (isinstance(code, ast.Call) and _callee(code) in ("str", "format", "repr"))
        or reads.name_hit
        or reads.output_hit
    )


Site = Tuple[int, str]


def _call_site(call: ast.Call, names: _Names) -> str:
    """The kind of site ``call`` is, or ``""``."""
    callee = _callee(call)
    if callee in NOT_SITES:
        return ""
    if callee in _TRACEBACK_TEXT:
        return "traceback-text"
    func = call.func
    if (
        isinstance(func, ast.Attribute)
        and callee in ("format", "format_exception_only")
        and isinstance(func.value, ast.Call)
        and _callee(func.value) in ("TracebackException", "from_exception")
    ):
        return "traceback-text"
    exiting = callee == "SystemExit" or (
        callee == "exit"
        and isinstance(func, ast.Attribute)
        and _receiver_name(func) == "sys"
    )
    if exiting and call.args and _may_be_text(call.args[0], names):
        return "systemexit-text"
    printing = _is_print(call)
    if not (printing or _is_log_call(call)):
        return ""
    name_hit = output_hit = False
    for value in _argument_values(call, printing, names):
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
    "bootstrap/composition.py": 4,
    "bootstrap/data_init.py": 8,
    "bootstrap/kb_generation_sweep.py": 3,
    "bootstrap/kb_incomplete_sweep.py": 3,
    "bootstrap/kb_init.py": 11,
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
    "infrastructure/base_client.py": 4,
    "infrastructure/caching/intelligent_cache.py": 13,
    "infrastructure/concurrency/teardown.py": 1,
    "infrastructure/embedding_guard.py": 1,
    "infrastructure/error_recovery.py": 4,
    "infrastructure/health/component_monitor.py": 4,
    "infrastructure/index_slot.py": 1,
    "infrastructure/jobs/job_service.py": 7,
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
    "infrastructure/redis_client.py": 1,
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


#: The areas S3 has switched (#2889): no file under one is on :data:`ALLOWED`.
SWITCHED_PREFIXES = (
    "infrastructure/llm/",
    "core/investigation/",
    "modules/agent/",
    "modules/evidence/",
    "modules/preprocessing/",
    "core/preprocessing/",
    "core/processing/",
    "modules/knowledge/",
    "infrastructure/knowledge/",
    "api/",
    "infrastructure/protection/",
    "main.py",
    "modules/auth/",
    "infrastructure/auth/",
    "infrastructure/security/",
)


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
    """S3 switched every site under :data:`SWITCHED_PREFIXES`: a site there is
    never allowed, so reverting one fails the census as an unlisted file."""
    assert not [path for path in ALLOWED if path.startswith(SWITCHED_PREFIXES)]
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
        "def f(t):\n    logger.warning('failed: %r', t.exception())",
        "def f(e):\n    for m in e.exceptions:\n        logger.info(f'{m}')",
        "async def f():\n    rs = await gather(a(), return_exceptions=True)\n    for r in rs:\n        logger.info(f'{r}')",
        "async def f():\n    for r in await gather(a(), return_exceptions=True):\n        logger.info(f'{r}')",
        "def f(e):\n    for x in e.args:\n        logger.info(f'{x}')",
        "def f(e):\n    for x in e.args:\n        log.warning('x', arg=x)",
        "def f(e):\n    for line in str(e).splitlines():\n"
        "        logger.info('x', extra={'line': line})",
        "def f(e):\n    sys.exit(e)",
        "sys.exit('refusing: {}'.format(path))",
        "sys.exit('refusing: ' + path)",
        "sys.exit('refusing: %s' % path)",
        "def f(e):\n    for line in str(e.exceptions[0]).splitlines():\n"
        "        log.warning('x', line=line)",
        "def f():\n    r = run()\n    sys.exit(r.stderr)",
        "sys.exit(f'bad {p}' if c else 0)",
        "async def f():\n    rs = await gather(a(), return_exceptions=True)\n"
        "    for line in str(rs[0]).splitlines():\n        log.warning('x', line=line)",
        "async def f():\n    rs = await gather(a(), return_exceptions=True)\n"
        "    for line in str(rs).split(','):\n        log.warning('x', extra={'line': line})",
        "traceback.print_exception(e)",
        "traceback.format_exception_only(type(e), e)",
        "lines = traceback.TracebackException.from_exception(e).format()",
        "lines = traceback.TracebackException(type(e), e, None).format()",
        "sys.exit(f'refusing: {path}')",
        "def f(e):\n    msg = 'x'\n    msg += str(e)\n    logger.info(msg)",
        "def f(e):\n    if (m := str(e)):\n        logger.info(m)",
        "try:\n    pass\nexcept Exception as e:\n    e = str(e)\n    log.warning('x', error=e)",
        "def f(e):\n    parser.error(f'{e}')",
        "def f(e):\n    retire_parser.error(f'{e}')",
        "def f(e):\n    print('x', end=str(e))",
        "def f(e):\n    print('x', sep=f'{e}')",
        "def f(group: ExceptionGroup):\n    log.warning(f'{group}')",
        "def f(group: BaseExceptionGroup):\n    log.warning(f'{group}')",
        "try:\n    pass\nexcept Exception as e:\n    self.log_error('op', e, detail=str(e))",
        "try:\n    pass\nexcept Exception as e:\n    helper.log_error('op', e)",
        "def f(c, e, s):\n    obj, paths, rest = _prune_invalid_sub_records(c, e, s)\n"
        "    log.warning(f'{rest}')",
        "def f(c, e, s):\n    obj, *_, rest = _prune_invalid_sub_records(c, e, s)\n"
        "    log.warning(f'{rest}')",
        "def f(c, e, s):\n    *_, rest = _prune_invalid_sub_records(c, e, s)\n"
        "    log.warning(f'{rest}')",
        "def f(e):\n    log.error('%s', e.message)",
        "def f(exc: RequestValidationError):\n    log.error('%s', exc.errors())",
        "def f(error: RateLimitError):\n    log.warning('%s', error.message)",
        "try:\n    pass\nexcept Exception as exc:\n"
        "    log.error('x', extra={'lookup': exc.args})",
        "try:\n    pass\nexcept Exception as exc:\n    _report_once('k', 'x (%s)', exc)",
        "def f(exc: RequestValidationError):\n    body = getattr(exc, 'body', None)\n"
        "    log.error('x', extra={'body': body})",
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
        "def f(e):\n    parser.error('refusing to run')",
        "def f(e):\n    print('x', end='\\n', sep=' ')",
        "def f(e):\n    logger.info(f'{getattr(e, \"status_code\", None)}')",
        "raise SystemExit('refusing to run')",
        "sys.exit(1)",
        "sys.exit(main())",
        "sys.exit(0 if ok else 1)",
        "sys.exit(base | 1)",
        "def f(e):\n    sys.exit(1 if e else 0)",
        "def f(e, names):\n    for n, m in zip(names, e.exceptions):\n"
        "        log.warning('x', error=m)",
        "def f(e):\n    for m in e.exceptions:\n        log.warning('x', error=m)",
        "def f(e):\n    for i, m in enumerate(e.exceptions):\n        log.warning('x', error=m)",
        "async def f():\n    rs = await gather(a(), return_exceptions=True)\n"
        "    for r in rs:\n        log.warning('x', error=r)",
        "try:\n    pass\nexcept Exception as e:\n    self.log_error('op', e, key=k)",
        "def f(e):\n    reason = classify_token_limit_reason(e)\n"
        "    log.warning('x %s', reason)",
        "def f(c, e, s):\n    obj, paths, rest = _prune_invalid_sub_records(c, e, s)\n"
        "    log.warning(f'{paths}')",
        "try:\n    g()\nexcept Exception as failure:\n"
        "    cancelled = _arrived_cancelled(failure)\n"
        "    log.warning('%s', 'a' if cancelled else 'b')",
        "def f(e):\n    log.error('item %s', e.item_id)",
        "def f(error: RateLimitError):\n    log.warning(f'{error.limit_type} "
        "{error.current_count}/{error.limit} {error.retry_after}')",
        "try:\n    pass\nexcept Exception as exc:\n"
        "    log.error('x', extra={'lookup': exc.lookup})",
        "try:\n    pass\nexcept Exception as exc:\n"
        "    _report_once('k', 'x (%s)', exception_log_name(exc))",
        "def f(exc):\n    _report_once('k', 'x %s', 'literal')",
    ],
)
def test_a_safe_form_is_not_a_site(source):
    assert not _found(source), source


def test_a_file_that_does_not_parse_fails_the_scan(tmp_path):
    broken = tmp_path / "broken.py"
    broken.write_text("def f(:\n")
    with pytest.raises(SyntaxError):
        scan_tree(parse_file(broken))


#: The fm#1156 validation handler's shape (``api/exception_handlers.py``): a
#: projection built from ``exc.errors()`` by ``logged.append(...)``, logged with
#: the body's shape.
_PROJECTION = (
    "def f(exc: RequestValidationError):\n    raw_errors = exc.errors()\n"
    "    body = getattr(exc, 'body', None)\n    logged = []\n"
    "    for e, raw in zip(raw_errors, raw_errors):\n"
    "        logged.append({0})\n"
    "    log.error('x %s', logged, extra={{'validation_errors': logged,"
    " 'body': describe_request_body(body)}})"
)


def test_describe_request_body_is_a_safe_call():
    """``body`` is a text alias of ``exc``; read through
    ``describe_request_body`` it is not a site, read bare it is."""
    projection = _PROJECTION.format("{'loc': e.get('loc'), 'input_len': len(raw)}")
    assert not _found(projection)
    assert _found(projection.replace("describe_request_body(body)", "body"))


def test_a_projection_appending_exception_text_is_a_blind_spot():
    """The stated blind spot: ``logged.append(str(e))`` builds ``logged``
    by a method call, which the scanner does not track, so the census does not
    count it. A line like this is kept off by review, not by this guard."""
    assert not _found(_PROJECTION.format("str(e)"))
