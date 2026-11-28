"""Presidio's spans are applied in one pass, and no character of any span
survives (#3116 B3).

``main`` spliced each span into the text right to left, one copy of the text
per entity: quadratic in the entity count (21.8 s for 80,000 e-mail-shaped
spans over 560 KB). Its offsets went stale once spans overlapped: a span
nested in a longer one, whose placeholder is longer than its text, moved the
outer span's end, and the outer value's tail survived. Now the spans are
sorted once and joined; overlapping spans are one region, typed by its first
span (the longest at an equal start), its registry value the region's text.
Non-overlapping spans answer as ``main`` did, registry order included.
"""

from __future__ import annotations

import random
from unittest.mock import MagicMock

import pytest

from faultmaven.infrastructure.security.redaction import (
    DataSanitizer,
    RedactionUnavailableError,
)
from tests.wallclock import assert_linear_growth

pytestmark = [pytest.mark.unit]


def _sanitizer(fail_open: bool = True) -> DataSanitizer:
    settings = MagicMock()
    settings.protection.entities_to_protect = ["EMAIL_ADDRESS", "PERSON"]
    settings.protection.min_score_threshold = 0.5
    settings.protection.presidio_analyzer_url = "http://fake:8080"
    settings.protection.presidio_anonymizer_url = "http://fake:8081"
    settings.protection.sanitize_pii = True
    settings.protection.fail_open = fail_open
    settings.protection.pseudonym_key = "test-pseudonym-key"
    settings.server.skip_service_checks = True
    sanitizer = DataSanitizer(settings=settings)
    sanitizer.analyzer_available = True
    return sanitizer


def _apply(sanitizer, text, spans, registry=None):
    sanitizer.call_external_sync = lambda **_kwargs: spans
    registry = {} if registry is None else registry
    return sanitizer._apply_presidio(text, registry), registry


def _main_splice(sanitizer, text, analyzer_results, entity_registry):
    """``main``'s step 2 at 92358b959, verbatim but for its names: the oracle
    for non-overlapping spans (placeholders are keyed, #971, so the oracle is
    the code, not stored strings)."""
    sorted_results = sorted(analyzer_results, key=lambda r: r["start"], reverse=True)
    result = text
    for entity in sorted_results:
        start = entity["start"]
        end = entity["end"]
        entity_type = entity["entity_type"]
        original_value = text[start:end]
        if entity_type not in entity_registry:
            entity_registry[entity_type] = {}
        type_map = entity_registry[entity_type]
        if original_value not in type_map:
            type_map[original_value] = sanitizer._hashed_placeholder(
                entity_type, original_value
            )
        result = result[:start] + type_map[original_value] + result[end:]
    return result


def _span(start, end, entity_type="PERSON"):
    return {"start": start, "end": end, "entity_type": entity_type, "score": 0.9}


def _registry_items(registry):
    return [(kind, list(values.items())) for kind, values in registry.items()]


# --- Non-overlapping spans: as on main ---------------------------------------

WORDS = ["alice", "bob", "10.0.0.1", "a@b.io", "x", "zz", " ", ", "]
TYPES = ["PERSON", "IP_ADDRESS", "EMAIL_ADDRESS"]


def _disjoint_case(rnd):
    """Repeated values (a small vocabulary), adjacent spans, spans at both
    ends, several types, in the analyzer's arbitrary order."""
    text = "".join(rnd.choice(WORDS) for _ in range(rnd.randrange(1, 40)))
    cuts = sorted(rnd.sample(range(len(text) + 1), min(len(text) + 1, 12)))
    spans = [
        _span(a, b, rnd.choice(TYPES))
        for a, b in zip(cuts, cuts[1:])
        if a < b and rnd.random() < 0.6
    ]
    rnd.shuffle(spans)
    return text, spans


def test_non_overlapping_spans_answer_as_on_main():
    rnd = random.Random(3116)
    sanitizer = _sanitizer()
    for _ in range(500):
        text, spans = _disjoint_case(rnd)
        seeded = {"PERSON": {"bob": sanitizer._hashed_placeholder("PERSON", "bob")}}
        for start in ({}, seeded):
            main_registry = {k: dict(v) for k, v in start.items()}
            expected = _main_splice(sanitizer, text, spans, main_registry)
            got, registry = _apply(
                sanitizer, text, spans, {k: dict(v) for k, v in start.items()}
            )
            assert got == expected, (text, spans)
            assert _registry_items(registry) == _registry_items(main_registry)


# --- Overlapping spans: no character survives --------------------------------

#: Every character distinct, and none in a placeholder, so a survivor is seen.
TEXT = "".join(chr(0x4E00 + i) for i in range(60))

OVERLAPS = {
    "nested": [_span(5, 40, "EMAIL_ADDRESS"), _span(10, 15)],
    "partial": [_span(5, 20), _span(15, 40, "LOCATION")],
    "duplicate": [_span(5, 20), _span(30, 35), _span(5, 20)],
    "equal start": [_span(5, 12), _span(5, 30, "EMAIL_ADDRESS")],
    "chain": [_span(0, 10), _span(8, 20), _span(19, 60), _span(25, 26)],
}


@pytest.mark.parametrize("spans", OVERLAPS.values(), ids=OVERLAPS.keys())
def test_no_character_inside_any_span_survives(spans):
    got, _ = _apply(_sanitizer(), TEXT, spans)

    covered = {i for s in spans for i in range(s["start"], s["end"])}
    assert [c for c in got if c in TEXT] == [
        c for i, c in enumerate(TEXT) if i not in covered
    ]


@pytest.mark.parametrize(
    "spans, kind, region",
    [
        (OVERLAPS["nested"], "EMAIL_ADDRESS", (5, 40)),
        (OVERLAPS["partial"], "PERSON", (5, 40)),
        (OVERLAPS["equal start"], "EMAIL_ADDRESS", (5, 30)),
    ],
)
def test_an_overlap_is_one_region_typed_by_its_first_span(spans, kind, region):
    sanitizer = _sanitizer()
    got, registry = _apply(sanitizer, TEXT, spans)

    value = TEXT[region[0] : region[1]]
    placeholder = sanitizer._hashed_placeholder(kind, value)
    assert got == TEXT[: region[0]] + placeholder + TEXT[region[1] :]
    assert registry == {kind: {value: placeholder}}


def test_an_email_holding_a_name_is_one_placeholder():
    """The shape that left ``@example.com`` on ``main``."""
    text = "contact john.smith@example.com SECRETTAILDATA end"
    start, end = text.index("john"), text.index(" SECRET")
    sanitizer = _sanitizer()

    got, _ = _apply(
        sanitizer,
        text,
        [_span(start, end, "EMAIL_ADDRESS"), _span(start + 5, start + 10)],
    )

    email = sanitizer._hashed_placeholder("EMAIL_ADDRESS", text[start:end])
    assert got == f"contact {email} SECRETTAILDATA end"


# --- Malformed spans -----------------------------------------------------------


def test_an_offset_outside_the_text_is_clamped_and_an_empty_span_skipped():
    sanitizer = _sanitizer()
    text = "abcdefghij"

    got, registry = _apply(
        sanitizer, text, [_span(-3, 2), _span(5, 5), _span(8, 99, "X")]
    )

    assert got == (
        sanitizer._hashed_placeholder("PERSON", "ab")
        + "cdefgh"
        + sanitizer._hashed_placeholder("X", "ij")
    )
    assert set(registry) == {"PERSON", "X"}


MALFORMED = {
    "missing key": {"start": 3, "entity_type": "PERSON"},
    "bool offset": _span(True, 4),
    "bool end": _span(1, True),
    "float offset": _span(1.0, 4),
}


@pytest.mark.parametrize("bad", MALFORMED.values(), ids=MALFORMED.keys())
@pytest.mark.parametrize("fail_open", [True, False])
def test_a_malformed_span_is_the_analyzers_error(fail_open, bad):
    """A missing key, or an offset that is not an int. Fail-open passes the
    text on unredacted by Presidio; fail-closed refuses (the posture's
    existing behaviour)."""
    sanitizer = _sanitizer(fail_open=fail_open)
    spans = [_span(0, 2), bad]
    if fail_open:
        assert _apply(sanitizer, "abcdef", spans)[0] == "abcdef"
    else:
        with pytest.raises(RedactionUnavailableError):
            _apply(sanitizer, "abcdef", spans)


# --- Growth in the entity count -----------------------------------------------

#: One e-mail-shaped span every seven characters (the triage's probe).
UNIT = "a@b.cd "


def _dense(n: int):
    text = UNIT * n
    return text, [_span(7 * i, 7 * i + 6, "EMAIL_ADDRESS") for i in range(n)]


@pytest.mark.performance
def test_applying_the_spans_is_linear_in_their_count():
    sanitizer = _sanitizer()
    assert_linear_growth(
        lambda payload: _apply(sanitizer, *payload),
        _dense,
        small=250,
        undecided_repeats=2,
        label="_apply_presidio over n e-mail spans",
    )


@pytest.mark.performance
def test_mains_splice_is_super_linear_in_their_count():
    """What "fails on ``main``" means, through its splice."""
    sanitizer = _sanitizer()
    with pytest.raises(AssertionError, match="is the usual cause"):
        assert_linear_growth(
            lambda payload: _main_splice(sanitizer, *payload, {}),
            _dense,
            small=250,
            max_escalations=1,
            undecided_repeats=2,
            label="main's splice over n e-mail spans",
        )
