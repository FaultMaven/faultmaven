"""The runbook-extraction prompt never quotes a row the server wrote (#1660).

#1451's rule — no row the server wrote is rendered to a model as something a
party SAID — reached five surfaces in PR #1658. The extraction prompt was
another, and it applied no check at all. It could not have: it read each row
with ``getattr``, which on the dicts ``get_messages`` returns finds nothing, so
every row went out as ``[unknown]: {<the whole row>}``. The doubles returned
attribute objects, so no test ever saw that.

Driven through ``extract_knowledge_from_case`` and read back from the prompt the
provider received. The rows come from a REAL repository, so the shape under
test is the production one and cannot drift the way the old doubles did.

The repository is used bare. It used to be wrapped, because the service fetched
the case through ``get_by_id`` and ``get_evidence``, which no case repository
has; since #1661 it reads through the contract's ``get``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from faultmaven.core.investigation.milestone_engine import (
    RESPONSE_EMPTY_TEXT,
    RESPONSE_NO_SIGNAL_TEXT,
    RESPONSE_TRUNCATED_TEXT,
    RESPONSE_WITHHELD_TEXT,
)
from faultmaven.core.investigation.prompts.context_builder import NO_ANSWER_LINE
from faultmaven.modules.case.contracts import (
    EMPTY_AGENT_RESPONSE_TEXT,
    EMPTY_TURN_TEXT,
    MESSAGE_METADATA_AGENT_SYNTHESIZED,
    MESSAGE_METADATA_USER_EMPTY,
)
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)
from faultmaven.modules.knowledge.domain.services.suggestion_service import (
    SuggestionService,
)
from faultmaven.modules.knowledge.infrastructure.persistence.suggestion_repository import (  # noqa: E501
    InMemorySuggestionRepository,
)
from tests.runbook_samples import valid_runbook
from tests.unit.modules.agent.conftest import create_sample_case

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]

#: The engine's four placeholders, the service backstop's, and one no writer
#: produces — so a guard keyed on known TEXTS rather than the flag fails here.
PLACEHOLDERS = [
    RESPONSE_WITHHELD_TEXT,
    RESPONSE_TRUNCATED_TEXT,
    RESPONSE_EMPTY_TEXT,
    RESPONSE_NO_SIGNAL_TEXT,
    EMPTY_AGENT_RESPONSE_TEXT,
    "(a placeholder no writer produces yet)",
]

USER_SAID = "checkout is 500ing at the evening peak"
ASSISTANT_SAID = "The connection pool was exhausted by transactions left open."


class _Provider:
    """Records the prompt; answers with a valid runbook so nothing retries."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def generate(self, *, prompt: str, **_kw) -> SimpleNamespace:
        self.prompts.append(prompt)
        return SimpleNamespace(content=valid_runbook(), is_truncated=False)


def _row(turn: int, role: str, content: str, **metadata) -> dict:
    return {
        "message_id": f"msg_{role[0]}{turn:02d}{len(content):04d}",
        "turn_number": turn,
        "role": role,
        "content": content,
        "created_at": f"2026-09-24T10:{turn:02d}:{0 if role == 'user' else 30:02d}Z",
        "metadata": metadata,
    }


async def _source_material(extra_rows: list[dict]) -> str:
    """The case section of the prompt the provider received."""
    real = InMemoryCaseRepository()
    case = create_sample_case(user_id="user_extractor")
    case.messages = [
        _row(1, "user", USER_SAID),
        _row(1, "assistant", ASSISTANT_SAID),
        *extra_rows,
    ]
    await real.save(case)
    rows = await real.get_messages(case.case_id)
    assert rows and all(isinstance(r, dict) for r in rows), "not the production shape"

    provider = _Provider()
    service = SuggestionService(
        case_repository=real,
        knowledge_service=None,
        sanitizer=None,
        llm_provider=provider,
        suggestion_repository=InMemorySuggestionRepository(),
    )
    await service.extract_knowledge_from_case(
        case_id=case.case_id, enterprise_id="ent_1660", extracted_by="user_extractor"
    )
    prompt = provider.prompts[0]
    return prompt[prompt.index("--- SOURCE MATERIAL: CASE ---") :]


class TestTheExtractionTranscript:
    async def test_a_row_renders_as_its_role_and_its_words(self):
        """Positive control, and the whole-dict defect: a row is what was said,
        not its storage record."""
        source = await _source_material([])

        assert f"[user]: {USER_SAID}" in source
        assert f"[assistant]: {ASSISTANT_SAID}" in source
        assert "[unknown]" not in source
        assert "message_id" not in source
        assert "'metadata'" not in source

    @pytest.mark.parametrize("placeholder", PLACEHOLDERS)
    async def test_a_server_written_assistant_row_is_skipped(self, placeholder):
        source = await _source_material(
            [
                _row(2, "user", "and the replica?"),
                _row(
                    2,
                    "assistant",
                    placeholder,
                    **{MESSAGE_METADATA_AGENT_SYNTHESIZED: True},
                ),
            ]
        )

        assert placeholder not in source
        assert "[user]: and the replica?" in source
        assert f"[assistant]: {ASSISTANT_SAID}" in source

    async def test_a_server_written_user_row_is_skipped(self):
        source = await _source_material(
            [
                _row(2, "user", EMPTY_TURN_TEXT, **{MESSAGE_METADATA_USER_EMPTY: True}),
                _row(2, "assistant", "Anything else you have seen since?"),
            ]
        )

        assert EMPTY_TURN_TEXT not in source
        assert "[assistant]: Anything else you have seen since?" in source

    async def test_it_is_skipped_rather_than_marked(self):
        """The marker line is for a model CONTINUING the conversation. This one
        distils a runbook from it, where a line saying the assistant did not
        answer is not incident content — the auto-titler's reading of #1451."""
        source = await _source_material(
            [
                _row(
                    2,
                    "assistant",
                    RESPONSE_WITHHELD_TEXT,
                    **{MESSAGE_METADATA_AGENT_SYNTHESIZED: True},
                )
            ]
        )

        assert NO_ANSWER_LINE not in source
        assert "no usable reply" not in source
