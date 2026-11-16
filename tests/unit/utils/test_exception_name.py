"""``exception_class_name``: the class name, or ``Exception`` for an odd one;
``exception_class_chain``: those of an exception's cause chain (#2587)."""

import pytest

from faultmaven.utils.exception_name import (
    exception_class_chain,
    exception_class_name,
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
