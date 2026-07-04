"""Shared EXHAUSTED / work-gate thresholds — the single definition read by both
the ``ProgressMonitor`` exhaustion detector (``progress_monitor.py``) and the
verification-status join (``verification_status.py``).

This module has **no imports** on purpose: it is the neutral base both readers
depend on, so neither has to import the other merely to obtain these integers
(``progress_monitor`` is heavy; ``verification_status`` is a lean read — and
``progress_monitor`` may one day need to read the verification status it helps
compute, which would close a cycle if the constants lived in either reader).

The two readers measure the same *situation* but not every ``2`` here is the same
quantity, so they share constants ONLY on the dimensions they measure identically:

  - PROGRESS AXIS (has progress stalled?) — ``EXHAUSTION_MIN_TURNS`` /
    ``EXHAUSTION_STALL_THRESHOLD``. Read by ``_detect_exhaustion`` and
    ``verification_status.is_stalled``.
  - WORK GATE breadth — ``WORK_GATE_MIN_CATEGORIES`` / ``WORK_GATE_MIN_EVIDENCE``.
    Both readers apply these to the SAME counts (distinct categories over all
    hypotheses; total evidence items).

The hypothesis dimension is deliberately NOT shared — the two readers count
different things:

  - ``WORK_GATE_MIN_HYPOTHESES`` — the work gate's *generation depth*: the
    minimum number of hypotheses that must EXIST (any state). Used by
    ``verification_status.work_gate_passed`` only.
  - ``EXHAUSTION_MIN_REFUTED`` — the exhaustion detector's *elimination depth*:
    the minimum number of hypotheses that must be REFUTED/INCONCLUSIVE. Used by
    ``_detect_exhaustion`` only.

They happen to both be 2 today, but they measure different things; sharing one
constant would couple two independent tunables (raising the work-gate bar would
silently move the exhaustion bar). Keep them separate.
"""

EXHAUSTION_MIN_TURNS = 8
EXHAUSTION_STALL_THRESHOLD = 5
EXHAUSTION_MIN_REFUTED = 2
WORK_GATE_MIN_CATEGORIES = 2
WORK_GATE_MIN_HYPOTHESES = 2
WORK_GATE_MIN_EVIDENCE = 2
