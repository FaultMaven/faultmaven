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
* **A construction of an audited refusal** (``AUDITED_REFUSALS`` in
  ``cli/_refusal.py``, matched by the class's bare name, or by a keyword whose
  value in the same file is one, ``misscoped=MembershipRemovalMisscoped`` then
  ``misscoped(...)``), and ``super().__init__(...)`` in such a class's body,
  whose arguments read an exception name or subprocess output other than
  through a safe form: ``refusal_text`` prints that class's message whole, so
  the message may hold no exception's text.

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
``type(e)``, ``len(result.stderr)``, ``_arrived_cancelled(e)`` and
``counts_against_breaker(e)``, which return a ``bool``,
``describe_request_body(exc.body)``, a body's type and size,
``_provider_codes(e)``, a first-party status and a pattern-checked code, ...);
``refusal_text(e)`` (:data:`CLI_SAFE_CALLS`), which returns an audited
refusal's sentence, else the class name, in a file under ``cli/`` anywhere but
a log call's arguments, and in an audited refusal's construction in any file;
an
attribute in :data:`SAFE_ATTRS` (``e.status_code``,
``e.returncode``, ``e.item_id``, the knowledge item id our code sets on
``IngestOutcomeUnknown``, ``e.limit_type``, ``e.current_count``, ``e.limit`` and
``e.retry_after``, the limit and counts our code sets on ``RateLimitError``,
``e.lookup``, the fixed label (``user_id``, ``username`` or ``email``) our code
sets on ``UserLookupFailed``, whose ``identifier`` stays unsafe, ...), and in
the files :data:`FILE_SAFE_ATTRS` names, the ids our code set on that file's
refusals (``cli/provision_sso_org.py``: ``RemapRefused.mapped_to``, ...); or read
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
and the census counts no call to it (:data:`NOT_SITES`). Its callers pass a
first-party refusal (``config/tls_root_set.py``'s ``RootSetRefusal``).

**Blind spots**, stated as what the scanner reads, so nobody mistakes this for
more. A log call's or a print's arguments are read for the exception names the
scanner tracks and a zero-argument ``.exception()`` (above), and for subprocess
output; text that reaches them through an untracked name is not counted. For an
exit (``SystemExit(...)``, ``sys.exit(...)``), only the first positional
argument is read, and the exit is a site only in the forms listed above;
nothing else about an exit makes it a site (its other arguments, a first
argument in any other form, or an exit spelled any other way). A refusal
construction is matched by name, so a class passed positionally, or a keyword
alias passed from another file, is not followed; ``refusal_text`` under
``cli/`` makes the name it is assigned to untracked, so that name logged later
is not counted. A name filled in place, by a
method call (``logged.append(str(e))``) or a subscript store
(``d[k] = str(e)``), is not tracked, so a projection built that way and then
logged is not counted. A method that forwards its keywords to a logger
(``UserService._refuse_reset``) is not a log call, since :data:`LOG_HELPERS`
names module functions only; that site is pinned by the S3e render test.

No site is allowed anywhere: S3 switched every one (#2889).
"""

from __future__ import annotations

import ast
import re
import textwrap
from functools import lru_cache
from pathlib import Path
from typing import FrozenSet, List, Tuple

import pytest

from faultmaven.cli._refusal import AUDITED_REFUSALS
from tests.source_tree import parse_file

pytestmark = [pytest.mark.unit, pytest.mark.architecture]

PACKAGE = Path(__file__).resolve().parents[3] / "faultmaven"

LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
    | {"fatal"}
)

#: Module functions that forward their arguments to a logger call
#: (``api/middleware/route_policy.py``, ``infrastructure/concurrency/owned_claim.py``,
#: ``modules/auth/api/auth.py``).
LOG_HELPERS = frozenset({"_report_once", "_warn_profile_degrade_once"})

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
        "_provider_codes",
        "counts_against_breaker",
    }
)

#: Safe in a file under ``cli/``, but not as a log call's argument: an audited
#: refusal's sentence, printed whole, else the class name (``cli/_refusal.py``).
CLI_SAFE_CALLS = frozenset({"refusal_text"})

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
        "expected_version",
        "actual_version",
    }
)

#: Per file: the ids our code sets on that file's refusals, safe there only.
FILE_SAFE_ATTRS = {
    # ``RemapRefused``, ``OrgAlreadyClaimed`` (``tenant_bootstrap.py``),
    # ``DomainMismatch``, ``SlugHeldByAnotherEnterprise``: enterprise and IdP
    # org ids, slugs and domains from the command's rows and arguments.
    "cli/provision_sso_org.py": frozenset(
        {"provider_org_id", "mapped_to", "requested", "enterprise_id"}
        | {"claimed_by", "requested_by", "current", "slug", "domain"}
    ),
}

#: The bare class names :data:`AUDITED_REFUSALS` holds.
AUDITED_NAMES = frozenset(name.rsplit(".", 1)[1] for name in AUDITED_REFUSALS)

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


def _bare_name(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


def _callee(call: ast.Call) -> str:
    return _bare_name(call.func)


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


def _is_safe_getattr(call: ast.Call, safe_attrs: FrozenSet[str]) -> bool:
    """``getattr(e, "status_code", None)``: a safe attribute, read by name."""
    args = call.args
    return (
        isinstance(call.func, ast.Name)
        and call.func.id == "getattr"
        and len(args) >= 2
        and isinstance(args[0], ast.Name)
        and isinstance(args[1], ast.Constant)
        and args[1].value in safe_attrs
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

    def __init__(self, names: FrozenSet[str], safe_calls, safe_attrs):
        self.names = names
        self.safe_calls = safe_calls
        self.safe_attrs = safe_attrs
        self.name_hit = False
        self.output_hit = False

    def visit_Call(self, node: ast.Call) -> None:
        if _is_task_exception(node):
            self.name_hit = True  # ``t.exception()`` read inline
        elif _is_safe_getattr(node, self.safe_attrs):
            for default in node.args[2:]:
                self.visit(default)
        elif _callee(node) not in self.safe_calls:
            self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        """``exc is not None``: a test, not text on the line."""

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in self.safe_attrs and isinstance(node.value, ast.Name):
            return
        if node.attr in ("stderr", "stdout") and not (
            isinstance(node.value, ast.Name) and node.value.id == "sys"
        ):
            self.output_hit = True
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in self.names:
            self.name_hit = True


def _reads(node: ast.AST, names: "_Names", *, logging: bool = False) -> _Reads:
    """What ``node`` reads of ``names``, with the safe forms of ``names.path``:
    :data:`CLI_SAFE_CALLS` under ``cli/`` unless ``logging``, and that file's
    :data:`FILE_SAFE_ATTRS`."""
    under_cli = names.path.startswith("cli/") and not logging
    safe_calls = SAFE_CALLS | (CLI_SAFE_CALLS if under_cli else frozenset())
    safe_attrs = SAFE_ATTRS | FILE_SAFE_ATTRS.get(names.path, frozenset())
    reads = _Reads(names.all, safe_calls, safe_attrs)
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

    def __init__(self, exceptions=frozenset(), texts=frozenset(), path: str = ""):
        self.exceptions = frozenset(exceptions)
        self.texts = frozenset(texts)
        self.path = path  # relative to the package: picks the safe forms

    @property
    def all(self) -> FrozenSet[str]:
        return self.exceptions | self.texts

    def adding(self, exceptions=(), texts=()) -> "_Names":
        return _Names(
            self.exceptions | set(exceptions), self.texts | set(texts), self.path
        )


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
                elif _reads(child.value, here).name_hit:
                    texts.update(bound)
            if loops and isinstance(child, (ast.For, ast.AsyncFor)):
                if _iterates_exceptions(child.iter, here.exceptions, gathered):
                    exceptions.update(_bound([child.target]))
                elif _reads(child.iter, here).name_hit:
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
    reads = _reads(code, names)
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
        reads = _reads(value, names, logging=not printing)
        name_hit = name_hit or reads.name_hit
        output_hit = output_hit or reads.output_hit
    if name_hit:
        return "print" if printing else "log"
    return "subprocess-text" if output_hit else ""


def _refusal_aliases(tree: ast.Module) -> FrozenSet[str]:
    """The keywords that, somewhere in ``tree``, are passed an audited refusal
    class by name (``misscoped=MembershipRemovalMisscoped``)."""
    return frozenset(
        keyword.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg and _bare_name(keyword.value) in AUDITED_NAMES
    )


def _is_super_init(call: ast.Call) -> bool:
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "__init__"
        and isinstance(func.value, ast.Call)
        and _callee(func.value) == "super"
    )


def _refusal_construction_reads(call: ast.Call, names: _Names) -> bool:
    """Whether an audited refusal's construction reads an exception name or
    subprocess output. ``refusal_text`` is safe here wherever it stands."""
    safe = _Names(names.exceptions, names.texts, "cli/")  # CLI_SAFE_CALLS
    values = list(call.args) + [keyword.value for keyword in call.keywords]
    for value in values:
        reads = _reads(value, safe)
        if reads.name_hit or reads.output_hit:
            return True
    return False


def scan_tree(tree: ast.Module, path: str = "") -> List[Site]:
    """Every census site in ``tree``, as ``(line, kind)``; ``path``, relative
    to the package, picks the safe forms."""
    sites: List[Site] = []
    refusals = AUDITED_NAMES | _refusal_aliases(tree)

    def walk(node: ast.AST, names: _Names, in_refusal: bool) -> None:
        for child in ast.iter_child_nodes(node):
            here, inside = names, in_refusal
            if isinstance(child, ast.ClassDef):
                inside = child.name in AUDITED_NAMES
            if isinstance(child, ast.ExceptHandler) and child.name:
                here = names.adding(exceptions={child.name})
            if isinstance(child, _SCOPES):
                here = _scope_aliases(
                    child, names.adding(exceptions=_exception_params(child))
                )
            if isinstance(child, ast.Call):
                kind = _call_site(child, here)
                constructs = _callee(child) in refusals or (
                    inside and _is_super_init(child)
                )
                if not kind and constructs:
                    if _refusal_construction_reads(child, here):
                        kind = "refusal-construction"
                if kind:
                    sites.append((child.lineno, kind))
            walk(child, here, inside)

    walk(tree, _scope_aliases(tree, _Names(path=path)), False)
    return sites


def scan_source(source: str, path: str = "") -> List[Site]:
    return scan_tree(ast.parse(source), path)


def census_of(package: Path) -> Tuple[Tuple[str, int, str], ...]:
    """Every site under ``package``, as ``(path, line, kind)``, the path
    relative to it. A file that does not parse fails the scan
    (``SyntaxError``), never counts as clean."""
    found = []
    for path in sorted(package.rglob("*.py")):
        relative = path.relative_to(package).as_posix()
        for line, kind in scan_tree(parse_file(path), relative):
            found.append((relative, line, kind))
    return tuple(found)


@lru_cache(maxsize=1)
def census() -> Tuple[Tuple[str, int, str], ...]:
    """Every site under ``faultmaven/``."""
    return census_of(PACKAGE)


RULE = (
    "Principles §7 rule 1: a log line or print names an exception by "
    "exception_log_name(e) (faultmaven.utils.exception_name), never its text, "
    "and subprocess output by exit code and length; an fm-* refusal prints "
    "through refusal_text (cli/_refusal.py), and an audited refusal's message "
    "holds no exception's text (#2889)."
)

#: A site in each kind the census counts, planted in a scratch package.
_PLANTED = {
    "cli/planted.py": (
        "try:\n    pass\nexcept Exception as e:\n    print(f'x {e}')\n"
        "    raise _Refused(f'x {e}')\n"
    ),
    "infrastructure/planted.py": "def f(e):\n    logger.error(f'x {e}')\n",
    "config/planted.py": "raise SystemExit(f'refusing: {path}')\n",
}


def test_the_scan_reads_the_package():
    assert len(list(PACKAGE.rglob("*.py"))) > 500


def test_the_scan_finds_a_planted_site_through_the_file_walk(tmp_path):
    """The positive control for the empty census: the same walk over a
    package holding one site of each kind finds each, at its path."""
    for relative, source in _PLANTED.items():
        planted = tmp_path / relative
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_text(source)
    assert census_of(tmp_path) == (
        ("cli/planted.py", 4, "print"),
        ("cli/planted.py", 5, "refusal-construction"),
        ("config/planted.py", 1, "systemexit-text"),
        ("infrastructure/planted.py", 2, "log"),
    )


def test_no_site_anywhere():
    sites = [f"{path}:{line} ({kind})" for path, line, kind in census()]
    assert not sites, RULE + "\n" + "\n".join(sites)


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
        "try:\n    pass\nexcept Exception as e:\n"
        "    _warn_profile_degrade_once('k', f'x {e}')",
        "try:\n    pass\nexcept Exception as exc:\n"
        "    log.error('x', extra={'identifier': exc.identifier})",
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


_EXC = "try:\n    pass\nexcept Exception as exc:\n"


@pytest.mark.parametrize(
    ("path", "source"),
    [
        ("cli/x.py", _EXC + "    print(f'❌ {exc}')"),
        ("cli/x.py", _EXC + "    logger.error(refusal_text(exc))"),
        ("cli/x.py", _EXC + "    log.warning('x', reason=refusal_text(exc))"),
        ("jobs/x.py", _EXC + "    print(f'❌ {refusal_text(exc)}')"),
        ("cli/x.py", _EXC + "    raise _Refused(f'x {exc}')"),
        ("cli/x.py", _EXC + "    raise _Refused(str(exc))"),
        (
            "cli/x.py",
            _EXC + "    raise tenant_retirement.EnterpriseRowMissing(exc.args)",
        ),
        (
            "modules/x.py",
            "def g():\n    f(misscoped=MembershipRemovalMisscoped)\n"
            "def f(e, misscoped):\n    raise misscoped(f'x {e}')",
        ),
        (
            "infrastructure/x.py",
            "class EnterpriseRowMissing(Exception):\n"
            "    def __init__(self, exc):\n        super().__init__(f'{exc}')",
        ),
        ("cli/x.py", _EXC + "    print(f'{exc.mapped_to}')"),
        ("cli/x.py", _EXC + "    print(f'{exc.enterprise_id}')"),
    ],
)
def test_a_refusal_site_is_found(path, source):
    """``refusal_text`` is safe only under ``cli/`` and never in a log call;
    an audited refusal's construction reads no exception; the ids are safe
    only in ``cli/provision_sso_org.py``."""
    assert scan_source(source, path), (path, source)


@pytest.mark.parametrize(
    ("path", "source"),
    [
        ("cli/x.py", _EXC + "    print(f'❌ {refusal_text(exc)}')"),
        (
            "cli/x.py",
            _EXC + "    refusal = knob(refusal_text(exc))\n    print(refusal)",
        ),
        ("cli/x.py", _EXC + "    raise _Refused(refusal_text(exc))"),
        ("modules/x.py", _EXC + "    raise _Refused(refusal_text(exc))"),
        ("cli/x.py", _EXC + "    raise _Refused(f'x {exception_log_name(exc)}')"),
        ("cli/x.py", "def f(path):\n    raise _Refused(f'no ids in {path!r}')"),
        ("cli/provision_sso_org.py", _EXC + "    print(f'{exc.mapped_to}')"),
        ("cli/provision_sso_org.py", _EXC + "    print(f'{exc.enterprise_id}')"),
        ("infrastructure/x.py", _EXC + "    log.warning('x', **_provider_codes(exc))"),
        (
            "infrastructure/x.py",
            _EXC + "    s = counts_against_breaker(exc)\n"
            '    log.warning(f\'{"a" if s else "b"}\')',
        ),
    ],
)
def test_a_refusal_safe_form_is_not_a_site(path, source):
    assert not scan_source(source, path), (path, source)


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
