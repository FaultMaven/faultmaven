"""No frontmatter load accepts a YAML anchor or alias, or nesting past three
levels (#2674, #3116 A).

``yaml.safe_load`` keeps an alias as a shared reference, so a few hundred
characters describe a value whose string form is enormous: the reviewer's
runbook, six levels of ten-fold aliases under ``service``, passed the gate and
then built a 5.2M-character string on the event loop. Every frontmatter load
now refuses an anchor or alias as it is composed, before any value is built:
the gate with its own message, every other load as unparseable frontmatter.
Nesting is bounded too: ``frontmatter.dumps`` re-emits a nested flow sequence
one indent per level, so an 81,504-character draft of sequences 200 deep, each
with a sibling at every level, passed the gate and published 3,313,586
characters at ``82d3830d7``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import frontmatter
import pytest
import yaml
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.exceptions import ValidationException
from faultmaven.infrastructure.persistence.models import (
    Base,
    ConversionDraftModel,
    EnterpriseModel,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.service import (
    ConversionService,
)
from faultmaven.modules.knowledge.domain.services.preparation import (
    _runbook_frontmatter_fields,
)
from faultmaven.modules.knowledge.domain.services.runbook_gate import (
    avalidate_and_score,
)
from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    RunbookValidator,
)
from faultmaven.utils import frontmatter as frontmatter_utils
from faultmaven.utils.frontmatter import (
    ANCHOR_REFUSAL,
    DEPTH_REFUSAL,
    MAX_FRONTMATTER_DEPTH,
    FrontmatterAnchorRefused,
    FrontmatterTooDeep,
    extract_frontmatter_metadata,
    load_frontmatter_yaml,
    load_post,
    match_frontmatter,
    parse_frontmatter,
)
from tests.runbook_samples import valid_runbook
from tests.unit.modules.knowledge.test_drafts_live_in_the_database_1950 import (
    TEMPLATE,
    _ingesting,
    _Replica,
    _row,
)

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]

_PACK = Path(__file__).resolve().parents[4] / "resources/knowledge/pack"


def _bomb(title: str = "Alias Bomb In The Service Field") -> str:
    """The reviewer's shape: ``service`` is ``*l5`` over six levels of
    ten-fold aliases, 1e5 copies of ten letters once expanded."""
    levels = ['l0: &l0 "aaaaaaaaaa"'] + [
        f"l{i}: &l{i} [" + ", ".join([f"*l{i - 1}"] * 10) + "]" for i in range(1, 6)
    ]
    return valid_runbook(title).replace(
        "service: postgresql\n", "\n".join(levels) + "\nservice: *l5\n", 1
    )


def _nested(depth: int = 200, total: int = 81_504) -> str:
    """Flow sequences ``depth`` deep with a sibling at every level, as many as
    fit a draft of ``total`` characters (the body padded to it)."""
    item = "[" * depth + "a" + ", b]" * depth
    base = valid_runbook("Nested Flow Sequences In Frontmatter")
    count = (total - len(base) - 8) // (len(item) + 2)
    line = "x: [" + ", ".join([item] * count) + "]\n"
    draft = base.replace("status: draft\n", "status: draft\n" + line, 1)
    return draft + "\n" + "z" * (total - len(draft) - 1)


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(
            EnterpriseModel(enterprise_id=STANDALONE_ENTERPRISE_ID, name="E", slug="e")
        )
        await session.commit()
    yield factory
    await engine.dispose()


# -- the loader ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "a: &x 1",
        "a: &x 1\nb: *x",
        "b: &b {x: 1}\nc:\n  <<: *b",
        "a: !!str &x 1",
        "- &a [1]",
    ],
    ids=["anchor", "alias", "merge-key", "tagged-anchor", "sequence-anchor"],
)
def test_an_anchor_or_alias_is_refused(text):
    with pytest.raises(FrontmatterAnchorRefused, match=r"anchors and aliases"):
        load_frontmatter_yaml(text)


@pytest.mark.parametrize(
    "text",
    [
        'glob: "*.log"',
        'pair: "a & b"',
        "plain: foo*bar",
        "plain: a&b",
        "quoted: '*x'",
        'list: [x, "&y"]',
    ],
)
def test_text_that_only_looks_like_an_anchor_loads_as_before(text):
    assert load_frontmatter_yaml(text) == yaml.safe_load(text)


@pytest.mark.parametrize(
    "text, refused",
    [
        ("a: [[x]]", False),
        ("a: [[[x]]]", True),
        ("a: {b: [x]}", False),
        ("a: {b: {c: x}}", False),
        ("a: {b: {c: [x]}}", True),
        ("- [[x]]", False),
        ("- [[[x]]]", True),
    ],
)
def test_nesting_past_the_declared_depth_is_refused(text, refused):
    assert MAX_FRONTMATTER_DEPTH == 3
    if refused:
        with pytest.raises(FrontmatterTooDeep, match="at most 3 levels"):
            load_frontmatter_yaml(text)
    else:
        assert load_frontmatter_yaml(text) == yaml.safe_load(text)


def test_the_loads_are_libyamls_where_it_is_built():
    """The pure-Python loader is about 10x slower, on the event loop."""
    if hasattr(yaml, "CSafeLoader"):
        assert frontmatter_utils._LOADER is yaml.CSafeLoader


def test_a_text_that_does_not_open_with_the_delimiter_loads_as_before():
    """Forcing the handler on a text with a leading BOM split a body rule as
    frontmatter; the handler is passed only when it detects its format."""
    bom = "\ufeff" + valid_runbook() + "\n---\n\nkey: from the body\n"
    assert load_post(bom).metadata == frontmatter.loads(bom).metadata == {}
    assert load_post(bom).content == frontmatter.loads(bom).content


def test_every_shipped_frontmatter_loads_as_before():
    blocks = [
        match.group(1)
        for path in sorted(_PACK.rglob("*.md"))
        if (match := match_frontmatter(path.read_text(encoding="utf-8")))
    ]
    assert len(blocks) > 50, "pack missing: this pin would be vacuous"
    for block in blocks:
        assert load_frontmatter_yaml(block) == yaml.safe_load(block)


# -- every load refuses the bomb ----------------------------------------------


def test_the_bomb_is_unparseable_frontmatter_to_the_in_process_reads(caplog):
    with caplog.at_level(logging.WARNING, logger="faultmaven.utils.frontmatter"):
        assert parse_frontmatter(_bomb()) == {}
        assert extract_frontmatter_metadata(_bomb()) == {}
    assert any("FrontmatterAnchorRefused" in r.getMessage() for r in caplog.records)
    assert _runbook_frontmatter_fields(_bomb()) == frozenset()


def test_python_frontmatter_loads_refuse_the_bomb_and_load_a_runbook():
    with pytest.raises(FrontmatterAnchorRefused):
        load_post(_bomb())
    runbook = valid_runbook()
    assert load_post(runbook).metadata == frontmatter.loads(runbook).metadata


def test_the_gate_refuses_the_bomb_in_its_own_words():
    errors = RunbookValidator().validate_content(_bomb()).errors

    assert ANCHOR_REFUSAL in errors
    assert "No YAML frontmatter found" not in errors
    assert ANCHOR_REFUSAL == (
        "YAML anchors and aliases (&name, *name) are not allowed in runbook "
        "frontmatter; write each value out in full"
    )


async def test_the_gate_worker_refuses_the_bomb():
    validation, _ = await avalidate_and_score(_bomb(), principal="u1")

    assert validation.passed is False
    assert ANCHOR_REFUSAL in validation.errors


async def test_the_scan_imports_the_bomb_refused_and_unexpanded(
    session_factory, tmp_path, monkeypatch
):
    pod = _Replica(tmp_path, session_factory)
    (tmp_path / "global").mkdir()
    (tmp_path / "global" / "bomb.md").write_text(_bomb())
    monkeypatch.setattr(ConversionService, "_data_dir", property(lambda self: tmp_path))

    result = await pod.service.scan_for_runbooks(user_id="u1", is_platform_admin=True)

    async with session_factory() as session:
        rows = (await session.execute(ConversionDraftModel.__table__.select())).all()
    assert len(rows) == 1, result
    row = rows[0]._mapping
    assert ANCHOR_REFUSAL in row["validation_errors"]
    assert row["service"] is None and row["domain"] is None


def test_the_gate_refuses_the_nested_draft_it_passed():
    draft = _nested()
    assert len(draft) == 81_504

    errors = RunbookValidator().validate_content(draft).errors

    assert DEPTH_REFUSAL in errors and "No YAML frontmatter found" not in errors


@pytest.mark.parametrize("draft", ["bomb", "nested"])
async def test_verify_of_a_stale_refused_draft_is_a_422_and_publishes_nothing(
    session_factory, tmp_path, monkeypatch, draft
):
    """Verify refuses a draft that failed the gate first (``validation_passed``),
    so its load is reached only by a draft stored before the refusals existed:
    refused as the gate refuses it, its row's verdict cleared."""
    content, refusal = {
        "bomb": (_bomb(), ANCHOR_REFUSAL),
        "nested": (_nested(), DEPTH_REFUSAL),
    }[draft]
    pod = _Replica(tmp_path, session_factory, _ingesting(session_factory))
    monkeypatch.chdir(pod.root)
    created = await pod.service.create_runbook_from_template(**TEMPLATE)
    cid, did = created["conversion_id"], created["draft"].draft_id
    async with session_factory() as session:
        await session.execute(
            update(ConversionDraftModel)
            .where(ConversionDraftModel.id == did)
            .values(content=content, validation_passed=True)
        )
        await session.commit()

    with pytest.raises(ValidationException) as refused:
        await pod.service.verify_draft(cid, did, "u1")

    assert str(refused.value) == refusal
    pod.service._knowledge_service.ingest_runbook.assert_not_awaited()
    row = await _row(session_factory, did)
    assert (row.status, row.validation_passed) == ("draft", False)
    assert row.validation_errors == [refusal]


async def test_a_verified_draft_reloads_through_the_loader(
    session_factory, tmp_path, monkeypatch
):
    """``frontmatter.dumps`` writes an anchor for a value two keys share; a
    published text that held one would be refused by the next load."""
    pod = _Replica(tmp_path, session_factory, _ingesting(session_factory))
    monkeypatch.chdir(pod.root)
    created = await pod.service.create_runbook_from_template(**TEMPLATE)
    cid, did = created["conversion_id"], created["draft"].draft_id
    edited = valid_runbook("Connection Pool Exhausted Round Trip").replace(
        "status: draft\n", "status: draft\ntags: [postgres, pool]\n", 1
    )
    assert (await pod.service.update_draft(cid, did, "u1", edited)).validation.passed

    await pod.service.verify_draft(cid, did, "u1")

    published = pod.service._knowledge_service.ingest_runbook.await_args
    post = load_post(published.kwargs["content"])
    assert post.metadata["status"] == "verified"
    assert post.metadata["tags"] == ["postgres", "pool"]
    assert post.metadata == frontmatter.loads(published.kwargs["content"]).metadata
