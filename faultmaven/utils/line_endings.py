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

WHY A LONE ``\\r`` IS IN SCOPE, and not only CRLF. Two reasons, and the first is
decisive: it is what the rest of the codebase already does. Every disk read here
goes through ``Path.read_text``, which is universal-newlines (``newline=None``)
and translates CR, CRLF and lone CR alike — that is why the conversion pipeline
was never affected by #1403 while the upload path was. Second, CommonMark 2.1
defines a line ending as "a newline, a carriage return not followed by a
newline, or a carriage return and a following newline" — the same set. A single
``replace("\\r\\n", "\\n")`` covers neither, and is not even idempotent:
``"\\r\\r\\n"`` becomes ``"\\r\\n"``, which still holds a CRLF. See
``normalize_line_endings`` for the two-pass form that does, and why it is
preferred over the equivalent regex.

The cost of treating it as a line ending is real and accepted: a lone ``\\r``
that is DATA rather than a terminator — a raw HTTP request quoted in a fenced
block (RFC 9112 2.2), a ``printf 'progress\\r'`` example — is rewritten along
with the rest. Excluding fenced spans was considered and refused: the chunker
and every ``^...$`` matcher treat a fence's line ends as line ends too, so a
document normalised everywhere EXCEPT inside fences would be one where the gate
and the chunker disagree again, which is the failure this exists to remove. An
author who needs a literal CR in an example should write the two-character
escape ``\\r``, which is untouched.

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
from typing import Optional


def normalize_line_endings(text: str) -> str:
    """``text`` with CRLF and lone-CR line endings translated to ``\\n``.

    Idempotent, and the identity on text that is already LF — verified against
    all 91 shipped runbooks.

    TWO ``str.replace`` PASSES, NOT A REGEX. ``re.sub(r"\\r\\n?", "\\n", text)``
    is the obvious spelling and is byte-equivalent (checked over 20 000 random
    strings drawn from ``{a, b, \\r, \\n, \\r\\n, \\r\\r\\n, \\n\\r}``), but it is
    an order of magnitude slower on the shapes that matter: 10 MB of ``\\r``
    costs 2012 ms against 55 ms, and 9.5 MB of CRLF 1130 ms against 121 ms.
    That is real, because several of these call sites run SYNCHRONOUSLY on the
    event loop over caller-sized bodies, next to a gate that was deliberately
    moved to a thread for 31.5 ms of CPU.

    The order is load-bearing and the pair is not the same as the single
    ``replace("\\r\\n", "\\n")`` the module docstring rejects: CRLF is collapsed
    first, so the second pass sees only the ``\\r`` that were never part of a
    pair. After both, no ``\\r`` remains anywhere, which is what makes it
    idempotent where the single form is not.

    The ``"\\r" not in text`` gate is load-bearing too, not a micro-optimisation.
    It is what makes LAYERING affordable: this is applied at several boundaries
    so that no single forgotten call site can reintroduce #1403, and every layer
    downstream of the one that actually converted sees LF and pays only a
    ``memchr`` instead of two passes. The expensive conversion happens once, at
    whichever boundary meets the CRLF first. Same fast-path reasoning as
    ``runbook_grammar.comment_spans``.

    A non-``str`` is returned UNCHANGED rather than raising. One caller reaches
    here from an untyped ``dict`` request body (``PUT /knowledge/documents/{id}``
    passes ``update_data`` straight through), where ``content`` may be any JSON
    scalar; ``RedactionService.sanitize`` downstream accepts ``int``/``float``/
    ``bool`` and stringifies the rest, so raising here would turn a value it used
    to handle into a 500. Without the guard the failure is also asymmetric in a
    way that hides it — ``"\\r" not in {...}`` is a KEY test, so ``dict`` and
    ``list`` bodies pass silently while ``int`` raises.
    """
    if not isinstance(text, str) or "\r" not in text:
        return text
    return text.replace("\r\n", "\n").replace("\r", "\n")


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
