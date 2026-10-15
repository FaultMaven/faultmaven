"""ModelText and the ways in (#1998): what the type allows, and who can mint it.

Every rule the type carries has a test here, and every minting function has a
test of what it mints and what it refuses. The refusals that matter most are
the posture-independent ones: a disabled redaction context (``SANITIZE_PII``
off, CI's Standalone profile) must refuse the same shapes an enabled one does.

The class is reached through a minted value (``type(non_case_text(...))``)
where a test must construct or subclass it: spelling either form is refused by
the lexical mint check everywhere but the minting module and the test minter,
and these tests show the runtime refusal holds without it.
"""

from __future__ import annotations

import copy
import json
import pickle
from types import SimpleNamespace

import pytest

from faultmaven.infrastructure.llm.providers.base import LLMResponse, ToolCall
from faultmaven.infrastructure.security.case_redaction import (
    CaseRedactionContext,
    model_boundary_redaction,
)
from faultmaven.infrastructure.security.model_content import (
    SOURCE_CASE,
    SOURCE_MODEL_BOUNDARY,
    SOURCE_MODEL_OUTPUT,
    SOURCE_NON_CASE,
    ModelText,
    case_redacted,
    case_redacted_messages,
    message_violations,
    model_boundary_redacted,
    model_output,
    non_case_text,
    plain_messages,
    plain_text,
    provenance,
    sanitizer_pass,
)
from tests.model_content import disabled_redaction, forged, vetted, vetted_messages
from tests.utils import regex_only_sanitizer, sanitize_pii_pinned

pytestmark = [pytest.mark.unit, pytest.mark.security]

IP = "10.1.2.3"


def _enabled_ctx(case_id: str = "case-mt-1998") -> CaseRedactionContext:
    return CaseRedactionContext(
        case_id=case_id, sanitizer=regex_only_sanitizer(), enabled=True
    )


class TestTheType:
    def test_a_minted_value_is_a_str_that_compares_equal(self):
        text = non_case_text("hello")
        assert type(text) is ModelText
        assert isinstance(text, str)
        assert text == "hello" and hash(text) == hash("hello")
        assert json.dumps({"content": text}) == '{"content": "hello"}'

    def test_it_records_source_and_redaction_posture(self):
        assert provenance(non_case_text("x")) == (SOURCE_NON_CASE, False)
        assert provenance(vetted("x")) == (SOURCE_CASE, False)

    def test_direct_construction_needs_the_token(self):
        cls = type(non_case_text("x"))
        with pytest.raises(TypeError, match="minted only by"):
            cls("raw", SOURCE_CASE, True)
        with pytest.raises(TypeError, match="minted only by"):
            cls("raw", SOURCE_CASE, True, object())

    def test_it_cannot_be_subclassed(self):
        cls = type(non_case_text("x"))
        with pytest.raises(TypeError, match="cannot be subclassed"):

            class _Sub(cls):  # noqa: N801 - the attempt is the test
                pass

        with pytest.raises(TypeError, match="cannot be subclassed"):
            type("_Sub", (cls,), {})

    def test_concatenating_two_minted_values_is_minted(self):
        joined = vetted("case ") + non_case_text("instruction")
        assert type(joined) is ModelText and joined == "case instruction"
        # The higher-precedence source wins, with its own posture.
        assert provenance(joined) == (SOURCE_CASE, False)

    async def test_concatenation_of_one_source_is_redacted_only_if_both_parts_were(
        self,
    ):
        redacted, unredacted = await case_redacted("a", _enabled_ctx()), vetted("b")
        assert provenance(redacted + redacted) == (SOURCE_CASE, True)
        assert provenance(redacted + unredacted) == (SOURCE_CASE, False)

    @pytest.mark.parametrize("raw", ["plain", "", "x" * 3])
    def test_mixing_with_a_plain_str_raises_in_either_order(self, raw):
        text = vetted("minted")
        with pytest.raises(TypeError, match="mint the other operand"):
            text + raw  # noqa: B018
        # str + ModelText reaches ModelText.__radd__ first: a subclass's
        # reflected operator takes priority over its base's.
        with pytest.raises(TypeError, match="mint the other operand"):
            raw + text  # noqa: B018
        with pytest.raises(TypeError):
            text += raw

    def test_mixing_with_a_forged_value_raises(self):
        with pytest.raises(TypeError, match="not minted"):
            vetted("a") + forged("b")  # noqa: B018

    def test_slicing_and_indexing_stay_minted(self):
        text = vetted("abcdef")
        for part in (text[1:4], text[:2], text[-1], text[::2]):
            assert type(part) is ModelText
            assert provenance(part) == (SOURCE_CASE, False)
        assert text[1:4] == "bcd"

    def test_other_str_methods_return_a_plain_str(self):
        text = vetted(" abc ")
        for result in (text.strip(), text.upper(), text.replace("a", "b"), text * 2):
            assert type(result) is str
        assert type("".join([text, text])) is str

    @pytest.mark.parametrize(
        "format_it",
        [
            lambda t: f"{t}",
            lambda t: f"prefix {t} suffix",
            lambda t: "{}".format(t),
            lambda t: format(t, ""),
        ],
        ids=["fstring", "fstring_embedded", "str_format", "format"],
    )
    def test_formatting_raises(self, format_it):
        with pytest.raises(TypeError, match="cannot be formatted"):
            format_it(vetted("x"))

    def test_str_returns_an_exact_str(self):
        text = vetted("abc")
        assert type(str(text)) is str and str(text) == "abc"
        # %-formatting and logging go through str(), not __format__.
        assert type("%s" % text) is str

    def test_copy_and_deepcopy_return_the_same_value(self):
        text = vetted("abc")
        assert copy.copy(text) is text
        assert copy.deepcopy(text) is text
        assert copy.deepcopy({"content": text})["content"] is text

    @pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
    def test_pickling_is_refused(self, protocol):
        with pytest.raises(TypeError, match="never pickled"):
            pickle.dumps(vetted("abc"), protocol=protocol)

    def test_its_slots_cannot_be_reassigned(self):
        text = vetted("abc")
        with pytest.raises(AttributeError, match="immutable"):
            text.source = SOURCE_NON_CASE
        with pytest.raises(AttributeError, match="immutable"):
            del text.redacted
        assert provenance(text) == (SOURCE_CASE, False)

    def test_a_value_built_without_the_token_has_no_provenance(self):
        """``str.__new__`` skips the class's own ``__new__``: a forged value."""
        value = forged("raw")
        assert type(value) is ModelText
        assert provenance(value) is None
        with pytest.raises(AttributeError, match="immutable"):
            value.source = SOURCE_CASE
        assert provenance(value) is None

    def test_a_plain_str_has_no_provenance(self):
        assert provenance("raw") is None
        assert provenance(None) is None


class TestCaseRedacted:
    async def test_redacts_through_the_context_and_records_the_posture(self):
        text = await case_redacted(f"host {IP} down", _enabled_ctx())
        assert type(text) is ModelText
        assert IP not in text and "<IP_ADDRESS_" in text
        assert provenance(text) == (SOURCE_CASE, True)

    async def test_a_disabled_context_mints_unredacted(self):
        text = await case_redacted(f"host {IP}", disabled_redaction())
        assert text == f"host {IP}"
        assert provenance(text) == (SOURCE_CASE, False)

    @pytest.mark.parametrize("ctx_factory", [_enabled_ctx, disabled_redaction])
    @pytest.mark.parametrize("value", [None, 7, ["a"], {"content": "a"}, b"x"])
    async def test_a_non_str_is_refused_under_every_posture(self, ctx_factory, value):
        with pytest.raises(TypeError, match="takes a str"):
            await case_redacted(value, ctx_factory())

    @pytest.mark.parametrize("ctx", [None, object(), SimpleNamespace(enabled=True)])
    async def test_the_context_is_required(self, ctx):
        with pytest.raises(TypeError, match="CaseRedactionContext"):
            await case_redacted("text", ctx)

    async def test_a_model_boundary_context_is_refused(self):
        with pytest.raises(TypeError, match="model-boundary"):
            await case_redacted("text", model_boundary_redaction(None))

    async def test_the_empty_string_is_minted(self):
        assert provenance(await case_redacted("", disabled_redaction())) is not None


class TestCaseRedactedMessages:
    async def test_every_content_is_redacted_and_minted(self):
        messages = [
            {"role": "system", "content": "fixed"},
            {"role": "user", "content": f"from {IP}"},
        ]
        out = await case_redacted_messages(messages, _enabled_ctx())
        assert [m["role"] for m in out] == ["system", "user"]
        assert all(provenance(m["content"]) == (SOURCE_CASE, True) for m in out)
        assert IP not in out[1]["content"]
        assert messages[1]["content"] == f"from {IP}", "the input is not mutated"

    @pytest.mark.parametrize("ctx_factory", [_enabled_ctx, disabled_redaction])
    @pytest.mark.parametrize(
        "messages",
        [
            "not a list",
            ["not a dict"],
            [{"role": "user", "content": ["structured"]}],
            [{"role": "user", "content": None}],
            [{"role": "user", "content": "x", "name": "extra"}],
            [{"role": "tool", "content": "x"}],
            [{"content": "no role"}],
        ],
        ids=[
            "str",
            "non_dict",
            "list_content",
            "none_content",
            "extra_key",
            "tool_role",
            "no_role",
        ],
    )
    async def test_shapes_are_refused_under_every_posture(self, ctx_factory, messages):
        with pytest.raises(TypeError):
            await case_redacted_messages(messages, ctx_factory())

    async def test_the_context_is_required(self):
        with pytest.raises(TypeError, match="CaseRedactionContext"):
            await case_redacted_messages([{"role": "user", "content": "x"}], None)


class TestModelBoundaryRedacted:
    async def test_text_is_minted_with_the_model_boundary_source(self):
        with sanitize_pii_pinned(True):
            text = await model_boundary_redacted(f"at {IP}", regex_only_sanitizer())
        assert IP not in text
        assert provenance(text) == (SOURCE_MODEL_BOUNDARY, True)

    async def test_messages_are_minted_and_shape_checked(self):
        with sanitize_pii_pinned(False):
            out = await model_boundary_redacted(
                [{"role": "user", "content": IP}], regex_only_sanitizer()
            )
            assert provenance(out[0]["content"]) == (SOURCE_MODEL_BOUNDARY, False)
            with pytest.raises(TypeError):
                await model_boundary_redacted(
                    [{"role": "user", "content": ["x"]}], regex_only_sanitizer()
                )
            with pytest.raises(TypeError):
                await model_boundary_redacted(42, regex_only_sanitizer())


class TestModelOutput:
    def test_the_reply_is_an_assistant_message_minted_as_model_output(self):
        response = LLMResponse(
            content="calling a tool",
            confidence=1.0,
            provider="p",
            model="m",
            tokens_used=1,
            response_time_ms=1,
            tool_calls=[
                ToolCall(
                    id="call_1",
                    type="function",
                    function={"name": "search_file", "arguments": '{"q": "x"}'},
                    provider_metadata={"thought_signature": "sig"},
                ),
                ToolCall(
                    id="call_2",
                    type="function",
                    function={"name": "other", "arguments": {"k": 1}},
                ),
            ],
            provider_metadata={"assistant_parts": [{"text": "x"}]},
        )
        message = model_output(response)
        assert message["role"] == "assistant"
        assert provenance(message["content"]) == (SOURCE_MODEL_OUTPUT, False)
        first, second = message["tool_calls"]
        assert first["function"]["name"] == "search_file"
        assert provenance(first["function"]["arguments"]) == (
            SOURCE_MODEL_OUTPUT,
            False,
        )
        assert first["provider_metadata"] == {"thought_signature": "sig"}
        assert second["function"]["arguments"] == '{"k": 1}'
        assert message["provider_metadata"] == {"assistant_parts": [{"text": "x"}]}

    def test_empty_content_is_minted_empty(self):
        response = SimpleNamespace(
            content=None, tool_calls=None, provider_metadata=None
        )
        message = model_output(response)
        assert message == {"role": "assistant", "content": ""}
        assert provenance(message["content"]) is not None


class TestNonCaseText:
    def test_a_plain_str_is_minted_as_non_case(self):
        assert provenance(non_case_text("Say hello")) == (SOURCE_NON_CASE, False)

    @pytest.mark.parametrize("value", [None, 1, b"x"], ids=["none", "int", "bytes"])
    def test_a_non_str_is_refused(self, value):
        with pytest.raises(TypeError, match="plain str"):
            non_case_text(value)

    def test_a_minted_value_cannot_be_relabelled_non_case(self):
        with pytest.raises(TypeError, match="plain str"):
            non_case_text(vetted("case content"))


class TestSanitizerPass:
    """The router's SANITIZE_PII pass: vetted in, vetted out (A2)."""

    async def test_off_returns_the_value_itself(self):
        text, messages = vetted(IP), vetted_messages([{"role": "user", "content": IP}])
        with sanitize_pii_pinned(False):
            assert await sanitizer_pass(text, regex_only_sanitizer()) is text
            assert await sanitizer_pass(messages, regex_only_sanitizer()) is messages
            assert await sanitizer_pass(None, regex_only_sanitizer()) is None

    async def test_on_a_vetted_prompt_is_sanitized_and_reminted(self):
        with sanitize_pii_pinned(True):
            out = await sanitizer_pass(vetted(f"host {IP}"), regex_only_sanitizer())
        assert IP not in out
        assert provenance(out) == (SOURCE_CASE, True)

    async def test_on_vetted_messages_keep_their_sources(self):
        messages = vetted_messages(
            [
                {"role": "system", "content": "fixed"},
                {"role": "user", "content": f"host {IP}"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "f", "arguments": f'{{"ip": "{IP}"}}'},
                            "provider_metadata": {"thought_signature": "s"},
                        }
                    ],
                    "provider_metadata": {"assistant_parts": ["opaque"]},
                },
                {"role": "tool", "tool_call_id": "c1", "name": "f", "content": IP},
            ]
        )
        with sanitize_pii_pinned(True):
            out = await sanitizer_pass(messages, regex_only_sanitizer())
        assert provenance(out[1]["content"]) == (SOURCE_CASE, True)
        assert IP not in out[1]["content"]
        assert provenance(out[2]["content"]) == (SOURCE_MODEL_OUTPUT, True)
        arguments = out[2]["tool_calls"][0]["function"]["arguments"]
        assert provenance(arguments) == (SOURCE_MODEL_OUTPUT, True)
        assert IP not in arguments
        assert out[2]["tool_calls"][0]["provider_metadata"] == {
            "thought_signature": "s"
        }
        assert out[2]["provider_metadata"] == {"assistant_parts": ["opaque"]}
        assert out[3]["tool_call_id"] == "c1" and out[3]["name"] == "f"
        assert provenance(out[3]["content"]) == (SOURCE_CASE, True)
        assert provenance(messages[1]["content"]) == (
            SOURCE_CASE,
            False,
        ), "the input is not mutated"

    async def test_on_a_raw_value_is_sanitized_and_left_raw(self):
        """No regression: raw text is still redacted, and still reported raw."""
        with sanitize_pii_pinned(True):
            out = await sanitizer_pass(f"host {IP}", regex_only_sanitizer())
            out_messages = await sanitizer_pass(
                [{"role": "user", "content": f"host {IP}"}], regex_only_sanitizer()
            )
        assert IP not in out and type(out) is str
        assert IP not in out_messages[0]["content"]
        assert type(out_messages[0]["content"]) is str

    async def test_on_a_forged_value_is_not_laundered(self):
        with sanitize_pii_pinned(True):
            out = await sanitizer_pass(forged(f"host {IP}"), regex_only_sanitizer())
        assert IP not in out
        assert provenance(out) is None


class TestPlainMessages:
    def test_a_list_with_nothing_minted_is_returned_as_is(self):
        messages = [{"role": "user", "content": "raw"}]
        assert plain_messages(messages) is messages

    def test_minted_channels_become_exact_str_without_mutating_the_input(self):
        messages = vetted_messages(
            [
                {"role": "user", "content": "u"},
                {
                    "role": "assistant",
                    "content": "a",
                    "tool_calls": [
                        {
                            "id": "c",
                            "type": "function",
                            "function": {"name": "f", "arguments": "{}"},
                        }
                    ],
                },
            ]
        )
        out = plain_messages(messages)
        assert type(out[0]["content"]) is str
        assert type(out[1]["content"]) is str
        assert type(out[1]["tool_calls"][0]["function"]["arguments"]) is str
        assert type(messages[0]["content"]) is ModelText
        assert type(messages[1]["tool_calls"][0]["function"]["arguments"]) is ModelText


def _mo(text: str):
    return vetted(text, SOURCE_MODEL_OUTPUT)


class TestReasoningArtifacts:
    """F1 (#2015): ``provider_metadata`` is a content channel — an adapter
    echoes it in place of the content — so its ``str`` leaves are minted."""

    def _response(self):
        return LLMResponse(
            content="checking",
            confidence=1.0,
            provider="p",
            model="m",
            tokens_used=1,
            response_time_ms=1,
            tool_calls=[
                ToolCall(
                    id="toolu_1",
                    type="function",
                    function={"name": "f", "arguments": "{}"},
                    provider_metadata={"thought_signature": "CikB"},
                )
            ],
            provider_metadata={
                "assistant_content": [
                    {"type": "thinking", "thinking": "full", "signature": "EqQB"},
                    {"type": "tool_use", "id": "toolu_1", "input": {"n": 3}},
                ],
                "assistant_parts": ({"text": "checking"},),
            },
        )

    def test_model_output_mints_every_str_leaf_and_keeps_the_rest(self):
        message = model_output(self._response())
        metadata = message["provider_metadata"]
        keys = [key for key in metadata] + [
            key for block in metadata["assistant_content"] for key in block
        ]
        assert keys and all(
            provenance(key) == (SOURCE_MODEL_OUTPUT, False) for key in keys
        ), "every key is minted too"
        thinking, tool_use = metadata["assistant_content"]
        for leaf in (thinking["thinking"], thinking["signature"], tool_use["id"]):
            assert provenance(leaf) == (SOURCE_MODEL_OUTPUT, False)
        assert tool_use["input"]["n"] == 3
        assert type(metadata["assistant_parts"]) is tuple
        signature = message["tool_calls"][0]["provider_metadata"]["thought_signature"]
        assert provenance(signature) == (SOURCE_MODEL_OUTPUT, False)
        assert not message_violations([message])

    def test_plain_messages_unwraps_the_leaves_to_the_original(self):
        response = self._response()
        [plain] = plain_messages([model_output(response)])
        assert plain["provider_metadata"] == response.provider_metadata
        assert all(type(key) is str for key in plain["provider_metadata"])
        for block in plain["provider_metadata"]["assistant_content"]:
            assert all(type(key) is str for key in block)
        assert type(plain["provider_metadata"]["assistant_parts"]) is tuple
        leaf = plain["provider_metadata"]["assistant_content"][0]["thinking"]
        assert type(leaf) is str
        call_meta = plain["tool_calls"][0]["provider_metadata"]
        assert type(call_meta["thought_signature"]) is str

    @pytest.mark.parametrize(
        "metadata,kind",
        [
            ({_mo("assistant_parts"): [{_mo("text"): "raw"}]}, "raw"),
            ({_mo("assistant_parts"): [{_mo("text"): forged("x")}]}, "forged"),
            (
                {_mo("assistant_parts"): [{_mo("text"): vetted("x")}]},
                "not_model_output",
            ),
            # Keys reach the wire as text too (#2015 delta R-1).
            ({_mo("assistant_parts"): [{"raw key": _mo("x")}]}, "raw"),
            ({_mo("assistant_content"): [{forged("k"): _mo("x")}]}, "forged"),
            ({vetted("case key"): _mo("x")}, "not_model_output"),
            # Inside a tuple as inside a list.
            ({_mo("assistant_parts"): ({_mo("text"): "raw"},)}, "raw"),
        ],
        ids=[
            "raw",
            "forged",
            "case_minted",
            "raw_key",
            "forged_key",
            "case_minted_key",
            "raw_in_tuple",
        ],
    )
    def test_the_grammar_checks_every_leaf(self, metadata, kind):
        message = {
            "role": "assistant",
            "content": vetted("", SOURCE_MODEL_OUTPUT),
            "provider_metadata": metadata,
        }
        [violation] = message_violations([message])
        assert (violation.argument, violation.kind) == (
            "messages.provider_metadata",
            kind,
        )

    def test_the_test_minter_mints_the_leaves_as_model_output(self):
        [message] = vetted_messages(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "provider_metadata": {"assistant_parts": [{"text": "x"}]},
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "f", "arguments": "{}"},
                            "provider_metadata": {"thought_signature": "s"},
                        }
                    ],
                }
            ]
        )
        assert not message_violations([message])


class TestUnwrapKeepsTheValue:
    """F4: ``str()`` of a ``(str, Enum)`` member is ``"Class.MEMBER"``."""

    def test_plain_text_and_plain_messages_keep_a_str_subclass_value(self):
        import enum

        class Kind(str, enum.Enum):
            X = "the value"

        assert str(Kind.X) != "the value"  # the trap
        assert plain_text(Kind.X) == "the value" and type(plain_text(Kind.X)) is str
        [message] = plain_messages([{"role": "user", "content": Kind.X}])
        assert message["content"] == "the value"
        assert type(message["content"]) is str
        assert vetted(Kind.X) == "the value"
