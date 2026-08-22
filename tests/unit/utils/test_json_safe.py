"""fm#1048: ``to_json_safe`` is total — no input makes it raise.

The unit that the validation handler leans on. These tests are deliberately
about the *contract* (nothing raises, everything renders under Starlette's
encoder) rather than about the five shapes that happened to be reported, so a
sixth shape is covered without a sixth test.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from faultmaven.utils.serialization import (
    DEFAULT_SAFE_DEPTH,
    DEFAULT_SAFE_STRING_CHARS,
    _safe_text,
    to_json_safe,
)


def render_like_starlette(value: object) -> bytes:
    """Exactly what JSONResponse does — the bar the function has to clear.

    Plain ``json.dumps`` is a weaker check: it accepts NaN and lone surrogates,
    both of which Starlette rejects.
    """
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        indent=None,
        separators=(",", ":"),
    ).encode("utf-8")


class Exploding:
    """An object whose repr raises — the fallback's own fallback."""

    def __repr__(self) -> str:
        raise RuntimeError("no repr for you")


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        b"grant_type=refresh_token&refresh_token=x",
        b"\xff\xfe\x00\x01binary",
        bytearray(b"ab"),
        memoryview(b"cd"),
        float("nan"),
        float("inf"),
        float("-inf"),
        "abc\ud800def",
        {"k": float("nan")},
        {"nested": [b"\xff", {"deep": float("inf")}]},
        {float("nan"): "nan-key"},
        {b"bytes-key": 1},
        (1, 2, 3),
        {1, 2, 3},
        frozenset({"a"}),
        Exploding(),
        [Exploding()],
        ValueError("boom"),
        Decimal("1.5"),
        UUID("12345678-1234-5678-1234-567812345678"),
        datetime(2026, 8, 21, tzinfo=timezone.utc),
        # pytest builds ids with str(), which hits the same 4300-digit limit
        # this value exists to test — hence the explicit id.
        pytest.param(10**5000, id="huge-int"),
        object(),
        None,
        True,
        False,
        0,
        "",
    ],
)
def test_renders_under_starlettes_encoder(value):
    """Every one of these raised somewhere before fm#1048."""
    render_like_starlette(to_json_safe(value))


@pytest.mark.unit
def test_deep_nesting_does_not_recurse_away():
    """`input` is attacker-supplied JSON: nesting must not reach RecursionError."""
    deep: object = "leaf"
    for _ in range(5000):
        deep = {"n": deep}

    rendered = render_like_starlette(to_json_safe(deep))

    assert b"items" in rendered  # the depth summary, not the 5000th level
    assert len(rendered) < 1000


@pytest.mark.unit
def test_long_strings_are_truncated():
    out = to_json_safe("x" * 10_000)

    assert out.startswith("x" * DEFAULT_SAFE_STRING_CHARS)
    assert out.endswith("[truncated]")
    assert len(out) < DEFAULT_SAFE_STRING_CHARS + 40


@pytest.mark.unit
def test_bytes_are_decoded_not_repred():
    """The form-encoded body should still be readable in the 422."""
    assert to_json_safe(b"grant_type=refresh_token") == "grant_type=refresh_token"


@pytest.mark.unit
def test_long_bytes_are_sliced_before_decoding():
    """A 10 MB body must not be copied whole to yield 512 characters.

    The result has to be identical to decoding everything and then cutting,
    including for multi-byte text where the slice can land mid-character.
    """
    for raw in (b"y" * 1_000_000, "\u98df".encode() * 400_000, b"\xff\xfe" * 300_000):
        assert to_json_safe(raw) == _safe_text(
            raw.decode("utf-8", "replace"), DEFAULT_SAFE_STRING_CHARS
        )


@pytest.mark.unit
def test_undecodable_bytes_survive_as_replacements():
    out = to_json_safe(b"\xff\xfeok")

    assert isinstance(out, str)
    assert out.endswith("ok")


@pytest.mark.unit
def test_exceptions_render_as_their_message():
    """Pre-#1048 the handler special-cased ValueError in ctx; keep that output."""
    assert to_json_safe({"ctx": {"error": ValueError("bad value")}}) == {
        "ctx": {"error": "bad value"}
    }


@pytest.mark.unit
def test_non_finite_floats_become_their_name():
    assert to_json_safe(float("nan")) == "nan"
    assert to_json_safe(float("inf")) == "inf"
    assert to_json_safe(-float("inf")) == "-inf"


@pytest.mark.unit
def test_finite_numbers_and_bools_keep_their_type():
    """A lossy converter that stringified everything would be useless."""
    assert to_json_safe({"a": 1, "b": 1.5, "c": True, "d": None, "e": "s"}) == {
        "a": 1,
        "b": 1.5,
        "c": True,
        "d": None,
        "e": "s",
    }


@pytest.mark.unit
def test_huge_int_is_summarized_not_stringified():
    """str(int) raises above sys.get_int_max_str_digits(); json.dumps uses str."""
    out = to_json_safe(10**5000)

    assert isinstance(out, str) and out.startswith("<int:")


@pytest.mark.unit
def test_depth_limit_is_configurable():
    nested = {"a": {"b": {"c": "deep"}}}

    assert to_json_safe(nested, max_depth=1) == {"a": "<dict: 1 items>"}
    assert to_json_safe(nested, max_depth=DEFAULT_SAFE_DEPTH) == nested


@pytest.mark.unit
def test_upload_file_renders_as_its_repr():
    """The multipart shape: a file part bound to a scalar Form field."""
    from fastapi import UploadFile

    out = to_json_safe(UploadFile(filename="q.txt", file=None))

    assert isinstance(out, str) and "q.txt" in out
