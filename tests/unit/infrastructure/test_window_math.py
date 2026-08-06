"""The sliding window's client-facing arithmetic, asserted as a property.

Three enforcers derive ``reset_time`` and ``Retry-After`` from these two
functions. The formula used to be hand-expanded at each site, so the obligation
here is to sweep the space rather than pin one instance — a single example
passes just as happily against a copy that diverged everywhere else.
"""

import math

import pytest

from faultmaven.infrastructure.protection.window_math import (
    quota_frees_at,
    retry_after_seconds,
)

pytestmark = pytest.mark.unit

WINDOWS = (1, 5, 60, 300, 3600, 86400)
NOW = 1_700_000_000.0


@pytest.mark.parametrize("window", WINDOWS)
def test_an_empty_window_frees_one_full_window_from_now(window):
    """Nothing to age out means the entry that just went in is the oldest."""
    assert quota_frees_at(None, window, NOW) == NOW + window


@pytest.mark.parametrize("window", WINDOWS)
@pytest.mark.parametrize("age_fraction", (0.0, 0.01, 0.25, 0.5, 0.75, 0.99, 1.0))
def test_the_answer_is_the_oldest_entry_plus_one_window(window, age_fraction):
    """The honest value, untouched wherever the clocks agree."""
    oldest = NOW - window * age_fraction

    assert quota_frees_at(oldest, window, NOW) == pytest.approx(oldest + window)


@pytest.mark.parametrize("window", WINDOWS)
@pytest.mark.parametrize("age_fraction", (0.0, 0.01, 0.25, 0.5, 0.75, 0.99, 1.0))
def test_the_wait_never_exceeds_the_window_and_never_rounds_to_zero(
    window, age_fraction
):
    """The two bounds every honest answer sits between.

    Zero would read as "retry immediately", which is what a refused client must
    not do; more than a window is an answer a window of that width cannot
    produce.
    """
    frees_at = quota_frees_at(NOW - window * age_fraction, window, NOW)
    wait = retry_after_seconds(frees_at, NOW)

    assert 1 <= wait <= math.ceil(window), (wait, window, age_fraction)


@pytest.mark.parametrize("window", WINDOWS)
@pytest.mark.parametrize("skew", (0.001, 1.0, 30.0, 1_000.0, 10_000.0, 1e6))
def test_a_score_from_a_clock_running_ahead_is_clamped_to_one_window(window, skew):
    """The skew clamp, swept rather than sampled.

    Scores are wall-clock and shared across replicas, so a host whose clock runs
    ahead writes entries scored in *this* host's future. Unclamped, ``oldest +
    window`` exceeds one full window — an answer no sliding window of that width
    can honestly produce, and one that grows without bound as the skew does.
    """
    frees_at = quota_frees_at(NOW + skew, window, NOW)

    assert frees_at <= NOW + window, (frees_at, skew, window)
    assert retry_after_seconds(frees_at, NOW) <= math.ceil(window)


@pytest.mark.parametrize("window", WINDOWS)
def test_the_clamp_leaves_a_score_at_exactly_now_alone(window):
    """The boundary the clamp must not move: agreement is not skew."""
    assert quota_frees_at(NOW, window, NOW) == NOW + window


@pytest.mark.parametrize("window", WINDOWS)
def test_both_numbers_come_from_one_frees_at(window):
    """``reset_time`` and ``Retry-After`` cannot name different instants.

    They are two renderings of a single value, so the wait must always land
    within a second of the difference the timestamp implies.
    """
    for age in (0.0, window * 0.3, window * 0.97, window):
        frees_at = quota_frees_at(NOW - age, window, NOW)
        wait = retry_after_seconds(frees_at, NOW)

        assert 0 <= wait - (frees_at - NOW) < 1 or wait == 1, (wait, frees_at, age)
