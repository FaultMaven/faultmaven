"""A section header is one whole line, ``=== NAME ===`` (#2949): a banner's
line, or a header quoted mid-line, stays in chunk text."""

import pytest

from faultmaven.core.preprocessing import vector_storage
from faultmaven.core.preprocessing.vector_storage import chunk_structural_index

pytestmark = pytest.mark.unit

RULE = "=" * 60
BANNER = f"{RULE}\nCRIME SCENE EXTRACTION: No errors detected\n{RULE}"
ISSUE = (
    "build started\n{0}\n ERROR: OOMKilled in payments-api pod\n{0}\nexit 137".format(
        "=" * 40
    )
)
QUOTED = 'You asked for "for c in a b; do echo "=== $c ==="; done":'


@pytest.mark.parametrize(
    "text,expected",
    [
        pytest.param(
            ISSUE + "\n", [(ISSUE, "HEADER")], id="the-issue-s-banner-is-content"
        ),
        pytest.param(
            f"=== FILE SUMMARY ===\n3 lines\n\n{BANNER}\n\nline one",
            [(f"3 lines\n\n{BANNER}\n\nline one", "FILE SUMMARY")],
            id="the-logs-extractor-s-banner-is-content",
        ),
        pytest.param(
            f"=== COMMANDS ===\n{QUOTED}\nsubject=CN=a",
            [(f"{QUOTED}\nsubject=CN=a", "COMMANDS")],
            id="a-header-quoted-mid-line-stays-in-its-line",
        ),
        pytest.param(
            "=== Error Summary ===\r\nOOMKilled x3\r\n=== Timeline ===\t\r\n10:40 deploy",
            [("OOMKilled x3", "Error Summary"), ("10:40 deploy", "Timeline")],
            id="a-crlf-header-is-a-header",
        ),
        pytest.param(
            "certs:\n=== /etc/tls/ca.crt ===\nsubject=CN=Corp Root CA",
            [("certs:", "HEADER"), ("subject=CN=Corp Root CA", "/etc/tls/ca.crt")],
            id="a-whole-line-header-is-a-label-whatever-produced-it",
        ),
        pytest.param(
            "\ufeff=== FILE SUMMARY ===\nfoo",
            [("\ufeff", "HEADER"), ("foo", "FILE SUMMARY")],
            id="a-header-after-a-byte-order-mark-is-a-header",
        ),
        pytest.param(
            "=== A ===\rfoo\r=== B ===\rbar",
            [("foo", "A"), ("bar", "B")],
            id="a-cr-only-header-is-a-header",
        ),
        pytest.param(
            "=== dGVzdA== ===\nfoo",
            [("foo", "dGVzdA==")],
            id="the-label-keeps-its-equals",
        ),
        pytest.param(
            "x\n===   ===\ny",
            [("x\n===   ===\ny", "HEADER")],
            id="a-blank-name-is-content",
        ),
    ],
)
def test_a_section_header_is_one_whole_line(text, expected):
    chunks = chunk_structural_index(text)

    assert [(c.text, c.metadata["section"]) for c in chunks] == expected


def test_a_long_run_of_blanks_in_a_header_line_is_matched_in_linear_time(
    monkeypatch,
):
    """Blanks two parts of the pattern could both claim made a match
    backtrack super-linearly. tiktoken is out of the way: its own encode of
    the run is slow."""
    monkeypatch.setattr(
        vector_storage, "_get_tiktoken_encoder", lambda model="gpt-4": None
    )

    chunks = chunk_structural_index("=== " + " " * 100_000 + "x\nmore")

    assert chunks[0].metadata["section"] == "HEADER"
