"""A model send whose content the redaction module did not mint fails its test.

#1998 P2b (Fable AMEND A4). Until P3 turns refusal on, the model boundary only
REPORTS a raw send: one log line, one metric and one ``RawModelContentWarning``,
and then the call goes ahead. In the suite that report has to be a failure,
or a new raw send would be green until P3 refused it in production. Two
layers make it one:

1. ``filterwarnings = error::...RawModelContentWarning`` in every pytest
   config (``pytest.ini``, ``tests/integration/pytest.ini``): the warning is
   raised at the send.
2. :func:`recording`, wrapped around every test by the autouse fixture
   ``_raw_model_content_fails_its_test`` in ``tests/conftest.py`` (re-exported
   by ``tests/integration/conftest.py``): it records the warning and fails
   the test at teardown.

Layer 1 alone is not enough. A raised warning is an ``Exception``, and the
send sites that fail open (the out-of-band triage, the intent resolver,
tier-2 deep analysis, the document triage) catch ``Exception`` and carry on,
so the test passes. A record is not an exception: no ``except`` in the code
under test catches it.

What the recorder covers, and only that: a test's setup, call and teardown,
and the function-scoped fixtures set up inside them, on whatever thread warns
while they run. Inside that window it decides: its ``always`` filter comes
after pytest applied the ini filters, so the warning is recorded, not raised,
and the code under test runs exactly as it would in production in warn mode.
Outside it, only the ini line applies, and only to a send nothing swallows: a
fixture wider than one test (set up before the recorder, torn down after it),
a thread that outlives its test, collection (a module that sends when
imported), and any run without the recorder. None sends raw content today
(the whole-suite count is 0 there); #1998 P3's refusal takes over from both
layers.

What it does not survive is code that changes the warnings machinery itself:
a filter that ignores the category (``simplefilter("ignore")``,
``filterwarnings("ignore", category=UserWarning)``), ``resetwarnings``,
``catch_warnings`` (whose ``record=True`` takes the warning first), or
``logging.captureWarnings(True)`` (which routes it to logging). Rule W of
``tests/unit/architecture/test_model_dispatch_lexical_1998.py`` bans those
spellings in ``faultmaven/``, so none happens by accident. A deliberate
category-scoped ignore of ``RawModelContentWarning``, a computed spelling, or
a send from a subprocess (its own interpreter, with no recorder) is out of
scope, as in #1998's D1: the boundary prevents accidental raw sends.

A test that drives a provider, the registry or the router mints its input
with the test minter (``tests/model_content.py``: ``vetted``,
``vetted_messages``, ``vetted_kwargs``). A test that hands the boundary
something it reports ON PURPOSE says so: with ``pytest.warns`` (or its own
``warnings.catch_warnings(record=True)``), which takes the warning before the
recorder sees it and also asserts it was reported; or, where the report is not
what the test asserts, with ``@pytest.mark.raw_model_content_expected``
(registered in both pytest configs) on the test itself, whose warnings are
recorded and not failed on. A module's ``pytestmark`` or a class decorator
does not opt a test out (:func:`marked_expected`).

Whatever the recorder recorded, of any category, is handed back to pytest's
own recording when the test ends, as ``pytest.warns`` re-emits what it did not
match: the warnings summary, and anything that reads it, is unchanged.
"""

from __future__ import annotations

import contextlib
import os
import re
import warnings
from typing import Iterator, List

import pytest

from faultmaven.infrastructure.llm.dispatch import RawModelContentWarning

#: The marker a test that sends raw content on purpose carries.
MARKER = "raw_model_content_expected"

#: The repository root, to name a send site by its repo-relative path.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + os.sep


#: How the dispatch's warning begins; the rest is the violation summary
#: (``dispatch._summary``): ``"<argument> <kind>"`` pairs joined by ``"; "``,
#: sorted, an unknown keyword's names in parentheses.
REPORT_PREFIX = "model-bound content not minted by the redaction module: "


def marked_expected(node: pytest.Item) -> bool:
    """Whether THIS test carries :data:`MARKER`: its own decorator, or a
    ``pytest.param(..., marks=...)`` of its own case. A module's ``pytestmark``
    or a class decorator does not count — an opt-out is made one test at a
    time, never for every test that happens to share a file or a class."""
    return any(mark.name == MARKER for mark in node.own_markers)


def report_pattern(summary: str) -> str:
    """A ``pytest.warns(match=...)`` pattern for EXACTLY this report.

    Anchored at both ends, so an expected report (``kwargs unknown_kwarg
    (case_id)``) cannot also absorb an unexpected one carried in the same
    warning (``...; prompt raw``).
    """
    return "^" + re.escape(REPORT_PREFIX + summary) + "$"


def _site(warning: warnings.WarningMessage) -> str:
    filename = str(warning.filename)
    if filename.startswith(_ROOT):
        filename = filename[len(_ROOT) :]
    return f"{filename}:{warning.lineno}: {warning.message}"


def _hand_back(recorded: List[warnings.WarningMessage]) -> None:
    """Re-emit what the recorder took, to whatever records outside it.

    Under the filters outside, as ``pytest.warns`` does for what it did not
    match. A ``RawModelContentWarning`` is re-emitted under a local ``always``
    for its category only, so the ini's ``error`` does not raise it a second
    time here, in the recorder's own exit.
    """
    for warning in recorded:
        with warnings.catch_warnings():
            if issubclass(warning.category, RawModelContentWarning):
                warnings.simplefilter("always", RawModelContentWarning)
            warnings.warn_explicit(
                message=warning.message,
                category=warning.category,
                filename=warning.filename,
                lineno=warning.lineno,
                source=warning.source,
            )


@contextlib.contextmanager
def recording(*, expected: bool, test: str) -> Iterator[None]:
    """Record every ``RawModelContentWarning`` raised inside; fail on any.

    ``expected``: the test carries :data:`MARKER`, so its raw sends are
    recorded and handed back, never failed on. ``test`` names the test in the
    failure.

    Raises:
        pytest.fail.Exception: a raw send was recorded and not expected.
    """
    with warnings.catch_warnings(record=True) as recorded:
        # This category only: a blanket ``always`` would also override every
        # other filter the configs set (the ini's ``error:Duplicate Operation
        # ID``, the ignores) for the whole test.
        warnings.simplefilter("always", RawModelContentWarning)
        yield
    raw = [w for w in recorded if issubclass(w.category, RawModelContentWarning)]
    _hand_back(recorded)
    if raw and not expected:
        pytest.fail(
            f"{test} sent model-bound content the redaction module did not mint "
            f"({len(raw)} send(s)), so it would be refused once #1998 P3 turns "
            "refusal on. Mint it at the send site "
            "(faultmaven/infrastructure/security/model_content.py); a test that "
            f"sends raw content on purpose carries @pytest.mark.{MARKER} "
            "(tests/raw_model_content_guard.py):\n  "
            + "\n  ".join(_site(w) for w in raw),
            pytrace=False,
        )
