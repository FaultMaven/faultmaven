"""A ``case_messages`` row reaches a case through ONE constructor (#1452).

``case_messages`` requires non-blank content, and a row is part of an
AGGREGATE save: a blank one aborts the whole save and takes the case row, its
evidence, its hypotheses and its uploaded files with it. Three writers had
each been fixed for that separately, each inventing its own answer (#1420,
#1433, #1443), and a fourth — the engine's runbook-conversion notice — had no
answer at all. ``append_message_row`` now owns the decision per row kind, and
this scan fails on any writer that goes around it.

Parsed, not grepped: a substring search matches comments and docstrings, and
this module's own prose would trip it.

**Where the rule can be violated, and what is scanned.** A row reaches the
table by exactly three routes, and each is an arm below:

1. into ``Case.messages``, which the aggregate save persists — by mutating the
   list (arm ``mutation``) or by building a case or copy around one
   (arm ``construction``);
2. through the repository's ``add_message`` (arm ``add_message``);
3. through SQL that inserts into the table (arm ``sql_insert``).

Arm ``row_literal`` sits under all three: whatever the delivery, a row has to
be BUILT, and a dict carrying ``role`` beside a column only a message row has
is one being built somewhere other than the constructor.

The scan covers the whole ``faultmaven`` package — the only tree that ships.
``tests/`` builds rows freely as fixtures, and ``scripts/`` and ``alembic/``
contain no writer (measured when this was written; a script is not a
production path either way).

**What it does not see.** Each of these has ZERO live sites, measured, and a
row built as a literal is still caught by ``row_literal`` wherever it ends up.
They are listed so nobody mistakes this scan for more than it is:

* a helper HANDED ``case.messages`` as an argument that appends a row it did
  not build as a literal — following the list into a callee needs
  interprocedural analysis, and flagging every call that receives
  ``x.messages`` would flag every reader (``len``, ``sorted``, the prompt
  builders);
* aliases other than a plain ``name = <messages>`` (or ``<messages> or []``):
  a bound method (``add = case.messages.append``), an attribute
  (``self.rows = case.messages``), a walrus, tuple unpacking, a ``for``/``with``
  target, and ``rows or case.messages`` (the list as the SECOND operand);
* reflective writes: ``operator.iadd``, ``object.__setattr__``,
  ``case.__dict__.update``, ``vars(case)[...]``;
* a message list the scan cannot see because it arrives through a variable:
  ``Case(**fields)``, ``Case.model_validate(data)``,
  ``case.model_copy(update=changes)``, ``type(case)(...)``;
* SQL it cannot read as one string: an ``INSERT INTO`` assembled by
  concatenation, and SQLAlchemy's ``insert(CaseMessageModel)`` or
  ``session.add(CaseMessageModel(...))``.

``case.atomic_update(messages=...)`` — a public ``Case`` method that sets
fields directly, and the one plausible future shape among these — IS covered,
by the ``construction`` arm.

**Every arm proves it looked.** Each has a site it MUST find — the
constructor's own append and its own row literal, the one copy the service
makes, the repository's own delegation, the four SQL inserts. An arm whose
shape stopped matching finds nothing and would otherwise report clean; here it
fails instead.
"""

from __future__ import annotations

import ast
import pathlib
import re
from dataclasses import dataclass

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.architecture]

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_PACKAGE = _ROOT / "faultmaven"

#: A floor on the walk, so one that resolved the wrong directory cannot report
#: clean. The package held 481 modules when this was written; a subtree that
#: lost a fifth of them does not clear it.
_MIN_MODULES_SCANNED = 400

_CONSTRUCTOR_FILE = "faultmaven/modules/case/domain/owned_models/message_row.py"
_CONSTRUCTOR = (_CONSTRUCTOR_FILE, "append_message_row")

#: Columns only a ``case_messages`` row has. A dict with ``role`` and one of
#: these is a conversation row; an LLM chat message is ``role`` + ``content``
#: (+ ``name`` / ``tool_calls`` / ``tool_call_id``) and matches none of them.
#: ``content`` and ``metadata`` are deliberately NOT here — every chat message
#: has the first, and many dicts have the second.
_ROW_ONLY_KEYS = {"message_id", "turn_number", "created_at", "author_id", "token_count"}

#: List methods that ADD to a list in place.
_MUTATORS = {"append", "extend", "insert", "__iadd__", "__setitem__"}

#: Receivers of ``model_validate`` / ``model_construct`` that build a case.
_CASE_CONSTRUCTORS = {"Case"}

_SQL_INSERT = re.compile(r"INSERT\s+INTO\s+case_messages\b", re.IGNORECASE)

# ---------------------------------------------------------------------------
# What each arm is allowed to find, and why. Keyed by (file, enclosing
# qualname). An entry that stops matching anything fails the test, so this
# list cannot outlive the code it excuses.
# ---------------------------------------------------------------------------

_ALLOWED = {
    "mutation": {
        _CONSTRUCTOR: "the constructor's own append",
        (
            "faultmaven/modules/case/infrastructure/case_repository.py",
            "InMemoryCaseRepository.add_message",
        ): (
            "the in-memory repository's add_message IS the storage write — the "
            "double of the SQL backends' INSERT — so the row it appends is the "
            "caller's, completed by normalise_message_row like theirs"
        ),
    },
    "row_literal": {
        _CONSTRUCTOR: "the constructor's own row",
        # The persistence layer turns rows it READ back into this shape, and
        # binds rows it was HANDED into SQL parameters. Neither builds a row.
        (
            "faultmaven/modules/case/infrastructure/sqlite_case_repository.py",
            "SQLiteCaseRepository._load_messages",
        ): "hydrates rows read from case_messages",
        (
            "faultmaven/modules/case/infrastructure/sqlite_case_repository.py",
            "SQLiteCaseRepository._load_messages_bulk",
        ): "hydrates rows read from case_messages",
        (
            "faultmaven/modules/case/infrastructure/sqlite_case_repository.py",
            "SQLiteCaseRepository.get_messages",
        ): "hydrates rows read from case_messages",
        (
            "faultmaven/modules/case/infrastructure/sqlite_case_repository.py",
            "SQLiteCaseRepository.add_message",
        ): "binds the caller's row into the INSERT's parameters",
        (
            "faultmaven/modules/case/infrastructure/sqlite_case_repository.py",
            "SQLiteCaseRepository._upsert_messages",
        ): "binds the case's rows into the upsert's parameters",
        (
            "faultmaven/modules/case/infrastructure/postgresql_hybrid_case_repository.py",
            "PostgreSQLHybridCaseRepository.add_message",
        ): "binds the caller's row into the INSERT's parameters",
        (
            "faultmaven/modules/case/infrastructure/postgresql_hybrid_case_repository.py",
            "PostgreSQLHybridCaseRepository.get_messages",
        ): "hydrates rows read from case_messages",
        (
            "faultmaven/modules/case/infrastructure/postgresql_hybrid_case_repository.py",
            "PostgreSQLHybridCaseRepository._upsert_messages",
        ): "binds the case's rows into the upsert's parameters",
        # Not a row of this table at all.
        (
            "faultmaven/_container_impl.py",
            "DIContainer._create_minimal_case_service.MinimalCaseService.create_case",
        ): (
            "the degraded-mode stand-in keeps its own in-memory transcript "
            "(self.case_messages), never case.messages and never the table: no "
            "aggregate save exists there for a blank row to abort, and its "
            "reader reads this dict's own shape (timestamp, user_id, "
            "initial_<case_id>). It already writes no row for a blank message"
        ),
        ("faultmaven/models/api_messages.py", "MessageResponse"): (
            "an OpenAPI example payload"
        ),
        ("faultmaven/models/api_messages.py", "MessageListResponse"): (
            "an OpenAPI example payload"
        ),
    },
    "construction": {},
    "add_message": {
        (
            "faultmaven/modules/case/infrastructure/sessionless_case_repository.py",
            "SessionlessCaseRepository.add_message",
        ): (
            "the sessionless repository forwards to the session-bound one; the "
            "row is its caller's, and add_message has no production caller"
        ),
    },
    "sql_insert": {
        (
            "faultmaven/modules/case/infrastructure/sqlite_case_repository.py",
            "SQLiteCaseRepository.add_message",
        ): "the repository's row-at-a-time write",
        (
            "faultmaven/modules/case/infrastructure/sqlite_case_repository.py",
            "SQLiteCaseRepository._upsert_messages",
        ): "the aggregate save",
        (
            "faultmaven/modules/case/infrastructure/postgresql_hybrid_case_repository.py",
            "PostgreSQLHybridCaseRepository.add_message",
        ): "the repository's row-at-a-time write",
        (
            "faultmaven/modules/case/infrastructure/postgresql_hybrid_case_repository.py",
            "PostgreSQLHybridCaseRepository._upsert_messages",
        ): "the aggregate save",
    },
}

#: How many hits each allowed site accounts for — one, unless named here. An
#: allowance excuses a SITE (a function), so without a count a second writer
#: added inside an allowed function would be excused along with the first.
_ALLOWED_COUNT = {
    ("row_literal", ("faultmaven/models/api_messages.py", "MessageListResponse")): 2,
}

#: Sites each arm must find, or it is not looking. Every allowed site is also
#: checked by ``test_every_allowance_excuses_exactly_what_it_names``; these are
#: the ones each arm is certain to have, and ``construction`` has one live site
#: that is permitted by its SHAPE rather than allowed by name.
_MUST_FIND = {
    "mutation": {_CONSTRUCTOR},
    "row_literal": {_CONSTRUCTOR},
    "construction": {
        (
            "faultmaven/modules/agent/domain/services/investigation_service.py",
            "InvestigationService._handle_file_reclassification",
        )
    },
    "add_message": {
        (
            "faultmaven/modules/case/infrastructure/sessionless_case_repository.py",
            "SessionlessCaseRepository.add_message",
        )
    },
    "sql_insert": set(_ALLOWED["sql_insert"]),
}


@dataclass(frozen=True)
class Hit:
    arm: str
    path: str
    qualname: str
    lineno: int
    what: str
    permitted: bool = False  # a shape the arm sees but that adds no row

    @property
    def site(self) -> tuple[str, str]:
        return (self.path, self.qualname)


def _called_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_messages(node: ast.expr, aliases: set[str]) -> bool:
    """``x.messages``, ``getattr(x, "messages"[, d])``, ``x.__dict__["messages"]``,
    ``<that> or []``, or a local name bound to one of those without copying it."""
    if isinstance(node, ast.Attribute):
        return node.attr == "messages"
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "__dict__"
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == "messages"
    ):
        return True
    if isinstance(node, ast.Name):
        return node.id in aliases
    if isinstance(node, ast.Call) and _called_name(node.func) == "getattr":
        return (
            len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "messages"
        )
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        # ``getattr(case, "messages", None) or []`` IS the list when it is
        # non-empty — the first operand is what gets mutated.
        return _is_messages(node.values[0], aliases)
    return False


def _same(a: ast.expr, b: ast.expr) -> bool:
    return ast.dump(a) == ast.dump(b)


def _is_copy_of_messages(node: ast.expr, owner: ast.expr | None) -> bool:
    """A value that adds no row: an empty list, or a copy of ``owner.messages``.

    ``owner`` is the case being copied or updated — the receiver of
    ``model_copy`` / ``atomic_update``. Any OTHER object's ``.messages`` is
    rows from somewhere else (a request body, an import payload), which is
    exactly a writer going around the constructor, so it is not permitted. A
    case BUILT from scratch (``Case(...)``, ``model_validate``,
    ``model_construct``) has no owner, so only the empty list is permitted
    there.
    """
    if isinstance(node, ast.List) and not node.elts:
        return True
    if owner is None:
        return False
    if isinstance(node, ast.Attribute) and node.attr == "messages":
        return _same(node.value, owner)
    if isinstance(node, ast.Call):
        name = _called_name(node.func)
        if name in {"list", "copy", "deepcopy"} and len(node.args) == 1:
            return _is_copy_of_messages(node.args[0], owner)
        if (
            name == "copy"
            and isinstance(node.func, ast.Attribute)
            and not node.args
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "messages"
        ):
            return _same(node.func.value.value, owner)
    return False


def _str_keys(keys) -> set:
    return {
        k.value
        for k in keys
        if isinstance(k, ast.Constant) and isinstance(k.value, str)
    }


class _Scanner(ast.NodeVisitor):
    def __init__(self, path: str):
        self.path = path
        self.scope: list[str] = []
        self.aliases: list[set[str]] = [set()]
        self.hits: list[Hit] = []

    # -- scope ------------------------------------------------------------

    def _enter(self, node) -> None:
        self.scope.append(node.name)
        self.aliases.append(set())
        self.generic_visit(node)
        self.aliases.pop()
        self.scope.pop()

    visit_FunctionDef = _enter
    visit_AsyncFunctionDef = _enter
    visit_ClassDef = _enter

    def _hit(self, arm, node, what, permitted=False) -> None:
        self.hits.append(
            Hit(arm, self.path, ".".join(self.scope), node.lineno, what, permitted)
        )

    @property
    def _aliases(self) -> set[str]:
        return self.aliases[-1]

    # -- arm: mutation ----------------------------------------------------

    def _assigned(self, target: ast.expr, node) -> None:
        if _is_messages(target, set()) and not isinstance(target, ast.Name):
            self._hit("mutation", node, f"assigns {ast.unparse(target)}")
        elif isinstance(target, ast.Subscript) and _is_messages(
            target.value, self._aliases
        ):
            self._hit("mutation", node, f"assigns {ast.unparse(target)}")
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._assigned(elt, node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._assigned(target, node)
            # ``msgs = case.messages`` binds the LIST, so a later
            # ``msgs.append(row)`` is an append to the case.
            if isinstance(target, ast.Name):
                if _is_messages(node.value, self._aliases):
                    self._aliases.add(target.id)
                else:
                    self._aliases.discard(target.id)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._assigned(node.target, node)
        if isinstance(node.target, ast.Name) and node.value is not None:
            if _is_messages(node.value, self._aliases):
                self._aliases.add(node.target.id)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if _is_messages(node.target, self._aliases) or (
            isinstance(node.target, ast.Subscript)
            and _is_messages(node.target.value, self._aliases)
        ):
            self._hit("mutation", node, f"augments {ast.unparse(node.target)}")
        self.generic_visit(node)

    # -- arms on calls ----------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = _called_name(func)

        # mutation: ``<messages>.append(...)`` and friends
        if (
            isinstance(func, ast.Attribute)
            and func.attr in _MUTATORS
            and _is_messages(func.value, self._aliases)
        ):
            self._hit("mutation", node, f".messages.{func.attr}(...)")

        # mutation: the unbound spelling, ``list.append(case.messages, row)``
        if (
            isinstance(func, ast.Attribute)
            and func.attr in _MUTATORS
            and isinstance(func.value, ast.Name)
            and func.value.id == "list"
            and node.args
            and _is_messages(node.args[0], self._aliases)
        ):
            self._hit("mutation", node, f"list.{func.attr}(<messages>, ...)")

        # mutation: ``setattr(case, "messages", ...)``
        if (
            name == "setattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "messages"
        ):
            self._hit("mutation", node, 'setattr(..., "messages", ...)')

        # construction: a case, or a copy of one, built around a message list
        for value, how, owner in self._message_lists_built(node, name):
            permitted = _is_copy_of_messages(value, owner)
            self._hit(
                "construction",
                node,
                f"{how} = {ast.unparse(value)[:60]}",
                permitted=permitted,
            )

        # add_message: a row written past the case, straight to the table.
        # Allowed by site and count, like every other arm — not by directory,
        # which would excuse a new repository file that called it.
        if name == "add_message" and isinstance(func, ast.Attribute):
            self._hit("add_message", node, "add_message(...)")

        # row_literal: ``dict(role=..., turn_number=...)``
        if name == "dict":
            kwargs = {k.arg for k in node.keywords if k.arg}
            if "role" in kwargs and kwargs & _ROW_ONLY_KEYS:
                self._hit("row_literal", node, f"dict({sorted(kwargs)})")

        self.generic_visit(node)

    @staticmethod
    def _message_lists_built(node: ast.Call, name: str | None):
        """Every message list handed to something that builds or updates a
        case, as ``(value, how, owner)`` — ``owner`` being the case whose own
        rows a permitted copy must come from, or ``None`` for a new case."""
        func = node.func
        receiver = _called_name(func.value) if isinstance(func, ast.Attribute) else None
        if name in _CASE_CONSTRUCTORS or (
            name == "model_construct" and receiver in _CASE_CONSTRUCTORS
        ):
            for kw in node.keywords:
                if kw.arg == "messages":
                    yield kw.value, f"{name}(messages=...)", None
        if name == "model_validate" and receiver in _CASE_CONSTRUCTORS:
            for arg in node.args[:1]:
                if isinstance(arg, ast.Dict):
                    for k, v in zip(arg.keys, arg.values):
                        if isinstance(k, ast.Constant) and k.value == "messages":
                            yield v, "model_validate({'messages': ...})", None
        if name == "model_copy" and isinstance(func, ast.Attribute):
            for kw in node.keywords:
                if kw.arg != "update":
                    continue
                if isinstance(kw.value, ast.Dict):
                    for k, v in zip(kw.value.keys, kw.value.values):
                        if isinstance(k, ast.Constant) and k.value == "messages":
                            yield v, "model_copy(update={'messages': ...})", func.value
                elif (
                    isinstance(kw.value, ast.Call)
                    and _called_name(kw.value.func) == "dict"
                ):
                    for inner in kw.value.keywords:
                        if inner.arg == "messages":
                            yield (
                                inner.value,
                                "model_copy(update=dict(messages=...))",
                                func.value,
                            )
        # ``Case.atomic_update`` sets fields directly, bypassing validation.
        if name == "atomic_update" and isinstance(func, ast.Attribute):
            for kw in node.keywords:
                if kw.arg == "messages":
                    yield kw.value, "atomic_update(messages=...)", func.value

    # -- arm: row_literal -------------------------------------------------

    def visit_Dict(self, node: ast.Dict) -> None:
        keys = _str_keys(node.keys)
        if "role" in keys and keys & _ROW_ONLY_KEYS:
            self._hit("row_literal", node, f"{{{', '.join(sorted(keys))}}}")
        self.generic_visit(node)

    # -- arm: sql_insert --------------------------------------------------

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and _SQL_INSERT.search(node.value):
            self._hit("sql_insert", node, "INSERT INTO case_messages")


def _scan_source(source: str, path: str) -> list[Hit]:
    scanner = _Scanner(path)
    scanner.visit(ast.parse(source, filename=path))
    return scanner.hits


def _offenders(hits: list[Hit]) -> list[str]:
    """Hits no rule excuses: not permitted by shape, not at an allowed site —
    or at an allowed site beyond the count it is allowed."""
    offenders = []
    seen: dict = {}
    for h in hits:
        if h.permitted:
            continue
        if h.site in _ALLOWED[h.arm]:
            key = (h.arm, h.site)
            seen[key] = seen.get(key, 0) + 1
            if seen[key] <= _ALLOWED_COUNT.get(key, 1):
                continue
        offenders.append(
            f"{h.path}:{h.lineno} [{h.arm}] {h.qualname or '<module>'}: {h.what}"
        )
    return offenders


def _scan() -> tuple[list[Hit], int]:
    hits: list[Hit] = []
    scanned = 0
    for path in sorted(_PACKAGE.rglob("*.py")):
        rel = path.relative_to(_ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        scanned += 1
        tree = ast.parse(source, filename=str(path))
        scanner = _Scanner(rel)
        scanner.visit(tree)
        hits.extend(scanner.hits)
    return hits, scanned


@pytest.fixture(scope="module")
def scan():
    return _scan()


def test_the_walk_covered_the_package(scan):
    _, scanned = scan
    assert scanned >= _MIN_MODULES_SCANNED, (
        f"only {scanned} modules scanned under {_PACKAGE} — the walk resolved "
        "the wrong directory, so its clean verdict says nothing"
    )
    assert (_ROOT / _CONSTRUCTOR_FILE).is_file(), (
        f"{_CONSTRUCTOR_FILE} is missing, so every allowance keyed on it is "
        "stale and this guard is guarding a file that no longer exists"
    )


@pytest.mark.parametrize("arm", sorted(_MUST_FIND))
def test_each_arm_found_the_sites_it_must(scan, arm):
    """An arm whose shape stopped matching finds nothing and reads as clean."""
    hits, _ = scan
    found = {h.site for h in hits if h.arm == arm}
    missing = _MUST_FIND[arm] - found
    assert not missing, (
        f"arm {arm!r} did not find {sorted(missing)} — either the code moved "
        "(update this guard to follow it) or the arm has stopped matching the "
        "shape it exists to catch, and its clean verdict below says nothing"
    )


@pytest.mark.parametrize("arm", sorted(_ALLOWED))
def test_every_allowance_excuses_exactly_what_it_names(scan, arm):
    """An allowance that matches nothing would silently excuse the next thing
    to land at that site; one that matches MORE than it names is excusing a
    second writer added beside the first."""
    hits, _ = scan
    found: dict = {}
    for h in hits:
        if h.arm == arm and not h.permitted:
            found[h.site] = found.get(h.site, 0) + 1
    wrong = {
        site: (_ALLOWED_COUNT.get((arm, site), 1), found.get(site, 0))
        for site in _ALLOWED[arm]
        if found.get(site, 0) != _ALLOWED_COUNT.get((arm, site), 1)
    }
    assert not wrong, (
        f"arm {arm!r}: allowed sites whose hit count moved, as "
        f"{{site: (allowed, found)}}: {wrong}"
    )


def test_no_message_row_goes_around_the_constructor(scan):
    hits, _ = scan
    offenders = _offenders(hits)
    assert offenders == [], (
        "a case_messages row must be built and appended by "
        "faultmaven.modules.case.contracts.append_message_row, which decides "
        "per row kind what blank content means — otherwise the first blank "
        "one aborts the whole aggregate save (#1452). Offenders:\n  "
        + "\n  ".join(offenders)
        + "\nIf the row fits no existing MessageRowKind, add a kind with its "
        "own blank-content answer; do not build the row here."
    )


# ---------------------------------------------------------------------------
# The guard's own reach. Each shape is compiled into a snippet and scanned the
# way the package is, so a change to an arm that stops seeing a shape fails
# here instead of reporting the package clean.
# ---------------------------------------------------------------------------

#: A path no allowance names, so anything found there is an offender.
_ELSEWHERE = "faultmaven/modules/agent/domain/services/some_service.py"


def _snippet(body: str) -> str:
    lines = "".join(f"    {line}\n" for line in body.strip("\n").splitlines())
    return f"def writer(case, body, repo, row, build):\n{lines}"


_CAUGHT = [
    ("case.messages.append(build())", "mutation"),
    ("case.messages.extend([build()])", "mutation"),
    ("case.messages.insert(0, build())", "mutation"),
    ("case.messages += [build()]", "mutation"),
    ("case.messages = case.messages + [build()]", "mutation"),
    ("case.messages = [*case.messages, build()]", "mutation"),
    ("case.messages[len(case.messages):] = [build()]", "mutation"),
    ("msgs = case.messages\nmsgs.append(build())", "mutation"),
    ("msgs = getattr(case, 'messages', None) or []\nmsgs.append(build())", "mutation"),
    ("getattr(case, 'messages').append(build())", "mutation"),
    ("setattr(case, 'messages', case.messages + [build()])", "mutation"),
    ("list.append(case.messages, build())", "mutation"),
    ("case.__dict__['messages'].append(build())", "mutation"),
    (
        "return case.model_copy(update={'messages': case.messages + [row]})",
        "construction",
    ),
    (
        "return case.model_copy(update=dict(messages=[*case.messages, row]))",
        "construction",
    ),
    ("return Case(title='t', messages=[row])", "construction"),
    ("return Case.model_validate({'title': 't', 'messages': [row]})", "construction"),
    ("return Case.model_construct(messages=[row])", "construction"),
    ("case.atomic_update(messages=[*case.messages, row])", "construction"),
    ("return repo.add_message(case.case_id, row)", "add_message"),
    (
        "return build({'role': 'system', 'turn_number': 1, 'content': ''})",
        "row_literal",
    ),
    ("return build(dict(role='system', turn_number=1, content=''))", "row_literal"),
    (
        "return build({'role': 'user', 'created_at': 'now', 'content': ''})",
        "row_literal",
    ),
    (
        "return repo.run('INSERT INTO case_messages (message_id) VALUES (1)')",
        "sql_insert",
    ),
]


@pytest.mark.parametrize(
    "body,arm", _CAUGHT, ids=[b.split("\n")[-1] for b, _ in _CAUGHT]
)
def test_each_shape_a_writer_could_take_is_caught(body, arm):
    offenders = _offenders(_scan_source(_snippet(body), _ELSEWHERE))
    assert any(
        f"[{arm}]" in o for o in offenders
    ), f"arm {arm!r} did not catch {body!r}: {offenders}"


#: Rows from something other than the case being copied — a request body, an
#: import payload. Each once read as a "copy" because the arm checked only that
#: the value was SOME object's ``.messages``.
_ANOTHER_OBJECTS_ROWS = [
    "return Case(messages=body.messages)",
    "return Case(messages=list(body.messages))",
    "return case.model_copy(update={'messages': body.messages})",
    "return Case.model_validate({'messages': copy.deepcopy(body.messages)})",
    "case.atomic_update(messages=list(body.messages))",
    "return case.model_copy(update={'messages': body.messages.copy()})",
]


@pytest.mark.parametrize("body", _ANOTHER_OBJECTS_ROWS)
def test_a_copy_of_another_objects_rows_is_not_a_copy(body):
    offenders = _offenders(_scan_source(_snippet(body), _ELSEWHERE))
    assert any("[construction]" in o for o in offenders), offenders


#: What IS permitted: nothing added. The first is the live reclassification
#: copy, verbatim.
_NOTHING_ADDED = [
    "return case.model_copy(update={'messages': list(case.messages)})",
    "return case.model_copy(update={'messages': case.messages.copy()})",
    "return self.case.model_copy(update=dict(messages=deepcopy(self.case.messages)))",
    "case.atomic_update(messages=list(case.messages))",
    "return Case(title='t', messages=[])",
]


@pytest.mark.parametrize("body", _NOTHING_ADDED)
def test_a_copy_of_the_same_cases_rows_is_permitted(body):
    hits = _scan_source(_snippet(body), _ELSEWHERE)
    assert [h.arm for h in hits] == ["construction"], hits
    assert _offenders(hits) == []


def test_add_message_is_allowed_by_site_and_count_not_by_directory():
    """The arm used to excuse all of ``modules/case/infrastructure/``, so a
    new repository file there could call it unseen."""
    call = "return await repo.add_message(case_id, row)"
    new_file = _scan_source(
        f"class NewRepository:\n    async def add_message(self, case_id, row, repo):\n"
        f"        {call}\n",
        "faultmaven/modules/case/infrastructure/new_repository.py",
    )
    assert any("[add_message]" in o for o in _offenders(new_file))

    allowed_path, allowed_fn = next(iter(_ALLOWED["add_message"]))
    cls, fn = allowed_fn.split(".")
    once = (
        f"class {cls}:\n    async def {fn}(self, case_id, row, repo):\n        {call}\n"
    )
    assert _offenders(_scan_source(once, allowed_path)) == []

    twice = once + f"        {call}\n"
    assert any(
        "[add_message]" in o for o in _offenders(_scan_source(twice, allowed_path))
    ), "a second call at the allowed site must exceed its count"
