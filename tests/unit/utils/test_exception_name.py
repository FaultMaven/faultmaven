"""``exception_class_name``: the class name, or ``Exception`` for an odd one;
``exception_class_chain``: those of an exception's chain, bounded (#2587)."""

import pytest

from faultmaven.utils.exception_name import (
    CLASS_CHAIN_LINKS,
    exception_class_chain,
    exception_class_name,
)


def test_a_plain_class_name_is_reported_as_is():
    assert exception_class_name(TimeoutError("db-internal:5432")) == "TimeoutError"


@pytest.mark.parametrize("odd_name", ["pkg.Odd", "Ошибка", "has space"])
def test_a_name_that_is_not_a_plain_identifier_is_reported_as_exception(odd_name):
    odd = type(odd_name, (Exception,), {})("text")
    assert exception_class_name(odd) == "Exception"


def test_a_chain_names_the_cause_or_else_the_context_and_no_text():
    try:
        try:
            raise KeyError("db-internal:5432")
        except KeyError:
            raise OSError("db-internal:5432")  # its context, no cause
    except OSError as context_link:
        error = ValueError("db-internal:5432")
        error.__cause__ = context_link

    assert exception_class_chain(error) == "ValueError <- OSError <- KeyError"


def test_a_cyclic_chain_stops_at_the_bound():
    first, second = ValueError("a"), KeyError("b")
    first.__cause__, second.__cause__ = second, first

    chain = exception_class_chain(first).split(" <- ")

    assert len(chain) == CLASS_CHAIN_LINKS
    assert chain[:2] == ["ValueError", "KeyError"]
