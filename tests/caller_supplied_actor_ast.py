"""Shared AST analysis for "the actor on a durable record is never caller-chosen".

The rule (fm#1461): a log record's **actor** — the field a reader takes as
*who did this* — may only hold a value the server verified. A value the caller
supplied may be recorded, and often should be, but never under an actor's name.

The defect that produced the rule: ``api/middleware/logging.py`` read a session
id off the ``X-Session-ID`` header, resolved it through the session store and
stamped the owner into ``user_id`` on every access-log line of an
unauthenticated request. Nothing was authorized by it. What was corrupted was
the record an incident review reads, and the account it named was the one the
caller picked.

**What this module does NOT check, and why.** Its vocabulary is actor names
only (:data:`ACTOR_FIELDS`). ``session_id``, ``case_id``, ``correlation_id`` and
``request_id`` are caller-supplied on this code path too, and are deliberately
out of scope: they say *what the request was about*, not *who made it*, and a
guard that flagged them would report a hundred correct lines and be switched
off. The naming convention that keeps them honest — a caller-asserted value
wears a ``claimed_`` prefix in the access log — is pinned where it is produced,
in ``tests/unit/api/middleware/test_logging_middleware_attribution.py``.

Two deliberate design choices in the analysis itself:

* **Route-handler parameters are caller-supplied.** FastAPI binds a path
  parameter, a query parameter and a request body to ordinary function
  arguments, so the most natural way to write this defect never mentions
  ``request`` at all. A scan that only followed ``request.headers`` reads would
  have been green while ``modules/auth/api/auth.py`` logged a caller-supplied
  ``{user_id}`` path parameter under the key ``user_id`` — which *displaces*
  the verified actor, because ``ClientConfig.add_request_context`` only fills
  ``user_id`` when the record does not already carry one.
* **A call with a tainted argument yields a tainted value.** One hop is exactly
  what the original defect needed (``_get_user_id_from_session(request,
  session_id)`` turns a claimed id into somebody's account), and following
  return values properly would need a call graph. Erring broad is the right
  direction for a guard: a false positive is read by a human, a false negative
  is a wrong name in an audit trail.
"""

from __future__ import annotations

import ast
import functools
import pathlib
import re
import warnings
from typing import Iterable, List, NamedTuple, Set

#: Attributes of a ``Request`` whose contents the caller writes.
CALLER_CHANNEL_ATTRS = frozenset(
    {"headers", "query_params", "cookies", "path_params", "json", "form", "body"}
)

#: Identifiers a ``Request`` is conventionally bound to in this codebase.
REQUEST_BASES = frozenset({"request", "req", "http_request", "raw_request"})

#: Callables that write a durable record. ``start_request`` is here because the
#: ``RequestContext`` it builds is stamped onto EVERY record of the request by
#: ``ClientConfig.add_request_context`` — the widest sink of the three the
#: original defect reached, and the one with no correction path at all.
LOG_SINKS = frozenset(
    {
        "debug",
        "info",
        "warning",
        "warn",
        "error",
        "exception",
        "critical",
        "log",
        "log_once",
        "start_request",
    }
)

#: Field names a reader takes as "who". Actor names ONLY — see the module
#: docstring for what is deliberately absent.
ACTOR_FIELDS = frozenset(
    {
        "account_id",
        "actor",
        "actor_id",
        "actor_user_id",
        "actor_username",
        # ``admin_user_id`` is the operator attribution on the CROSS-TENANT case
        # list (``api/routes/admin_cases.py``) — the most audit-critical actor
        # field in the app, and one prefix away from a name already covered.
        "admin_user_id",
        "enterprise_id",
        # The two halves of a break-glass grant's provenance
        # (``api/routes/admin_grants.py``): who ended it, and who held it.
        "held_by",
        "operator_user_id",
        "operator_username",
        "organization_id",
        "owner",
        "owner_id",
        "principal",
        "revoked_by",
        "sub",
        "subject",
        "user",
        "user_id",
        "user_name",
        "username",
    }
)

#: Deliberately NOT actor fields. A ``target_``/``requested_`` prefix is this
#: codebase's way of saying "the caller named this, and it is the OBJECT of the
#: action rather than its author" — which is the remedy fm#1461 applied to
#: ``POST /users/{user_id}/revoke-tokens``. Flagging them would punish the fix.
CLAIM_PREFIXES = ("target_", "requested_", "claimed_")

#: The same vocabulary as it appears as a LABEL in a human-readable message:
#: ``[user: {x}]``, ``user={x}``, ``actor: {x}``. A prose label is a claim about
#: what the value is exactly as much as a structured key is.
_LABEL_RE = re.compile(r"([A-Za-z][A-Za-z _-]*?)\s*[:=]\s*$")

#: Dependency markers whose parameter is resolved by the server, not sent by the
#: caller. ``Depends(require_platform_admin)`` yields a verified operator; the
#: caller cannot choose it.
_SERVER_RESOLVED_DEFAULTS = frozenset({"Depends", "Security"})

#: Parameter annotations that are the request itself rather than a value off it.
_NON_VALUE_ANNOTATIONS = frozenset({"Request", "Response", "BackgroundTasks"})


class Finding(NamedTuple):
    """One place an actor field takes a caller-supplied value."""

    path: str
    lineno: int
    field: str
    expression: str

    def __str__(self) -> str:  # pragma: no cover - failure message only
        return f"{self.path}:{self.lineno}  {self.field}= {self.expression}"


def _base_name(node: ast.AST) -> str | None:
    """The leftmost identifier of an attribute chain, if it has one."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _reads_caller_channel(node: ast.AST) -> bool:
    """Whether evaluating ``node`` reads something off the incoming request."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr in CALLER_CHANNEL_ATTRS:
            base = _base_name(sub.value)
            if base and (base in REQUEST_BASES or base.endswith("_request")):
                return True
    return False


def _called_names(node: ast.AST) -> Set[str]:
    names: Set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            if isinstance(sub.func, ast.Name):
                names.add(sub.func.id)
            elif isinstance(sub.func, ast.Attribute):
                names.add(sub.func.attr)
    return names


def _loaded_names(node: ast.AST) -> Set[str]:
    return {
        sub.id
        for sub in ast.walk(node)
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load)
    }


def _stored_names(node: ast.AST) -> Set[str]:
    return {
        sub.id
        for sub in ast.walk(node)
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store)
    }


def _is_route_handler(fn: ast.AST) -> bool:
    """Whether ``fn`` is a FastAPI endpoint, i.e. the caller fills its arguments."""
    for decorator in getattr(fn, "decorator_list", []):
        func = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(func, ast.Attribute) and func.attr in {
            "get",
            "post",
            "put",
            "patch",
            "delete",
            "head",
            "options",
            "api_route",
        }:
            base = _base_name(func.value)
            if base and ("router" in base.lower() or base in {"app", "api"}):
                return True
    return False


def _caller_filled_parameters(fn: ast.AST) -> Set[str]:
    """The parameters of a route handler the CALLER supplies.

    A path parameter, a query parameter and a parsed body all arrive this way.
    Excluded: anything the server resolves (``Depends``/``Security``) and the
    request/response objects themselves.
    """
    if not _is_route_handler(fn):
        return set()

    args = fn.args
    positional = list(args.posonlyargs) + list(args.args)
    defaults: list = [None] * (len(positional) - len(args.defaults)) + list(
        args.defaults
    )
    pairs = list(zip(positional, defaults)) + list(
        zip(args.kwonlyargs, args.kw_defaults)
    )

    caller_filled: Set[str] = set()
    for arg, default in pairs:
        if arg.arg in {"self", "cls"}:
            continue
        if isinstance(default, ast.Call):
            marker = default.func
            name = (
                marker.id
                if isinstance(marker, ast.Name)
                else getattr(marker, "attr", None)
            )
            if name in _SERVER_RESOLVED_DEFAULTS:
                continue
        annotation = arg.annotation
        if annotation is not None:
            rendered = ast.unparse(annotation)
            if any(marker in rendered for marker in _NON_VALUE_ANNOTATIONS):
                continue
            if "Depends" in rendered or "Security" in rendered:
                continue
        caller_filled.add(arg.arg)
    return caller_filled


def _functions(tree: ast.AST) -> Iterable[ast.AST]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _tainting_functions(tree: ast.AST) -> Set[str]:
    """Functions in this module whose return value can carry a caller's value."""
    return {
        fn.name  # type: ignore[attr-defined]
        for fn in _functions(tree)
        if any(_reads_caller_channel(stmt) for stmt in fn.body)  # type: ignore[attr-defined]
    }


def _sink_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _label_before(values: List[ast.AST], index: int) -> str | None:
    """The field label a message writes immediately before an interpolation."""
    for previous in reversed(values[:index]):
        if isinstance(previous, ast.Constant) and isinstance(previous.value, str):
            match = _LABEL_RE.search(previous.value.rstrip())
            if match is None:
                return None
            return match.group(1).strip().lower().replace(" ", "_").replace("-", "_")
        return None
    return None


def _actor_labelling_functions(tree: ast.AST) -> Set[str]:
    """Module-local functions that WRITE an actor label into a string.

    ``api/middleware/logging.py`` does not spell ``[user: …]`` at the call
    site; it calls ``_attribution_suffix``, which spells it. So the label arm
    looking only at the message's own f-string sees a ``FormattedValue`` whose
    previous sibling is another ``FormattedValue``, finds no label, and reports
    the production line clean — while catching the same defect written inline.
    A guard that cannot see the line it was written for is the failure mode
    this whole module exists to avoid, so the label is followed one hop into
    the helper that produces it.
    """
    labelling: Set[str] = set()
    for fn in _functions(tree):
        for sub in ast.walk(fn):
            if not isinstance(sub, ast.JoinedStr):
                continue
            for index, part in enumerate(sub.values):
                if (
                    isinstance(part, ast.FormattedValue)
                    and _label_before(sub.values, index) in ACTOR_FIELDS
                ):
                    labelling.add(fn.name)  # type: ignore[attr-defined]
    return labelling


def scan_source(source: str, path: str) -> List[Finding]:
    """Every actor field in ``source`` that can hold a caller-supplied value."""
    tree = ast.parse(source)
    tainting = _tainting_functions(tree)
    labelling = _actor_labelling_functions(tree)
    findings: List[Finding] = []

    for fn in _functions(tree):
        tainted: Set[str] = _caller_filled_parameters(fn)

        def is_tainted(expr: ast.AST) -> bool:
            return bool(
                _reads_caller_channel(expr)
                or (_loaded_names(expr) & tainted)
                or (_called_names(expr) & tainting)
            )

        # Grow the tainted set to a FIXED POINT before looking at any sink.
        # ``ast.walk`` is breadth-first, so it can reach a log call before the
        # assignment that taints the name that call passes — a one-pass version
        # of this analysis missed a re-introduction of the very defect it was
        # written for, because the sink happened to be shallower in the tree
        # than the assignment. Iterating until nothing new is tainted removes
        # the ordering question instead of depending on getting it right.
        assignments = [
            node
            for node in ast.walk(fn)
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr))
            and node.value is not None
        ]
        while True:
            grown = False
            for node in assignments:
                if not is_tainted(node.value):
                    continue
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for target in targets:
                    new_names = _stored_names(target) - tainted
                    if new_names:
                        tainted |= new_names
                        grown = True
            if not grown:
                break

        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            if _sink_name(node) not in LOG_SINKS:
                continue

            for keyword in node.keywords:
                # ``**mapping`` — the keys are not in the source, so no key
                # check is possible and the WHOLE mapping is the sink. This is
                # the house idiom for the logging facade (20 live call sites in
                # ``infrastructure/logging/unified.py`` alone), and a two-line
                # re-introduction of fm#1461 through it passed every test this
                # module ships. Over-approximating here is the direction this
                # module's docstring argues for: a false positive is read by a
                # human, a false negative is a wrong name in an audit trail.
                if keyword.arg is None:
                    if is_tainted(keyword.value):
                        findings.append(
                            Finding(
                                path,
                                keyword.value.lineno,
                                "** (opaque mapping)",
                                ast.unparse(keyword.value),
                            )
                        )
                    continue
                if keyword.arg in ACTOR_FIELDS and is_tainted(keyword.value):
                    findings.append(
                        Finding(
                            path,
                            keyword.value.lineno,
                            keyword.arg,
                            ast.unparse(keyword.value),
                        )
                    )
                if keyword.arg == "extra":
                    # A dict literal can be read key by key. Anything else —
                    # a name, a call, a conditional — cannot, so it is treated
                    # exactly like a splat.
                    if isinstance(keyword.value, ast.Dict):
                        for key, value in zip(keyword.value.keys, keyword.value.values):
                            if (
                                isinstance(key, ast.Constant)
                                and key.value in ACTOR_FIELDS
                                and is_tainted(value)
                            ):
                                findings.append(
                                    Finding(
                                        path,
                                        value.lineno,
                                        key.value,
                                        ast.unparse(value),
                                    )
                                )
                    elif is_tainted(keyword.value):
                        findings.append(
                            Finding(
                                path,
                                keyword.value.lineno,
                                "extra= (opaque mapping)",
                                ast.unparse(keyword.value),
                            )
                        )

            # The human-readable half of the same record.
            messages = [arg for arg in node.args[:1]] + [
                keyword.value for keyword in node.keywords if keyword.arg == "message"
            ]
            for message in messages:
                for joined in [
                    sub for sub in ast.walk(message) if isinstance(sub, ast.JoinedStr)
                ]:
                    for index, part in enumerate(joined.values):
                        if not isinstance(part, ast.FormattedValue):
                            continue
                        label = _label_before(joined.values, index)
                        if not is_tainted(part.value):
                            continue
                        if label in ACTOR_FIELDS:
                            findings.append(
                                Finding(
                                    path,
                                    part.lineno,
                                    f"{label} (message label)",
                                    ast.unparse(part.value),
                                )
                            )
                        elif _called_names(part.value) & labelling:
                            findings.append(
                                Finding(
                                    path,
                                    part.lineno,
                                    "actor label via helper",
                                    ast.unparse(part.value),
                                )
                            )

    return sorted(set(findings))


def scan_actor_sinks(source: str, path: str) -> List[Finding]:
    """Every actor field written to a durable record, tainted or not.

    The liveness half of the guard. :func:`scan_source` reporting nothing is
    the *desired* answer and an unfalsifiable one on its own — a detector that
    has stopped recognising the sink shape says exactly the same thing. This
    says how many actor fields the analysis can SEE, so a change that blinds it
    fails loudly instead of passing quietly.
    """
    tree = ast.parse(source)
    found: List[Finding] = []
    for fn in _functions(tree):
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call) or _sink_name(node) not in LOG_SINKS:
                continue
            for keyword in node.keywords:
                if keyword.arg in ACTOR_FIELDS:
                    found.append(
                        Finding(
                            path,
                            keyword.value.lineno,
                            keyword.arg,
                            ast.unparse(keyword.value),
                        )
                    )
                if keyword.arg == "extra" and isinstance(keyword.value, ast.Dict):
                    for key, value in zip(keyword.value.keys, keyword.value.values):
                        if isinstance(key, ast.Constant) and key.value in ACTOR_FIELDS:
                            found.append(
                                Finding(
                                    path, value.lineno, key.value, ast.unparse(value)
                                )
                            )
    return sorted(set(found))


@functools.lru_cache(maxsize=4)
def scan_package(
    root: pathlib.Path,
) -> tuple[tuple[Finding, ...], tuple[pathlib.Path, ...]]:
    """Scan every module under ``root``; return the findings and what was read.

    Cached because several assertions ask the same question of the same tree
    and parsing the package is most of a minute. Nothing here writes, so the
    answer cannot go stale within a run.

    Invalid string escapes in the sources are the linter's business, not this
    guard's: ``ast.parse`` raises a ``DeprecationWarning`` for each one, and
    letting them through would make this guard's output a report about escape sequences.
    """
    files = tuple(sorted(p for p in root.rglob("*.py")))
    findings: List[Finding] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        warnings.simplefilter("ignore", SyntaxWarning)
        for file in files:
            findings += scan_source(
                file.read_text(encoding="utf-8"), str(file.relative_to(root.parent))
            )
    return tuple(sorted(set(findings))), files
