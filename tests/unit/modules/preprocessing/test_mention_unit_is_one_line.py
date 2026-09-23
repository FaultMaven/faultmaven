"""fm#1587 — every entity type's ``mention_count`` uses its declared unit.

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
**twice**, and requires every count to match that data type's DECLARED
scope — 1 for the per-line types, 2 for ``STRUCTURED_CONFIG``, which
counts matches over the whole document because a config has no events
and per-line counting collapses its ranking into a tie (the measurement
is in ``entities/line_tally.py``). Declaring the scope per data type is
what keeps this a divergence guard rather than a silent exemption: a
type with no declared scope, or a *second* type going document-scoped,
fails. It is driven off the registry's own dispatch table, so a newly
registered extractor fails here until someone writes it a line — and it
asserts each fixture still produces the full set of types that extractor
declares, so a fixture that quietly stops matching cannot make the guard
vacuous.

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
from faultmaven.modules.preprocessing.extractors.utils import (
    PID_MAX,
    distinct_on_line,
    distinct_values,
    is_pid,
    is_port,
)

# ---------------------------------------------------------------------------
# One line per data type, on which EVERY entity type that extractor can
# produce is named TWICE. Written by hand rather than generated: the point
# is to exercise the real regexes on text that looks like the logs, command
# output, configs and traces they were built for.
# ---------------------------------------------------------------------------
#: ``data type -> (line, {entity type: doubled value}, scope)``. ``scope``
#: is "line" for the time-ordered data types and "document" for
#: STRUCTURED_CONFIG, which counts matches over the whole file; declaring
#: it per data type is what keeps this a DIVERGENCE guard rather than a
#: silent exemption — a type with no declared scope fails below.
_DOUBLED_LINE: dict[DataType, tuple[str, dict[EntityType, str], str]] = {
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
        "line",
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
        "line",
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
        # Document scope: a config has no events, so "one line is one
        # event" does not reach it. See ``line_tally`` for the measurement.
        "document",
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
        "line",
    ),
}


#: The scopes a data type may declare, and what a value named twice on ONE
#: line must then count as.
_SCOPE_DOUBLED_COUNT = {"line": 1, "document": 2}


@pytest.mark.unit
class TestEveryEntityTypeCountsLines:
    def test_every_registered_extractor_has_a_doubling_line(self):
        """A new extractor is covered by this file or it fails here.

        The divergence fm#1587 fixes happened because a rule moved for one
        type and nothing asked the others. An extractor registered without
        a fixture would reopen exactly that hole silently.
        """
        assert set(_EXTRACTORS) == set(_DOUBLED_LINE)

    def test_every_fixture_declares_a_known_scope(self):
        """ "Document scope" must be chosen, never inherited by omission."""
        declared = {dt: scope for dt, (_l, _e, scope) in _DOUBLED_LINE.items()}
        assert set(declared.values()) <= set(_SCOPE_DOUBLED_COUNT)
        # Exactly one exception is expected today. A second one arriving
        # unremarked is the thing this line is here to make loud.
        document_scoped = {dt for dt, sc in declared.items() if sc == "document"}
        assert document_scoped == {DataType.STRUCTURED_CONFIG}, (
            "a data type changed scope. Per-line is the rule (fm#1587); "
            "STRUCTURED_CONFIG is the one exception and carries its reason "
            "and measurement in entities/line_tally.py"
        )

    @pytest.mark.parametrize("data_type", sorted(_DOUBLED_LINE, key=str))
    def test_fixture_still_matches_every_declared_type(self, data_type):
        """Positive control: the line really does exercise every rule.

        Without this, a regex change that stopped matching would make the
        per-line assertion below pass on an empty result set.
        """
        line, expected, _scope = _DOUBLED_LINE[data_type]
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
        line, expected, scope = _DOUBLED_LINE[data_type]
        want = _SCOPE_DOUBLED_COUNT[scope]
        observations = extract_entities_for_data_type(data_type, line + "\n")

        counts = {
            (o.entity_type, o.entity_value): o.mention_count for o in observations
        }
        for entity_type, value in expected.items():
            assert counts[(entity_type, value)] == want, (
                f"{data_type} ({scope} scope) {entity_type} {value!r} is named "
                f"twice on ONE line and counted {counts[(entity_type, value)]}, "
                f"expected {want}"
            )
        assert max(counts.values()) == want, counts

    @pytest.mark.parametrize("data_type", sorted(_DOUBLED_LINE, key=str))
    def test_counts_still_accumulate_across_lines(self, data_type):
        """Per line or per document, a second line is still more mentions."""
        line, expected, scope = _DOUBLED_LINE[data_type]
        want = _SCOPE_DOUBLED_COUNT[scope] * 2
        observations = extract_entities_for_data_type(
            data_type, line + "\n" + line + "\n"
        )

        counts = {
            (o.entity_type, o.entity_value): o.mention_count for o in observations
        }
        for entity_type, value in expected.items():
            assert counts[(entity_type, value)] == want, (
                f"{data_type} {entity_type} {value!r} appears on TWO lines "
                f"and counted {counts[(entity_type, value)]}, expected {want}"
            )

    def test_config_ranking_survives_a_single_line_file(self):
        """Why STRUCTURED_CONFIG is document-scoped, asserted not asserted-at.

        Flow-style YAML, minified JSON and ``key=v key=v`` blocks put a
        whole config on one physical line. Per-line counting flattens every
        value to 1 there, and ``SUM(mention_count) DESC`` — which picks the
        top five entities for the investigation prompt — becomes an
        insertion-ordered tie. The same bytes must rank the same either way.
        """
        pairs = (
            "host: db1.internal, hostname: db1.internal, port: 5432, "
            "listen: 5432, service: pgbouncer, server: db2.internal, "
            "target_port: 6432"
        )
        one_line = "{" + pairs + "}\n"
        many_lines = pairs.replace(", ", "\n") + "\n"

        def ranked(content):
            rows = extract_entities_for_data_type(DataType.STRUCTURED_CONFIG, content)
            return sorted(
                ((r.entity_type.value, r.entity_value), r.mention_count) for r in rows
            )

        assert ranked(one_line) == ranked(many_lines)
        counts = dict(ranked(one_line))
        assert counts[("hostname", "db1.internal")] == 2
        assert counts[("hostname", "db2.internal")] == 1
        assert len(set(counts.values())) > 1, "the ranking collapsed into a tie"


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

    def _block_values(self, content: str, header: str) -> set[str]:
        """The entry values listed under one ENTITY PROFILE block.

        Substring-searching the whole ``search_map`` is not a test of a
        block: the crime-scene excerpt and the template counts echo raw
        lines, so a dropped PID still appears verbatim in the output.
        """
        values: set[str] = set()
        inside = False
        for raw in self._search_map(content).split("\n"):
            if raw.strip().startswith(header):
                inside = True
                continue
            if inside:
                entry = re.match(r"^ {4}(\S+): ", raw)
                if entry is None:
                    break
                values.add(entry.group(1))
        return values

    def test_profile_counts_a_doubled_line_once(self):
        line, _expected, _scope = _DOUBLED_LINE[DataType.LOGS_AND_ERRORS]
        search_map = self._search_map(line + "\n")

        assert "10.0.0.5: 1 line occurrences (all event types)" in search_map
        assert "root: 1 lines" in search_map
        assert "8080: 1 lines" in search_map
        assert "4242: 1 lines" in search_map
        assert "/v1/x: 1 lines" in search_map

    #: How each entity type's count is spelled in the rendered profile.
    _RENDERED = {
        EntityType.IP: r"{value}: (\d+) line occurrences",
        EntityType.USER: r"{value}: (\d+) lines",
        EntityType.PORT: r"    {value}: (\d+) lines",
        EntityType.PID: r"    {value}: (\d+) lines",
        EntityType.PATH: r"{value}: (\d+) lines",
    }

    def test_profile_and_registry_agree_on_every_entity_type(self):
        """The two paths report the same number for the same value.

        This is the drift itself, asserted directly, for EVERY type rather
        than for IP alone: the port and PID limits used to be written out
        twice, so a ceiling raised on one path only would land here.
        """
        line, expected, _scope = _DOUBLED_LINE[DataType.LOGS_AND_ERRORS]
        content = line + "\n" + line + "\n"
        search_map = self._search_map(content)
        rows = {
            (row.entity_type, row.entity_value): row.mention_count
            for row in extract_entities_for_data_type(DataType.LOGS_AND_ERRORS, content)
        }

        checked = 0
        for entity_type, value in expected.items():
            registry = rows[(entity_type, value)]
            pattern = self._RENDERED[entity_type].format(value=re.escape(value))
            rendered = re.search(pattern, search_map)
            assert rendered is not None, f"{entity_type} {value!r} did not render"
            assert int(rendered.group(1)) == registry == 2, (
                f"{entity_type} {value!r}: profile says {rendered.group(1)}, "
                f"registry says {registry}"
            )
            checked += 1
        assert checked == len(self._RENDERED) == 5

    #: Values OUTSIDE the shared limits — one above ``PID_MAX``
    #: (4,194,304) and one above the TCP port ceiling. Both paths must drop
    #: them. Picked to be outside the limit but inside the regexes' digit
    #: widths, which is the only band where a divergence is observable.
    _OVER_THE_LIMIT = "Jul 27 14:41:59 h app[9999999]: pid 9999999 port 99999 x"

    def test_the_two_paths_share_the_port_and_pid_limits(self):
        """One ceiling, not two — raise it in one place and both move.

        Asserted behaviourally as well as by identity: the identity checks
        alone pass while a CALL SITE keeps its own inline literal, which is
        exactly what the two paths did before fm#1587 (`65535` written out
        in the profile, `is_port` in the registry).
        """
        import faultmaven.modules.preprocessing.entities.logs as registry_logs
        from faultmaven.modules.preprocessing.extractors.logs_extractor import (
            LogsAndErrorsExtractor as Profile,
        )

        assert Profile._PID_MAX is PID_MAX
        rules = {
            rule.entity_type: rule for rule in registry_logs.LogsEntityExtractor._RULES
        }
        assert rules[EntityType.PID].keep is is_pid
        assert rules[EntityType.PORT].keep is is_port

        content = self._OVER_THE_LIMIT + "\n"
        registry = {
            (row.entity_type, row.entity_value)
            for row in extract_entities_for_data_type(DataType.LOGS_AND_ERRORS, content)
        }
        assert self._block_values(content, "Distinct PIDs") == set(), (
            "the rendered profile kept a PID above the shared ceiling — "
            "a limit was raised on one path only"
        )
        assert self._block_values(content, "Distinct Ports") == set()
        assert (EntityType.PID, "9999999") not in registry
        assert (EntityType.PORT, "99999") not in registry

    def test_the_over_the_limit_fixture_is_otherwise_recognisable(self):
        """Positive control for the test above.

        A fixture the regexes never match would make "neither path kept it"
        vacuously true. Shift both values inside the limits and both paths
        must report them.
        """
        inside = (
            self._OVER_THE_LIMIT.replace("app[9999999]", "app[999999]")
            .replace("pid 9999999", "pid 999999")
            .replace("port 99999", "port 9999")
        )
        registry = {
            (row.entity_type, row.entity_value)
            for row in extract_entities_for_data_type(
                DataType.LOGS_AND_ERRORS, inside + "\n"
            )
        }
        assert self._block_values(inside + "\n", "Distinct PIDs") == {"999999"}
        assert self._block_values(inside + "\n", "Distinct Ports") == {"9999"}
        assert (EntityType.PID, "999999") in registry
        assert (EntityType.PORT, "9999") in registry

    def test_auth_breakdown_does_not_double_a_repeated_ip(self):
        """fm#1587 item 1, plus its residual fm#1596. The total moved 2 -> 1.

        ``Failed password for invalid user`` is ONE line matching TWO event
        categories. fm#1587 stopped the ENTITY repeating on the line from
        doubling it again (4 -> 2); this assertion pinned the 2 that was
        left, and fm#1596 is why the number is now 1: ``auth total`` counts
        the LINES carrying an auth event instead of summing the categories,
        so one line recording one attempt reports one attempt. The
        per-category numbers are unchanged and still say which categories
        fired.

        Both shapes are kept: they are the two independent ways this one
        line used to be counted twice, and a regression in either is a
        different bug.
        """
        one_ip = (
            "Jul 27 14:41:59 combo sshd[1]: Failed password for invalid user "
            "test from 10.0.0.5 port 1 ssh2\n"
        )
        twice_on_the_line = (
            "Jul 27 14:41:59 combo sshd[1]: Failed password for invalid user "
            "test from 10.0.0.5 port 1 ssh2 (src 10.0.0.5)\n"
        )
        expected = "10.0.0.5: failed_password=1, invalid_user=1 \u2192 auth total=1"
        assert expected in self._search_map(one_ip)
        assert expected in self._search_map(twice_on_the_line)

    def test_auth_total_is_neither_the_sum_nor_the_largest_category(self):
        """The shape that separates counting lines from its two near-misses.

        Six lines from one IP: three match BOTH ``failed_password`` and
        ``invalid_user``, two match ``failed_password`` only, one matches
        ``invalid_user`` only. So ``failed_password=5``, ``invalid_user=4``,
        and the number of auth LINES is 6.

        Summing gives 9 (fm#1596, the defect), taking the largest category
        gives 5, counting lines gives 6. The single-line fixture above
        cannot tell those three apart, because there all three answer 1.
        """
        both = (
            "Jul 27 14:4{i}:59 combo sshd[1]: Failed password for invalid "
            "user test from 10.0.0.9 port 1 ssh2"
        )
        failed_only = (
            "Jul 27 14:5{i}:59 combo sshd[1]: Failed password for root "
            "from 10.0.0.9 port 1 ssh2"
        )
        invalid_only = (
            "Jul 27 14:59:59 combo sshd[1]: Invalid user oracle "
            "from 10.0.0.9 port 1 ssh2"
        )
        content = (
            "\n".join(
                [both.format(i=i) for i in range(3)]
                + [failed_only.format(i=i) for i in range(2)]
                + [invalid_only]
            )
            + "\n"
        )
        search_map = self._search_map(content)
        assert (
            "10.0.0.9: failed_password=5, invalid_user=4 \u2192 auth total=6"
            in search_map
        ), search_map

    def test_auth_total_counts_only_auth_lines(self):
        """A non-auth event on the same IP is not an auth attempt.

        The categories the table renders are the auth ones; the tally behind
        the total has to agree, or a ``Connection closed`` line — which
        carries the IP and matches an event — is reported as an attempt that
        never happened. Five lines for one IP, three of them auth: the total
        is 3, and the two connection_closed lines are visible only in the
        line-occurrence count above the table.
        """
        auth = (
            "Jul 27 14:4{i}:59 combo sshd[1]: Failed password for invalid "
            "user test from 10.0.0.8 port 1 ssh2"
        )
        closed = "Jul 27 14:5{i}:59 combo sshd[1]: Connection closed by 10.0.0.8"
        content = (
            "\n".join(
                [auth.format(i=i) for i in range(3)]
                + [closed.format(i=i) for i in range(2)]
            )
            + "\n"
        )
        search_map = self._search_map(content)
        assert (
            "10.0.0.8: failed_password=3, invalid_user=3 \u2192 auth total=3"
            in search_map
        ), search_map
        assert (
            "10.0.0.8: 5 line occurrences (all event types)" in search_map
        ), search_map

    def test_the_header_does_not_call_a_line_count_an_attempt_count(self):
        """One sshd attempt is three lines, so the total is an upper bound.

        For one password try against an invalid user, OpenSSH writes three
        lines carrying the source IP: ``Invalid user``, the ``pam_unix``
        authentication failure, and ``Failed password for invalid user``.
        Three attempts logged that way are nine auth LINES, which is what
        fm#1596's ruling has the table count — so ``auth total=9`` is right
        as a line count and wrong as an attempt count.

        The header must therefore say which it is. An earlier revision of
        this change told the model the total "is the attempt count", on
        exactly the shape where it is off by 3x — a more confident claim
        than the hedge it replaced.
        """
        lines = []
        for i, user in enumerate(("admin", "oracle", "test")):
            lines += [
                f"Jul 27 14:4{i}:57 combo sshd[10{i}]: Invalid user {user} "
                "from 5.36.59.76",
                f"Jul 27 14:4{i}:58 combo sshd[10{i}]: pam_unix(sshd:auth): "
                "authentication failure; logname= uid=0 euid=0 tty=ssh ruser= "
                "rhost=5.36.59.76",
                f"Jul 27 14:4{i}:59 combo sshd[10{i}]: Failed password for "
                f"invalid user {user} from 5.36.59.76 port 22 ssh2",
            ]
        search_map = self._search_map("\n".join(lines) + "\n")

        # The count itself is the ruled one: lines, not attempts, not a sum.
        assert (
            "5.36.59.76: failed_password=3, pam_auth_failure=3, invalid_user=6 "
            "\u2192 auth total=9" in search_map
        ), search_map

        # Anchored on the header's own start: the Distinct IPs header above
        # it also names "IP auth breakdown", in a cross-reference.
        header = next(
            line
            for line in search_map.splitlines()
            if line.lstrip().startswith("IP auth breakdown")
        )
        assert "attempt count" not in header.lower(), header
        assert "upper bound" in header.lower(), header


@pytest.mark.unit
class TestTheMentionRule:
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

    def test_a_bad_pattern_is_refused_when_the_RULE_is_built(self):
        """Not per line, where it degrades to "no entities at all".

        ``PreprocessingService`` wraps entity extraction in a blanket
        ``except Exception`` and returns ``[]``, so a pattern that only
        raises at scan time costs an evidence's whole entity index behind
        one WARNING. These tables are module level, so this fires at import.
        """
        with pytest.raises(ValueError, match="0 or 1 capture groups"):
            EntityRule(EntityType.IP, patterns=(re.compile(r"(\w)=(\d)"),))

    def test_a_matcher_cannot_opt_out_of_the_rule(self, monkeypatch):
        """Normalisation added to the shared core must reach USER too.

        USER is the one entity type that is not a regex — it goes through
        ``log_usernames.extract_usernames`` — and it is the type fm#1574
        moved and fm#1587 is about. If the matcher branch had its own
        de-duplication, anything added to the core (case folding, trimming,
        a length cap) would reach IP/PORT/PID/PATH and not USER: the
        one-type-diverges shape, reintroduced by the fix for it.
        """
        import faultmaven.modules.preprocessing.entities.line_tally as tally

        monkeypatch.setattr(
            tally, "distinct_values", lambda values: [v.upper() for v in values]
        )
        pattern_rule = EntityRule(EntityType.IP, patterns=(re.compile(r"[a-z]+"),))
        matcher_rule = EntityRule(EntityType.USER, matcher=lambda line: line.split())

        assert pattern_rule.values_on("ab cd") == ["AB", "CD"]
        assert matcher_rule.values_on("ab cd") == [
            "AB",
            "CD",
        ], "the matcher path bypassed the shared rule"

    def test_distinct_values_is_what_both_routes_call(self):
        assert distinct_values(["a", "b", "a"]) == ["a", "b"]


# ---------------------------------------------------------------------------
# Structural census. Before fm#1587 these modules held 25 loops that
# iterated a ``findall`` result; 22 of them counted entities, and each was
# its own chance to disagree about the unit. Three remain and none of them
# produces a ``mention_count``.
#
# The ENTITIES package is GLOBBED, not listed: a hardcoded list is a guard
# that stops looking the moment someone adds ``entities/newtype.py``, which
# is precisely the module most likely to hand-roll a loop.
# ---------------------------------------------------------------------------
_PREPROCESSING = pathlib.Path("faultmaven/modules/preprocessing")
_ENTITIES_PACKAGE = _PREPROCESSING / "entities"
_ALSO_SCANNED = (
    _PREPROCESSING / "extractors" / "logs_extractor.py",
    _PREPROCESSING / "log_usernames.py",
)

#: ``(module basename, loop target, iterated expression)`` for every
#: raw-match loop that is allowed to remain. All three are occurrence
#: tallies rendered as prose in the logs extractor's structural index —
#: they answer "how many times was this code emitted", never "how many
#: lines mentioned this entity", and none reaches ``case_entities``. A
#: line may legitimately carry two KB references or two HRESULTs and both
#: are events.
#:
#: The iterated expression is part of the key, and the comparison is a
#: sorted LIST rather than a set, because neither a duplicate entry nor a
#: new loop that happens to reuse an allowlisted target name in the same
#: module may be absorbed silently.
_PER_MATCH_ALLOWLIST = sorted(
    [
        # Windows Update KB packages (ISS-020)
        ("logs_extractor.py", "kb_num", "self._KB_PACKAGE_RE.findall(line)"),
        # Apache mod_jk worker states (ISS-045)
        ("logs_extractor.py", "state_num", "self._MOD_JK_STATE_RE.findall(line)"),
        # Windows CBS HRESULTs (ISS-037)
        (
            "logs_extractor.py",
            "(hresult_hex, hresult_sym)",
            "self._HRESULT_RE.findall(line)",
        ),
    ]
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]


def _iterates_findall(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr == "findall"
        for child in ast.walk(node)
    )


def _scanned_paths() -> list[pathlib.Path]:
    package = _REPO_ROOT / _ENTITIES_PACKAGE
    assert package.is_dir(), f"{_ENTITIES_PACKAGE} moved; the census is blind"
    paths = sorted(package.glob("*.py"))
    for relative in _ALSO_SCANNED:
        path = _REPO_ROOT / relative
        assert path.exists(), f"{relative} moved; the census is not looking at it"
        paths.append(path)
    return paths


@pytest.mark.unit
@pytest.mark.architecture
def test_no_entity_loop_iterates_raw_matches():
    """Every entity count goes through the shared per-line rule.

    A loop over ``pattern.findall(line)`` counts matches; the entity
    modules must count lines, which is what ``distinct_values`` and
    ``tally_entity_lines`` do. New per-match loops are not forbidden —
    they are declared, with a reason, in ``_PER_MATCH_ALLOWLIST``.
    """
    paths = _scanned_paths()
    # Positive control: the glob must actually be finding the four
    # extractors plus the package's own modules, not an empty directory.
    names = {path.name for path in paths}
    assert {
        "logs.py",
        "command_output.py",
        "config.py",
        "trace.py",
        "line_tally.py",
        "logs_extractor.py",
        "log_usernames.py",
    } <= names, names

    found: list[tuple[str, str, str]] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.For, ast.AsyncFor)) and _iterates_findall(
                node.iter
            ):
                found.append(
                    (path.name, ast.unparse(node.target), ast.unparse(node.iter))
                )

    assert sorted(found) == _PER_MATCH_ALLOWLIST, (
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
