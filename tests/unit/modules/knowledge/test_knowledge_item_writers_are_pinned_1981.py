"""The writers of a knowledge item's title are pinned (#1981).

``test_item_title_scrubbed_on_create_1981.py`` drives every path that writes a
title. These guards are what make that list complete: one place creates a
``knowledge_items`` row, one function updates its title, one call site says
``shipped=True``, and every caller of the slug mint is known. A new writer, or a
new spelling of one, fails here until its author decides what its title does.

They are AST walks, not text searches: they find the CALL, in whatever function
and under whatever spelling, and are not fooled by a comment or a docstring that
names the class.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from tests.source_tree import parse_file

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]


#: What slugs a title, directly or through a wrapper that reads one. A caller of
#: ANY of these is fed a title, and a new one fed a raw title is the leak.
SLUG_MINTS = {
    "generate_runbook_id",
    "runbook_id_from_parts",
    # The case path's wrappers: each mints from, or claims a slot for, an id
    # slugged from the title the model wrote.
    "mint_case_runbook_id",
    "case_disambiguated_runbook_id",
    "claim_case_draft_slot",
}


def slug_mint_callers(tree: ast.AST) -> set[tuple[str, str]]:
    """``(enclosing function, name)`` of every call to a slug mint or a wrapper."""
    parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name not in SLUG_MINTS:
            continue
        scope, cursor = "<module>", node
        while cursor in parents:
            cursor = parents[cursor]
            if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope = cursor.name
                break
        found.add((scope, name))
    return found


class TestEveryCallerOfTheSlugMintIsAudited:
    """``generate_runbook_id`` and ``runbook_id_from_parts`` slug whatever title
    they are given, and so do the case path's wrappers around them
    (``mint_case_runbook_id``, ``case_disambiguated_runbook_id``,
    ``claim_case_draft_slot``). Each caller below is fed a title that is already
    scrubbed; a new caller of ANY of them fails here until its author shows the
    same.

    =====================================  ====================================
    caller                                 the title it is fed
    =====================================  ====================================
    ``failure_modes._partition...``        a failure mode (document path):
    ``draft_slots`` (the taken-id refusal) scrubbed by ``_scrub_failure_mode_
    ``service._prepare_conversion``        titles`` right after the analysis,
                                           before the partition
    ``service._convert_single_...``        the case draft's frontmatter,
    (``mint_case_runbook_id``,             scrubbed and rewritten by
    ``claim_case_draft_slot``)             ``_scrub_content_title`` first
    ``draft_slots.claim_case_...``         the minted id (above) and the stem
    (``case_disambiguated_...``)
    ``case_authoring.mint_case_...``       the frontmatter it is handed: the
                                           scrubbed text above
    ``case_authoring.usable_title``        a predicate over the title; the
                                           result is never stored
    ``case_authoring.case_stem_...``       the case id, not a title
    ``case_authoring.case_disambig...``    the minted id (above) and the stem
    ``service.create_runbook_from_...``    the typed title, scrubbed at entry
    ``conversion.generate_runbook_id``     the definition of the above, over
                                           ``failure_mode.title``
    =====================================  ====================================
    """

    ALLOWED = {
        ("faultmaven/modules/knowledge/domain/models/conversion.py", "generate_runbook_id", "runbook_id_from_parts"),
        ("faultmaven/modules/knowledge/domain/case_authoring.py", "case_stem_runbook_id", "runbook_id_from_parts"),
        ("faultmaven/modules/knowledge/domain/case_authoring.py", "usable_title", "runbook_id_from_parts"),
        ("faultmaven/modules/knowledge/domain/case_authoring.py", "mint_case_runbook_id", "runbook_id_from_parts"),
        ("faultmaven/modules/knowledge/domain/case_authoring.py", "case_disambiguated_runbook_id", "runbook_id_from_parts"),
        ("faultmaven/modules/knowledge/domain/services/conversion_service/failure_modes.py", "_partition_failure_modes", "generate_runbook_id"),
        ("faultmaven/modules/knowledge/domain/services/conversion_service/draft_slots.py", "_refuse_modes_whose_id_is_taken", "generate_runbook_id"),
        ("faultmaven/modules/knowledge/domain/services/conversion_service/draft_slots.py", "claim_case_draft_slot", "case_disambiguated_runbook_id"),
        ("faultmaven/modules/knowledge/domain/services/conversion_service/service.py", "_prepare_conversion", "generate_runbook_id"),
        ("faultmaven/modules/knowledge/domain/services/conversion_service/service.py", "_convert_single_failure_mode", "mint_case_runbook_id"),
        ("faultmaven/modules/knowledge/domain/services/conversion_service/service.py", "_convert_single_failure_mode", "claim_case_draft_slot"),
        ("faultmaven/modules/knowledge/domain/services/conversion_service/service.py", "create_runbook_from_template", "runbook_id_from_parts"),
    }  # fmt: skip

    def test_the_callers_are_the_audited_ones(self):
        found = set()
        for path in sorted((REPO_ROOT / "faultmaven").rglob("*.py")):
            tree = _parse(path)
            if tree is None:
                continue
            where = path.relative_to(REPO_ROOT).as_posix()
            found |= {(where, scope, name) for scope, name in slug_mint_callers(tree)}
        assert found, "the walk found nothing: this guard is vacuous"
        assert found == self.ALLOWED, (
            f"a new caller of the slug mint: {sorted(found - self.ALLOWED)}; "
            f"a vanished one: {sorted(self.ALLOWED - found)}"
        )

    @pytest.mark.parametrize("name", sorted(SLUG_MINTS))
    def test_a_new_caller_of_each_mint_and_wrapper_is_seen(self, name):
        source = f"async def a_new_path(x):\n    return await {name}(x)\n"
        assert slug_mint_callers(ast.parse(source)) == {("a_new_path", name)}
        method = f"def a_new_path(self, x):\n    return self.{name}(x)\n"
        assert slug_mint_callers(ast.parse(method)) == {("a_new_path", name)}


# ---------------------------------------------------------------------------
# One creator of a knowledge item, one title updater, one `shipped=True`
# ---------------------------------------------------------------------------

#: What constructs a knowledge item row. ``KnowledgeItem`` is the domain object,
#: ``KnowledgeItemModel`` the ORM row; either one in a new place is a create
#: path ``test_item_title_scrubbed_on_create_1981.py`` does not cover.
ITEM_NAMES = {"KnowledgeItem", "KnowledgeItemModel"}

#: The table's name, as a string a script or a migration would hand SQLAlchemy.
TABLE_NAME = "knowledge_items"

#: ``insert into knowledge_items`` in a string: raw SQL is a create path too.
RAW_INSERT_RE = re.compile(r"(?is)\binsert\b[^;]{0,60}?\binto\s+\"?knowledge_items\b")

#: ``update knowledge_items set ... title``: raw SQL that rewrites a title.
RAW_TITLE_UPDATE_RE = re.compile(
    r"(?is)\bupdate\b\s+\"?knowledge_items\"?\s+set\b[^;]*\btitle\b"
)

#: Scanned: the package, and the two trees outside it that run against the same
#: database (a script or a migration can write a row as surely as a service).
SCANNED_ROOTS = ("faultmaven", "scripts", "alembic")

#: Function names that build a table object from its name.
TABLE_BUILDERS = {"table", "Table"}


def _parse(path: pathlib.Path) -> ast.AST | None:
    try:
        return parse_file(path)
    except SyntaxError:
        return None


def item_creators(tree: ast.AST) -> list[tuple[str, str]]:
    """``(spelling, enclosing function)`` for every way a module can write a
    knowledge item row that this guard knows.

    Catches: a bare call; a call through an alias import (``import X as Y``) or
    an attribute (``models.KnowledgeItemModel(...)``); ``getattr(module, "X")(...)``
    with a literal name; ``insert(KnowledgeItemModel)``, ``update(...)`` and
    ``bulk_insert(...)`` naming the model; ``KnowledgeItemModel.__table__``;
    ``Base.metadata.tables["knowledge_items"]``; ``sa.table("knowledge_items",
    ...)`` / ``Table(...)``; a raw ``INSERT INTO knowledge_items`` string and a
    raw ``UPDATE knowledge_items SET ... title``; and any of those at module
    level.

    RESIDUAL, not caught: ``instance.__class__(...)``, ``dataclasses.replace(item,
    ...)`` (both build a new item from one that already exists, which a creator
    must first have made); ``getattr(module, name)`` with a VARIABLE name;
    ``session.add(row)`` of a row built somewhere this walk does not look; and
    SQL assembled at run time. A reviewer is the guard for those.
    """
    aliases = set(ITEM_NAMES)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name.split(".")[-1] in ITEM_NAMES and alias.asname:
                    aliases.add(alias.asname)

    def names_an_item(node: ast.AST) -> bool:
        return any(
            (isinstance(x, ast.Name) and x.id in aliases)
            or (isinstance(x, ast.Attribute) and x.attr in ITEM_NAMES)
            or (isinstance(x, ast.Constant) and x.value in ITEM_NAMES)
            for x in ast.walk(node)
        )

    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    def enclosing(node: ast.AST) -> str:
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node.name
        return "<module>"

    def called(func: ast.AST) -> str:
        return func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")

    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Name) and func.id in aliases) or (
                isinstance(func, ast.Attribute) and func.attr in ITEM_NAMES
            ):
                found.append(("call", enclosing(node)))
            elif (
                isinstance(func, ast.Call)
                and isinstance(func.func, ast.Name)
                and func.func.id == "getattr"
                and names_an_item(func)
            ):
                found.append(("getattr-call", enclosing(node)))
            elif called(func) in {"insert", "bulk_insert"}:
                if any(names_an_item(arg) for arg in node.args) or (
                    isinstance(func, ast.Attribute) and names_an_item(func.value)
                ):
                    found.append(("insert()", enclosing(node)))
            elif called(func) == "update" and any(
                names_an_item(arg) for arg in node.args
            ):
                found.append(("update()", enclosing(node)))
            elif (
                called(func) in TABLE_BUILDERS
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == TABLE_NAME
            ):
                found.append(("table()", enclosing(node)))
        elif (
            isinstance(node, ast.Attribute)
            and node.attr == "__table__"
            and names_an_item(node.value)
        ):
            found.append(("__table__", enclosing(node)))
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "tables"
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == TABLE_NAME
        ):
            found.append(("tables[]", enclosing(node)))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if RAW_INSERT_RE.search(node.value):
                found.append(("raw-sql", enclosing(node)))
            elif RAW_TITLE_UPDATE_RE.search(node.value):
                found.append(("raw-update-title", enclosing(node)))
    return found


def _scan(roots=SCANNED_ROOTS) -> set[tuple[str, str, str]]:
    found = set()
    for root in roots:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            tree = _parse(path)
            if tree is None:
                continue
            for spelling, function in item_creators(tree):
                found.add((path.relative_to(REPO_ROOT).as_posix(), function, spelling))
    return found


def _creators(source: str) -> list[tuple[str, str]]:
    return item_creators(ast.parse(source))


REPOSITORY = (
    "faultmaven/modules/knowledge/infrastructure/persistence/"
    "knowledge_item_repository.py"
)


class TestOneCreatorOfAKnowledgeItem:
    """The premise ``test_item_title_scrubbed_on_create_1981.py`` rests on:
    ``ingest_runbook`` is the only place a ``knowledge_items`` row is created,
    and the repository the only place one is written. A second writer would be
    a path those tests do not cover, so it fails here until its author decides
    what its title does (and adds it to the table at the top of that file).
    """

    #: ``(file, function, spelling)``: the sites that may write the rows.
    ALLOWED = {
        # The one creator.
        (
            "faultmaven/modules/knowledge/domain/services/knowledge_service.py",
            "ingest_runbook",
            "call",
        ),
        # Read side: builds the domain object from a row it just selected.
        (REPOSITORY, "_to_domain", "call"),
        # The writer of the ``knowledge_items`` row ``ingest_runbook`` hands it.
        (REPOSITORY, "create", "call"),
        # The one targeted UPDATE (``update_fields``); WHICH fields it is handed
        # is pinned by ``TestTheOnlyTitleUpdaterIsUpdateDocumentMetadata``.
        (REPOSITORY, "update_fields", "update()"),
        # A no-op UPDATE (``is_published`` and ``updated_at`` set to themselves)
        # that takes SQLite's writer lock before a read (#1966); no title.
        (REPOSITORY, "read_is_published", "update()"),
        # Names the table, writes nothing: the ``has_table_privilege(..., 'UPDATE')``
        # probe the maintenance role check (#1966) reads the table's full name for.
        (REPOSITORY, "may_update", "__table__"),
        # Names the table, writes nothing: the authored-orphan sweep's
        # ``row_security_active(<table>)`` proof (#1982) reads its full name.
        (
            "faultmaven/bootstrap/kb_orphan_sweep.py",
            "_read_rows_if_complete",
            "__table__",
        ),
    }

    def test_no_other_function_writes_one(self):
        found = _scan()
        assert found, "the walk found nothing: this guard is vacuous"
        assert found == self.ALLOWED, (
            f"a new writer of a knowledge item: {sorted(found - self.ALLOWED)}; "
            f"a vanished one: {sorted(self.ALLOWED - found)}"
        )

    @pytest.mark.parametrize(
        "source",
        [
            "KnowledgeItem(item_id='x')",
            "from m import KnowledgeItemModel as Row\nRow(item_id='x')",
            "import m\ndef f():\n    return m.KnowledgeItemModel(item_id='x')",
            "def f():\n    return getattr(m, 'KnowledgeItemModel')(item_id='x')",
            "def f(s):\n    s.execute(insert(KnowledgeItemModel).values(a=1))",
            "def f(s):\n    s.execute(KnowledgeItemModel.__table__.insert())",
            "def f(s):\n    s.execute(text('INSERT INTO knowledge_items (a) VALUES (1)'))",
            "def f(op):\n    op.execute('insert into \"knowledge_items\" values (1)')",
            "def f(s):\n    t = Base.metadata.tables['knowledge_items']\n    s.execute(t.insert())",
            "def f(op):\n    t = sa.table('knowledge_items', sa.column('title'))\n    op.bulk_insert(t, [])",
            "def f(op):\n    op.bulk_insert(KnowledgeItemModel.__table__, [])",
            "def f(s):\n    s.execute(update(KnowledgeItemModel).values(title='x'))",
            "def f(s):\n    s.execute(sa.update(KnowledgeItemModel).values(title='x'))",
            "def f(op):\n    op.execute(\"UPDATE knowledge_items SET title = 'x'\")",
        ],
        ids=[
            "module-level",
            "alias",
            "attribute",
            "getattr",
            "insert()",
            "__table__",
            "raw-sql",
            "raw-sql-quoted",
            "metadata.tables[]",
            "sa.table()+bulk_insert",
            "bulk_insert(__table__)",
            "update().values(title)",
            "sa.update().values(title)",
            "raw-update-title",
        ],
    )
    def test_the_guard_bites_each_spelling_it_claims(self, source):
        assert _creators(source), source

    @pytest.mark.parametrize(
        "source",
        [
            "def f(s):\n    return s.execute(select(KnowledgeItemModel))",
            "def f(x):\n    return isinstance(x, KnowledgeItem)",
            "def f(s):\n    s.execute(text('SELECT * FROM knowledge_items'))",
            "def f(s):\n    s.execute(insert(OtherModel))",
            "def f(s):\n    s.execute(update(OtherModel).values(title='x'))",
            'def f(op):\n    op.execute("UPDATE knowledge_items SET is_published = false")',
            "def f(s, name):\n    return Base.metadata.tables[name]",
            "def f(s):\n    return Base.metadata.tables['cases']",
        ],
        ids=[
            "select",
            "isinstance",
            "raw-select",
            "insert-other",
            "update-other",
            "raw-update-other-column",
            "tables[name]",
            "tables-other",
        ],
    )
    def test_the_guard_does_not_flag_a_read_or_another_table(self, source):
        assert _creators(source) == [], source

    def test_the_scan_reaches_scripts_and_migrations(self):
        """A script or a migration can write a row as surely as a service, so
        the walk covers them, and each root has Python in it (an empty root
        would make the guard quietly narrower)."""
        assert {"faultmaven", "scripts", "alembic"} <= set(SCANNED_ROOTS)
        for root in SCANNED_ROOTS:
            assert list((REPO_ROOT / root).rglob("*.py")), root

    def test_the_residual_is_declared_not_hidden(self):
        """What the walk cannot see is named in its docstring."""
        assert _creators("def f(i):\n    return i.__class__(item_id='x')") == []
        assert _creators("def f(n):\n    return getattr(m, n)(item_id='x')") == []
        doc = item_creators.__doc__ or ""
        for residual in (
            "RESIDUAL",
            "__class__",
            "dataclasses.replace",
            "VARIABLE",
            "session.add",
        ):
            assert residual in doc, residual


def update_fields_calls(tree: ast.AST) -> list[tuple[str, set[str] | None]]:
    """``(enclosing function, literal field names)`` of every ``update_fields``
    call; the names are ``None`` when the fields are not a literal dict.

    The fields are read from where they are PASSED: the second positional
    argument, or the ``fields=`` keyword. A call with neither (``**kwargs``), a
    dict that spreads another (``{**other, ...}``), and anything but a dict
    literal are all "not a literal", which the pin below treats as a caller it
    must name. RESIDUAL: ``update_fields(id, **{"fields": ...})`` is a dict
    literal spread into keywords, which reads as no ``fields`` and so as
    "not a literal" too: flagged, not missed.
    """
    parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    calls = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", None) == "update_fields"
        ):
            continue
        scope, cursor = "<module>", node
        while cursor in parents:
            cursor = parents[cursor]
            if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope = cursor.name
                break
        fields = node.args[1] if len(node.args) >= 2 else None
        for keyword in node.keywords:
            if keyword.arg == "fields":
                fields = keyword.value
        if isinstance(fields, ast.Dict) and None not in fields.keys:
            names = {
                key.value
                for key in fields.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            # A computed key (`{name: v}`) is not a name the walk can read.
            calls.append((scope, names if len(names) == len(fields.keys) else None))
        else:
            calls.append((scope, None))
    return calls


class TestTheOnlyTitleUpdaterIsUpdateDocumentMetadata:
    """``repo.update_fields(item_id, fields)`` writes ANY listed column,
    ``title`` included. Every caller either hands it a literal dict that does
    not name ``title``, or is ``update_document_metadata``, the one function that
    puts a scrubbed, column-cut title into the fields it passes. The fields are
    read whether they are passed positionally or as ``fields=``."""

    ALLOWED_VARIABLE_FIELDS = {
        ("faultmaven/modules/knowledge/domain/services/knowledge_service.py", "update_document_metadata")
    }  # fmt: skip

    def _callers(self):
        literal_keys, variable = [], set()
        for path in sorted((REPO_ROOT / "faultmaven").rglob("*.py")):
            tree = _parse(path)
            if tree is None:
                continue
            where = path.relative_to(REPO_ROOT).as_posix()
            for scope, names in update_fields_calls(tree):
                if names is None:
                    variable.add((where, scope))
                else:
                    literal_keys.append((where, scope, names))
        return literal_keys, variable

    @pytest.mark.parametrize(
        "call,expected",
        [
            ("repo.update_fields(i, {'is_published': False})", {"is_published"}),
            ("repo.update_fields(i, {'title': t})", {"title"}),
            ("repo.update_fields(i, fields={'is_published': False})", {"is_published"}),
            ("repo.update_fields(i, fields={'a': 1, 'title': t}, commit=False)", {"a", "title"}),
            ("repo.update_fields(item_id=i, fields={'title': t})", {"title"}),
            ("repo.update_fields(i, fields)", None),
            ("repo.update_fields(i, fields=fields)", None),
            ("repo.update_fields(i, **kw)", None),
            ("repo.update_fields(i, {**other, 'a': 1})", None),
            ("repo.update_fields(i, {name: v})", None),
            ("repo.update_fields(i, fields=dict(title=t))", None),
        ],
    )  # fmt: skip
    def test_the_fields_are_read_wherever_they_are_passed(self, call, expected):
        tree = ast.parse(f"async def f(repo, i):\n    await {call}\n")
        ((scope, names),) = update_fields_calls(tree)
        assert (scope, names) == ("f", expected)

    def test_a_keyword_form_title_write_is_not_skipped(self):
        """The shape the first version of this pin missed: ``fields=`` as a
        keyword was skipped by a ``len(node.args) >= 2`` filter."""
        tree = ast.parse(
            "async def delete_document(repo, i, t):\n"
            "    await repo.update_fields(i, fields={'is_published': False, 'title': t})\n"
        )
        ((_, names),) = update_fields_calls(tree)
        assert "title" in names

    def test_only_update_document_metadata_writes_a_title_through_it(self):
        literal_keys, variable = self._callers()
        assert literal_keys, "the walk found no literal caller: this guard is vacuous"
        assert variable == self.ALLOWED_VARIABLE_FIELDS, sorted(
            variable ^ self.ALLOWED_VARIABLE_FIELDS
        )
        assert [c for c in literal_keys if "title" in c[2]] == []

    def test_update_document_metadata_scrubs_the_title_it_puts_in_the_fields(self):
        """The variable caller is the one that matters: the title it passes is
        the output of ``sanitize_item_title`` cut to the item's column."""
        path = (
            REPO_ROOT
            / "faultmaven/modules/knowledge/domain/services/knowledge_service.py"
        )
        tree = _parse(path)
        function = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
            and n.name == "update_document_metadata"
        )
        assignments = [
            n
            for n in ast.walk(function)
            if isinstance(n, ast.Assign)
            and any(
                isinstance(t, ast.Subscript)
                and isinstance(t.slice, ast.Constant)
                and t.slice.value == "title"
                for t in n.targets
            )
        ]
        assert len(assignments) == 1, "exactly one place assigns updates['title']"
        value = assignments[0].value
        assert isinstance(value, ast.Await) and (
            getattr(value.value.func, "id", None) == "sanitize_item_title"
        ), ast.dump(value)[:200]


class TestOnlyKbInitIsShipped:
    """``shipped=True`` skips the title scrub, so it is stated in exactly one
    place: the pack's ingest. Any other call that passes it anything but the
    literal ``False`` is a user path that opted out. (``**{"shipped": ...}`` is
    the residual an AST keyword scan cannot see.)"""

    ALLOWED = {("faultmaven/bootstrap/kb_init.py", "_ingest_pack_runbook")}

    def test_nothing_else_passes_it(self):
        found = set()
        for root in SCANNED_ROOTS:
            for path in sorted((REPO_ROOT / root).rglob("*.py")):
                tree = _parse(path)
                if tree is None:
                    continue
                parents = {
                    c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)
                }
                for node in ast.walk(tree):
                    if not (isinstance(node, ast.Call) and node.keywords):
                        continue
                    for keyword in node.keywords:
                        if keyword.arg != "shipped":
                            continue
                        if (
                            isinstance(keyword.value, ast.Constant)
                            and keyword.value.value is False
                        ):
                            continue
                        scope, cursor = "<module>", node
                        while cursor in parents:
                            cursor = parents[cursor]
                            if isinstance(
                                cursor, (ast.FunctionDef, ast.AsyncFunctionDef)
                            ):
                                scope = cursor.name
                                break
                        found.add((path.relative_to(REPO_ROOT).as_posix(), scope))
        assert found == self.ALLOWED, sorted(found ^ self.ALLOWED)
