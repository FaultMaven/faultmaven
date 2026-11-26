"""`_echo` renders any client value without raising (#2974)."""

import pytest

from faultmaven.modules.auth.api.oauth import _echo

pytestmark = pytest.mark.unit


class _Unrenderable:
    """What `str()` of a list nested past the interpreter's limit does."""

    def __str__(self) -> str:
        raise RecursionError("maximum recursion depth exceeded")

    __repr__ = __str__  # a list renders its members by repr


@pytest.mark.parametrize("value", [_Unrenderable(), [_Unrenderable()], "a\ud800"])
def test_a_value_str_or_utf8_cannot_render_is_a_placeholder(value):
    assert _echo(value) == "<unrenderable value>"


@pytest.mark.parametrize(
    "value, rendered",
    [
        ("client_credentials", "client_credentials"),
        (["x"], "['x']"),
        ("z" * 200, "z" * 120 + "…"),
        ("語" * 41, "語" * 40 + "…"),
    ],
)
def test_a_renderable_value_is_echoed_as_before(value, rendered):
    assert _echo(value) == rendered
