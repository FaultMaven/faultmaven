"""A keyed conversion's retry is answered from its job, not a copy (#2452),
and the job reads back as its first answer did (#2451).

The receipt of a keyed ``POST /knowledge/convert`` stored the whole 201 body,
every draft's text included, and a draft edit never reached it: text edited
out of a draft stayed stored and was served again under the key. The receipt
now names the job only, and a retry is answered with the job as
``GET /knowledge/conversions/{id}`` reads it. That read dropped each draft's
``quality_warning`` and, on SQLite, ``created_at``'s offset; it now returns
what the convert answer returned.

Driven through the routes on the #2433 harness (FK-on SQLite, the model calls
stubbed), so the naive-datetime read is the one exercised.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.idempotency_key import (
    IDEMPOTENCY_REPLAY_UNAVAILABLE,
    IDEMPOTENCY_REPLAYED_HEADER,
)
from faultmaven.infrastructure.persistence.models import (
    ConversionDraftModel,
    ConversionJobModel,
    KbRequestReceiptModel,
)
from faultmaven.modules.knowledge.domain.models.conversion import (
    AnalysisResult,
    ConversionDraft,
    ConversionStatus,
    SourceAssessment,
    SourceFileInfo,
)
from faultmaven.modules.knowledge.domain.models.validation import (
    QualityScore,
    ValidationResult,
)
from faultmaven.modules.knowledge.domain.services.conversion_service import (
    runbook_scan,
)
from faultmaven.modules.knowledge.domain.services.conversion_service import (
    service as conversion_module,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.job_persistence import (
    _persist_job,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.runbook_scan import (
    CONVERSION_QUALITY_WARNING,
    SCAN_QUALITY_WARNING,
    TEMPLATE_QUALITY_WARNING,
)
from tests.integration.modules.knowledge.test_conversion_read_back import (
    RUNBOOK,
    _mode,
)
from tests.integration.modules.knowledge.test_kb_request_receipts_2433 import (  # noqa: F401
    OWNER,
    session_factory,
    world,
)
from tests.runbook_samples import valid_runbook
from tests.utils import regex_only_sanitizer

pytestmark = [pytest.mark.integration, pytest.mark.knowledge_base]

#: Text the user edits out of a draft after converting it.
MARKER = "Marker2452EditedOut"

TWO_MODES = AnalysisResult(
    is_actionable=True,
    failure_modes=[
        _mode("fm-0", "Redis Memory Shortfall", "saturation"),
        _mode("fm-1", "Redis Eviction Storm", "latency"),
    ],
    source_assessment=SourceAssessment(
        content_type="doc", actionability_rating="high", missing_information=[]
    ),
)


def _scored(low_word: str):
    """The runbook check, scoring a draft 30 when it names ``low_word`` and 80
    otherwise, so one draft is below the warning threshold and one above."""

    async def check(content, **_kwargs):
        overall = 30.0 if low_word in content.lower() else 80.0
        return ValidationResult(passed=True), _score(overall)

    return check


def _score(overall: float) -> QualityScore:
    return QualityScore(
        overall=overall,
        grade="F" if overall < 50 else "B",
        completeness=overall,
        clarity=overall,
        actionability=overall,
        comprehensiveness=overall,
    )


@pytest.fixture
def two_drafts(world, monkeypatch):
    """A conversion of two drafts, the shortfall one below the threshold."""
    world.analyze.return_value = TWO_MODES
    monkeypatch.setattr(conversion_module, "avalidate_and_score", _scored("shortfall"))
    return world


def _url(conversion_id: str, draft_id: str = "") -> str:
    url = f"/api/v1/knowledge/conversions/{conversion_id}"
    return f"{url}/drafts/{draft_id}" if draft_id else url


def _drafts_by_id(body: dict) -> dict:
    return {d["draft_id"]: d for d in body["drafts"]}


def _ordered(body: dict) -> dict:
    """``body`` with its drafts in ``draft_id`` order. The read is unsorted:
    on PostgreSQL its order need not be the first answer's, so bodies are
    compared by draft."""
    return {**body, "drafts": sorted(body["drafts"], key=lambda d: d["draft_id"])}


async def _read(world, conversion_id: str) -> dict:
    response = await world.client.get(_url(conversion_id))
    assert response.status_code == 200, response.text
    return response.json()


async def _receipt_response(world) -> dict:
    async with world.factory() as session:
        return (
            await session.execute(select(KbRequestReceiptModel.response))
        ).scalar_one()


async def _set_status(world, draft_id: str, status: str) -> None:
    """The row as a discard or a verify leaves it (both run more, which this
    harness does not wire: the trace eraser, the indexing)."""
    async with world.factory() as session:
        await session.execute(
            update(ConversionDraftModel)
            .where(ConversionDraftModel.id == draft_id)
            .values(status=status)
        )
        await session.commit()


async def _count(world, model) -> int:
    async with world.factory() as session:
        return (
            await session.execute(select(func.count()).select_from(model))
        ).scalar_one()


async def _edit_out_marker(world, body: dict) -> str:
    """Edit ``MARKER`` out of the body's first draft; the edited text."""
    draft = body["drafts"][0]
    assert MARKER in draft["content"]
    edited = draft["content"].replace(MARKER, "Edited")
    put = await world.client.put(
        _url(body["conversion_id"], draft["draft_id"]), json={"content": edited}
    )
    assert put.status_code == 200, put.text
    return edited


def _with_marker(world):
    world.conversion._llm_router.route = AsyncMock(
        return_value=SimpleNamespace(
            content=RUNBOOK.replace("Alert on the", f"{MARKER}: alert on the"),
            is_truncated=False,
        )
    )


def _claimless(world):
    """No claim store, and a lookup that misses once: the next keyed request
    runs, and its receipt INSERT is refused at the key (``KbReceiptExistsError``)."""
    world.app.state.redis_client = None
    real, calls = world.conversion.get_request_receipt, []

    async def lookup(key):
        calls.append(key)
        return None if len(calls) == 1 else await real(key)

    world.conversion.get_request_receipt = lookup


async def test_an_edited_drafts_text_is_neither_stored_nor_replayed(
    world,
):
    _with_marker(world)
    first = await world.post("convert")
    assert first.status_code == 201, first.text
    original = first.json()["drafts"][0]["content"]

    edited = await _edit_out_marker(world, first.json())
    stored = json.dumps(await _receipt_response(world))
    again = await world.post("convert")

    assert json.loads(stored) == {"conversion_id": first.json()["conversion_id"]}
    assert MARKER not in stored and original not in stored
    assert again.status_code == 201, again.text
    assert again.headers[IDEMPOTENCY_REPLAYED_HEADER] == "true"
    assert again.json()["drafts"][0]["content"] == edited
    assert MARKER not in again.text
    assert again.json() == await _read(world, first.json()["conversion_id"])
    assert await _count(world, ConversionJobModel) == 1
    assert await _count(world, ConversionDraftModel) == 1


async def test_the_read_back_warns_and_dates_as_the_first_answer(two_drafts):
    """On SQLite, where ``created_at`` reads back naive."""
    answer = await two_drafts.post("convert")
    assert answer.status_code == 201, answer.text
    body = answer.json()

    read = await two_drafts.conversion.get_conversion(body["conversion_id"], OWNER)

    warnings = {d.draft_id: d.quality_warning for d in read.drafts}
    assert warnings == {
        draft_id: d["quality_warning"] for draft_id, d in _drafts_by_id(body).items()
    }
    assert set(warnings.values()) == {CONVERSION_QUALITY_WARNING, None}
    assert read.created_at.utcoffset() == timedelta(0)
    assert read.created_at == datetime.fromisoformat(body["created_at"])


async def test_get_answers_the_convert_body(two_drafts):
    """The two HTTP bodies, compared draft by draft: the warning, and
    ``created_at`` in JSON mode (``Z``), are the convert answer's."""
    answer = await two_drafts.post("convert", key=None)
    assert answer.status_code == 201, answer.text

    read = await _read(two_drafts, answer.json()["conversion_id"])

    assert read["created_at"].endswith("Z")
    assert read == answer.json()


async def test_a_two_draft_replay_answers_each_draft(two_drafts):
    first = await two_drafts.post("convert")
    again = await two_drafts.post("convert")

    assert again.status_code == 201, again.text
    assert again.headers[IDEMPOTENCY_REPLAYED_HEADER] == "true"
    assert len(first.json()["drafts"]) == 2
    assert _ordered(again.json()) == _ordered(first.json())
    assert again.json() == await _read(two_drafts, first.json()["conversion_id"])
    assert await _count(two_drafts, ConversionJobModel) == 1


TEMPLATE = dict(
    title="Connection Pool Exhausted",
    service_name="checkout-api",
    domain="platform",
    symptom_class=["availability"],
    severity="high",
    scope="personal",
    tags=[],
    difficulty="medium",
    symptom_recognition="Requests start failing with 503 across the fleet.",
    applicability="Any deployment of the affected service.",
    diagnostic_steps="### Step 1. Look at the logs",
    causes="### Cause A: Something\nStatement: it broke.",
    prevention="Watch the dashboard.",
    user_id=OWNER,
    enterprise_id=STANDALONE_ENTERPRISE_ID,
)


@pytest.mark.parametrize(
    "writer, expected",
    [
        ("template", TEMPLATE_QUALITY_WARNING),
        ("scan", SCAN_QUALITY_WARNING),
        ("upload", None),
    ],
)
async def test_each_writers_warning_reads_back(
    world, monkeypatch, tmp_path, writer, expected
):
    """Below the threshold, a draft reads back with its writer's own text,
    chosen by the job's stored analysis; a KB upload's draft was never
    warned, and reads back with none."""
    low = _scored("")  # every draft scores 30
    if writer == "template":
        monkeypatch.setattr(conversion_module, "avalidate_and_score", low)
        made = await world.conversion.create_runbook_from_template(**TEMPLATE)
        assert made["draft"].quality_warning == expected
    elif writer == "scan":
        monkeypatch.setattr(runbook_scan, "avalidate_and_score", low)
        monkeypatch.setattr(
            type(world.conversion), "_data_dir", property(lambda self: tmp_path)
        )
        folder = tmp_path / "global" / "database"
        folder.mkdir(parents=True)
        (folder / "rb.md").write_text(valid_runbook("Redis Evictions Under Load"))
        scanned = await world.conversion.scan_for_runbooks(
            user_id=OWNER,
            enterprise_id=STANDALONE_ENTERPRISE_ID,
            is_platform_admin=True,
        )
        assert len(scanned["drafts"]) == 1, scanned
    else:
        assert (await world.post("upload", key=None)).status_code == 201
        async with world.factory() as session:
            row = (await session.execute(select(ConversionDraftModel))).scalar_one()
            details = {**row.quality_details, "overall": 30.0}
            await session.execute(
                update(ConversionDraftModel).values(
                    quality_score=30.0, quality_details=details
                )
            )
            await session.commit()
    async with world.factory() as session:
        conversion_id = (
            await session.execute(select(ConversionJobModel.id))
        ).scalar_one()

    read = await _read(world, conversion_id)

    assert [d["quality_warning"] for d in read["drafts"]] == [expected]


async def test_a_replay_shows_discards_and_verifies_since(two_drafts):
    first = (await two_drafts.post("convert")).json()
    kept, dropped = sorted(_drafts_by_id(first))

    async def statuses():
        again = await two_drafts.post("convert")
        assert again.status_code == 201, again.text
        assert again.headers[IDEMPOTENCY_REPLAYED_HEADER] == "true"
        return [(d["draft_id"], d["status"]) for d in again.json()["drafts"]]

    await _set_status(two_drafts, dropped, "discarded")
    assert await statuses() == [(kept, "draft")]
    await _set_status(two_drafts, kept, "verified")
    assert await statuses() == [(kept, "verified")]
    await _set_status(two_drafts, kept, "discarded")
    assert await statuses() == []
    assert await _count(two_drafts, ConversionJobModel) == 1


async def test_a_verified_draft_reads_back_with_no_warning(two_drafts):
    """The warning is advice before verification: a verified draft below the
    threshold carries none on the read."""
    body = (await two_drafts.post("convert", key=None)).json()
    low = next(d["draft_id"] for d in body["drafts"] if d["quality_warning"])
    await _set_status(two_drafts, low, "verified")

    read = _drafts_by_id(await _read(two_drafts, body["conversion_id"]))

    assert read[low]["status"] == "verified"
    assert read[low]["quality_score"]["overall"] < 50
    assert read[low]["quality_warning"] is None


async def test_the_read_keeps_the_written_draft_order(world):
    """On SQLite the read returns a job's drafts in the order the convert's
    writer stored them, its answer's ``drafts`` order. Ids in neither
    ascending nor descending order catch any sort."""
    order = ["draft_9", "draft_5", "draft_7"]
    await _persist_job(
        world.factory,
        None,
        conversion_id="conv_order",
        user_id=OWNER,
        enterprise_id=STANDALONE_ENTERPRISE_ID,
        scope="personal",
        team_id=None,
        status=ConversionStatus.COMPLETED,
        source_file=SourceFileInfo(filename="doc.md", size_bytes=1, content_type="x"),
        analysis=TWO_MODES,
        drafts=[
            ConversionDraft(
                draft_id=draft_id,
                runbook_id=f"rb-{draft_id}",
                title=f"Runbook {draft_id}",
                scope="personal",
                validation=ValidationResult(passed=True),
                quality_score=_score(80.0),
                content_preview="x",
                content="x",
            )
            for draft_id in order
        ],
        created_at=datetime.now(timezone.utc),
        sanitizer=regex_only_sanitizer(),
    )

    read = await _read(world, "conv_order")

    assert [d["draft_id"] for d in read["drafts"]] == order


@pytest.mark.parametrize(
    "column, value",
    [("analysis_result", {"bogus": True}), ("source_type", "bogus")],
    ids=["validation-error", "value-error"],
)
async def test_a_replay_whose_rows_no_longer_parse_is_a_409(world, column, value):
    """A stored row the read can no longer parse is the replay's 409, never
    the convert route's 500."""
    first = await world.post("convert")
    model = ConversionJobModel if column == "analysis_result" else ConversionDraftModel
    async with world.factory() as session:
        await session.execute(update(model).values({column: value}))
        await session.commit()

    again = await world.post("convert")

    assert again.status_code == 409, again.text
    assert again.headers["x-error-code"] == IDEMPOTENCY_REPLAY_UNAVAILABLE
    assert "bogus" not in again.text
    assert first.json()["conversion_id"]


async def test_a_failed_conversion_replays_as_its_job(world):
    world.conversion._llm_router.route = AsyncMock(side_effect=RuntimeError("down"))
    first = await world.post("convert")
    again = await world.post("convert")

    assert first.status_code == 201, first.text
    assert first.json()["status"] == "failed"
    assert again.status_code == 201, again.text
    assert again.headers[IDEMPOTENCY_REPLAYED_HEADER] == "true"
    assert again.json() == first.json()
    assert again.json() == await _read(world, first.json()["conversion_id"])
    assert await _count(world, ConversionJobModel) == 1


async def test_a_receipt_holding_a_pre_change_body_replays_the_job(world):
    """A receipt written before #2452 holds the first answer's full body: no
    code reads it, and the retry is the job as it now stands."""
    _with_marker(world)
    first = await world.post("convert")
    async with world.factory() as session:
        await session.execute(
            update(KbRequestReceiptModel).values(response=first.json())
        )
        await session.commit()
    edited = await _edit_out_marker(world, first.json())

    again = await world.post("convert")

    assert again.status_code == 201, again.text
    assert again.json()["drafts"][0]["content"] == edited
    assert MARKER not in again.text


async def test_a_claimless_duplicate_after_an_edit_answers_the_edited_job(
    world,
):
    _with_marker(world)
    first = await world.post("convert")
    edited = await _edit_out_marker(world, first.json())
    _claimless(world)

    duplicate = await world.post("convert")

    assert duplicate.status_code == 201, duplicate.text
    assert duplicate.headers[IDEMPOTENCY_REPLAYED_HEADER] == "true"
    assert duplicate.json()["drafts"][0]["content"] == edited
    assert MARKER not in duplicate.text
    assert world.analyze.await_count == 2, "the duplicate ran"
    assert await _count(world, ConversionJobModel) == 1
