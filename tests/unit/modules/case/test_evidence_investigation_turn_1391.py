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


class TestEveryEvidenceSchemaCarriesIt:
    @pytest.mark.parametrize(
        "model_name",
        [
            "SourceFileReference",
            "EvidenceDetailsResponse",
            "DerivedEvidenceSummary",
            "UploadedFileMetadata",
            "UploadedFileDetailsResponse",
        ],
    )
    def test_the_field_is_present_and_nullable(self, model_name):
        # Shipping it on some of them is how 3.5.0 left this surface behind:
        # one row with the ordinal beside one without is the same contradiction
        # in a smaller space.
        from faultmaven.models import api_models

        model = getattr(api_models, model_name)
        field = model.model_fields.get("investigation_turn")

        assert field is not None, f"{model_name} publishes a turn but not the ordinal"
        assert (
            field.default is None
        ), f"{model_name}.investigation_turn must default to None"
