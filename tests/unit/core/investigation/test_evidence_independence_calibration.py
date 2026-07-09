"""Calibration pins for the §7.1 evidence-independence knobs (INV-29).

One executable home for the two knobs — `_EVIDENCE_MIRROR_JACCARD` (when two
causal-evidence contents are ONE observation) and the connected-components
count — mirroring the pattern `test_restatement_guard_calibration.py` set for
INV-27. Each pin documents a decided behavior class:

- TRUE POSITIVES: near-verbatim re-records collapse.
- KNOWN ESCAPES (accepted limit): a paraphrase re-record of one datum with
  disjoint vocabulary reads as independent — the bar is lexical, one layer.
- KNOWN FALSE COLLAPSES (accepted limit, conservative direction): terse
  scaffold-dominated summaries about the same component can collapse; the
  root holds at INCONCLUSIVE (never a wrong validation) and the context
  annotation steers the model to a genuinely different observation.
- Boundary and structural pins: just-under-threshold pairs stay independent;
  the count is order-invariant (connected components, not greedy leaders);
  unjudgeable rows count ZERO.

If a knob value changes, these pins say exactly which behavior classes moved.
"""

import pytest

from faultmaven.core.investigation.causal_graph import (
    _EVIDENCE_MIRROR_JACCARD,
    _content_tokens,
    _independent_causal_support_count,
    _mutual_mirror,
)

pytestmark = pytest.mark.unit


def _count(*summaries: str) -> int:
    tokens_by_id = {f"ev_{i}": _content_tokens(s) for i, s in enumerate(summaries)}
    return _independent_causal_support_count(list(tokens_by_id), tokens_by_id)


def _mirrors(a: str, b: str) -> bool:
    return _mutual_mirror(
        _content_tokens(a), _content_tokens(b), _EVIDENCE_MIRROR_JACCARD
    )


# ---------------------------------------------------------------------------
# True positives — re-records of one datum collapse
# ---------------------------------------------------------------------------


def test_verbatim_rerecord_collapses():
    assert (
        _count(
            "config diff shows pool max_size dropped from 100 to 5 at deploy",
            "config diff shows pool max_size dropped from 100 to 5 at deploy window",
        )
        == 1
    )


def test_light_rewording_of_same_log_line_collapses():
    assert (
        _count(
            "nginx error log shows upstream timed out while connecting to backend",
            "the nginx error log shows upstream timed out connecting to the backend pool",
        )
        == 1
    )


# ---------------------------------------------------------------------------
# Known escapes (accepted limit — lexical bar, one layer of the #656 defense)
# ---------------------------------------------------------------------------


def test_known_escape_full_paraphrase_reads_independent():
    # The SAME config-change datum re-phrased with disjoint vocabulary escapes
    # the collapse (J ~= 0.13). Accepted: semantic dedup is not this layer.
    assert (
        _count(
            "config diff shows pool max_size dropped from 100 to 5 at deploy",
            "deployment changelog: connection pool maximum size reduced from "
            "100 to 5 during the release",
        )
        == 2
    )


def test_known_escape_unit_shifted_metric_reads_independent():
    assert (
        _count(
            "grafana shows heap usage at 95 percent of the 2GB limit",
            "heap consumption reached 1.9GB out of a 2GB maximum per the dashboard",
        )
        == 2
    )


# ---------------------------------------------------------------------------
# Known false collapses (accepted limit — conservative direction: holds at
# INCONCLUSIVE, never mints; the annotation steers to a different observation)
# ---------------------------------------------------------------------------


def test_known_false_collapse_terse_scaffold_pair():
    # Two genuinely distinct findings from the same command share so much
    # scaffold vocabulary they collapse (J ~= 0.71). Documented limit.
    assert (
        _count(
            "kubectl describe pod payment-api shows OOMKilled",
            "kubectl describe pod payment-api shows CrashLoopBackOff",
        )
        == 1
    )


def test_richer_summaries_from_same_component_stay_independent():
    # The mitigation for the scaffold collapse: a second observation with its
    # own substance (values, mechanism) does not mirror.
    assert (
        _count(
            "kubectl describe pod payment-api shows OOMKilled at 512Mi limit",
            "kubectl top pod payment-api shows memory climbing 50Mi per minute "
            "toward the container limit",
        )
        == 2
    )


# ---------------------------------------------------------------------------
# Boundary + structural pins
# ---------------------------------------------------------------------------


def test_just_under_threshold_pair_stays_independent():
    # J ~= 0.55 < 0.6 — nginx upstream timeout vs connection refused.
    a = "nginx log shows upstream timed out while reading response header"
    b = "nginx log shows upstream connection refused while connecting"
    assert not _mirrors(a, b)
    assert _count(a, b) == 2


def test_count_is_order_invariant_across_a_bridge():
    # A~B and B~C but not A~C (a "bridge" restatement). Greedy leader
    # clustering would count 1 or 2 depending on iteration order; connected
    # components always collapse the chain to ONE observation — and DB link
    # reload order is not stable, so order-dependence would mean
    # nondeterministic validation. Controlled token construction: A={w1..w10},
    # B={w1..w8,w11,w12} (J=0.667), C={w1..w6,w11..w14} (J(B,C)=0.667,
    # J(A,C)=0.429).
    w = [f"tok{i}x" for i in range(1, 15)]  # distinct, stem-stable tokens
    a = " ".join(w[0:10])
    b = " ".join(w[0:8] + w[10:12])
    c = " ".join(w[0:6] + w[10:14])
    ta, tb, tc = _content_tokens(a), _content_tokens(b), _content_tokens(c)
    assert _mutual_mirror(ta, tb, _EVIDENCE_MIRROR_JACCARD)
    assert _mutual_mirror(tb, tc, _EVIDENCE_MIRROR_JACCARD)
    assert not _mutual_mirror(ta, tc, _EVIDENCE_MIRROR_JACCARD)
    for order in ([a, b, c], [b, a, c], [c, a, b], [a, c, b]):
        assert _count(*order) == 1, order


def test_unjudgeable_rows_count_zero():
    # All-stopword content tokenizes to nothing — it can never be the
    # decisive observation, alone or beside a real row.
    assert _count("it is as was") == 0
    assert _count("it is as was", "to be or not to be") == 0
    assert _count("config diff shows pool max_size dropped to 5", "it is as was") == 1
