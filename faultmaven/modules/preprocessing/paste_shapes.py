"""Content-shape rules for pasted evidence (#2361).

A paste's filename is minted by the client, so the classifier types it by
content alone. These rules recognise the shapes engineers paste most —
tool reports and tables, config documents, timestamped log lines, diffs —
and name each with the one detailed type the #2361 taxonomy assigns it
(2026-11-13):

- ``STRUCTURED_CONFIG`` only for a document a config parser reads (JSON, a
  YAML mapping, a ``KEY=value`` block) and whose meaning is configuration:
  JSON that is a log event, a metrics result or a trace is not;
- ``COMMAND_OUTPUT`` for a report or table a tool prints for people
  (``systemctl status``, ``kubectl get``, ``curl -v``);
- ``LOGS_AND_ERRORS`` for timestamped event lines (text, logfmt, JSON lines);
- ``SOURCE_CODE`` for a diff.

Every rule here passed #2361's gate 4: switching it on made no paste wrong
and confident on either half of the frozen evaluation set. A ``vault read``
``Key  Value`` table and a Prometheus exposition each failed it, so they
have no rule and stay uncertain.

Precision comes first: for a short paste a confident wrong type is worse than
an uncertain one, which passes to the model as unstructured text (#2355). So
a rule speaks only when its shape accounts for at least two thirds of the
paste's non-empty lines (the same bar the evaluation labels use), and a
paste no rule claims is left to the classifier's other checks. A shape whose
extractor cannot read it has no rule. A whitespace metrics table was one until
the metrics extractor learned to read it (#2791); a rule for it waits on
pastes from new scenarios, since the frozen set's held-out half has been read.

Every rule reads one bounded sample (``SAMPLE_CHARS``), the YAML loader
included; the JSON rule alone reads up to ``JSON_PARSE_CHARS``, since the C
JSON parser is cheap. The classifier runs on the event loop, so no rule may
cost more than that. A JSON document larger than its limit does not parse
and is left uncertain; a YAML document is judged on its first sample.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, List, Optional, Tuple

from faultmaven.models.api import DataType

#: The share of a paste's non-empty lines a shape must account for.
SHAPE_SHARE = 2 / 3

#: Confidence of a shape verdict: above the clarification threshold (0.50),
#: below auto-accept (0.85) — a shape match, not a signature.
SHAPE_CONFIDENCE = 0.80

#: The most of a paste any rule reads, the YAML parser included.
SAMPLE_CHARS = 20_000

#: The most of a paste the JSON rule parses. The C JSON parser is cheap
#: (about 10 ms per MB), unlike the YAML loader, so a JSON document
#: (``kubectl get -o json``) is read whole up to this size.
JSON_PARSE_CHARS = 2_000_000

_SYSTEMD_HEAD = re.compile(r"^[●×○*] \S+\.(service|socket|timer|mount|target) - ")
_SYSTEMD_FIELD = re.compile(
    r"^\s+(Loaded|Active|Docs|Main PID|Tasks|Memory|CPU|CGroup|Process|TriggeredBy):"
)
_TABLE_HEAD = re.compile(
    r"^[A-Z][A-Z0-9_()%./:-]*(?:\s{2,}[A-Z][A-Z0-9_()%./:-]*(?: [A-Z][A-Z0-9_()%./:-]*)?){2,}\s*$"
)
_COLUMNS = re.compile(r"^\S+(?:\s{2,}\S+(?: \S+)*){2,}\s*$")
_KEY_VALUE = re.compile(r"^[A-Za-z_][\w.-]*=\S")
_STATUS_KV = re.compile(r"^[A-Za-z_][\w.-]*=(True|False|Unknown)\s+\S")
# A timestamp, then a level word on the same line. The whitespace run is
# possessive so it cannot trade characters with ``.*`` (quadratic on long
# padded lines).
_LOG_LINE = re.compile(
    r"^(?:\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*\]?"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})"
    r"\s++.*?\b(trace|debug|info|notice|warn|warning|error|err|fatal|critical|crit)\b",
    re.IGNORECASE,
)
_LOGFMT_TIME = {"time", "ts", "timestamp", "t"}
_LOGFMT_LEVEL = {"level", "lvl", "severity"}
_CURL_LINE = re.compile(r"^[*<>] |^[{}] \[\d+ bytes data\]")
# A diff line; `[+\- ]` covers the `--- ` and `+++ ` file headers too.
_DIFF_LINE = re.compile(r"^(?:diff --git |index [0-9a-f]+\.\.|@@ -\d|[+\- ])")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", re.M)
_ALIGNED_FIELD = re.compile(r"^\s*[A-Z][\w ./()-]*:\s{2,}\S")
_YAML_LINE = re.compile(r"^\s*(?:- )?[\w.\-/\"']+:(?: \S.*)?$|^\s*- \S")
_TOP_KEY = re.compile(r"^([\w.\-/\"']+):")

# JSON whose keys mark it as something other than configuration (#2361).
_TIME_KEYS = {"time", "timestamp", "ts", "@timestamp", "eventtime", "datetime"}
_EVENT_KEYS = {"level", "severity", "lvl", "message", "msg", "log", "event"}
# A JSON-lines event: an audit record's ``type`` (request / response) counts.
_LINE_EVENT_KEYS = _EVENT_KEYS | {"type"}
_HTTP_HEADER_KEY = re.compile(r"^(?:[A-Z][a-z0-9]*)(?:-[A-Z][A-Za-z0-9]*)+$|^X-")
_NOT_CONFIG_KEYS = {
    "resulttype",
    "resourcespans",
    "resourcemetrics",
    "spans",
    "traceid",
    "spanid",
}


def _lines(sample: str) -> List[str]:
    return [line for line in sample.splitlines() if line.strip()]


def _share(lines: List[str], matches: Callable[[str], Any]) -> float:
    return sum(1 for line in lines if matches(line)) / len(lines)


def _is_log_line(line: str) -> bool:
    if _LOG_LINE.search(line):
        return True
    # logfmt, keys in any order: the line starts with a ``key=value`` token,
    # and a time and a level key are among its first tokens.
    tokens = line.split(None, 5)[:5]
    if not tokens or "=" not in tokens[0]:
        return False
    keys = {tok.split("=", 1)[0].lower() for tok in tokens if "=" in tok}
    return bool(keys & _LOGFMT_TIME) and bool(keys & _LOGFMT_LEVEL)


def _parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


def _keys(doc: Any) -> set:
    """Lower-cased keys of a JSON object, or of an array's first object, and
    one level into ``data`` (a query API wraps its result there)."""
    if isinstance(doc, list) and doc and isinstance(doc[0], dict):
        doc = doc[0]
    if not isinstance(doc, dict):
        return set()
    keys = {str(k).lower() for k in doc}
    inner = doc.get("data")
    if isinstance(inner, dict):
        keys |= {str(k).lower() for k in inner}
    return keys


def _is_event(keys: set) -> bool:
    return bool(keys & _TIME_KEYS) and bool(keys & _EVENT_KEYS)


def _systemd_status(sample: str, lines: List[str]) -> bool:
    # One unit report: its header line, then indented fields and a journal
    # tail. The whole paste is the report.
    return bool(_SYSTEMD_HEAD.search(lines[0])) and any(
        _SYSTEMD_FIELD.search(line) for line in lines[1:6]
    )


def _diff_head(sample: str) -> bool:
    # `diff --git`, or a `--- <path>` line followed by a `+++ <path>` line.
    # Plain string tests: a diff's file header is not a frontmatter delimiter,
    # and that grammar's one definition is in faultmaven.utils.frontmatter.
    rows = sample.split("\n")
    for row, after in zip(rows, rows[1:] + [""]):
        if row.startswith("diff --git "):
            return True
        if (
            row.startswith("--- ")
            and row[4:5].strip()
            and after.startswith("+++ ")
            and after[4:5].strip()
        ):
            return True
    return False


def _diff(sample: str, lines: List[str]) -> bool:
    # A unified diff: file headers, a hunk, and mostly diff lines.
    return (
        _diff_head(sample)
        and bool(_HUNK.search(sample))
        and _share(lines, _DIFF_LINE.search) >= 0.5
    )


def _json_document(text: str) -> bool:
    # A JSON document is config only when its meaning is configuration: a log
    # event (or an array of them), a metrics query result or a trace is left
    # to the log, metrics and trace checks. Read up to JSON_PARSE_CHARS.
    text = text.strip()
    if not text or text[0] not in "{[":
        return False
    doc = _parse_json(text)
    if not isinstance(doc, (dict, list)):
        return False
    keys = _keys(doc)
    return not _is_event(keys) and not keys & _NOT_CONFIG_KEYS


def _json_lines_log(sample: str, lines: List[str]) -> bool:
    # One JSON object per line, each stamped with a time: structured logs
    # (audit logs, zap/logrus/bunyan JSON output).
    if len(lines) < 2:
        return False

    def stamped(line: str) -> bool:
        line = line.strip()
        if not line.startswith("{"):
            return False
        keys = _keys(_parse_json(line))
        # A time and an event key: a metrics or record dump with a timestamp
        # (``{"timestamp", "metric", "value"}``) is not a log.
        return bool(keys & _TIME_KEYS) and bool(keys & _LINE_EVENT_KEYS)

    return _share(lines, stamped) >= SHAPE_SHARE


def _log_lines(sample: str, lines: List[str]) -> bool:
    return len(lines) >= 2 and _share(lines, _is_log_line) >= SHAPE_SHARE


def _column_table(sample: str, lines: List[str]) -> bool:
    # ``kubectl get`` and other tools that print a header of capitalised
    # column names separated by two or more spaces, then rows as wide as that
    # header. A paste may hold several tables; each header opens its own.
    if not _TABLE_HEAD.search(lines[0]):
        return False
    width, in_table = 0, 0
    for line in lines:
        cells = len(re.split(r"\s{2,}", line.strip()))
        if _TABLE_HEAD.search(line):
            width = cells
            in_table += 1
        elif width and _COLUMNS.search(line) and cells == width:
            in_table += 1
    return len(lines) >= 2 and in_table / len(lines) >= SHAPE_SHARE


def _curl_verbose(sample: str, lines: List[str]) -> bool:
    # ``curl -v``: ``*`` connection notes, ``>`` request and ``<`` response
    # headers. A tool's trace for people, not timestamped events.
    return len(lines) >= 3 and _share(lines, _CURL_LINE.search) >= SHAPE_SHARE


def _key_value_block(sample: str, lines: List[str]) -> bool:
    # ``.env``, ``openssl x509 -noout -subject -dates``: ``KEY=value`` lines.
    # ``Approved=True <message>`` is a status message and logfmt is a log,
    # not a setting.
    if len(lines) < 2 or any(
        _STATUS_KV.search(line) or _is_log_line(line) for line in lines
    ):
        return False
    return _share(lines, _KEY_VALUE.search) >= SHAPE_SHARE


def _yaml_mapping(sample: str, lines: List[str]) -> bool:
    # A document a YAML parser reads as a mapping, written as one: keys with
    # a single space after the colon, nesting by indentation.
    # - Aligned ``Key:   value`` columns are a tool's report
    #   (``kubectl describe``), not YAML.
    # - A repeated top-level key is not a mapping the parser keeps whole: it
    #   silently drops all but the last (``error: …`` lines).
    # - A flat block of a few ``Name: value`` lines (an error summary) or of
    #   HTTP headers is a message, not a config document: flat config needs
    #   at least five keys, and not header-style ones.
    if len(lines) < 3 or any(_ALIGNED_FIELD.search(line) for line in lines):
        return False
    if _share(lines, _YAML_LINE.search) < SHAPE_SHARE:
        return False
    top = [m.group(1) for m in map(_TOP_KEY.match, lines) if m]
    if len(top) != len(set(top)):
        return False
    nested = any(line.startswith((" ", "\t", "- ")) for line in lines)
    headers = sum(1 for k in top if _HTTP_HEADER_KEY.match(k))
    if not nested and (len(top) < 5 or headers * 2 >= len(top)):
        return False
    try:
        import yaml

        loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
        doc = yaml.load(sample, Loader=loader)  # noqa: S506 - a safe loader
    except Exception:
        return False
    return isinstance(doc, dict) and len(doc) >= 1


#: Checked in order; the first rule that claims the paste names it.
_RULES: List[Tuple[str, DataType, Optional[Callable[[str, List[str]], bool]]]] = [
    ("systemd_status", DataType.COMMAND_OUTPUT, _systemd_status),
    ("diff", DataType.SOURCE_CODE, _diff),
    # The JSON rule reads its own, larger slice (see ``paste_shape``).
    ("json_document", DataType.STRUCTURED_CONFIG, None),
    ("json_lines_log", DataType.LOGS_AND_ERRORS, _json_lines_log),
    ("log_lines", DataType.LOGS_AND_ERRORS, _log_lines),
    ("column_table", DataType.COMMAND_OUTPUT, _column_table),
    ("curl_verbose", DataType.COMMAND_OUTPUT, _curl_verbose),
    ("key_value_block", DataType.STRUCTURED_CONFIG, _key_value_block),
    ("yaml_mapping", DataType.STRUCTURED_CONFIG, _yaml_mapping),
]


def paste_shape(content: str) -> Optional[Tuple[DataType, str]]:
    """The type and rule name of the shape that claims this paste, if any.
    Reads at most ``SAMPLE_CHARS`` of it."""
    sample = content[:SAMPLE_CHARS]
    lines = _lines(sample)
    if not lines:
        return None
    for name, data_type, matches in _RULES:
        if matches is None:
            claimed = _json_document(content[:JSON_PARSE_CHARS])
        else:
            claimed = matches(sample, lines)
        if claimed:
            return data_type, name
    return None
