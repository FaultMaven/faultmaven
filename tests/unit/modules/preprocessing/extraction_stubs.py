"""Tier 1 extractors for the preprocessing service's tests (#2637).

The service runs an extractor's class in a worker process, by name, so a test
double is a module-level class that takes no arguments, never a mock: a mock
cannot cross the process boundary. The worker imports this module, so it
imports the extractor protocol and the standard library, and no test
framework.

Every result carries ``MARKER`` with the stub's class name in its
``file_meta``, which reaches the service's ``structural_index``: a test that
asserted a mock's calls reads which stub's result it got instead.
"""

import logging
import re
import threading
import time

from faultmaven.modules.preprocessing.extractors.protocol import ExtractResult

logger = logging.getLogger(__name__)

#: The ``file_meta`` key naming the stub that produced a result.
MARKER = "extracted_by_stub"

#: The record a stub that runs on logs once it has entered ``extract``.
ENTERED = "the test extractor has entered extract"

#: Set by the deadline regression once it has asserted: the regression's stubs
#: work until it is set or their worker is killed. In a worker nothing sets it,
#: so only the deadline ends them, at any runner speed (#2696); on a thread,
#: where nothing can stop them, the test still releases them.
RELEASED = threading.Event()

#: Cubic backtracking: one ``sub`` over 500 characters is one C call of about
#: 50 ms that never drops the interpreter lock. Polynomial, not exponential,
#: over a constant: no input reaches it.
_BACKTRACKING = re.compile(r"a*a*b")


def marked(stub: type) -> str:
    """The text a result from ``stub`` carries in its structural index."""
    return f'"{MARKER}": "{stub.__name__}"'


class _Answers:
    """Answers ``FILE_EXTRACT``, marked with its class name."""

    strategy_name = "crime_scene"
    llm_calls_used = 0
    FILE_EXTRACT = ""

    def extract(self, content: str) -> ExtractResult:
        return ExtractResult(
            file_extract=self.FILE_EXTRACT,
            search_map="",
            file_meta={MARKER: type(self).__name__},
        )


class CrimeScene(_Answers):
    FILE_EXTRACT = (
        "=== Crime Scene ===\n"
        "Error cluster at line 42: NullPointerException\n"
        "Context: Connection pool exhausted"
    )


class NoErrorsLogs(_Answers):
    FILE_EXTRACT = "CRIME SCENE EXTRACTION: No errors detected - showing last 2 lines\n"


class HealthyMetrics(_Answers):
    strategy_name = "statistical"
    FILE_EXTRACT = (
        "=== METRICS ANALYSIS SUMMARY ===\n"
        "Analyzed 1 metric(s)\n"
        "\n"
        "--- COVERAGE METADATA ---\n"
        "Format: csv\n"
        "Total data points: 100\n"
    )


class DegenerateMetrics(_Answers):
    """Fails the metrics sanity check: a single data point."""

    strategy_name = "statistical"
    FILE_EXTRACT = "=== METRICS ANALYSIS SUMMARY ===\nTotal data points: 1\n"


class TextFindings(_Answers):
    strategy_name = "direct"
    FILE_EXTRACT = "=== ERROR MESSAGES ===\nsome content here\n"


class Empty(_Answers):
    """Fails every sanity check: no output."""

    strategy_name = "direct"


class EmptyConfig(_Answers):
    FILE_EXTRACT = "\n\n--- COVERAGE METADATA ---\nFormat: key-value\nTotal keys: 0\n"


class UnknownSource(_Answers):
    FILE_EXTRACT = (
        "=== SOURCE CODE ANALYSIS (Pattern-based) ===\n\nDetected Language: Unknown\n"
    )


class HoldsTheLockInC(_Answers):
    def extract(self, content: str) -> ExtractResult:
        logger.warning(ENTERED)
        while not RELEASED.is_set():
            _BACKTRACKING.sub("", "a" * 500)
        return super().extract(content)


class LoopsInPython(_Answers):
    def extract(self, content: str) -> ExtractResult:
        logger.warning(ENTERED)
        while not RELEASED.is_set():
            for _ in range(400_000):
                pass
        return super().extract(content)


class NeverReturns(_Answers):
    def extract(self, content: str) -> ExtractResult:
        logger.warning(ENTERED)
        while True:
            time.sleep(1)


class RunsOutOfMemory(_Answers):
    def extract(self, content: str) -> ExtractResult:
        bytearray(64 * 1024**3)
        return super().extract(content)


class AnswersTooMuch(_Answers):
    FILE_EXTRACT = "x" * 200_000


class AnswersAString(_Answers):
    """Breaks the protocol: ``extract`` returns ``str``."""

    def extract(self, content: str) -> str:  # type: ignore[override]
        return self.FILE_EXTRACT


if __name__ == "__main__":
    # Run as a worker module (``-m``), this one never reports ready: the start
    # bound's test.
    while True:
        time.sleep(1)
