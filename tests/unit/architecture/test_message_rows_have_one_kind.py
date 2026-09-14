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

#: The package root, resolved from this file rather than from a cwd.
_PACKAGE = pathlib.Path(__file__).resolve().parents[3] / "faultmaven"

#: A floor, so a scan that resolved the wrong directory cannot report clean.
#: The package is ~700 modules; this only has to be high enough that an empty
#: or truncated walk is impossible.
_MIN_MODULES_SCANNED = 200


def _modules():
    # `encoding` is pinned: the package is full of em dashes and `∪`, and
    # `read_text()` without it follows the ambient locale — under a
    # POSIX-locale runner this would die with UnicodeDecodeError instead of
    # returning a verdict.
    for path in sorted(_PACKAGE.rglob("*.py")):
        yield path, path.read_text(encoding="utf-8")


#: Keys that identify a dict literal as a CONVERSATION row rather than some
#: other dict that happens to carry a `message_type`. `ComponentMessage` in
#: `modules/agent/domain/models/agentic.py` is inter-component messaging and
#: has a legitimate field of that name — banning the word everywhere would
#: catch it, which is a guard that has to be argued with rather than obeyed.
_CONVERSATION_ROW_KEYS = {"message_id", "role", "turn_number"}


def test_no_conversation_row_carries_a_message_type():
    """Parsed, not grepped: a substring search matches comments and docstrings,
    and this module's own prose would trip it.

    Only the WRITERS are guarded, and that is sufficient rather than a
    compromise: a row that never carries the key cannot be screened on it, so
    the reads that #1390 was made of are unreachable once the writes are. It is
    also the arm that can be identified precisely — a read like
    `msg.message_type` is indistinguishable from any other object's field
    without types, which is exactly the over-reach this guard's first version
    committed.
    """
    scanned = 0
    offenders = []

    for path, source in _modules():
        scanned += 1
        tree = ast.parse(source)
        for node in ast.walk(tree):
            # A row being built as a dict, identified by its siblings.
            if isinstance(node, ast.Dict):
                keys = {
                    k.value
                    for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
                if "message_type" in keys and keys & _CONVERSATION_ROW_KEYS:
                    offenders.append(f"{path.relative_to(_PACKAGE)}:{node.lineno}")
            # A row being built as a model — the #1390 shape exactly.
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"CaseMessage", "Message"}
            ):
                for keyword in node.keywords:
                    if keyword.arg == "message_type":
                        offenders.append(
                            f"{path.relative_to(_PACKAGE)}:{keyword.lineno}"
                        )

    assert scanned >= _MIN_MODULES_SCANNED, (
        f"only {scanned} modules scanned under {_PACKAGE} — this walk resolved "
        "the wrong directory, so its clean verdict says nothing"
    )
    assert offenders == [], (
        "a conversation row's kind is `role`; these build one with "
        "`message_type`:\n  "
        + "\n  ".join(offenders)
        + "\nSee #1397 — and #1390 for what the second name cost."
    )


def test_the_enum_is_gone():
    """It had no members referenced anywhere and two public re-exports, which
    is what keeps a retired vocabulary importable and therefore reachable."""
    import faultmaven.models as models
    import faultmaven.modules.case as case_module
    from faultmaven.modules.case.domain import models as domain_models

    assert not hasattr(domain_models, "MessageType")
    assert not hasattr(case_module, "MessageType")
    assert not hasattr(models, "MessageType")
