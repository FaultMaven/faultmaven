"""A migration revision does not change once it is on ``main``.

Alembic records which revisions a database has applied by id, not by content.
A change to a revision some database already applied never reaches that
database: ``alembic upgrade head`` finds nothing to do there, and the stamp then
claims a schema the database does not have. A table added to the baseline in
place went missing exactly that way from a database that had applied the
baseline before the amendment; nothing noticed until the startup schema check
(#1514) refused the boot. Schema changes therefore arrive as new revisions.

``alembic/revision_fingerprints.txt`` records each revision's fingerprint, one
``<revision> <fingerprint>`` line per revision: a digest of its code with
comments, docstrings and formatting left out, so an edit to prose leaves it
alone and a change to what the revision runs moves it. Plain text, not JSON:
detect-secrets reads every quoted hex string as a possible secret, and an
unquoted one is not.
A revision imports nothing from the application, so its own code is all that
decides what it runs. These tests fail when a revision's code no longer matches
its recorded fingerprint, when a revision has none recorded, and when a recorded
revision has no file.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_VERSIONS = _PROJECT_ROOT / "alembic" / "versions"
_MANIFEST = _PROJECT_ROOT / "alembic" / "revision_fingerprints.txt"
_MANIFEST_NAME = "alembic/revision_fingerprints.txt"

_DOCSTRING_HOLDERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _strip_docstrings(tree: ast.Module) -> None:
    """Drop each module, class and function docstring, in place."""
    for node in ast.walk(tree):
        if not isinstance(node, _DOCSTRING_HOLDERS):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            node.body = node.body[1:] or [ast.Pass()]


def _canonical(value: object) -> str:
    """The tree as text, the same under every supported interpreter.

    Not ``ast.dump``: its output varies with the Python version (3.12 added
    ``type_params`` to every function, 3.13 stopped printing empty fields). A
    field holding ``None`` or an empty list is left out, which is all those
    differences amount to; line and column attributes are never read.
    """
    if isinstance(value, ast.AST):
        fields = []
        for name in value._fields:
            item = getattr(value, name, None)
            if item is None or (isinstance(item, list) and not item):
                continue
            fields.append(f"{name}={_canonical(item)}")
        return f"{type(value).__name__}({','.join(fields)})"
    if isinstance(value, list):
        return "[" + ",".join(_canonical(item) for item in value) + "]"
    return repr(value)


def fingerprint(source: str) -> str:
    """What a revision runs, as 16 hex digits: comments, docstrings and
    formatting do not move it; any change to its statements does."""
    tree = ast.parse(source)
    _strip_docstrings(tree)
    return hashlib.sha256(_canonical(tree).encode("utf-8")).hexdigest()[:16]


def _revision_id(source: str, path: Path) -> str:
    """The module-level ``revision`` the file declares."""
    for node in ast.parse(source).body:
        target = None
        if isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        if (
            isinstance(target, ast.Name)
            and target.id == "revision"
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            return value.value
    raise AssertionError(f"{path.name} declares no `revision`")


def _revisions_on_disk() -> dict[str, tuple[Path, str]]:
    """Every revision in ``alembic/versions``: id -> (file, fingerprint)."""
    found: dict[str, tuple[Path, str]] = {}
    for path in sorted(_VERSIONS.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        revision = _revision_id(source, path)
        assert revision not in found, (
            f"{path.name} and {found[revision][0].name} both declare "
            f"revision {revision}"
        )
        found[revision] = (path, fingerprint(source))
    return found


def _recorded() -> dict[str, str]:
    """The manifest: revision -> fingerprint. Text from ``#`` to the end of a
    line is prose (each entry names its file that way); what remains on a line
    is nothing, or exactly ``<revision> <fingerprint>``."""
    recorded: dict[str, str] = {}
    for number, line in enumerate(
        _MANIFEST.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        assert (
            len(fields) == 2
        ), f"{_MANIFEST_NAME}:{number} is not `<revision> <fingerprint>`: {line!r}"
        revision, recorded_fingerprint = fields
        assert (
            revision not in recorded
        ), f"{_MANIFEST_NAME}:{number} records revision {revision} a second time"
        recorded[revision] = recorded_fingerprint
    return recorded


@pytest.mark.unit
@pytest.mark.architecture
def test_no_revision_changed_after_its_fingerprint_was_recorded() -> None:
    recorded = _recorded()
    changed = [
        (revision, path, now)
        for revision, (path, now) in _revisions_on_disk().items()
        if revision in recorded and recorded[revision] != now
    ]
    assert not changed, "\n\n".join(
        f"Revision {revision} ({path.name}) no longer matches its recorded "
        "fingerprint. A database that already applied it will never run it "
        "again: the change would reach new databases only, and every existing "
        "one would stay stamped at a schema it does not have. Put the change in "
        "a new revision instead. Only when the edit cannot change the schema any "
        "database ends with (a fix to a statement that could not run, say) "
        f"record the new fingerprint, `{revision} {now}`, in {_MANIFEST_NAME}, "
        "and say in the PR why no database is affected."
        for revision, path, now in changed
    )


@pytest.mark.unit
@pytest.mark.architecture
def test_every_revision_has_a_recorded_fingerprint() -> None:
    recorded = _recorded()
    missing = {
        revision: (path, now)
        for revision, (path, now) in _revisions_on_disk().items()
        if revision not in recorded
    }
    assert not missing, (
        f"Record the fingerprint of each new revision in {_MANIFEST_NAME}, so a "
        "later change to it is caught. Append, in chain order:\n"
        + "\n".join(
            f"{revision} {now}  # {path.stem}"
            for revision, (path, now) in missing.items()
        )
    )


@pytest.mark.unit
@pytest.mark.architecture
def test_every_recorded_revision_still_has_its_file() -> None:
    gone = sorted(set(_recorded()) - set(_revisions_on_disk()))
    assert not gone, (
        f"{_MANIFEST_NAME} records revisions no file declares: {gone}. A "
        "database stamped at a deleted revision cannot be migrated past it; "
        "restore the file, or drop the entry only for a revision that never "
        "reached main."
    )


_SAMPLE = '''"""Revision docstring."""
revision: str = "abc"
down_revision = None


def upgrade() -> None:
    """Upgrade."""
    op.execute("CREATE TABLE t (id int)")
'''


@pytest.mark.unit
@pytest.mark.architecture
@pytest.mark.parametrize(
    "variant",
    [
        _SAMPLE.replace('"""Revision docstring."""', '"""Reworded at length."""'),
        _SAMPLE.replace('    """Upgrade."""\n', ""),
        _SAMPLE.replace(
            '    op.execute("CREATE', '    # why\n    op.execute(\n        "CREATE'
        ).replace('int)")', 'int)"\n    )'),
    ],
    ids=["docstring reworded", "docstring removed", "comment and reformat"],
)
def test_prose_and_formatting_leave_the_fingerprint_alone(variant: str) -> None:
    assert fingerprint(variant) == fingerprint(_SAMPLE)


@pytest.mark.unit
@pytest.mark.architecture
@pytest.mark.parametrize(
    "variant",
    [
        _SAMPLE.replace("id int", "id bigint"),
        _SAMPLE.replace("down_revision = None", 'down_revision = "xyz"'),
        _SAMPLE + '    op.execute("CREATE INDEX i ON t (id)")\n',
        _SAMPLE.replace(
            '    """Upgrade."""\n', '    """Upgrade."""\n    op.drop_table("old")\n'
        ),
    ],
    ids=[
        "statement changed",
        "re-parented",
        "statement appended",
        "statement prepended",
    ],
)
def test_a_change_to_what_a_revision_runs_moves_the_fingerprint(variant: str) -> None:
    assert fingerprint(variant) != fingerprint(_SAMPLE)


@pytest.mark.unit
@pytest.mark.architecture
def test_the_fingerprint_is_pinned_for_a_known_source() -> None:
    """Pinned so a Python whose tree differs fails here, by name, rather than
    as every revision changing at once."""
    assert fingerprint(_SAMPLE) == "d2e5a52620a2ab19"  # pragma: allowlist secret
