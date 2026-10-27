"""``exception_class_name``: the class name, or ``Exception`` for an odd one."""

import pytest

from faultmaven.utils.exception_name import exception_class_name


def test_a_plain_class_name_is_reported_as_is():
    assert exception_class_name(TimeoutError("db-internal:5432")) == "TimeoutError"


@pytest.mark.parametrize("odd_name", ["pkg.Odd", "Ошибка", "has space"])
def test_a_name_that_is_not_a_plain_identifier_is_reported_as_exception(odd_name):
    odd = type(odd_name, (Exception,), {})("text")
    assert exception_class_name(odd) == "Exception"
