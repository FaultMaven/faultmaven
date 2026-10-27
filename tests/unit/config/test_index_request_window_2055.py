"""``INDEX_REQUEST_WINDOW_SECONDS`` is 0 (refuse nothing up front) or 60-3600,
and anything else fails startup (#2055)."""

import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from faultmaven.config.settings import KnowledgeSettings

pytestmark = [pytest.mark.unit]


def _window(value=None) -> int:
    env = {k: v for k, v in os.environ.items() if k != "INDEX_REQUEST_WINDOW_SECONDS"}
    if value is not None:
        env["INDEX_REQUEST_WINDOW_SECONDS"] = value
    with patch.dict(os.environ, env, clear=True):
        return KnowledgeSettings().index_request_window_seconds


def test_the_default_refuses_nothing_up_front():
    assert _window() == 0


@pytest.mark.parametrize("value", ["0", "60", "600", "3600"])
def test_off_or_inside_the_bounds_is_accepted(value):
    assert _window(value) == int(value)


@pytest.mark.parametrize("value", ["-1", "1", "59", "3601"])
def test_anything_else_fails_startup(value):
    with pytest.raises(ValidationError, match="INDEX_REQUEST_WINDOW_SECONDS"):
        _window(value)
