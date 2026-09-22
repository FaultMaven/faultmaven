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
still, in ``extractors.utils.distinct_on_line`` — the logs extractor's
entity profile has fifteen other things to do per line and keeps its own
loop, so it takes the rule without taking this tally.

What a rule may vary is deliberately small:

* ``patterns`` or ``matcher`` — how values are found on a line. A matcher
  is for a rule that is not a regex: USER goes through
  ``log_usernames.extract_usernames``, which already de-duplicates per
  line and carries the fm#522 guards a second regex copy would not.
* ``keep`` — a value-level validity test (port range, PID ceiling).
* ``error_context`` — whether this type records "appeared on an error
  line". Per-rule rather than global because the logs extractor records
  it for IP and USER only, and making PORT/PID/PATH start reporting it
  would be a different change from this one.

It may not vary the unit, the line split, or the counting.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from faultmaven.modules.case.contracts import EntityType
from faultmaven.modules.preprocessing.entities.protocol import EntityObservation
from faultmaven.modules.preprocessing.extractors.utils import (
    distinct_on_line,
    split_log_lines,
)

__all__ = ["EntityRule", "tally_entity_lines", "is_port", "is_pid", "PID_MAX"]

#: Linux's default ``pid_max`` ceiling on 64-bit. Values above it are not
#: process ids whatever the surrounding text claims.
PID_MAX = 4_194_304


def is_port(value: str) -> bool:
    """True when ``value`` is a legal TCP/UDP port number."""
    return value.isdigit() and 0 < int(value) <= 65535


def is_pid(value: str) -> bool:
    """True when ``value`` is inside the kernel's pid range."""
    return value.isdigit() and 0 < int(value) <= PID_MAX


@dataclass(frozen=True)
class EntityRule:
    """How one entity type is recognised on a line.

    Exactly one of ``patterns`` and ``matcher`` is given.
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

    def values_on(self, line: str) -> list[str]:
        """The distinct values this rule finds on one physical line."""
        if self.matcher is not None:
            # A matcher owns its own de-duplication (``extract_usernames``
            # does), but running the shared rule over its output as well
            # costs nothing and means no matcher can opt out of the unit.
            found = list(dict.fromkeys(self.matcher(line)))
        else:
            found = distinct_on_line(line, *self.patterns)
        if self.keep is None:
            return found
        return [value for value in found if self.keep(value)]


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
    the order the four extractors emitted before the loop moved here.
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

    observations: list[EntityObservation] = []
    for rule_index, rule in enumerate(rules):
        for value, count in totals[rule_index].items():
            observations.append(
                EntityObservation(
                    entity_type=rule.entity_type,
                    entity_value=value,
                    mention_count=count,
                    in_error_context=errors[rule_index].get(value, 0) > 0,
                )
            )
    return observations
