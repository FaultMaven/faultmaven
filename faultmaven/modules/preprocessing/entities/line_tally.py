"""One tally for every ``EntityExtractor``, so the unit cannot diverge.

Every extractor in this package used to carry its own copy of the same
four-step loop — split the content into lines, run some regexes over each
line, count what they matched, build an :class:`EntityObservation` per
distinct value. Four copies of a loop is four places for the *unit* of
``mention_count`` to drift, and it did: fm#1574 made USER count lines and
left IP, PORT, PID and HTTP_PATH counting matches, so one column carried
two units and the ``SUM(mention_count)`` ranking that picks the top five
entities for the investigation prompt was comparing quantities that were
not the same quantity (fm#1587).

So the loop lives here once and each extractor declares a table of
:class:`EntityRule` instead. The mention rule itself is one level lower
still, in ``extractors.utils.distinct_values`` — the logs extractor's
entity profile has fifteen other things to do per line and keeps its own
loop, so it takes the rule without taking this tally.

What a rule may vary is deliberately small:

* ``patterns`` or ``matcher`` — how values are found on a line. A matcher
  is for a rule that is not a regex: USER goes through
  ``log_usernames.extract_usernames``, which carries the fm#522 guards a
  second regex copy would not. Both routes run ``distinct_values`` over
  their output, so a matcher cannot opt out of the unit.
* ``keep`` — a value-level validity test (``is_port``, ``is_pid``).
* ``error_context`` — whether this type records "appeared on an error
  line". Per-rule rather than global because the logs extractor records
  it for IP and USER only, and making PORT/PID/PATH start reporting it
  would be a different change from this one.

It may not vary the unit, the line split, or the counting.

**Two scopes, and why the second exists.** :func:`tally_entity_lines`
counts LINES and is what every time-ordered data type uses.
:func:`tally_document_matches` counts MATCHES over the whole document and
exists for exactly one caller, ``config.py``. The per-line unit's whole
justification is *a line is one event*, and a config has no events — it is
a structure. Applied to flow-style YAML, minified JSON or a single-line
``key=v key=v`` block, per-line counting flattens every value to 1 and
``SUM(mention_count) DESC`` returns an insertion-ordered tie, which is
strictly worse than the match count it would replace. Measured on the same
bytes:

    ONE physical line   db1.internal 1, 5432 1, pgbouncer 1, db2.internal 1
    newline-separated   db1.internal 2, 5432 2, pgbouncer 1, db2.internal 1

Keeping configs on match counting is therefore not a carve-out from
fm#1587's ruling so much as declining to apply an argument that does not
reach; the scope question is with the owner, and
``docs/architecture/data-processing/entity-registry.md`` records the state.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from faultmaven.modules.case.contracts import EntityType
from faultmaven.modules.preprocessing.entities.protocol import EntityObservation
from faultmaven.modules.preprocessing.extractors.utils import (
    PID_MAX,
    check_entity_pattern,
    distinct_values,
    is_pid,
    is_port,
    split_log_lines,
)

__all__ = [
    "EntityRule",
    "tally_entity_lines",
    "tally_document_matches",
    "is_port",
    "is_pid",
    "PID_MAX",
]


@dataclass(frozen=True)
class EntityRule:
    """How one entity type is recognised.

    Exactly one of ``patterns`` and ``matcher`` is given. Patterns are
    validated here, at construction — these tables are module-level, so
    construction is import — because the per-call check would surface as
    "this evidence produced no entities at all": the preprocessing service
    wraps entity extraction in a blanket ``except Exception`` and degrades
    to an empty list behind a single WARNING line.
    """

    entity_type: EntityType
    patterns: tuple[re.Pattern[str], ...] = field(default=())
    matcher: Callable[[str], Iterable[str]] | None = None
    keep: Callable[[str], bool] | None = None
    error_context: bool = False

    def __post_init__(self) -> None:
        if bool(self.patterns) == (self.matcher is not None):
            raise ValueError(
                f"{self.entity_type} rule needs either patterns or a matcher, "
                "not both and not neither"
            )
        for pattern in self.patterns:
            check_entity_pattern(pattern)

    def values_on(self, line: str) -> list[str]:
        """The distinct values this rule finds on one physical line."""
        if self.matcher is not None:
            found = distinct_values(self.matcher(line))
        else:
            found = distinct_values(
                value for pattern in self.patterns for value in pattern.findall(line)
            )
        return self._kept(found)

    def matches_in(self, content: str) -> list[str]:
        """Every match in ``content``, NOT de-duplicated.

        Document scope — see :func:`tally_document_matches`. A matcher rule
        has no document form (``extract_usernames`` is a per-line rule), so
        this is patterns only.
        """
        if self.matcher is not None:
            raise ValueError(
                f"{self.entity_type} rule has a matcher; document-scope "
                "counting is patterns-only"
            )
        return self._kept(
            [value for pattern in self.patterns for value in pattern.findall(content)]
        )

    def _kept(self, values: list[str]) -> list[str]:
        if self.keep is None:
            return values
        return [value for value in values if self.keep(value)]


def _observations(
    rules: Sequence[EntityRule],
    totals: list[Counter],
    errors: list[Counter],
) -> list[EntityObservation]:
    observations: list[EntityObservation] = []
    for index, rule in enumerate(rules):
        for value, count in totals[index].items():
            observations.append(
                EntityObservation(
                    entity_type=rule.entity_type,
                    entity_value=value,
                    mention_count=count,
                    in_error_context=errors[index].get(value, 0) > 0,
                )
            )
    return observations


def tally_entity_lines(
    content: str,
    rules: Sequence[EntityRule],
    *,
    is_error: Callable[[int, str], bool] | None = None,
) -> list[EntityObservation]:
    """Count, per rule, the LINES of ``content`` each value appears on.

    ``is_error`` receives ``(line_index, line)`` and says whether that line
    is an error line; rules with ``error_context`` set report
    ``in_error_context=True`` for any value seen on one. Observations come
    back grouped in rule order, and within a rule in first-seen order —
    the order the extractors emitted before the loop moved here.
    """
    totals: list[Counter] = [Counter() for _ in rules]
    errors: list[Counter] = [Counter() for _ in rules]

    for index, line in enumerate(split_log_lines(content)):
        line_is_error = bool(is_error(index, line)) if is_error else False
        for rule_index, rule in enumerate(rules):
            for value in rule.values_on(line):
                totals[rule_index][value] += 1
                if line_is_error and rule.error_context:
                    errors[rule_index][value] += 1

    return _observations(rules, totals, errors)


def tally_document_matches(
    content: str,
    rules: Sequence[EntityRule],
) -> list[EntityObservation]:
    """Count, per rule, the MATCHES in ``content`` — the whole document.

    For declarative evidence with no line-as-event semantics; see the
    module docstring for why configs are counted this way and the
    measurement that settles it. No error context: a config is declarative
    and has no error lines.
    """
    totals: list[Counter] = [Counter() for _ in rules]
    for index, rule in enumerate(rules):
        for value in rule.matches_in(content):
            totals[index][value] += 1
    return _observations(rules, totals, [Counter() for _ in rules])
