"""fm#1587 — the logs entity profile counts every entity type in lines.

The defect this file exists to prevent is not a wrong count. It is a
wrong count *in one entity type while the others are right*: fm#1574
moved USER to "one line = one mention" and left IP, PORT, PID and
HTTP_PATH counting regex matches, so the rendered ENTITY PROFILE ranked
quantities that were not the same quantity. Nothing noticed for a round.

``TestLogsExtractorProfileCountsLines`` is behavioural: it feeds the logs
extractor a line that names every entity type the profile renders
**twice**, and requires each to count once per line.

``test_no_entity_loop_iterates_raw_matches`` is structural: it walks the
modules that produce entity counts and fails on any loop that iterates a
``findall`` result directly, because that is the shape the rule is
written in when it is written per consumer. The allowlist below is the
census — three occurrence tallies that are deliberately per match and
carry no entity count.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from faultmaven.modules.case.domain.models.evidence import EntityType
from faultmaven.modules.preprocessing.extractors.logs_extractor import (
    LogsAndErrorsExtractor,
)
from faultmaven.modules.preprocessing.extractors.utils import (
    PID_MAX,
    distinct_on_line,
    distinct_values,
)
from tests.unit.core.investigation.given_tools import DEFAULT_GIVEN

#: One log line on which every entity type the profile renders is named
#: TWICE, with the doubled value of each. Written by hand rather than
#: generated: the point is to exercise the real regexes on text that looks
#: like the logs they were built for.
_DOUBLED_LINE = (
    "Jul 27 14:41:59 combo sshd[4242]: Failed password for invalid user "
    "root from 10.0.0.5 port 8080 ssh2 (peer 10.0.0.5 backend:8080 "
    "pid 4242) GET /v1/x GET /v1/x"
)
_DOUBLED = {
    EntityType.IP: "10.0.0.5",
    EntityType.USER: "root",
    EntityType.PORT: "8080",
    EntityType.PID: "4242",
    EntityType.PATH: "/v1/x",
}


@pytest.mark.unit
class TestLogsExtractorProfileCountsLines:
    """The rendered ENTITY PROFILE.

    The profile is checked here through ``extract()``/``search_map`` rather
    than by calling the private builder, because the profile a caller sees
    is the one that matters.
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
        search_map = self._search_map(_DOUBLED_LINE + "\n")

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

    def test_profile_counts_every_entity_type_per_line(self):
        """Every type counts lines, not matches: two doubled lines are 2.

        For EVERY type rather than for IP alone: fm#1574 moved one type's
        unit and left the others, and a rule moved for one type only would
        land here.
        """
        content = _DOUBLED_LINE + "\n" + _DOUBLED_LINE + "\n"
        search_map = self._search_map(content)

        checked = 0
        for entity_type, value in _DOUBLED.items():
            pattern = self._RENDERED[entity_type].format(value=re.escape(value))
            rendered = re.search(pattern, search_map)
            assert rendered is not None, f"{entity_type} {value!r} did not render"
            assert (
                int(rendered.group(1)) == 2
            ), f"{entity_type} {value!r}: profile says {rendered.group(1)}"
            checked += 1
        assert checked == len(self._RENDERED) == 5

    #: Values OUTSIDE the shared limits — one above ``PID_MAX``
    #: (4,194,304) and one above the TCP port ceiling. The profile must drop
    #: them. Picked to be outside the limit but inside the regexes' digit
    #: widths, which is the only band where a divergence is observable.
    _OVER_THE_LIMIT = "Jul 27 14:41:59 h app[9999999]: pid 9999999 port 99999 x"

    def test_the_profile_keeps_the_shared_port_and_pid_limits(self):
        """The profile's ceiling is the shared one, and it drops what is over it.

        Asserted behaviourally as well as by identity: the identity check
        alone passes while a CALL SITE keeps its own inline literal.
        """
        from faultmaven.modules.preprocessing.extractors.logs_extractor import (
            LogsAndErrorsExtractor as Profile,
        )

        assert Profile._PID_MAX is PID_MAX
        content = self._OVER_THE_LIMIT + "\n"
        assert (
            self._block_values(content, "Distinct PIDs") == set()
        ), "the rendered profile kept a PID above the shared ceiling"
        assert self._block_values(content, "Distinct Ports") == set()

    def test_the_over_the_limit_fixture_is_otherwise_recognisable(self):
        """Positive control for the test above.

        A fixture the regexes never match would make "the profile dropped
        it" vacuously true. Shift both values inside the limits and the
        profile must report them.
        """
        inside = (
            self._OVER_THE_LIMIT.replace("app[9999999]", "app[999999]")
            .replace("pid 9999999", "pid 999999")
            .replace("port 99999", "port 9999")
        )
        assert self._block_values(inside + "\n", "Distinct PIDs") == {"999999"}
        assert self._block_values(inside + "\n", "Distinct Ports") == {"9999"}

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

    def test_auth_total_is_neither_the_sum_nor_the_line_count(self):
        """The shape that separates the three rules the table has shipped.

        Six lines from one IP: three match BOTH ``failed_password`` and
        ``invalid_user``, two match ``failed_password`` only, one matches
        ``invalid_user`` only. So ``failed_password=5``, ``invalid_user=4``,
        six auth LINES, and five OUTCOME lines.

        Summing gives 9 (fm#1596's defect), counting auth lines gives 6
        (fm#1596's fix), counting attempts by outcome line gives 5
        (fm#1627's ruling). The lone ``Invalid user`` line offered no
        credential, so it is context, not an attempt.
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
            "10.0.0.9: failed_password=5, invalid_user=4 \u2192 auth total=5"
            in search_map
        ), search_map

    def test_a_non_auth_event_line_is_not_an_attempt(self):
        """A non-auth event on the same IP is not an auth attempt.

        A ``Connection closed`` line carries the IP and matches an event, but
        it is neither an attempt outcome nor an auth category, so it must
        neither raise the total nor appear beside it. Five lines for one IP,
        three of them ``Failed password`` outcomes: the total is 3, and the
        two connection_closed lines are visible only in the line-occurrence
        count above the table.
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

    def test_three_line_sshd_attempts_count_as_attempts(self):
        """One sshd attempt is three lines; ``auth total`` is the attempt.

        For one password try against an invalid user, OpenSSH writes three
        lines carrying the source IP: ``Invalid user``, the ``pam_unix``
        authentication failure, and ``Failed password for invalid user``.
        Three attempts logged that way rendered ``auth total=12`` before
        fm#1596 (summed categories) and ``auth total=9`` after it (auth
        lines). fm#1627 ruled the total an attempt count, taken from the one
        line sshd writes exactly once per attempt — the outcome — so it is
        3, and the header says it is attempts rather than hedging a line
        count as an upper bound.
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

        # The count itself is the ruled one: attempts, by outcome line. The
        # per-category counts stay beside it — they are what to search on.
        assert (
            "5.36.59.76: failed_password=3, pam_auth_failure=3, invalid_user=6 "
            "\u2192 auth total=3" in search_map
        ), search_map

        # Anchored on the header's own start: the Distinct IPs header above
        # it also names "IP auth breakdown", in a cross-reference.
        header = next(
            line
            for line in search_map.splitlines()
            if line.lstrip().startswith("IP auth breakdown")
        )
        # The total is exact now, so the fm#1596 hedge would be untrue in
        # the other direction; the header names the unit and the fallback.
        assert "upper bound" not in header.lower(), header
        assert "lines carrying an auth event" not in header.lower(), header
        assert "attempts" in header.lower(), header
        assert "outcome" in header.lower(), header
        assert "pam" in header.lower(), header

    def test_the_prompt_routing_agrees_with_the_header(self):
        """The model reads ``auth total`` through two channels; both agree.

        The table's own header is one. The evidence-grounding block routes
        "auth counts per IP" questions to that table — the other. fm#1596
        taught both that the total was an upper bound on attempts; fm#1627
        made it the attempt count, so the routing line must stop calling it
        a line count or an upper bound, or it contradicts the header.
        """
        from faultmaven.core.investigation.prompts.templates.blocks import (
            evidence_grounding_block,
        )

        routing = " ".join(evidence_grounding_block(DEFAULT_GIVEN).split())
        start = routing.index("For auth counts per IP")
        clause = routing[start : routing.index('For "list all X"', start)]
        assert "IP auth breakdown" in clause, clause
        assert "upper bound" not in clause.lower(), clause
        assert "auth lines" not in clause.lower(), clause
        assert "attempts" in clause.lower(), clause


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

    def test_distinct_values_is_what_both_routes_call(self):
        assert distinct_values(["a", "b", "a"]) == ["a", "b"]


# ---------------------------------------------------------------------------
# Structural census. Before fm#1587 these modules held 25 loops that
# iterated a ``findall`` result; 22 of them counted entities, and each was
# its own chance to disagree about the unit. Three remain and none of them
# produces an entity count.
# ---------------------------------------------------------------------------
_PREPROCESSING = pathlib.Path("faultmaven/modules/preprocessing")
_SCANNED = (
    _PREPROCESSING / "extractors" / "logs_extractor.py",
    _PREPROCESSING / "log_usernames.py",
)

#: ``(module basename, loop target, iterated expression)`` for every
#: raw-match loop that is allowed to remain. All three are occurrence
#: tallies rendered as prose in the logs extractor's structural index —
#: they answer "how many times was this code emitted", never "how many
#: lines mentioned this entity". A line may legitimately carry two KB references or two HRESULTs and both
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
    paths = []
    for relative in _SCANNED:
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
    ``distinct_on_line`` do. New per-match loops are not forbidden —
    they are declared, with a reason, in ``_PER_MATCH_ALLOWLIST``.
    """
    paths = _scanned_paths()
    names = {path.name for path in paths}
    assert {"logs_extractor.py", "log_usernames.py"} == names, names

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
