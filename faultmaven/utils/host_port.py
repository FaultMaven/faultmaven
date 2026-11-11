"""The host-and-port pattern the logs evidence paths and ``search_file`` read.

The logs extractor's profile and the entity registry read it in logs evidence
(``modules/preprocessing``), and ``search_file``'s vocabulary scan
(``modules/agent/tools/search_file_tool.py``) reads it inline over the first
``SearchFileTool._VOCAB_SCAN_CHARS`` characters of an evidence file (#2723).
"""

import re

HOST_PORT_RE = re.compile(
    r"(?<![\w.-])(?=[\w.-]*?[A-Za-z.])[\w.-]++"
    r"(?<!\d{4}-\d\d-\d\d[Tt]\d\d)(?<!\d{4}-\d{3}[Tt]\d\d)(?<!\d{8}[Tt]\d\d)"
    r":([0-9]{1,5})(?!\w|\.[0-9])"
)
"""A port after a colon that follows a ``[\\w.-]`` run with an ASCII letter or dot.

``db.local:5432`` and ``10.0.0.5:22`` match. A digits-only run (``04:47`` in
a syslog time) does not. Excluded (fm#2677): a run ending in an ISO-8601 date's
``T`` and hour, the date whole (extended ``2024-01-15T06``, ordinal
``2024-015T06``, compact ``20240115T06``); a number followed by ``.`` and a
digit, so a dotted version tag (``nginx:1.25``, ``python:3.11-slim``); and
non-ASCII digits. A rejected reading can leave the real port behind it:
``ipv4:10.0.0.5:51234`` gives ``51234``.

Still read as ports, because each rule found for them also reads a real host
or drops a real port (``127.0.0.1:5432:5432``, ``zk1:2888:3888``,
``web-01:80``, ``docs.rs:443``): a ``line:col`` tail (``index.js:10:5`` gives
``10``); an undated time (``T06:55:45Z``); a UTC offset (``…45.123-03:30``
gives ``30``); a slash, dot or underscore date (``2024/01/15T06:55``); an
undotted image tag (``postgres:16``, ``postgres:16-alpine``, ``redis:7``); a
``key:value`` label (``status:200``); auditd's ``audit(…:N)``; logback's
``%logger:%line``; MAC address pairs after one with a letter
(``00:0c:29:…`` gives ``29``); sshd ``SHA256:`` fingerprints; and a bare
``name.ext:line`` (``app.py:118``). A bracketed IPv6 port (``[::1]:8080``) is
not read (fm#1573).

The cost is linear because of the first lookbehind and the lookahead together.
The lookbehind lets a match start only at the beginning of a run, and the lazy
lookahead scans that run once. The possessive ``++`` adds nothing: a plain
``+`` measures the same. The old ``[\\w-]*[A-Za-z.][\\w.-]*`` tried every
split of a long run without a colon: 23 s on a 64 KB hex run (fm#2641).

In logs evidence, ``PORT_KEYWORD_RE`` and this pattern are the one port rule
of the logs extractor's profile and the entity registry. Command output pairs
``PORT_KEYWORD_RE`` with an IP-and-port pattern of its own, and config evidence
has its own port pattern. ``PORT_KEYWORD_RE`` is in
``modules/preprocessing/extractors/utils.py``.
"""
