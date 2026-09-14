"""A conversation row has ONE kind field, and it is `role` (#1397).

`CaseMessage` carries `role` (`"user" | "assistant" | "system"`), per
`case-storage-design.md` §4.7, and that is what the CHECK constraint enforces.
A `message_type` was written beside it into every persisted row and read by
nobody — `MessageType`'s last member reference went with #1392.

Two names for one fact, one of them written and never read, is not untidiness:
it is what produced #1390. Three call sites in `CaseService` screened on
`message_type` because the rows appeared to have it, each swallowed the
resulting `AttributeError`, and one of them turned a successful resume into a
404. The scan below is the recurrence guard, generalised from the single-file
one #1392 added.
"""

import ast
import pathlib

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.architecture]

#: The trees this walks, resolved from this file rather than from a cwd.
_ROOT = pathlib.Path(__file__).resolve().parents[3]
_PACKAGE = _ROOT / "faultmaven"

#: `tests/` too. A fixture that still builds a row the retired way models
#: something no writer in the codebase produces, so a regression that re-added
#: a READ of the key would find it present in exactly the tests meant to catch
#: that — which is how a suite ends up agreeing with the bug.
_TREES = (_PACKAGE, _ROOT / "tests")

#: A floor, so a scan that resolved the wrong directory cannot report clean.
#: Set against the real count (~1,500 across both trees at the time of
#: writing) rather than a guess:
#: a floor of 200 clears even a walk that lost more than half the tree, which
#: is exactly the resolution error it is supposed to exclude.
_MIN_MODULES_SCANNED = 1200

#: A module the walk must have visited. The floor bounds SIZE; this bounds
#: IDENTITY, because a large-but-wrong subtree clears any count.
_MUST_HAVE_SCANNED = pathlib.Path(
    "faultmaven/modules/case/domain/services/case_service.py"
)

#: Keys that identify a dict literal as a CONVERSATION row rather than some
#: other dict that happens to carry a `message_type`. `ComponentMessage` in
#: `modules/agent/domain/models/agentic.py` is inter-component messaging and
#: has a legitimate field of that name — banning the word everywhere would
#: catch it, which is a guard that has to be argued with rather than obeyed.
#:
#: TWO of these are required, not one. One was too weak in both directions: a
#: row that carries `message_type` INSTEAD of `role` — the shape
#: `MinimalCaseService` actually had, and the shape this issue is about — has
#: no `role`, and was caught only because it happened to also carry a
#: `message_id` that repositories will mint for it anyway.
_CONVERSATION_ROW_KEYS = {
    "message_id",
    "case_id",
    "role",
    "turn_number",
    "content",
    "author_id",
    "created_at",
}

#: Constructors that build a conversation row. `CaseMessage` is unambiguous.
#: `Message` is NOT — it is a generic name, and `agentic.py` already has a
#: `create_component_message` factory passing `message_type=`; one rename and
#: an unqualified ban would fail citing this issue on code that has nothing to
#: do with conversations. So a `Message(...)` only counts when it also carries
#: a row keyword.
_ROW_CONSTRUCTORS = {"CaseMessage"}
_AMBIGUOUS_CONSTRUCTORS = {"Message"}

#: Fields a conversation row must never carry. `session_id` is here because it
#: was half of the #1390 constructor shape — `CaseMessage(session_id=...,
#: message_type=...)` — and the single-file guard in
#: `test_case_service.py` bans both. A package-wide generalisation that dropped
#: one would be strictly weaker than the thing it generalises.
_NEVER_ON_A_ROW = {"message_type", "session_id"}


def _modules():
    # `encoding` is pinned: the package is full of em dashes and `∪`, and
    # `read_text()` without it follows the ambient locale — under a
    # POSIX-locale runner this would die with UnicodeDecodeError instead of
    # returning a verdict.
    for tree in _TREES:
        for path in sorted(tree.rglob("*.py")):
            yield path, path.read_text(encoding="utf-8")


def _called_name(func: ast.expr) -> str | None:
    """The bare name of a call target, through a qualified path.

    `api_models.CaseMessage(...)` is an `ast.Attribute`, not an `ast.Name`;
    matching only the latter let a qualified import reintroduce the shape.
    """
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _row_keys(keys) -> set:
    return {
        k.value
        for k in keys
        if isinstance(k, ast.Constant) and isinstance(k.value, str)
    }


def test_no_conversation_row_carries_a_retired_kind():
    """Parsed, not grepped: a substring search matches comments and docstrings,
    and this module's own prose would trip it.

    Only the WRITERS are guarded, and that is sufficient rather than a
    compromise: a row that never carries the key cannot be screened on it, so
    the reads that #1390 was made of are unreachable once the writes are. It is
    also the arm that can be identified precisely — a read like
    `msg.message_type` is indistinguishable from any other object's field
    without types, which is the over-reach this guard's first version
    committed.
    """
    scanned = set()
    offenders = []

    def report(path, lineno, what):
        offenders.append(f"{path.relative_to(_ROOT)}:{lineno} ({what})")

    for path, source in _modules():
        scanned.add(path.relative_to(_ROOT))

        # Cheap pre-filter: a file whose TEXT does not contain the name cannot
        # contain an AST node naming it, so there is nothing to parse. This is
        # not the verdict — the tree below is — it just keeps a two-tree walk
        # from parsing ~1,500 modules to look at about ten. (Parsing all of
        # them cost 11s; this is a fraction of a second.)
        if not any(name in source for name in _NEVER_ON_A_ROW):
            continue

        # `filename` so a SyntaxWarning or SyntaxError names the module rather
        # than `<unknown>`, and a broken module reports instead of aborting the
        # whole verdict with a traceback naming nothing.
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:  # pragma: no cover - a broken module
            offenders.append(f"{path.relative_to(_ROOT)}: unparseable ({exc})")
            continue

        for node in ast.walk(tree):
            # A row built as a dict literal, identified by its siblings.
            if isinstance(node, ast.Dict):
                keys = _row_keys(node.keys)
                banned = keys & _NEVER_ON_A_ROW
                if banned and len(keys & _CONVERSATION_ROW_KEYS) >= 2:
                    for key in node.keys:
                        if isinstance(key, ast.Constant) and key.value in banned:
                            # The KEY's line, not the opening brace: these
                            # literals run to 18 lines in `investigation_service`.
                            report(path, key.lineno, f"dict key {key.value!r}")

            elif isinstance(node, ast.Call):
                name = _called_name(node.func)
                kwargs = {k.arg for k in node.keywords if k.arg}
                # `dict(message_type=..., role=...)` is a Call, never an ast.Dict.
                if name == "dict":
                    banned = kwargs & _NEVER_ON_A_ROW
                    if banned and len(kwargs & _CONVERSATION_ROW_KEYS) >= 2:
                        for keyword in node.keywords:
                            if keyword.arg in banned:
                                report(path, keyword.lineno, f"dict() {keyword.arg}=")
                elif name in _ROW_CONSTRUCTORS or (
                    name in _AMBIGUOUS_CONSTRUCTORS and kwargs & _CONVERSATION_ROW_KEYS
                ):
                    for keyword in node.keywords:
                        if keyword.arg in _NEVER_ON_A_ROW:
                            report(path, keyword.lineno, f"{name}({keyword.arg}=)")

            # `row["message_type"] = ...` after the literal was built.
            #
            # `message_type` ONLY, not the whole banned set: a subscript store
            # cannot be attributed to a conversation row the way a literal can
            # (there are no sibling keys to read), and `x["session_id"] = ...`
            # is ordinary elsewhere — `infrastructure/logging/config.py` sets
            # one on a log record. `message_type` has no such second life; the
            # constructor arm above is what covers `session_id`.
            elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
                key = node.slice
                if isinstance(key, ast.Constant) and key.value == "message_type":
                    report(path, node.lineno, "assigned ['message_type']")

    assert len(scanned) >= _MIN_MODULES_SCANNED, (
        f"only {len(scanned)} modules scanned under {_TREES} — this walk "
        "resolved the wrong directory, so its clean verdict says nothing"
    )
    assert _MUST_HAVE_SCANNED in scanned, (
        f"{_MUST_HAVE_SCANNED} was not visited, so the walk covered a subtree "
        "rather than the package and its clean verdict says nothing"
    )
    assert offenders == [], (
        "a conversation row's kind is `role` and it carries no session; these "
        "build one otherwise:\n  "
        + "\n  ".join(offenders)
        + "\nSee #1397 — and #1390 for what the second name cost."
    )


def test_the_enum_is_gone():
    """It had no members referenced anywhere and two public re-exports, which
    is what keeps a retired vocabulary importable and therefore reachable.

    Each absence is paired with a presence: this package is installed editable
    and the campaign runs a worktree per lane, so `import faultmaven.models`
    can bind a different tree than `_PACKAGE` scans — and a renamed module
    would make every `not hasattr` pass vacuously.
    """
    import faultmaven.models as models
    import faultmaven.modules.case as case_module
    from faultmaven.modules.case.domain import models as domain_models

    for module, control in (
        (domain_models, "CaseState"),
        (case_module, "CaseState"),
        (models, "CaseState"),
    ):
        assert hasattr(module, control), (
            f"{module.__name__} does not export {control}, so it is not the "
            "module this test means and its verdict below says nothing"
        )
        assert not hasattr(module, "MessageType")
