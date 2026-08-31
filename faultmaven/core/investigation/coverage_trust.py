"""How much a coverage span's provenance is worth.

``coverage_start_ts`` / ``coverage_end_ts`` are consumed as fact in several
places. ``coverage_source`` says which pattern produced them (or
``caller_declared`` when a forwarding client supplied the instant). This module
is the single place that decides what each provenance licenses, so the prompt
and the symptom-currency machinery cannot drift into disagreeing about the same
row — the failure mode that motivated recording provenance at all.

Three states, not two:

``VOUCHED``   the instant may be stated as an observation time.
``INFERRED``  the instant is real to the minute but its YEAR was supplied by
              the parser (classic BSD syslog carries no year). Usable, with the
              uncertainty made explicit.
``unknown``   provenance was never recorded (rows predating the column), or a
              pattern nobody has classified. Not trusted.

Note ``epoch_s`` / ``epoch_ms`` are VOUCHED. The false positives they used to
produce — ``maxBytes: 2147483647`` read as 2038-01-19 — are prevented at the
source now (``_NO_BARE_EPOCH_TYPES`` in ``preprocessing_service``): the
bare-integer patterns no longer run on configs, source, screenshots or docs.
Distrusting them here instead would discard the real timestamps in
epoch-formatted logs, which are common and correct.
"""

from __future__ import annotations

from typing import Optional

__all__ = [
    "VOUCHED_COVERAGE_SOURCES",
    "INFERRED_COVERAGE_SOURCES",
    "is_vouched",
    "is_inferred",
    "may_be_stated",
]

VOUCHED_COVERAGE_SOURCES = frozenset(
    {
        "caller_declared",
        "iso8601",
        "iso8601_t",
        "healthapp",
        "syslog_bsd",
        "yymmdd",
        "yy_slash_mmdd",
        "epoch_s",
        "epoch_ms",
    }
)

INFERRED_COVERAGE_SOURCES = frozenset({"syslog_bsd_noyear"})


def is_vouched(source: Optional[str]) -> bool:
    """The instant may be stated plainly."""
    return source in VOUCHED_COVERAGE_SOURCES


def is_inferred(source: Optional[str]) -> bool:
    """The instant is usable but its year was invented by the parser."""
    return source in INFERRED_COVERAGE_SOURCES


def may_be_stated(source: Optional[str]) -> bool:
    """The instant may reach the model at all, plainly or with a marker."""
    return is_vouched(source) or is_inferred(source)
