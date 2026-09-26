"""Latency of the Tier-1/Tier-2 extraction paths that carry a product target.

Three timings moved here from unit tests (#1579), each of which compared a
raw wall-clock number against a literal in both required gates:

* ``SearchFileTool._extract_file_vocabulary`` on ~1 MB of logs, "< 500 ms"
  (``tests/unit/modules/agent/tools/test_search_file_tool.py``);
* ``extract_timestamp``, "under 1 ms" per call
  (``tests/unit/modules/preprocessing/extractors/test_coverage_utils.py``);
* ``LogsAndErrorsExtractor`` on a 64 KB hostile line, inside
  ``TIER1_TIMEOUT_SECONDS`` — past which the pipeline throws the whole
  file's extraction away
  (``tests/unit/modules/preprocessing/extractors/test_sshd_auth_anchoring.py``).

Each number is a product target, so it stays: as a row's
``product_target`` in ``budgets.py``, asserted raw by the
``FM_BENCHMARK_ABSOLUTE`` nightly, while a pull request asserts a regression
anchor 2-3x measured cost through ``assert_latency_within``.

The third is a latency budget rather than a growth check for a measured
reason: the extractor is NOT linear on two of these shapes. ``tag-chain``
and ``sshd-word-chain`` cost 3.5-3.9x per doubling between 64 KB and 128 KB
on the development box (``sshd-word-chain``: 0.70 s at 64 KB, 2.58 s at
128 KB), so a growth bound would either fail today or be set loose enough
to hide it — filed as #1700. The sshd READER alone is linear and is checked
as such next to the correctness test.

‼ Every comparison goes through ``assert_latency_within`` (#1557).
"""

import time

import pytest

from faultmaven.modules.agent.tools.search_file_tool import SearchFileTool
from faultmaven.modules.preprocessing.extractors.logs_extractor import (
    LogsAndErrorsExtractor,
)
from faultmaven.modules.preprocessing.extractors.utils import extract_timestamp
from tests.unit.modules.preprocessing.extractors.test_sshd_auth_anchoring import (
    ADVERSARIAL_LINES,
    BSD,
    SRC,
)
from tests.wallclock import assert_latency_within

from .budgets import (
    ADVERSARIAL_LINE_EXTRACTION,
    TIMESTAMP_EXTRACTION,
    VOCABULARY_EXTRACTION,
)


def _fastest_seconds(work, samples: int) -> float:
    best = float("inf")
    for _ in range(samples):
        started = time.perf_counter()
        work()
        best = min(best, time.perf_counter() - started)
    return best


def _megabyte_of_logs() -> str:
    """~1.3 MB of log-like content with tokens in the 2-10 frequency range."""
    services = [
        "auth-service",
        "payment-gateway",
        "order-processor",
        "inventory-manager",
        "notification-hub",
        "cache-layer",
    ]
    errors = [
        "ConnectionTimeout",
        "NullPointerException",
        "OutOfMemoryError",
        "SocketException",
        "DatabaseFailure",
        "AuthError",
    ]
    lines = []
    for i in range(20000):
        svc = services[i % len(services)]
        err = errors[i % len(errors)] if i % 200 == 0 else ""
        lines.append(
            f"2024-01-15 10:30:{i % 60:02d} INFO {svc} request "
            f"id={i} from 192.168.1.{i % 256} {err}"
        )
    return "\n".join(lines)


@pytest.mark.performance
def test_vocabulary_extraction_on_a_megabyte_of_logs():
    """Vocabulary extraction on ~1 MB; the product target is 500 ms."""
    tool = SearchFileTool(storage_service=None, context_lines=5, max_results=3)
    content = _megabyte_of_logs()
    tool._extract_file_vocabulary(content)  # warm-up

    vocab = {}

    def extract():
        vocab.update(tool._extract_file_vocabulary(content))

    seconds = _fastest_seconds(extract, samples=3)

    print(f"\nVocabulary extraction, {len(content)} chars: {seconds * 1000:.1f}ms")
    assert vocab["patterns"], "the vocabulary came back empty"
    assert_latency_within(seconds, VOCABULARY_EXTRACTION, "vocabulary extraction")


@pytest.mark.performance
def test_timestamp_extraction_per_line():
    """``extract_timestamp`` per call; the product target is 1 ms."""
    line = "2024-03-15T14:30:45.123Z ERROR something broke"
    calls = 1000
    assert extract_timestamp(line) is not None  # warm-up, and a live path

    def extract_many():
        for _ in range(calls):
            extract_timestamp(line)

    per_call = _fastest_seconds(extract_many, samples=5) / calls

    print(f"\nextract_timestamp: {per_call * 1e6:.2f}us per call")
    assert_latency_within(per_call, TIMESTAMP_EXTRACTION, "extract_timestamp call")


@pytest.mark.performance
@pytest.mark.parametrize("name", sorted(ADVERSARIAL_LINES))
def test_an_adversarial_line_extracts_inside_the_tier1_timeout(name):
    """A 64 KB hostile line beside a genuine one, through the whole extractor.

    The product target is ``TIER1_TIMEOUT_SECONDS`` itself: past it the
    pipeline replaces the file's extraction with a text preview, which is
    what a crafted line is trying to cause.
    """
    genuine = BSD + f"Failed password for root from {SRC} port 22 ssh2"
    body = genuine + "\n" + ADVERSARIAL_LINES[name] + "\n"
    extractor = LogsAndErrorsExtractor()
    extractor.extract(genuine + "\n")  # warm-up on the cheap line only

    seconds = _fastest_seconds(lambda: extractor.extract(body), samples=2)

    print(f"\nExtraction beside 64 KB {name}: {seconds * 1000:.1f}ms")
    assert_latency_within(
        seconds, ADVERSARIAL_LINE_EXTRACTION, f"extraction beside {name}"
    )
