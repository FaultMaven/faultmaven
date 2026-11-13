"""Content-shape rules for pasted evidence (#2361).

A paste's filename is minted by the client, so the classifier types it by
content alone. These rules recognise the shapes engineers paste most —
tool reports and tables, config documents, timestamped log lines, metrics
expositions, diffs — and name each with the one detailed type the #2361
taxonomy assigns it (2026-11-13):

- ``STRUCTURED_CONFIG`` only for a document a config parser reads (JSON, a
  YAML mapping, a ``KEY=value`` block);
- ``COMMAND_OUTPUT`` for a report or table a tool prints for people
  (``systemctl status``, ``kubectl get``, ``curl -v``);
- ``LOGS_AND_ERRORS`` for timestamped event lines;
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
extractor cannot read it (a whitespace metrics table, #2791) has no rule.
"""

from __future__ import annotations

import json
import re
from typing import Callable, List, Optional, Tuple

from faultmaven.models.api import DataType

#: The share of a paste's non-empty lines a shape must account for.
SHAPE_SHARE = 2 / 3

#: Confidence of a shape verdict: above the clarification threshold (0.50),
#: below auto-accept (0.85) — a shape match, not a signature.
SHAPE_CONFIDENCE = 0.80

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
_LOG_LINE = re.compile(
    r"^(?:\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*\]?"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})"
    r"\s+.*\b(trace|debug|info|notice|warn|warning|error|err|fatal|critical|crit)\b"
    # logfmt: ``time=<ts> level=<level> msg=…``
    r"|^time=\S+\s+level=\w+",
    re.IGNORECASE,
)
_CURL_LINE = re.compile(r"^[*<>] |^[{}] \[\d+ bytes data\]")
_DIFF_LINE = re.compile(
    r"^(?:diff --git |index [0-9a-f]+\.\.|--- |\+\+\+ |@@ -\d|[+\- ])"
)
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", re.M)


def _lines(content: str) -> List[str]:
    return [line for line in content.splitlines() if line.strip()]


def _share(lines: List[str], pattern: re.Pattern) -> float:
    return sum(1 for line in lines if pattern.search(line)) / len(lines)


def _systemd_status(lines: List[str]) -> bool:
    # One unit report: its header line, then indented fields and a journal
    # tail. The whole paste is the report.
    return bool(_SYSTEMD_HEAD.search(lines[0])) and any(
        _SYSTEMD_FIELD.search(line) for line in lines[1:6]
    )


def _column_table(lines: List[str]) -> bool:
    # ``kubectl get``, ``ps``, ``df``: a header of capitalised column names
    # separated by two or more spaces, then rows as wide as that header. A
    # paste may hold several tables; each header opens its own.
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


def _curl_verbose(lines: List[str]) -> bool:
    # ``curl -v``: ``*`` connection notes, ``>`` request and ``<`` response
    # headers. A tool's trace for people, not timestamped events.
    return len(lines) >= 3 and _share(lines, _CURL_LINE) >= SHAPE_SHARE


def _json_document(content: str) -> bool:
    text = content.strip()
    if not text or text[0] not in "{[":
        return False
    try:
        return isinstance(json.loads(text), (dict, list))
    except ValueError:
        return False


def _yaml_mapping(content: str, lines: List[str]) -> bool:
    # A document a YAML parser reads as a mapping, written as one: keys with
    # a single space after the colon, nesting by indentation. Aligned
    # ``Key:   value`` columns are a tool's report (``kubectl describe``), not YAML.
    if any(re.search(r"^\s*[A-Z][\w ./()-]*:\s{2,}\S", line) for line in lines):
        return False
    yaml_line = re.compile(r"^\s*(?:- )?[\w.\-/\"']+:(?: \S.*)?$|^\s*- \S")
    if len(lines) < 3 or _share(lines, yaml_line) < SHAPE_SHARE:
        return False
    try:
        import yaml

        doc = yaml.safe_load(content)
    except Exception:
        return False
    return isinstance(doc, dict) and len(doc) >= 1


def _key_value_block(lines: List[str]) -> bool:
    # ``.env``, ``openssl x509 -noout -subject -dates``: ``KEY=value`` lines.
    # ``Approved=True <message>`` is a status message, not a setting.
    if len(lines) < 2 or any(
        _STATUS_KV.search(line) or _LOG_LINE.search(line) for line in lines
    ):
        return False
    return _share(lines, _KEY_VALUE) >= SHAPE_SHARE


def _log_lines(lines: List[str]) -> bool:
    return len(lines) >= 2 and _share(lines, _LOG_LINE) >= SHAPE_SHARE


def _diff(content: str, lines: List[str]) -> bool:
    return bool(_HUNK.search(content)) and _share(lines, _DIFF_LINE) >= 0.5


#: Checked in order; the first rule that claims the paste names it.
_RULES: List[Tuple[str, DataType, Callable[[str, List[str]], bool]]] = [
    ("systemd_status", DataType.COMMAND_OUTPUT, lambda c, ls: _systemd_status(ls)),
    ("diff", DataType.SOURCE_CODE, _diff),
    ("json_document", DataType.STRUCTURED_CONFIG, lambda c, ls: _json_document(c)),
    ("log_lines", DataType.LOGS_AND_ERRORS, lambda c, ls: _log_lines(ls)),
    ("column_table", DataType.COMMAND_OUTPUT, lambda c, ls: _column_table(ls)),
    ("curl_verbose", DataType.COMMAND_OUTPUT, lambda c, ls: _curl_verbose(ls)),
    ("key_value_block", DataType.STRUCTURED_CONFIG, lambda c, ls: _key_value_block(ls)),
    ("yaml_mapping", DataType.STRUCTURED_CONFIG, _yaml_mapping),
]


def paste_shape(content: str) -> Optional[Tuple[DataType, str]]:
    """The type and rule name of the shape that claims this paste, if any."""
    lines = _lines(content[:20000])
    if not lines:
        return None
    for name, data_type, matches in _RULES:
        if matches(content, lines):
            return data_type, name
    return None
