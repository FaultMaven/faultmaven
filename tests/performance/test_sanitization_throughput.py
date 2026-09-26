"""Throughput of ``DataSanitizer.sanitize`` on log-shaped content.

Moved here from ``tests/infrastructure/test_security_processing.py`` (#1579).
That module asserted twelve raw wall-clock numbers in both required gates:
five correctness tests each timed ONE call against 0.5-2 s, two load tests
asserted ``lines_per_second > 100`` and ``documents_per_second > 10``, and a
consistency test bounded the spread between its fastest and slowest call.
The sanitizer does 1000 log lines in ~20 ms, so those budgets sat at a
fraction of a percent of their thresholds: they could not notice a 100x
regression, and they could still go red on a runner that stalled for a
second. The correctness assertions stayed where they were; the two numbers
that describe this code's throughput are measured here, against a
calibrated budget in ``budgets.py``.

‼ Like everything in this directory, every comparison goes through
``assert_throughput_at_least`` — never a literal (#1557).
"""

import time

import pytest

from faultmaven.infrastructure.security.redaction import DataSanitizer
from tests.wallclock import assert_throughput_at_least

from .budgets import SANITIZE_DOCUMENT_BATCH, SANITIZE_LARGE_DOCUMENT

#: Timed repetitions per statistic; the FASTEST is the statistic. Every
#: source of noise is additive, so the minimum is the least contaminated.
SAMPLES = 5

_SENSITIVE_PATTERNS = [
    "user{i}@company.com",
    "192.168.1.{i}",
    "+1-555-{i:03d}-{i:04d}",
    "AWS_KEY_ID=AKIA{i}EXAMPLE{i}",
    "postgresql://user{i}:pass{i}@db{i}.local:5432/app",
]


def _large_document() -> list:
    """1000 log lines, one in ten carrying something to redact.

    The document ``test_real_large_document_sanitization`` sanitizes, line
    for line.
    """
    lines = []
    for i in range(1000):
        line = f"2025-01-15 {i:02d}:30:{i % 60:02d} [INFO] Processing request {i} "
        if i % 10 == 0:
            pattern = _SENSITIVE_PATTERNS[i % len(_SENSITIVE_PATTERNS)]
            line += f"for {pattern.format(i=i % 100)}"
        else:
            line += f"for user-{i}"
        lines.append(line)
    return lines


def _document_batch() -> list:
    """The 50 small documents ``test_real_concurrent_sanitization_load`` uses."""
    return [f"""
            Document {i}:
            User: test{i}@example.com
            IP: 192.168.{i % 255}.{(i * 7) % 255}
            API_KEY: sk-test{i:06d}abcdef
            Database: mysql://user{i}:pass{i}@server{i}.local/db
            Phone: +1-555-{i:03d}-{(i * 13) % 10000:04d}
            """ for i in range(50)]


def _fastest_seconds(work) -> float:
    work()  # warm-up: compiled patterns and first-call costs are nobody's budget
    best = float("inf")
    for _ in range(SAMPLES):
        started = time.perf_counter()
        work()
        best = min(best, time.perf_counter() - started)
    return best


@pytest.mark.performance
def test_large_document_sanitization_throughput():
    """Lines per second through one 1000-line document.

    The product target is the ``lines_per_second > 100`` the load test
    asserted. Its ``processing_time < 10.0`` beside it was the same
    constraint — 1000 lines in 10 s is 100 lines/s — so it is one budget,
    not two.
    """
    sanitizer = DataSanitizer()
    lines = _large_document()
    document = "\n".join(lines)

    seconds = _fastest_seconds(lambda: sanitizer.sanitize(document))
    lines_per_second = len(lines) / seconds

    print(f"\nSanitize 1000-line document: {lines_per_second:.0f} lines/s")
    assert_throughput_at_least(
        lines_per_second, SANITIZE_LARGE_DOCUMENT, "DataSanitizer lines/s"
    )


@pytest.mark.performance
def test_document_batch_sanitization_throughput():
    """Documents per second through a batch of 50 small documents.

    A separate measurement from the one above because the shape differs:
    fifty short calls pay the per-call cost fifty times, where one long call
    pays it once. The product target is the ``documents_per_second > 10``
    the load test asserted.

    Sequential on purpose. The old test gathered 50 coroutines around a
    SYNCHRONOUS ``sanitize`` call, so nothing overlapped, and its per-document
    "processing time" included the wait for every document scheduled ahead
    of it — which is why its ``avg_processing_time < 1.0`` was not a second
    measurement but a rescaling of the total.
    """
    sanitizer = DataSanitizer()
    documents = _document_batch()

    seconds = _fastest_seconds(lambda: [sanitizer.sanitize(d) for d in documents])
    documents_per_second = len(documents) / seconds

    print(f"\nSanitize 50-document batch: {documents_per_second:.0f} documents/s")
    assert_throughput_at_least(
        documents_per_second, SANITIZE_DOCUMENT_BATCH, "DataSanitizer documents/s"
    )
