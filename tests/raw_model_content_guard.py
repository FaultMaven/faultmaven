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
so the test passes. A record cannot be caught by the code under test.

Inside a test, layer 2 decides: its ``always`` filter comes after pytest
applied the ini filters, so the warning is recorded, not raised, and the code
under test runs exactly as it would in production in warn mode. The ini line
decides wherever the recorder does not reach: at collection (a module that
sends when imported), in a fixture wider than one test (set up before the
recorder, torn down after it), and in any run without the recorder.

A test that drives a provider, the registry or the router mints its input
with the test minter (``tests/model_content.py``: ``vetted``,
``vetted_messages``, ``vetted_kwargs``). A test that hands the boundary
something it reports ON PURPOSE says so: with ``pytest.warns`` (or its own
``warnings.catch_warnings(record=True)``), which takes the warning before the
recorder sees it and also asserts it was reported; or, where the report is not
what the test asserts, with ``@pytest.mark.raw_model_content_expected``
(registered in both pytest configs), whose warnings are recorded and not
failed on.

Whatever the recorder recorded, of any category, is handed back to pytest's
own recording when the test ends, as ``pytest.warns`` re-emits what it did not
match: the warnings summary, and anything that reads it, is unchanged.
"""

from __future__ import annotations

import contextlib
import os
import warnings
from typing import Iterator, List

import pytest

from faultmaven.infrastructure.llm.dispatch import RawModelContentWarning

#: The marker a test that sends raw content on purpose carries.
MARKER = "raw_model_content_expected"

#: The repository root, to name a send site by its repo-relative path.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + os.sep


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
