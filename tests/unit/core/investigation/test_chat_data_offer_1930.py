"""System output typed into a chat message is offered back for the user to declare (#1930).

What the user writes in a chat message is a claim; only submitted data is
evidence. The model names each block of system output in the message by its
first and last line; the engine finds those lines in the user's OWN message and
records one offer holding the blocks as the user wrote them. These pin:

- the locator: whole-line matching, the message's own text and whitespace, no
  overlap, and an anchor that finds nothing dropping its block (the safe way);
- the offer: one per message, never on a closed case or an empty message,
  placeholders reversed, a block never offered twice;
- the state request: a reported state with no data is asked for, once, until
  data arrives;
- the engine: a live turn mints it and names it in its result; the
  statement-revision replay cannot, because the blocks never enter the staged
  state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.chat_data import (
    TURN_METADATA_CHAT_DATA_OFFER,
    block_key,
    locate_blocks,
    mint_chat_data_offer,
)
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.response_application import (
    _STAGED_FIELDS,
)
from faultmaven.core.investigation.schemas import (
    ChatDataBlock,
    InvestigationResponse_Diagnosis,
    StateReport,
)
from faultmaven.modules.case.contracts import (
    MAX_CHAT_DATA_BLOCKS,
    Case,
    CaseState,
    ChatDataOffer,
    ChatDataOfferState,
    InvestigationProgress,
    ProblemVerification,
    UploadedFile,
)

pytestmark = pytest.mark.unit

_MESSAGE = """Before I restart anything, here is the cert on the node:

$ ssh node-worker-3 'sudo openssl x509 -in /var/lib/certs/api.crt -noout -dates'
notBefore=May  6 00:00:00 2025 GMT
notAfter=May  6 13:59:59 2026 GMT

Same serial as what NGINX serves. And the pods:

NAME          READY   STATUS
api-0         0/1     CrashLoopBackOff
"""

_CERT = """$ ssh node-worker-3 'sudo openssl x509 -in /var/lib/certs/api.crt -noout -dates'
notBefore=May  6 00:00:00 2025 GMT
notAfter=May  6 13:59:59 2026 GMT"""

_PODS = """NAME          READY   STATUS
api-0         0/1     CrashLoopBackOff"""


def _block(first: str, last: str) -> ChatDataBlock:
    return ChatDataBlock(first_line=first, last_line=last)


def _case(**overrides) -> Case:
    case = Case(
        case_id="case_000000001930",
        user_id="u",
        enterprise_id="e",
        title="t",
        description="d",
        state=CaseState.INQUIRY,
        current_turn=4,
    )
    case.inquiry.proposed_problem_statement = "api pods crash"
    case.inquiry.problem_statement_confirmed = True
    case.state = CaseState.INVESTIGATING
    for key, value in overrides.items():
        setattr(case, key, value)
    return case


class TestTheLocator:
    def test_it_takes_every_line_between_the_anchors_as_the_message_has_them(self):
        blocks, unmatched = locate_blocks(
            _MESSAGE,
            [
                (_CERT.splitlines()[0], "notAfter=May  6 13:59:59 2026 GMT"),
                (
                    "NAME          READY   STATUS",
                    "api-0         0/1     CrashLoopBackOff",
                ),
            ],
        )
        assert blocks == [_CERT, _PODS]
        assert unmatched == 0

    def test_surrounding_whitespace_on_an_anchor_does_not_matter(self):
        """A model copying a line drops indentation and trailing spaces; the
        block keeps the message's own."""
        message = "look:\n    key: value\n    other: thing  \nthanks"
        blocks, _ = locate_blocks(message, [("key: value", " other: thing")])
        assert blocks == ["    key: value\n    other: thing  "]

    def test_a_one_line_block(self):
        blocks, _ = locate_blocks(
            "got this: \nHTTP/1.1 503 Service Unavailable\nany idea?",
            [("HTTP/1.1 503 Service Unavailable",) * 2],
        )
        assert blocks == ["HTTP/1.1 503 Service Unavailable"]

    @pytest.mark.parametrize(
        "anchor",
        [
            ("notAfter=May 6 13:59:59 2026 GMT", "notAfter=May 6 13:59:59 2026 GMT"),
            ("a line the user never wrote", "a line the user never wrote"),
            ("", ""),
            # A first line that only appears AFTER its last line.
            ("api-0         0/1     CrashLoopBackOff", "NAME          READY   STATUS"),
            # Part of a line is not a line.
            ("notAfter=May", "notAfter=May"),
        ],
    )
    def test_an_anchor_that_finds_nothing_drops_its_block(self, anchor):
        blocks, unmatched = locate_blocks(_MESSAGE, [anchor])
        assert blocks == []
        assert unmatched == 1

    def test_blocks_never_overlap(self):
        """The second anchor is searched after the first block's end, so the
        same output named twice is offered once."""
        anchor = (
            "NAME          READY   STATUS",
            "api-0         0/1     CrashLoopBackOff",
        )
        blocks, unmatched = locate_blocks(_MESSAGE, [anchor, anchor])
        assert blocks == [_PODS]
        assert unmatched == 1

    def test_crlf_messages_match(self):
        blocks, _ = locate_blocks("a\r\nERROR x\r\nb", [("ERROR x", "ERROR x")])
        assert blocks == ["ERROR x"]


class TestTheOffer:
    def test_one_offer_holds_the_blocks_found(self):
        case = _case()
        offer = mint_chat_data_offer(
            case,
            user_message=_MESSAGE,
            named_blocks=[
                _block(_CERT.splitlines()[0], _CERT.splitlines()[-1]),
                _block("not in the message", "not in the message"),
                _block(_PODS.splitlines()[0], _PODS.splitlines()[-1]),
            ],
        )
        assert offer is not None
        assert case.chat_data_offers == [offer]
        assert offer.blocks == [_CERT, _PODS]
        assert offer.turn == 4
        assert offer.state == ChatDataOfferState.OFFERED

    @pytest.mark.parametrize(
        ("named", "message", "state"),
        [
            (None, _MESSAGE, CaseState.INVESTIGATING),
            ([], _MESSAGE, CaseState.INVESTIGATING),
            ([_block("x", "x")], "   ", CaseState.INVESTIGATING),
            ([_block("nowhere", "nowhere")], _MESSAGE, CaseState.INVESTIGATING),
            (
                [_block(_PODS.splitlines()[0], _PODS.splitlines()[-1])],
                _MESSAGE,
                CaseState.RESOLVED,
            ),
            (
                [_block(_PODS.splitlines()[0], _PODS.splitlines()[-1])],
                _MESSAGE,
                CaseState.CLOSED,
            ),
        ],
    )
    def test_nothing_is_offered(self, named, message, state):
        case = _case()
        stamp = {
            CaseState.RESOLVED: {"resolved_at": case.created_at},
            CaseState.CLOSED: {"closed_at": case.created_at},
        }.get(state, {})
        case = case.model_copy(update={"state": state, **stamp})
        assert (
            mint_chat_data_offer(case, user_message=message, named_blocks=named) is None
        )
        assert case.chat_data_offers == []

    def test_works_in_inquiry(self):
        """The first message, before any investigation, is where output is
        pasted most."""
        case = _case(state=CaseState.INQUIRY)
        offer = mint_chat_data_offer(
            case,
            user_message=_MESSAGE,
            named_blocks=[_block(_PODS.splitlines()[0], _PODS.splitlines()[-1])],
        )
        assert offer is not None and offer.blocks == [_PODS]

    def test_redaction_placeholders_are_reversed_before_matching(self):
        """The model saw a redacted message; the stored one is raw."""
        message = (
            "pinged it:\nPING 10.4.2.17 (10.4.2.17): 56 data bytes\nRequest timeout"
        )
        reverse_map = {"<IP_ADDRESS_1>": "10.4.2.17"}

        def reverse(text: str) -> str:
            for placeholder, original in reverse_map.items():
                text = text.replace(placeholder, original)
            return text

        case = _case()
        offer = mint_chat_data_offer(
            case,
            user_message=message,
            named_blocks=[
                _block(
                    "PING <IP_ADDRESS_1> (<IP_ADDRESS_1>): 56 data bytes",
                    "Request timeout",
                )
            ],
            reverse=reverse,
        )
        assert offer is not None
        assert offer.blocks == [
            "PING 10.4.2.17 (10.4.2.17): 56 data bytes\nRequest timeout"
        ]

    @pytest.mark.parametrize("state", list(ChatDataOfferState))
    def test_a_block_already_offered_is_not_offered_again(self, state):
        """Saved or ignored, the user has seen it once; offering it again would
        be nagging. Trailing whitespace does not make it new."""
        case = _case()
        case.chat_data_offers = [
            ChatDataOffer(turn=2, blocks=[_PODS + "   "], state=state)
        ]
        offer = mint_chat_data_offer(
            case,
            user_message=_MESSAGE,
            named_blocks=[
                _block(_CERT.splitlines()[0], _CERT.splitlines()[-1]),
                _block(_PODS.splitlines()[0], _PODS.splitlines()[-1]),
            ],
        )
        assert offer is not None and offer.blocks == [_CERT]

    def test_at_most_the_cap_is_considered(self):
        lines = [f"ERROR {i}" for i in range(MAX_CHAT_DATA_BLOCKS + 2)]
        case = _case()
        offer = mint_chat_data_offer(
            case,
            user_message="\n".join(lines),
            named_blocks=[_block(line, line) for line in lines],
        )
        assert offer is not None and len(offer.blocks) == MAX_CHAT_DATA_BLOCKS

    def test_the_block_key_ignores_trailing_whitespace_only(self):
        assert block_key("a  \nb") == block_key("a\nb\n")
        assert block_key("a\nb") != block_key("a\n b")


class TestTheStateRequest:
    """A message that REPORTS the state of the user's system ("it's stable",
    "it's working now") with no data showing it: FaultMaven asks for the output
    that would, for verification and for the record."""

    _REQUEST = "the output of kubectl get pods -n payments"

    def _report(self):
        return SimpleNamespace(request=self._REQUEST)

    def test_a_reported_state_with_no_data_is_asked_for(self):
        case = _case()
        offer = mint_chat_data_offer(
            case,
            user_message="it's stable now",
            named_blocks=None,
            state_report=self._report(),
        )
        assert offer is not None
        assert offer.blocks == [] and offer.request == self._REQUEST

    def test_output_and_a_reported_state_share_one_offer(self):
        case = _case()
        offer = mint_chat_data_offer(
            case,
            user_message=_MESSAGE,
            named_blocks=[_block(_PODS.splitlines()[0], _PODS.splitlines()[-1])],
            state_report=self._report(),
        )
        assert offer.blocks == [_PODS] and offer.request == self._REQUEST

    def test_not_asked_when_the_turn_submitted_data(self):
        case = _case()
        assert (
            mint_chat_data_offer(
                case,
                user_message="it's stable, see the attached",
                named_blocks=None,
                state_report=self._report(),
                submitted_data=True,
            )
            is None
        )

    def test_not_asked_again_while_a_request_waits(self):
        """The user who keeps saying "still stable" is not asked every turn; a
        file submitted since the request reopens it."""
        case = _case()
        mint_chat_data_offer(
            case, user_message="stable", named_blocks=None, state_report=self._report()
        )
        case.current_turn += 1
        assert (
            mint_chat_data_offer(
                case,
                user_message="still stable",
                named_blocks=None,
                state_report=self._report(),
            )
            is None
        )

        case.uploaded_files = [
            UploadedFile(
                file_id="file_0123456789ab",
                filename="pods.txt",
                size_bytes=10,
                uploaded_at_turn=case.current_turn,
            )
        ]
        case.current_turn += 1
        assert (
            mint_chat_data_offer(
                case,
                user_message="it broke and is stable again",
                named_blocks=None,
                state_report=self._report(),
            )
            is not None
        )

    def test_placeholders_in_the_request_are_reversed(self):
        case = _case()
        offer = mint_chat_data_offer(
            case,
            user_message="it's reachable again",
            named_blocks=None,
            state_report=SimpleNamespace(request="ping <IP_ADDRESS_1>"),
            reverse=lambda text: text.replace("<IP_ADDRESS_1>", "10.4.2.17"),
        )
        assert offer.request == "ping 10.4.2.17"

    def test_an_overlong_request_is_cut_not_refused(self):
        from faultmaven.core.investigation.schemas import StateReport

        report = StateReport(request="x" * 400)
        assert len(report.request) == 300


class TestTheReplayCannotMint:
    def test_the_blocks_are_not_staged_cause_work(self):
        """A statement-revision replay re-applies STAGED state updates. The
        blocks ride the response, not ``state_updates``, so nothing a replay
        carries can mint an offer."""
        assert "chat_data_blocks" not in _STAGED_FIELDS
        assert (
            "chat_data_blocks"
            not in InvestigationResponse_Diagnosis.DiagnosisStateUpdate.model_fields
        )


def _investigating_case() -> Case:
    case = Case(
        case_id="case_5db5417fe930",
        title="API down",
        state=CaseState.INQUIRY,
        user_id="user_test",
        enterprise_id="org_test",
        description="api pods crash",
        problem_verification=ProblemVerification(
            symptom_statement="api pods crash",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
    )
    case.inquiry.proposed_problem_statement = "api pods crash"
    case.inquiry.problem_statement_confirmed = True
    case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
    case.state = CaseState.INVESTIGATING
    case.progress = InvestigationProgress()
    case.current_turn = 7
    return case


def _engine(blocks) -> MilestoneEngine:
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c, **_: c)
    repo.get = AsyncMock(side_effect=lambda cid: None)
    engine = MilestoneEngine(MagicMock(), repo, investigation_tools=MagicMock())
    engine.generator.generate_structured_output = AsyncMock(
        return_value=InvestigationResponse_Diagnosis(
            agent_response="The pod is crash-looping.",
            state_updates={},
            chat_data_blocks=blocks,
        )
    )
    return engine


class TestTheEngineMintsOnTheLiveTurn:
    async def test_a_turn_with_named_output_records_and_names_its_offer(self):
        case = _investigating_case()
        engine = _engine([_block(_PODS.splitlines()[0], _PODS.splitlines()[-1])])

        result = await engine.process_turn(case=case, user_message=_MESSAGE)

        (offer,) = case.chat_data_offers
        assert offer.blocks == [_PODS]
        assert result["metadata"][TURN_METADATA_CHAT_DATA_OFFER] == offer.offer_id
        # The turn's own work is not held back: the reply is the model's.
        assert "crash-looping" in result["agent_response"]

    async def test_a_reported_state_is_asked_for_on_the_live_turn(self):
        case = _investigating_case()
        engine = _engine(None)
        engine.generator.generate_structured_output.return_value.system_state_report = (
            StateReport(request="the output of kubectl get pods")
        )

        result = await engine.process_turn(case=case, user_message="it's stable now")

        (offer,) = case.chat_data_offers
        assert offer.request == "the output of kubectl get pods"
        assert result["metadata"][TURN_METADATA_CHAT_DATA_OFFER] == offer.offer_id

    async def test_a_turn_with_nothing_named_records_nothing(self):
        case = _investigating_case()
        result = await _engine(None).process_turn(case=case, user_message=_MESSAGE)
        assert case.chat_data_offers == []
        assert result["metadata"][TURN_METADATA_CHAT_DATA_OFFER] is None


def test_the_offer_round_trips_through_its_json_form():
    """Persisted inside ``cases.metadata`` (both repositories)."""
    offer = ChatDataOffer(turn=3, blocks=[_PODS], file_ids=["file_0123456789ab"])
    dumped = offer.model_dump(mode="json")
    assert ChatDataOffer.model_validate(dumped) == offer
    assert SimpleNamespace(**dumped).state == "offered"
