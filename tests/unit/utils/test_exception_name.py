"""``exception_class_name``: the class name, or ``Exception`` for an odd one;
``exception_class_chain``: those of an exception's cause chain (#2587);
``exception_frames``: the frames a traceback would show, without a message (#2844),
into a group's members as far as ``traceback`` goes (#2888);
``exception_log_name``: the chain with each first-party ``error_code`` (#2889)."""

import subprocess
from enum import Enum

import pytest

from faultmaven.core.investigation.milestone_engine.errors import (
    MilestoneEngineError,
)
from faultmaven.exceptions import (
    CircuitBreakerError,
    ConflictError,
    LLMException,
    ServiceException,
)
from faultmaven.utils.exception_name import (
    exception_class_chain,
    exception_class_name,
    exception_frames,
    exception_log_name,
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


def test_a_group_shows_its_first_members_and_counts_the_rest():
    """As ``traceback`` caps a group: 15 members shown, the rest counted (#2888)."""
    group = ExceptionGroup("g", [ValueError(str(i)) for i in range(40)])
    headers = exception_frames(group).splitlines()
    assert headers == [
        "ExceptionGroup:",
        *(f"[{i}] ValueError:" for i in range(1, 16)),
        "and 25 more exceptions",
    ]


def test_nested_groups_stop_at_ten_deep():
    error: BaseException = ValueError("leaf")
    for _ in range(12):
        error = ExceptionGroup("g", [error])
    headers = exception_frames(error).splitlines()
    assert len(headers) == 12
    assert headers[-1] == "[1] " * 10 + "and 1 more exceptions"


def test_a_member_shown_already_is_listed_as_shown_above():
    shared = _raised(KeyError("a"))
    try:
        raise ExceptionGroup("g1", [shared])
    except ExceptionGroup as first:
        later = ExceptionGroup("g2", [shared, ValueError("b")])
        later.__cause__ = first
    lines = exception_frames(later).splitlines()
    headers = [line for line in lines if not line.startswith(" ")]
    assert headers == [
        "ExceptionGroup:",
        "[1] KeyError:",
        "[2] ValueError:",
        "ExceptionGroup:",
        "[1] KeyError (shown above)",
    ]


# ── exception_log_name (#2889) ────────────────────────────────────────────

MARKER = "zqxmarker2889"


class ThirdPartyError(Exception):
    """A vendor's exception: its ``error_code`` is the vendor's text."""

    def __init__(self):
        super().__init__(MARKER)
        self.error_code = f"sk_live_{MARKER}"


def _first_party(name: str = "FirstPartyError", **attributes) -> type:
    cls = type(name, (Exception,), attributes)
    cls.__module__ = "faultmaven.modules.example"
    return cls


class _Code(str, Enum):
    FILE_TOO_LARGE = "FILE_TOO_LARGE"


class _Formatted(str):
    def __format__(self, spec):
        return f"{MARKER} any text"


class _RaisingCode(Exception):
    @property
    def error_code(self):
        raise RuntimeError(MARKER)


_RaisingCode.__module__ = "faultmaven.modules.example"


def _with_code(code, module="faultmaven.modules.example"):
    cls = _first_party()
    cls.__module__ = module
    error = cls(MARKER)
    error.error_code = code
    return error


@pytest.mark.parametrize(
    "error, named",
    [
        (ValueError(f"case text {MARKER}"), "ValueError"),
        (
            _raised(RuntimeError(MARKER), cause=KeyError(MARKER)),
            "RuntimeError <- KeyError",
        ),
        (_raised(ValueError(MARKER), context=KeyError(MARKER)), "ValueError"),
        (ThirdPartyError(), "ThirdPartyError"),
        (
            CircuitBreakerError(MARKER, error_code=f"{MARKER} text"),
            "CircuitBreakerError",
        ),
        (
            CircuitBreakerError(MARKER, error_code=ThirdPartyError().error_code),
            "CircuitBreakerError",
        ),
        (
            MilestoneEngineError(MARKER, error_code=ThirdPartyError().error_code),
            "MilestoneEngineError",
        ),
        (type("has space", (Exception,), {})(MARKER), "Exception"),
        (subprocess.CalledProcessError(1, ["x"], MARKER, MARKER), "CalledProcessError"),
        (_with_code(_Code.FILE_TOO_LARGE), "FirstPartyError"),
        (_with_code(_Formatted("OK_CODE")), "FirstPartyError"),
        (_with_code("CODE\n"), "FirstPartyError"),
        (_with_code("A" * 65), "FirstPartyError"),
        (_with_code(401), "FirstPartyError"),
        (
            _with_code("CLOUD_CODE", module="faultmaven_cloud.billing"),
            "FirstPartyError",
        ),
        (_with_code("CODE", module=None), "FirstPartyError"),
        (_RaisingCode(MARKER), "_RaisingCode"),
    ],
)
def test_a_log_name_holds_no_text_and_no_unchecked_code(error, named):
    assert exception_log_name(error) == named


@pytest.mark.parametrize(
    "error, named",
    [
        (LLMException(MARKER, status_code=401), "LLMException[PROVIDER_AUTH_FAILED]"),
        (
            _raised(
                ServiceException(MARKER), cause=LLMException(MARKER, status_code=401)
            ),
            "ServiceException <- LLMException[PROVIDER_AUTH_FAILED]",
        ),
        (
            ConflictError(MARKER, error_code="TEAM_NAME_TAKEN"),
            "ConflictError[TEAM_NAME_TAKEN]",
        ),
        (ServiceException(MARKER), "ServiceException"),
        (_with_code("A" * 64), "FirstPartyError[" + "A" * 64 + "]"),
    ],
)
def test_a_first_party_code_is_named_beside_its_class(error, named):
    assert exception_log_name(error) == named
