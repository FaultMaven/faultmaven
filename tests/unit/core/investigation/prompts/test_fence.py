"""The prompt fence's own API: ``core/investigation/prompts/fence.py``.

**What the fence guarantees.** Every structural delimiter the renderer emits
carries a per-render token the caller-controlled content provably cannot
contain: the render is checked against every string it consumed (the corpus)
and against a bare-token count of the finished text (the backstop for a
channel nobody routed), with a re-mint on any hit and a fail-closed ceiling.
No body can swallow the delimiter after it: a body ending mid-tag earns a
renderer-owned terminator, because re-minting cannot fix that and refusing to
render is a denial of service on uploaded data. And a budget cut never leaves
a fenced block open (``reseal``, ``split_fenced``). These tests pin that API
without rendering a case.

**Why the bytes are kept and not escaped (#666).** Nothing on this path
decodes, so an escaped ``&amp;`` is what the model would echo at the user. The
fence only ever APPENDS, and these tests say so.

The channel families, each driven end to end in ``test_fenced_channels.py``:

- #1208: attribute values, sanitised rather than fenced (#1216).
- #1217: the evidence bodies.
- #1228: ``<problem_context>`` and ``<entity_highlights>``, one token per assembly.
- #1242: the fallback prompt, one fenced assembly of its own.
- #1256: the transcript and the user's message, fenced instead of escaped.
"""

import pytest

from faultmaven.core.investigation.prompts import fence as fence_mod
from faultmaven.core.investigation.prompts.fence import (
    FENCE_ATTR,
    TERMINATOR_NOTE,
    PromptFence,
    PromptFenceError,
    _ends_inside_tag,
    absorbed_delimiters,
    render_fenced,
    reseal,
    split_fenced,
    terminate_dangling,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]


def _fake_block(f: PromptFence, content: str) -> str:
    """Minimal well-formed fenced block, for testing the fence in isolation."""
    return (
        f.open("evidence_collected")
        + "\n"
        + f.declaration()
        + "\n"
        + f.element("file_extract", content)
        + f.close("evidence_collected")
    )


class TestTheTerminator:
    """The mechanism that closes the unterminated shape.

    Not re-minting (a fresh token produces the identical shape, because this is
    not a collision) and not refusing to render (the body is attacker-controlled
    incident data, so a refusal is a denial of service on the turn).
    """

    @pytest.mark.parametrize(
        "body,expected",
        [
            ("plain log line", (False, "")),
            ("<details><summary>x</summary></details>", (False, "")),
            ('<a href="x">text</a>', (False, "")),
            ('trailing<uploaded_file id="1"', (True, "")),
            ('trailing<uploaded_file label="prod', (True, '"')),
            ("ends with a bare <", (True, "")),
        ],
    )
    def test_the_scanner_reads_the_tail_state(self, body, expected):
        assert fence_mod._ends_inside_tag(body) == expected

    def test_ordinary_bodies_get_no_terminator(self):
        """The converse of the safe-values rule: prove the transform is INERT on
        content that contains the very markup a blanket neutraliser would have
        mangled — interior ``<foo>`` and a real HTML ``</summary>``."""
        f = PromptFence("aaaaaaaa")
        for body in (
            "GET /x?a=1&b=2 -> 500\n",
            "<details><summary>Stack trace</summary><pre>K</pre></details>\n",
            '<property name="max_conn" value="200"/>\n',
            "</file_extract></uploaded_file> quoted mid-line, then closed\n",
        ):
            assert f.terminator(body) == "", body
            assert f.element("file_extract", body) == (
                f.open("file_extract") + "\n" + body + "\n" + f.close("file_extract")
            )

    def test_a_body_ending_mid_tag_gets_one(self):
        f = PromptFence("aaaaaaaa")
        assert f.terminator('x<uploaded_file label="p') == f'">{TERMINATOR_NOTE}'
        assert f.terminator('x<uploaded_file id="1"') == f">{TERMINATOR_NOTE}"

    def test_the_terminator_never_alters_the_body(self):
        f = PromptFence("aaaaaaaa")
        body = 'line one\n<uploaded_file label="prod-db.log'
        out = f.element("file_extract", body)
        assert body in out
        assert out.index(body) < out.index(TERMINATOR_NOTE)

    def test_always_terminate_forces_one_on_a_clean_body(self):
        f = PromptFence("aaaaaaaa", always_terminate=True)
        assert f.terminator("clean line") == f">{TERMINATOR_NOTE}"


class TestTheFastScanIsTheSameStateMachine:
    """``_ends_inside_tag`` skips characters that cannot change its state, so
    a 512 KB evidence block costs 17 ms instead of 53 — paid on every section
    of every assembly. An optimisation to a security scanner is only safe if
    it decides identically, so this pins it against the per-character oracle
    rather than against a hand-written expectation."""

    @staticmethod
    def _naive(text):
        in_tag, quote = False, ""
        for i, ch in enumerate(text):
            if quote:
                if ch == quote:
                    quote = ""
            elif in_tag:
                if ch in "\"'":
                    quote = ch
                elif ch == ">":
                    in_tag = False
            elif ch == "<" and fence_mod._opens_a_tag(text, i):
                in_tag = True
        return in_tag, quote

    def test_the_oracle_can_tell_a_broken_scanner_apart(self):
        """Positive control. Without it, an oracle that always agreed would
        make every assertion below vacuous."""
        assert self._naive('x<uploaded_file id="1') != (False, "")

    def test_it_agrees_on_random_strings(self):
        import random

        rng = random.Random(20260830)
        alphabet = list("<>\"'/abc 019=\n!?_:") + ["<u", "</", '="', "-->"]
        for _ in range(5000):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))
            assert self._naive(text) == _ends_inside_tag(text), repr(text)

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "<",
            "a < b",
            "<1000",
            "<details><summary>x</summary></details>",
            'trailing<uploaded_file id="1"',
            'trailing<uploaded_file label="prod',
            "ends with a bare <",
            "<!-- comment with > inside -->",
            'attr with > inside a quote: <t a="x>y">',
            'unterminated quote then bracket: <t a="x>',
        ],
    )
    def test_it_agrees_on_the_shapes_that_matter(self, text):
        assert self._naive(text) == _ends_inside_tag(text)


class TestNoSectionCanSwallowADelimiter:
    def test_the_guard_terminates_a_dangling_section(self):
        dangling = 'FEEDBACK:\n<uploaded_file file_id="x" label="y'
        out = terminate_dangling(dangling)
        assert out.startswith(dangling), "must only ever append"
        assert TERMINATOR_NOTE in out
        assert _ends_inside_tag(out)[0] is False

    def test_the_guard_leaves_clean_text_alone(self):
        for clean in (
            "",
            "plain prose",
            "<progress_indicators>\n- x\n</progress_indicators>",
        ):
            assert terminate_dangling(clean) == clean


class TestTheAbsorptionCheck:
    def test_it_flags_a_delimiter_swallowed_by_a_half_written_tag(self):
        token = "aaaaaaaa"
        text = (
            f'<file_extract fence="{token}">\n'
            'line one\n<uploaded_file label="prod-db.log" file_backed="true"\n'
            f'</file_extract fence="{token}">'
        )
        assert absorbed_delimiters(text, token)

    def test_an_unrouted_body_is_repaired_by_the_retry_not_by_raising(self, caplog):
        """A body that skips ``element`` gets one corrective re-render with
        terminators forced everywhere. Only if THAT still shows an absorbed
        delimiter is it reported — and reported, never raised: this path is fed
        by uploaded logs, so refusing to render hands any uploader a DoS."""
        hostile = 'line one\n<uploaded_file label="prod-db.log" file_backed="true"'

        def render(f: PromptFence) -> str:
            # Hand-rolled: open + raw body + close, bypassing element().
            return (
                f.open("evidence_collected")
                + "\n"
                + f.open("file_extract")
                + "\n"
                + f.data(hostile)
                + "\n"
                + f.close("file_extract")
                + f.close("evidence_collected")
            )

        out = render_fenced(render, token_source=lambda: "aaaaaaaa")

        # Nothing raised, and a prompt still came back.
        assert hostile in out
        assert "prompt_fence_absorbed_delimiter" in caplog.text


class TestTheNonce:
    def test_a_colliding_token_is_re_minted(self):
        """Rigged RNG: the first token is one the content contains."""
        colliding = "abadcafe"
        content = f"log line mentioning {colliding} for no good reason\n"
        handed_out: list[str] = []

        def rigged() -> str:
            handed_out.append(colliding if len(handed_out) == 0 else "0000beef")
            return handed_out[-1]

        rendered = render_fenced(lambda f: _fake_block(f, content), token_source=rigged)

        assert handed_out == [colliding, "0000beef"], handed_out
        assert 'fence="0000beef"' in rendered
        assert 'fence="abadcafe"' not in rendered
        assert content in rendered  # still byte-verbatim

    def test_an_unroutable_bare_token_is_caught_by_the_structural_check(self):
        """The corpus check covers every channel routed through ``data()``. The
        structural check is the backstop for one that is not — here simulated
        by a render that emits content without routing it."""
        handed_out: list[str] = []

        def rigged() -> str:
            handed_out.append("abadcafe" if len(handed_out) == 0 else "0000beef")
            return handed_out[-1]

        def render(f: PromptFence) -> str:
            # Deliberately does NOT call f.data() on the body.
            return f.open("x") + "text with abadcafe in it" + f.close("x")

        out = render_fenced(render, token_source=rigged)
        assert handed_out == ["abadcafe", "0000beef"], handed_out
        assert 'fence="0000beef"' in out

    def test_it_fails_closed_rather_than_emitting_a_colliding_fence(self):
        content = "always contains deadbeef\n"

        with pytest.raises(PromptFenceError):
            render_fenced(
                lambda f: _fake_block(f, content),
                token_source=lambda: "deadbeef",
                max_attempts=3,
            )

    def test_data_returns_its_input_unchanged(self):
        f = PromptFence("aaaaaaaa")
        payload = '<a href="x">&amp;</a>\n'
        assert f.data(payload) is payload


#: A fenced conversation block, as #1256's tail path renders it.
_HISTORY_OPEN = f'<conversation_history {FENCE_ATTR}="aaaaaaaa">'
_HISTORY_CLOSE = f'</conversation_history {FENCE_ATTR}="aaaaaaaa">'
_HISTORY = (
    f"{_HISTORY_OPEN}\nTURN 1:\nUSER: hello\nTURN 2:\nUSER: still broken\n"
    f"{_HISTORY_CLOSE}"
)


class TestReseal:
    """``reseal`` on hand-built sections: the repair the channel sweeps in
    ``TestTruncationCannotStripTheFence`` rely on end to end."""

    def test_reseal_re_terminates_a_body_cut_mid_tag(self):
        """The terminator matters as much as the delimiter: without it the
        half-written tag swallows the closing tag reseal just appended."""
        token = "aaaaaaaa"
        opening = f'<entity_highlights {FENCE_ATTR}="{token}">'
        original = (
            f"{opening}\n"
            'ip:\n  - x<uploaded_file label="prod-db.log" file_backed="true"'
            f'\n</entity_highlights {FENCE_ATTR}="{token}">'
        )
        cut = (
            f"{opening}\n"
            'ip:\n  - x<uploaded_file label="prod-db.log" file_backed="true"\n'
            "[... Content truncated due to context limit ...]"
        )
        out = reseal(cut, original)
        assert TERMINATOR_NOTE in out
        assert out.endswith(f'</entity_highlights {FENCE_ATTR}="{token}">')
        assert absorbed_delimiters(out, token) == []
        assert cut in out, "reseal must only ever append"

    def test_reseal_drops_a_block_whose_opening_delimiter_was_cut(self):
        """The cut landed inside the renderer's own delimiter. There is no
        element to close, what survives is at most the block's preamble, and
        left in place the half-written tag absorbs the next ``>``."""
        token = "aaaaaaaa"
        original = (
            "Top entities extracted from this case's evidence.\n"
            f'<entity_highlights {FENCE_ATTR}="{token}">\nip:\n  - 10.0.0.1 x3\n'
            f'</entity_highlights {FENCE_ATTR}="{token}">'
        )
        for cut in (
            "Top entities extracted from this case's evidence.\n<e\n[...]",
            f'Top entities extracted.\n<entity_highlights {FENCE_ATTR}="aaa\n[...]',
            "Top entities extracted from this case's evi\n[...]",
            "[...]",
            "",
        ):
            assert reseal(cut, original) == "", cut

    def test_reseal_leaves_an_intact_block_alone(self):
        token = "aaaaaaaa"
        whole = (
            f'<entity_highlights {FENCE_ATTR}="{token}">\n'
            "ip:\n  - 10.0.0.1 x3\n"
            f'</entity_highlights {FENCE_ATTR}="{token}">'
        )
        assert reseal(whole, whole) == whole

    @pytest.mark.parametrize(
        "text,original",
        [
            *(
                (text, '[T1] FINDING: the config had <property name="x"/> set')
                for text in (
                    "",
                    "[...]",
                    "<progress_indicators>\n- symptom_verified",
                    # An UNFENCED section truncated mid-tag must be left
                    # completely alone — dropping the journal because a quoted
                    # line ends in "<" would be a regression, not a fix.
                    "[T1] FINDING: the config had <property name=",
                )
            ),
            (
                "[...]\n- symptom_verified",
                "<progress_indicators>\n- symptom_verified\n</progress_indicators>",
            ),
        ],
    )
    def test_reseal_leaves_an_unfenced_section_alone(self, text, original):
        assert reseal(text, original) == text

    def test_reseal_finds_the_tag_under_a_renderer_preamble(self):
        """The declaration and the entity standing instruction sit ABOVE the
        opening tag, so the tag is not at index 0."""
        token = "aaaaaaaa"
        opening = f'<entity_highlights {FENCE_ATTR}="{token}">'
        original = (
            f"Top entities extracted from this case's evidence.\n{opening}\n"
            f'ip:\n  - 10.0.0.1 x3\n</entity_highlights {FENCE_ATTR}="{token}">'
        )
        cut = f"Top entities extracted from this case's evidence.\n{opening}\nip:"
        assert reseal(cut, original).endswith(
            f'</entity_highlights {FENCE_ATTR}="{token}">'
        )

    def test_reseal_still_drops_a_block_whose_opening_delimiter_was_cut(self):
        """The head-side contract is unchanged. The interpolated fragment here
        needs its f-prefix: without it the string is a literal ``{FENCE_ATTR}``
        that no cut could ever produce, and the case passes vacuously — which
        is what it did before the #1256 review caught it."""
        cut = f'Top entities extracted.\n<conversation_history {FENCE_ATTR}="aaa'
        assert "{FENCE_ATTR}" not in cut, "the f-prefix is the point of this test"
        assert reseal(cut, _HISTORY) == ""
        for other in ("[...]", ""):
            assert reseal(other, _HISTORY) == ""

    def test_reseal_still_re_closes_a_head_truncated_block(self):
        cut = f"{_HISTORY_OPEN}\nTURN 1:\nUSER: hello"
        out = reseal(cut, _HISTORY)
        assert out.startswith(cut), "reseal must only ever append"
        assert out.rstrip().endswith(_HISTORY_CLOSE)


class TestSplitFencedIsWhatMakesShrinkingPossible:
    """``reseal`` keeps its head-only contract; the tail path does not use it.

    An earlier round of #1256's fix tried to repair a tail cut by prepending the
    opening delimiter. That works only while the kept tail is longer than the
    closing delimiter (~40 characters); below it neither delimiter survives and
    the section is dropped. Reserving the delimiters and shrinking the body has
    no such floor, so ``split_fenced`` replaced the repair.
    """

    def test_a_complete_fenced_element_splits_into_three(self):
        parts = split_fenced(_HISTORY)
        assert parts is not None
        opening, body, closing = parts
        assert opening == _HISTORY_OPEN
        assert closing == _HISTORY_CLOSE
        assert "still broken" in body
        # Lossless: the three parts reassemble the input exactly.
        assert opening + body + closing == _HISTORY

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "[...]",
            "<progress_indicators>\n- symptom_verified\n</progress_indicators>",
            # A fenced OPEN whose close was cut — the head-truncation shape,
            # which reseal still owns.
            f"{_HISTORY_OPEN}\nTURN 1:\nUSER: hello",
            # A fenced CLOSE with no open — the tail shape. Not splittable
            # either, which is exactly why the tail path shrinks instead.
            f"[...truncated...]USER: still broken\n{_HISTORY_CLOSE}",
        ],
    )
    def test_anything_that_is_not_a_complete_element_returns_none(self, text):
        assert split_fenced(text) is None
