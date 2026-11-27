"""The KB query runs its identifier patterns under a declared limit, linearly (#2721).

Principle 14 lets this path stay in the API process only under a declared input
limit and a growth test over it (#3116 B1). ``KB_QUERY_MAX_CHARS`` is the limit:
``hybrid_search`` refuses a longer query before anything runs, and ``kb_qa``,
the one caller the model writes, answers it as a tool error with no store call.
Three of ``IDENTIFIER_PATTERNS`` were quadratic over a separator-free run
(``"A" * n``, ``"a" * n``); a negative lookbehind for their leading class makes
each linear with the same yes/no answer.
"""

from __future__ import annotations

import itertools
import logging
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.core.investigation.kb_question import KB_QUESTION_QUERY_MAX_CHARS
from faultmaven.core.investigation.milestone_engine.kb_prefetch import (
    SYMPTOM_QUERY_MAX_CHARS,
)
from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    IDENTIFIER_PATTERNS,
    KnowledgeVectorStore,
)
from faultmaven.models.exceptions import KnowledgeQueryTooLong
from faultmaven.models.interfaces import KB_QUERY_MAX_CHARS
from faultmaven.modules.agent.domain.services.query_classifier import (
    _KB_CONTEXT_MAX_CHARS,
    _KB_TOPIC_MAX_CHARS,
)
from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.kb_tool_adapter import KBToolAdapter
from faultmaven.modules.case.domain.models.conclusion import (
    RootCauseConclusion,
    WorkingConclusion,
)
from tests.model_content import disabled_redaction
from tests.utils import admit_every_parent
from tests.wallclock import assert_linear_growth

pytestmark = [pytest.mark.unit]

_EMBEDDER = "faultmaven.infrastructure.model_cache.model_cache.aembed_query"

#: Growth windows are ``small``, ``8*small`` and ``64*small``. The first tops
#: out at the limit; one too small to decide on a noisy runner moves up past
#: it, where the patterns are the same.
STEP = 8
SMALL = KB_QUERY_MAX_CHARS // STEP**2

#: The three patterns as they stood before #2721's fix, each with the index of
#: its rewrite and the alphabet and length its yes/no answer is compared over,
#: exhaustively.
MAIN_PATTERNS = [
    (0, r"[A-Z]{2,}[-_]\d+", "Aa-_1 ", 7),
    (2, r"[a-z]+-[a-z]+-[a-z]+", "aA-1 ", 7),
    (5, r"\w+\.\w+\.\w+", "aA_.- ", 7),
]

#: A run of one character, quadratic under a pattern before the fix; ``"abc "``
#: many tokens, each long enough to be scored, for the tokenizer split and the
#: per-token scoring.
SHAPES = ["A", "a", "_", "abc "]


def _payload(unit: str, n: int) -> str:
    return (unit * n)[:n]


def _context() -> ToolContext:
    return ToolContext(
        session_id="sess_1",
        case_id="case_1",
        enterprise_id="org_1",
        user_id="user_1",
        kb_scope_filter={"scope": "global"},
        redaction_ctx=disabled_redaction(),
    )


def _max_length(model, field: str) -> int:
    return next(
        m.max_length
        for m in model.model_fields[field].metadata
        if getattr(m, "max_length", None) is not None
    )


# --- The patterns ----------------------------------------------------------


def test_the_window_tops_out_at_the_limit():
    assert KB_QUERY_MAX_CHARS - STEP**2 < SMALL * STEP**2 <= KB_QUERY_MAX_CHARS


@pytest.mark.parametrize("unit", SHAPES)
def test_keyword_extraction_is_linear(unit):
    assert_linear_growth(
        lambda q: KnowledgeVectorStore._extract_search_keywords(q, stats=None),
        lambda n: _payload(unit, n),
        small=SMALL,
        step=STEP,
        undecided_repeats=2,
        label=f"_extract_search_keywords over {unit!r} * n",
    )


@pytest.mark.parametrize("unit", SHAPES)
def test_the_identifier_test_is_linear(unit):
    assert_linear_growth(
        lambda q: KnowledgeVectorStore._query_has_identifier(q, [], None),
        lambda n: _payload(unit, n),
        small=SMALL,
        step=STEP,
        undecided_repeats=2,
        label=f"_query_has_identifier over {unit!r} * n",
    )


@pytest.mark.parametrize("index, source, alphabet, longest", MAIN_PATTERNS)
def test_each_rewrite_answers_yes_no_as_before(index, source, alphabet, longest):
    """Every string over the alphabet up to ``longest`` characters."""
    before, after = re.compile(source), IDENTIFIER_PATTERNS[index]
    assert after.pattern != source
    differ = [
        text
        for length in range(longest + 1)
        for text in map("".join, itertools.product(alphabet, repeat=length))
        if bool(before.search(text)) != bool(after.search(text))
    ]
    assert differ == []


# --- The limit -------------------------------------------------------------


@pytest.mark.parametrize(
    "bound",
    [
        SYMPTOM_QUERY_MAX_CHARS,
        KB_QUESTION_QUERY_MAX_CHARS,
        _KB_TOPIC_MAX_CHARS,
        _KB_CONTEXT_MAX_CHARS,
        _max_length(RootCauseConclusion, "root_cause"),
        _max_length(WorkingConclusion, "statement"),
    ],
)
def test_every_bounded_caller_fits_the_limit(bound):
    assert bound <= KB_QUERY_MAX_CHARS


async def test_hybrid_search_refuses_a_longer_query_first():
    """Before the admission check, the filter check and the embed."""
    store = KnowledgeVectorStore(client=MagicMock())
    store._get_or_create_collection = MagicMock()
    embedder = AsyncMock(return_value=[0.1] * 8)
    with patch(_EMBEDDER, new=embedder):
        with pytest.raises(KnowledgeQueryTooLong) as refused:
            await store.hybrid_search(
                collection_name="faultmaven_kb",
                query="a" * (KB_QUERY_MAX_CHARS + 1),
                where=None,
                admit=None,
            )
    assert (refused.value.length, refused.value.limit) == (
        KB_QUERY_MAX_CHARS + 1,
        KB_QUERY_MAX_CHARS,
    )
    embedder.assert_not_awaited()
    store._get_or_create_collection.assert_not_called()


async def test_kb_qa_answers_a_longer_question_with_no_store_call():
    wrapped = MagicMock(answer=AsyncMock(return_value=("answer", [])))
    adapter = KBToolAdapter(wrapped, admit=admit_every_parent)
    length = KB_QUERY_MAX_CHARS + 1

    result = await adapter.execute_with_context({"question": "a" * length}, _context())

    assert not result.success
    assert result.error == (
        f"Knowledge base query not run: the question is {length} characters "
        f"and the limit is {KB_QUERY_MAX_CHARS}. Ask one focused question (a "
        "symptom, an error message, a component), not pasted output. This says "
        "nothing about what the knowledge base holds."
    )
    wrapped.answer.assert_not_awaited()


async def test_kb_qa_runs_a_question_at_the_limit():
    wrapped = MagicMock(answer=AsyncMock(return_value=("answer", [])))
    adapter = KBToolAdapter(wrapped, admit=admit_every_parent)

    result = await adapter.execute_with_context(
        {"question": "a" * KB_QUERY_MAX_CHARS}, _context()
    )

    assert result.success
    wrapped.answer.assert_awaited_once()


def test_kb_qa_states_the_limit_on_its_question():
    adapter = KBToolAdapter(MagicMock(), admit=admit_every_parent)
    question = adapter.parameters_schema["properties"]["question"]["description"]
    assert question.endswith(f"At most {KB_QUERY_MAX_CHARS} characters.")


def _service(sanitized: str):
    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        KnowledgeService,
    )

    store = MagicMock(hybrid_search=AsyncMock(return_value=[]))
    tracer = MagicMock()
    tracer.trace = MagicMock(
        return_value=MagicMock(__enter__=lambda s: s, __exit__=lambda *a: None)
    )
    service = KnowledgeService(
        sanitizer=MagicMock(asanitize=AsyncMock(return_value=sanitized)),
        tracer=tracer,
        vector_store=store,
        db_session_factory=MagicMock(),
    )
    return service, store


@pytest.mark.parametrize(
    "sanitized, sent",
    [
        # A placeholder across the limit is dropped whole, not split.
        ("a" * 990 + " <EMAIL_ADDRESS> tail", "a" * 990),
        ("a" * 995 + " " * 10 + "b" * 10, "a" * 995),
        # No whitespace to cut back to: a hard cut.
        ("a" * (KB_QUERY_MAX_CHARS + 50), "a" * KB_QUERY_MAX_CHARS),
        (" " + "a" * KB_QUERY_MAX_CHARS, " " + "a" * (KB_QUERY_MAX_CHARS - 1)),
        ("a" * KB_QUERY_MAX_CHARS, "a" * KB_QUERY_MAX_CHARS),
    ],
)
async def test_search_knowledge_cuts_a_lengthened_query_at_whitespace(
    sanitized, sent, caplog
):
    service, store = _service(sanitized)

    with caplog.at_level(logging.DEBUG):
        await service.search_knowledge("the caller's query")

    assert store.hybrid_search.await_args.kwargs["query"] == sent
    cut = [r.getMessage() for r in caplog.records if "cut to the limit" in r.message]
    assert cut == (
        []
        if sent == sanitized
        else [
            f"KB query cut to the limit: query_len={len(sanitized)} "
            f"cut_len={len(sent)}"
        ]
    )
