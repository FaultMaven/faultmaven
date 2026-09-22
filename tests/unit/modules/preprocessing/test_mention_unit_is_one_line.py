"""fm#1587 — ``mention_count`` is LINES, for every entity type, everywhere.

The defect this file exists to prevent is not a wrong count. It is a
wrong count *in one entity type while the others are right*: fm#1574
moved USER to "one line = one mention" and left IP, PORT, PID and
HTTP_PATH counting regex matches, so ``case_entities.mention_count``
carried two units in one column and the ``SUM(mention_count) DESC``
ranking that picks the top five entities for the investigation prompt
was comparing quantities that were not the same quantity. Nothing
noticed for a round.

So there are two guards here, and they answer different questions.

``TestEveryEntityTypeCountsLines`` is behavioural: it feeds each
registered extractor a line that names every one of its entity types
**twice**, and requires every count to be 1. It is driven off the
registry's own dispatch table, so a newly registered extractor fails
here until someone writes it a line — and it asserts each fixture still
produces the full set of types that extractor declares, so a fixture
that quietly stops matching cannot make the guard vacuous.

``test_no_entity_loop_iterates_raw_matches`` is structural: it walks the
modules that produce entity counts and fails on any loop that iterates a
``findall`` result directly, because that is the shape the rule is
written in when it is written per consumer. The allowlist below is the
census — three occurrence tallies that are deliberately per match and
carry no ``mention_count``.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from faultmaven.models.api import DataType
from faultmaven.modules.case.domain.models import EntityType
from faultmaven.modules.preprocessing.entities.line_tally import EntityRule
from faultmaven.modules.preprocessing.entities.registry import (
    _EXTRACTORS,
    extract_entities_for_data_type,
)
from faultmaven.modules.preprocessing.extractors.logs_extractor import (
    LogsAndErrorsExtractor,
)
from faultmaven.modules.preprocessing.extractors.utils import distinct_on_line

# ---------------------------------------------------------------------------
# One line per data type, on which EVERY entity type that extractor can
# produce is named TWICE. Written by hand rather than generated: the point
# is to exercise the real regexes on text that looks like the logs, command
# output, configs and traces they were built for.
# ---------------------------------------------------------------------------
_DOUBLED_LINE: dict[DataType, tuple[str, dict[EntityType, str]]] = {
    DataType.LOGS_AND_ERRORS: (
        "Jul 27 14:41:59 combo sshd[4242]: Failed password for invalid user "
        "root from 10.0.0.5 port 8080 ssh2 (peer 10.0.0.5 backend:8080 "
        "pid 4242) GET /v1/x GET /v1/x",
        {
            EntityType.IP: "10.0.0.5",
            EntityType.USER: "root",
            EntityType.PORT: "8080",
            EntityType.PID: "4242",
            EntityType.PATH: "/v1/x",
        },
    ),
    DataType.COMMAND_OUTPUT: (
        "tcp  4242  0 10.0.0.5:8080 10.0.0.5:8080  /var/run/a.sock "
        "/var/run/a.sock  4242 ",
        {
            EntityType.IP: "10.0.0.5",
            EntityType.PID: "4242",
            EntityType.PORT: "8080",
            EntityType.PATH: "/var/run/a.sock",
        },
    ),
    DataType.STRUCTURED_CONFIG: (
        "{host: db.internal, hostname: db.internal, port: 5432, listen: 5432, "
        "service: pgbouncer, daemon: pgbouncer, path: /var/lib/pg, "
        "data_dir: /var/lib/pg, bind: 10.0.0.5 10.0.0.5}",
        {
            EntityType.HOSTNAME: "db.internal",
            EntityType.PORT: "5432",
            EntityType.SERVICE: "pgbouncer",
            EntityType.PATH: "/var/lib/pg",
            EntityType.IP: "10.0.0.5",
        },
    ),
    DataType.TRACE_DATA: (
        '{"service.name":"checkout","peer.service":"checkout",'
        '"host.name":"node-1","net.host.name":"node-1",'
        '"http.url":"/cart","http.target":"/cart",'
        '"client":"10.0.0.5","peer":"10.0.0.5"}',
        {
            EntityType.SERVICE: "checkout",
            EntityType.HOSTNAME: "node-1",
            EntityType.PATH: "/cart",
            EntityType.IP: "10.0.0.5",
        },
    ),
}


@pytest.mark.unit
class TestEveryEntityTypeCountsLines:
    def test_every_registered_extractor_has_a_doubling_line(self):
        """A new extractor is covered by this file or it fails here.

        The divergence fm#1587 fixes happened because a rule moved for one
        type and nothing asked the others. An extractor registered without
        a fixture would reopen exactly that hole silently.
        """
        assert set(_EXTRACTORS) == set(_DOUBLED_LINE)

    @pytest.mark.parametrize("data_type", sorted(_DOUBLED_LINE, key=str))
    def test_fixture_still_matches_every_declared_type(self, data_type):
        """Positive control: the line really does exercise every rule.

        Without this, a regex change that stopped matching would make the
        per-line assertion below pass on an empty result set.
        """
        line, expected = _DOUBLED_LINE[data_type]
        observations = extract_entities_for_data_type(data_type, line + "\n")
        assert {o.entity_type for o in observations} == set(expected)

        declared = {
            rule.entity_type
            for rule in getattr(type(_EXTRACTORS[data_type]), "_RULES", ())
        }
        assert declared == set(expected), (
            "the extractor declares entity types this fixture does not name "
            "twice — the unfixtured type is where the unit can drift"
        )

    @pytest.mark.parametrize("data_type", sorted(_DOUBLED_LINE, key=str))
    def test_one_line_is_one_mention(self, data_type):
        line, expected = _DOUBLED_LINE[data_type]
        observations = extract_entities_for_data_type(data_type, line + "\n")

        counts = {
            (o.entity_type, o.entity_value): o.mention_count for o in observations
        }
        for entity_type, value in expected.items():
            assert counts[(entity_type, value)] == 1, (
                f"{data_type} {entity_type} {value!r} is named twice on ONE "
                f"line and counted {counts[(entity_type, value)]}"
            )
        # Nothing else on the line may count above 1 either — a type that
        # slipped back to per-match is caught even without a named value.
        assert max(counts.values()) == 1, counts

    @pytest.mark.parametrize("data_type", sorted(_DOUBLED_LINE, key=str))
    def test_counts_still_accumulate_across_lines(self, data_type):
        """Per-line, not per-file: the same line twice is two mentions."""
        line, expected = _DOUBLED_LINE[data_type]
        observations = extract_entities_for_data_type(
            data_type, line + "\n" + line + "\n"
        )

        counts = {
            (o.entity_type, o.entity_value): o.mention_count for o in observations
        }
        for entity_type, value in expected.items():
            assert counts[(entity_type, value)] == 2, (
                f"{data_type} {entity_type} {value!r} appears on TWO lines "
                f"and counted {counts[(entity_type, value)]}"
            )


@pytest.mark.unit
class TestLogsExtractorProfileCountsLines:
    """The other real entry point: the rendered ENTITY PROFILE.

    ``entities/logs.py`` and ``logs_extractor._build_entity_profile`` scan
    the same file with the same regex shapes and are the two paths fm#1574
    let drift apart. The profile is checked here through
    ``extract()``/``search_map`` rather than by calling the private
    builder, because the profile a caller sees is the one that matters.
    """

    def _search_map(self, content: str) -> str:
        return LogsAndErrorsExtractor().extract(content).search_map or ""

    def test_profile_counts_a_doubled_line_once(self):
        line, _ = _DOUBLED_LINE[DataType.LOGS_AND_ERRORS]
        search_map = self._search_map(line + "\n")

        assert "10.0.0.5: 1 line occurrences (all event types)" in search_map
        assert "root: 1 lines" in search_map
        assert "8080: 1 lines" in search_map
        assert "4242: 1 lines" in search_map
        assert "/v1/x: 1 lines" in search_map

    def test_profile_and_registry_agree_on_the_same_file(self):
        """The two paths report the same number for the same value.

        This is the drift itself, asserted directly: a change to one path
        that does not reach the other fails here.
        """
        line, _ = _DOUBLED_LINE[DataType.LOGS_AND_ERRORS]
        content = line + "\n" + line + "\n"

        rows = extract_entities_for_data_type(DataType.LOGS_AND_ERRORS, content)
        registry_ip = next(
            row.mention_count
            for row in rows
            if row.entity_type == EntityType.IP and row.entity_value == "10.0.0.5"
        )
        rendered = re.search(
            r"10\.0\.0\.5: (\d+) line occurrences", self._search_map(content)
        )
        assert rendered is not None, "the IP block did not render"
        assert int(rendered.group(1)) == registry_ip == 2

    def test_auth_breakdown_does_not_double_a_repeated_ip(self):
        """fm#1587 item 1, the half entity-level de-duplication reaches.

        ``Failed password for invalid user`` is ONE line matching TWO event
        categories, and ``auth total`` sums across categories — so the
        total below is 2 for a single event. That residual is fm#1596 and
        is deliberately not fixed here; what IS fixed is that naming the IP
        twice on the line no longer doubles it again to 4.
        """
        one_ip = (
            "Jul 27 14:41:59 combo sshd[1]: Failed password for invalid user "
            "test from 10.0.0.5 port 1 ssh2\n"
        )
        twice_on_the_line = (
            "Jul 27 14:41:59 combo sshd[1]: Failed password for invalid user "
            "test from 10.0.0.5 port 1 ssh2 (src 10.0.0.5)\n"
        )
        expected = "10.0.0.5: failed_password=1, invalid_user=1 → auth total=2"
        assert expected in self._search_map(one_ip)
        assert expected in self._search_map(twice_on_the_line)


@pytest.mark.unit
class TestDistinctOnLine:
    """The rule itself — one implementation, so it gets one test."""

    def test_de_duplicates_within_one_pattern(self):
        assert distinct_on_line("a a b", re.compile(r"[ab]")) == ["a", "b"]

    def test_de_duplicates_across_patterns(self):
        """One entity type written two ways on one line is one mention."""
        keyword = re.compile(r"port[ =](\d+)")
        host_port = re.compile(r"[a-z]+:(\d+)")
        assert distinct_on_line("port 8080 host:8080", keyword, host_port) == ["8080"]

    def test_order_is_first_seen(self):
        assert distinct_on_line("b a b", re.compile(r"[ab]")) == ["b", "a"]

    def test_rejects_multi_group_patterns(self):
        """``findall`` returns tuples past one group; a tuple is not a value."""
        with pytest.raises(ValueError, match="0 or 1 capture groups"):
            distinct_on_line("x=1", re.compile(r"(\w)=(\d)"))


# ---------------------------------------------------------------------------
# Structural census. Before fm#1587 these five modules held 25 loops that
# iterated a ``findall`` result; 22 of them counted entities, and each was
# its own chance to disagree about the unit. Three remain and none of them
# produces a ``mention_count``.
# ---------------------------------------------------------------------------
_SCANNED_MODULES = (
    "faultmaven/modules/preprocessing/entities/logs.py",
    "faultmaven/modules/preprocessing/entities/command_output.py",
    "faultmaven/modules/preprocessing/entities/config.py",
    "faultmaven/modules/preprocessing/entities/trace.py",
    "faultmaven/modules/preprocessing/extractors/logs_extractor.py",
    "faultmaven/modules/preprocessing/log_usernames.py",
)

#: ``(module basename, loop target)`` for every raw-match loop that is
#: allowed to remain. All three are occurrence tallies rendered as prose
#: in the logs extractor's structural index — they answer "how many times
#: was this code emitted", never "how many lines mentioned this entity",
#: and none reaches ``case_entities``. A line may legitimately carry two
#: KB references or two HRESULTs and both are events.
_PER_MATCH_ALLOWLIST = {
    ("logs_extractor.py", "kb_num"),  # Windows Update KB packages (ISS-020)
    ("logs_extractor.py", "state_num"),  # Apache mod_jk worker states (ISS-045)
    ("logs_extractor.py", "(hresult_hex, hresult_sym)"),  # Windows CBS (ISS-037)
}

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]


def _iterates_findall(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr == "findall"
        for child in ast.walk(node)
    )


@pytest.mark.unit
@pytest.mark.architecture
def test_no_entity_loop_iterates_raw_matches():
    """Every entity count goes through the shared per-line rule.

    A loop over ``pattern.findall(line)`` counts matches; the entity
    modules must count lines, which is what ``distinct_on_line`` and
    ``tally_entity_lines`` do. New per-match loops are not forbidden —
    they are declared, with a reason, in ``_PER_MATCH_ALLOWLIST``.
    """
    found: set[tuple[str, str]] = set()
    scanned = 0
    for relative in _SCANNED_MODULES:
        path = _REPO_ROOT / relative
        assert path.exists(), f"{relative} moved; this census is not looking at it"
        scanned += 1
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.For, ast.AsyncFor)) and _iterates_findall(
                node.iter
            ):
                found.add((path.name, ast.unparse(node.target)))

    assert scanned == len(_SCANNED_MODULES)
    assert found == _PER_MATCH_ALLOWLIST, (
        "raw-match loops changed. A NEW one in an entity module means the "
        "mention unit is being decided per consumer again (fm#1587); a "
        "MISSING one means the allowlist needs updating with the reason."
    )


@pytest.mark.unit
def test_entity_rule_needs_exactly_one_way_to_match():
    """A rule with both a matcher and patterns has two units in one row."""
    with pytest.raises(ValueError, match="either patterns or a matcher"):
        EntityRule(EntityType.IP)
    with pytest.raises(ValueError, match="either patterns or a matcher"):
        EntityRule(
            EntityType.IP,
            patterns=(re.compile(r"x"),),
            matcher=lambda line: [],
        )
