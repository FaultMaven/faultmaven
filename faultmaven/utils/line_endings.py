"""The ONE line-ending decision for caller-supplied document text.

Markdown structure matching is anchored on line ends (``^## Causes$`` under
``re.MULTILINE``), and ``$`` matches at end-of-string or immediately before a
``\\n``. On a CRLF line the ``\\r`` sits between the heading text and the ``\\n``,
and a horizontal-whitespace class (``[ \\t]*``) does not consume it — so the
anchor never reaches ``$`` and the section reads as ABSENT. Measured on the 91
shipped runbooks before #1403: 91/91 validated under LF, **0/91** under CRLF,
mean quality score 90.5 -> 75.8, with 546 "Missing required section" errors on
documents carrying every one of them.

Fixing the matchers instead was considered and refused. ``[ \\t]*$`` is the
CORRECT narrow idiom after the ``\\s``-ReDoS work (#1395/#1406) — ``\\s`` matches
``\\n``, which is what made those patterns quadratic — so a per-matcher repair
asks every future author to write the ReDoS-safe spelling AND remember the
``\\r``, in a codebase that has already watched one grammar drift into nine
copies (#1404). Normalising at the boundary is one decision instead of N, and it
leaves ``runbook_grammar`` — a manual mirror of the kb-toolkit grammar, locked by
a frozen-literal test and an upstream cross-repo CI job — untouched.

WHY ``\\r\\n?`` AND NOT ``replace("\\r\\n", "\\n")``. Three reasons, and the first
is decisive: it is what the rest of the codebase already does. Every disk read
here goes through ``Path.read_text``, which is universal-newlines
(``newline=None``) and translates CR, CRLF and lone CR alike — that is why the
conversion pipeline was never affected by #1403 while the upload path was.
Second, CommonMark 2.1 defines a line ending as "a newline, a carriage return
not followed by a newline, or a carriage return and a following newline" —
exactly this. Third, the narrow form is NOT idempotent:
``"\\r\\r\\n".replace("\\r\\n", "\\n")`` is ``"\\r\\n"``, which still holds a CRLF.
A lone ``\\r`` is a line ending under all three authorities, so treating it as
data would be the divergence, not the safety.

``decode_text`` exists because the translation is FREE when fused into the
decode and expensive when bolted on afterwards. Measured on a 10 MB body:
plain ``.decode()`` 3.11 ms, fused 3.01 ms, ``.decode()`` + ``re.sub`` 11.3 ms;
on an all-CR body, fused 58.7 ms against 1618 ms — 28x. Peak memory is lower
too (20.0 MiB vs 38.9 MiB on 10 MiB of CRLF), because the two-pass form
materialises both the decoded string and the substituted one. Use it wherever
bytes become text; use ``normalize_line_endings`` only where the text already
IS a ``str`` (a JSON body field), which has no decode to fuse with.
"""

import io
import re
from typing import Optional

# Matched explicitly rather than left to ``\s``: ``\s`` also matches ``\n``, and
# a pattern that can consume the very character it is looking for is the shape
# that made the frontmatter grammar quadratic (#1395). See the module docstring
# for why a lone ``\r`` is in scope.
_LINE_ENDING_RE = re.compile(r"\r\n?")


def normalize_line_endings(text: str) -> str:
    """``text`` with CRLF and lone-CR line endings translated to ``\\n``.

    Idempotent, and the identity on text that is already LF — verified against
    all 91 shipped runbooks.

    The ``"\\r" not in text`` gate is load-bearing, not a micro-optimisation.
    It is what makes LAYERING affordable: this is applied at several boundaries
    so that no single forgotten call site can reintroduce #1403, and every layer
    downstream of the one that actually converted sees LF and pays only a
    ``memchr`` (0.68 ms on 10 MB) instead of a full scan. The expensive
    conversion happens once, at whichever boundary meets the CRLF first. Same
    fast-path reasoning as ``runbook_grammar.comment_spans``.
    """
    if "\r" not in text:
        return text
    return _LINE_ENDING_RE.sub("\n", text)


def decode_text(
    raw: bytes, encoding: str = "utf-8", errors: Optional[str] = "strict"
) -> str:
    """Decode ``raw`` the way ``Path.read_text`` does: universal newlines.

    The point is CONSISTENCY, not a new transformation. Every other path that
    turns bytes into document text in this codebase is a ``Path.read_text``, so
    it already normalises; the multipart upload route was the one place that
    called ``bytes.decode()`` and therefore didn't. That asymmetry is #1403's
    reachable half.

    Raises ``UnicodeDecodeError`` exactly as ``bytes.decode(encoding, errors)``
    does, so an encoding-fallback caller keeps working unchanged. A UTF-8 BOM is
    PRESERVED (only ``utf-8-sig`` strips one), which matters because
    ``document_preprocessor._LEADING_NOISE`` is what removes it and #1375 was
    reopened once by a BOM already.

    Decoding is incremental, so both a multi-byte character and a CRLF pair can
    straddle an internal buffer boundary; ``IncrementalDecoder`` holds the
    partial sequence and ``IncrementalNewlineDecoder`` holds a pending ``\\r``,
    which is pinned by test rather than assumed.
    """
    return io.TextIOWrapper(
        io.BytesIO(raw), encoding=encoding, errors=errors, newline=None
    ).read()
