"""``exception_class_name``: the class name, or ``Exception`` for an odd one;
``exception_class_chain``: those of an exception's cause chain (#2587);
``exception_frames``: the frames a traceback would show, without a message (#2844)."""

import pytest

from faultmaven.utils.exception_name import (
    exception_class_chain,
    exception_class_name,
    exception_frames,
)


def test_a_plain_class_name_is_reported_as_is():
    assert exception_class_name(TimeoutError("db-internal:5432")) == "TimeoutError"


@pytest.mark.parametrize("odd_name", ["pkg.Odd", "Ошибка", "has space"])
def test_a_name_that_is_not_a_plain_identifier_is_reported_as_exception(odd_name):
    odd = type(odd_name, (Exception,), {})("text")
    assert exception_class_name(odd) == "Exception"


def test_a_chain_names_each_cause_and_no_context():
    try:
        try:
            raise KeyError("db-internal:5432")
        except KeyError:
            raise OSError("db-internal:5432")  # KeyError is its context only
    except OSError as cause:
        error = ValueError("db-internal:5432")
        error.__cause__ = cause

    assert exception_class_chain(error) == "ValueError <- OSError"


def test_a_cyclic_chain_terminates():
    first, second = ValueError("a"), KeyError("b")
    first.__cause__, second.__cause__ = second, first

    assert exception_class_chain(first) == "ValueError <- KeyError"


class FalsyError(Exception):
    def __len__(self) -> int:
        return 0


class UnaskableError(Exception):
    def __bool__(self) -> bool:
        raise RuntimeError("db-internal:5432")


@pytest.mark.parametrize("cause_class", [FalsyError, UnaskableError])
def test_a_cause_whose_truth_is_odd_is_named(cause_class):
    error = ValueError("db-internal:5432")
    error.__cause__ = cause_class("db-internal:5432")

    assert exception_class_chain(error) == f"ValueError <- {cause_class.__name__}"


def _raised(error, cause=None, context=None, suppress=False):
    """``error`` as raised: from ``cause``, or while handling ``context``."""
    try:
        try:
            if context is not None:
                raise context
        except type(context):
            if suppress:
                raise error from None
            raise error  # the implicit link is the point
        raise error from cause
    except type(error) as caught:
        return caught


def test_frames_follow_the_links_a_traceback_shows_without_a_message():
    implicit = _raised(ValueError("db-internal:5432"), context=KeyError("secret"))
    explicit = _raised(OSError("db-internal:5432"), cause=implicit)

    frames = exception_frames(explicit)
    assert [line for line in frames.splitlines() if not line.startswith(" ")] == [
        "OSError:",
        "ValueError:",
        "KeyError:",
    ]
    assert "db-internal" not in frames and "secret" not in frames.replace(
        "KeyError", ""
    )


@pytest.mark.parametrize("cause_class", [FalsyError, UnaskableError])
def test_frames_stop_where_a_traceback_does(cause_class):
    suppressed = _raised(ValueError("a"), context=KeyError("b"), suppress=True)
    assert exception_frames(suppressed).splitlines()[0] == "ValueError:"
    assert "KeyError" not in exception_frames(suppressed)

    first, second = ValueError("a"), cause_class("b")
    first.__cause__, second.__context__ = second, first
    assert exception_frames(first) == f"ValueError:\n{cause_class.__name__}:\n"
