"""#1391 — evidence and uploaded-file rows carry their own investigation turn.

3.5.0 gave every schema that publishes a turn *for display* a nullable
``investigation_turn`` and missed the two that name a turn they do not
themselves render: an evidence row's "collected at turn N" and a file's
"uploaded at turn N". Both carried only the message clock, so on any case with
an aside a client printed ``turn 5`` beside an evidence row while its own
transcript called that same exchange ``Turn 4`` — #1387's defect, one surface
over.

The clock fields are unchanged and still the thing an anchor is keyed on. These
pin both halves: the new field is the ordinal, and the old one did not move.
"""

from __future__ import annotations

import json
import pathlib
import re
from datetime import datetime, timezone

import pytest

from faultmaven.models.api_models import UploadedFileMetadata
from faultmaven.modules.case.contracts import UploadedFile
from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    TurnOutcome,
    TurnProgress,
)

pytestmark = pytest.mark.unit


def _turn(n: int, outcome: TurnOutcome) -> TurnProgress:
    return TurnProgress(
        turn_number=n,
        timestamp=datetime.now(timezone.utc),
        progress_made=False,
        outcome=outcome,
        user_message_summary=f"u{n}",
        agent_response_summary=f"a{n}",
    )


def _case(*outcomes: TurnOutcome) -> Case:
    """A case whose turn N has ``outcomes[N - 1]``."""
    case = Case(
        case_id="case_aabb11223344",
        title="OOM",
        description="d",
        user_id="u",
        enterprise_id="ent_1",
        state=CaseState.INQUIRY,
        current_turn=len(outcomes),
    )
    case.turn_history = [_turn(i, o) for i, o in enumerate(outcomes, start=1)]
    return case


def _file(turn: int, file_id: str = "file_aabb11223344") -> UploadedFile:
    return UploadedFile(
        file_id=file_id,
        filename="app.log",
        size_bytes=10,
        content_type="text/plain",
        content_hash="h",
        uploaded_at_turn=turn,
        uploaded_at=datetime.now(timezone.utc),
        uploaded_by="u",
        upload_source="file_upload",
        storage_ref="s",
    )


def _work() -> TurnOutcome:
    """An ordinary investigation turn — anything that is not an aside."""
    return TurnOutcome.DATA_PROVIDED


def _evidence(*, collected_at_turn: int, source_file_id: str | None = None):
    """Evidence at a given clock turn.

    Without a source file the model requires ``source_type=user_description``
    — the chat-quote case — which is the honest shape for evidence that came
    out of the conversation rather than an attachment.
    """
    from faultmaven.modules.case.domain.models import Evidence

    return Evidence(
        evidence_id="ev_aabb11223344",
        category="symptom_evidence",
        primary_purpose="OTHER",
        summary="s",
        extract="x",
        source_type="logs" if source_file_id else "user_description",
        source_file_id=source_file_id,
        collected_at_turn=collected_at_turn,
        collected_at=datetime.now(timezone.utc),
        collected_by="u",
    )


class TestTheOrdinalOnAFileRow:
    def test_an_aside_does_not_advance_it(self):
        # Clock 1 work, clock 2 aside, clock 3 work. A file uploaded at clock 3
        # belongs to investigation turn 2, not 3.
        case = _case(_work(), TurnOutcome.OUT_OF_BAND, _work())

        assert case.investigation_turn_at(3) == 2

    def test_the_row_reports_the_ordinal_while_the_clock_field_does_not_move(self):
        # Both numbers are published, and they are different on purpose:
        # `uploaded_at_turn` is what an anchor and jump-to-turn are keyed on, so
        # re-basing IT onto the ordinal would break them silently.
        case = _case(_work(), TurnOutcome.OUT_OF_BAND, _work())
        row = UploadedFileMetadata.from_uploaded_file(
            _file(3), investigation_turn=case.investigation_turn_at(3)
        )

        assert row.uploaded_at_turn == 3
        assert row.investigation_turn == 2

    def test_a_caller_without_the_case_says_NOTHING(self):
        # Nullable on purpose. An `UploadedFile` does not carry its parent's
        # turn history, so a caller that cannot resolve the ordinal must read as
        # "did not say" — which every client already handles for an older
        # server — rather than fall back to the clock under the new name.
        row = UploadedFileMetadata.from_uploaded_file(_file(3))

        assert row.uploaded_at_turn == 3
        assert row.investigation_turn is None


class TestItAgreesWithTheConversation:
    def test_a_file_and_the_exchange_it_belongs_to_report_the_same_turn(self):
        # The whole point: one screen, one number. The evidence row and the
        # transcript row on the same clock turn must not disagree, which is what
        # sharing `investigation_turn_at` guarantees.
        case = _case(_work(), TurnOutcome.OUT_OF_BAND, _work(), _work())

        for clock in (1, 2, 3, 4):
            row = UploadedFileMetadata.from_uploaded_file(
                _file(clock), investigation_turn=case.investigation_turn_at(clock)
            )
            assert row.investigation_turn == case.investigation_turn_at(clock)

    def test_it_never_exceeds_the_case_level_count(self):
        # The invariant the design rests on: a row's ordinal cannot be past the
        # investigation's own total.
        case = _case(TurnOutcome.OUT_OF_BAND, _work(), _work())

        for clock in range(1, 6):
            assert case.investigation_turn_at(clock) <= case.investigation_turn_count

    def test_a_case_whose_only_turn_was_an_aside_reports_zero(self):
        # 0 is a real answer, not "unknown": the investigation has had no turns.
        # Clients suppress the label there rather than printing "Turn 0".
        case = _case(TurnOutcome.OUT_OF_BAND)

        assert case.investigation_turn_at(1) == 0
        assert case.investigation_turn_count == 0


class TestEveryPublishedSchemaThatNamesATurn:
    """The inventory, over the CONTRACT rather than a hand-written list.

    3.5.0 missed two schemas. This PR's first pass missed a third —
    ``case_ui.EvidenceSummary``, served inside ``CaseUIResponse_Investigating``
    — and the parametrize list meant to catch that enumerated ONE module, so it
    was structurally incapable of seeing into another: the same blind spot one
    level up.

    Walking the generated spec removes the list. Anything the API publishes with
    a clock turn on it has to publish the ordinal beside it, and a new schema is
    covered the day it is added rather than the day someone remembers to add it
    here.
    """

    SPEC = (
        pathlib.Path(__file__).resolve().parents[4] / "docs/reference/api/openapi.json"
    )

    # A clock field NAMES a turn; these are the shapes that do.
    CLOCK = re.compile(r".*_at_turn$|^turn_number$|^current_turn$")

    def _schemas(self) -> dict:
        return json.loads(self.SPEC.read_text())["components"]["schemas"]

    def test_the_spec_is_where_we_are_looking(self):
        # Fail closed: a moved or unreadable spec must break this rather than
        # make every assertion below vacuous over an empty dict.
        assert len(self._schemas()) > 50, "the spec looks wrong, not merely clean"

    def test_every_schema_naming_a_turn_also_publishes_the_ordinal(self):
        offenders = []
        for name, schema in self._schemas().items():
            props = schema.get("properties", {})
            clocks = [p for p in props if self.CLOCK.match(p)]
            if clocks and "investigation_turn" not in props:
                offenders.append(f"{name} publishes {clocks} but no investigation_turn")

        assert not offenders, (
            "these schemas name a turn without saying which turn of the "
            "INVESTIGATION it is, so a client rendering them prints the message "
            "clock beside a transcript printing the ordinal:\n  "
            + "\n  ".join(offenders)
        )

    def test_the_ordinal_is_always_nullable(self):
        # Nullable is what lets an older server read as "did not say" rather
        # than as turn zero, and what lets a caller without the case decline to
        # guess. `default=None` alone does not establish it: a field annotated
        # `int` with `default=None` passes that check and then raises the moment
        # a route passes None explicitly.
        not_nullable = []
        for name, schema in self._schemas().items():
            field = schema.get("properties", {}).get("investigation_turn")
            if field is None:
                continue
            variants = field.get("anyOf") or [field]
            if not any(v.get("type") == "null" for v in variants):
                not_nullable.append(name)

        assert (
            not not_nullable
        ), f"investigation_turn must accept null on: {not_nullable}"


class TestTheRoutesActuallyStampIt:
    """The wiring, not just the contract.

    The first version of this suite exercised ``from_uploaded_file`` and
    asserted field presence — everything except the five call sites where the
    fix lives. A copy-paste swap there (stamping a file row from
    ``collected_at_turn``, say) would have shipped green, which is precisely the
    mistake this class of change invites.

    These drive the real builders with a real ``Case``.
    """

    def _case_with_history(self) -> Case:
        # Clock 1 work, clock 2 ASIDE, clock 3 work → ordinals 1, 1, 2.
        return _case(_work(), TurnOutcome.OUT_OF_BAND, _work())

    def test_the_evidence_row_is_stamped_from_its_OWN_clock_turn(self):
        from faultmaven.modules.case.api.routes import _build_evidence_response

        case = self._case_with_history()
        evidence = _evidence(collected_at_turn=3)
        case.evidence = [evidence]

        row = _build_evidence_response(case, evidence, case.case_id)

        # 3 on the clock is investigation turn 2 — and the clock field is intact.
        assert row.collected_at_turn == 3
        assert row.investigation_turn == 2

    def test_the_source_file_reference_is_stamped_from_the_FILE_turn(self):
        # The two turns on one row come from different clocks, so a swap here is
        # invisible unless they differ. They do: the file landed at clock 1.
        from faultmaven.modules.case.api.routes import _build_evidence_response

        case = self._case_with_history()
        uploaded = _file(1)
        case.uploaded_files = [uploaded]
        evidence = _evidence(collected_at_turn=3, source_file_id=uploaded.file_id)
        case.evidence = [evidence]

        row = _build_evidence_response(case, evidence, case.case_id)

        assert row.investigation_turn == 2, "the evidence row follows its own turn"
        assert row.source_file is not None
        assert row.source_file.uploaded_at_turn == 1
        assert (
            row.source_file.investigation_turn == 1
        ), "the file follows the file's turn"

    def test_a_prepared_asides_list_gives_the_same_answer(self):
        # The hoisted list is an optimisation, so it must not change the result.
        from faultmaven.modules.case.api.routes import _build_evidence_response

        case = self._case_with_history()
        evidence = _evidence(collected_at_turn=3)
        case.evidence = [evidence]

        assert _build_evidence_response(
            case, evidence, case.case_id, asides=case.out_of_band_turns
        ) == _build_evidence_response(case, evidence, case.case_id)

    def test_the_ui_adapter_stamps_its_evidence_rows(self):
        # The sixth schema, and the one both 3.5.0 and this PR's first pass
        # missed: `/cases/{id}/ui` publishes its own evidence summaries.
        from faultmaven.modules.case.domain.services.case_ui_adapter import (
            transform_case_for_ui,
        )

        case = self._case_with_history()
        # INVESTIGATING is only a valid state once the inquiry has confirmed the
        # problem statement — the model enforces it, and this test is about the
        # adapter rather than that rule.
        case.inquiry.problem_statement_confirmed = True
        case.inquiry.decided_to_investigate = True
        case.state = CaseState.INVESTIGATING
        case.evidence = [_evidence(collected_at_turn=3)]

        ui = transform_case_for_ui(case)
        rows = getattr(ui, "latest_evidence", None)

        assert rows, "the investigating UI publishes evidence rows"
        assert rows[0].collected_at_turn == 3
        assert rows[0].investigation_turn == 2
