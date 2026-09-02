"""Canonical v4 runbook (causal-chain) markdown **parse** grammar — the single
in-repo source for turning a ``## Causes`` section into structured records.

This module mirrors the upstream producer grammar
(``kb_toolkit/core/runbook_grammar.py``) verbatim: the regexes and sub-field
parser here are byte-for-byte the ones the KB pack builder uses to enumerate
each runbook's Causes. The in-repo consumer is the validator gate: anchoring it
on this shared grammar keeps what the gate passes identical to what the
toolkit parses. (An in-repo extractor that turned the section into a
``metadata["causes"]`` record for the KB cause seeder went with the seeder,
fm#1295.)

LAYER NOTE — three grammar surfaces exist and must agree:

  1. ``cause_grammar`` — the AUTHORING vocabulary (sub-field labels, quadrant
     tags, fallback token), a manual mirror of the kb-toolkit config defaults;
  2. this module — the PARSE grammar (regexes + ``parse_cause_subfields``), a
     manual mirror of ``kb_toolkit/core/runbook_grammar.py``;
  3. ``runbook_validator`` — the GATE. It anchors its cause ENUMERATION and
     sub-field parsing on THIS module (the same ``CAUSE_HEADING_RE`` /
     ``CAUSES_SECTION_RE`` / ``parse_cause_subfields`` the toolkit uses), so a
     draft the gate passes is exactly one the toolkit can parse — the gate can
     no longer be looser than the parser it fronts. Only its message-oriented
     present-vs-empty wording is validator-private; the grammar is shared.

The two repos cannot import one another, so (2) is a **manual mirror**. Three
guards keep it honest. One is in-repo and sees only this checkout:
``test_runbook_grammar`` (frozen-literal drift-guard — trips if a pattern here is
edited without updating the test). It cannot see the upstream grammar, so a
corpus-invariant change upstream (e.g. a widened regex) slips it. The other
guard closes that: kb-toolkit's
``scripts/check_grammar_cross_repo.py`` CI job checks out BOTH repos and compares
the shared regex primitives (pattern + flags) and ``CONVERGES_REF`` across the
two ``runbook_grammar.py`` files. A change to the upstream grammar MUST be
mirrored here and vice-versa.

COMMENT BLINDNESS (#1241): every regex here matches inside an HTML comment as
readily as outside one, so a sub-field label or a Cause heading written as a
commented-out worked example is parsed as real content — an empty runbook that
passes the gate, or a fabricated one whose whole ``## Causes`` section is
commented out. The countermeasure is ONE function, ``mask_html_comments``, and
ONE enumerator, ``iter_cause_blocks``, that every consumer routes through.
Nothing else in the codebase decides what a comment is.

MIRROR NOTE. ``scripts/check_grammar_cross_repo.py`` upstream compares an
EXPLICIT allowlist of symbols (``_REGEX_SYMBOLS``) and hard-exits when a listed
symbol is missing from either file — name shape is irrelevant, so a private
name is not "excluded" from it. That allowlist is therefore the lever: adding
``CODE_FENCE_LINE_RE`` and ``CODE_SPAN_RE`` to it makes a mirror that lacks the
comment countermeasure fail upstream CI rather than pass quietly. Both are
named publicly here so that addition is a one-line change there.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CauseBlock:
    """One ``### Cause X:`` block, in both the views its consumers need.

    ``body`` is comment-MASKED and post-heading: what the sub-field parser and
    every content check read. ``raw_block`` is verbatim from the heading line
    through the block terminus, comments included: what the retrieval-chunk gate
    measures, because ``ContentChunker`` sees the raw markdown. They are the
    same length as their raw/masked counterparts, so offsets agree.
    """

    letter: str
    name: str
    body: str
    raw_block: str


# The ``## Causes`` H2 section body — everything up to the next H2 or EOF.
CAUSES_SECTION_RE = re.compile(r"(?ms)^##\s+Causes\s*\n(.*?)(?=^##\s+|\Z)")

# A ``### Cause X: <name>`` heading inside a markdown block (captures letter + name).
CAUSE_HEADING_RE = re.compile(r"^### Cause ([A-Z]):\s*(.+?)\s*$", re.MULTILINE)

# A ``### Step N: <title>`` diagnostic-step heading — the step numbers an
# Indicator's ``[Step N]`` token must resolve to.
STEP_HEADING_RE = re.compile(r"^### Step (\d+):", re.MULTILINE)

# An Indicator token: ``[Step N]`` (an operator step), ``[Symptom]`` (an observed
# condition), or ``[Default]`` (the fallback Cause marker).
INDICATOR_TOKEN_RE = re.compile(r"\[(Step \d+|Symptom|Default)\]")

# A ``[Step N]`` reference inside an Indicator entry (captures N so the validator
# can resolve it against ``## Diagnostic Steps``).
STEP_REF_RE = re.compile(r"\[Step (\d+)\]")

# The shape of an HTML comment (``<!-- match: ... -->`` directives etc.). Used to
# RECOGNISE one; where a comment may legally start is decided by the code-aware
# scan below, not by this pattern alone.
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# A fenced code block's opening/closing line (``` or ~~~, up to 3 spaces indent).
# Runbooks put operator commands in fences, and a command may legitimately print
# or contain ``<!--``; text inside a fence is never a comment.
CODE_FENCE_LINE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")

# An inline code span (`` `x` ``, ``` ``x`` ``` …). Same reason: a Statement that
# quotes ``<!--`` in backticks is describing markup, not opening a comment.
CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.DOTALL)

# Every non-newline character of a comment span, for the length-preserving mask.
COMMENT_BODY_CHAR_RE = re.compile(r"[^\n]")

# A v4 intervention bullet: ``- **<quadrant>** (<ref>): ...`` (indent-tolerant).
INTERVENTION_RE = re.compile(
    r"^\s*[-*+]\s*\*\*([A-Za-z_]+)\*\*\s*\(([^)]*)\)", re.MULTILINE
)

# A v4 chain / indicator rung: ``- <ref>: <statement>`` (ref = root|sN|D, or the
# ``converges`` directive). Indent-tolerant.
CHAIN_RUNG_RE = re.compile(
    r"^\s*[-*+]\s*([A-Za-z][A-Za-z0-9]*)\s*:\s*(.*)$", re.MULTILINE
)

# The ``converges: <Cause>.<ref>`` directive — a rung that maps onto another
# Cause's node rather than introducing a new one.
CONVERGES_REF = "converges"


def _protected_spans(text: str) -> list[tuple[int, int]]:
    """Character ranges that are markdown CODE — fenced blocks and inline spans.

    A comment cannot start or end inside one. Without this the mask is a plain
    ``<!--.*?-->`` sweep, and two measured regressions follow (#1241 review): a
    Statement quoting ``` `<!--` ``` loses the rest of its text, and literal
    tokens in two different Causes blank everything between them, deleting the
    second Cause's heading outright.
    """
    spans: list[tuple[int, int]] = []
    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    running = 0
    for line in lines:
        offsets.append(running)
        running += len(line)

    i = 0
    while i < len(lines):
        opener = CODE_FENCE_LINE_RE.match(lines[i])
        if not opener:
            i += 1
            continue
        marker, width = opener.group(1)[0], len(opener.group(1))
        j = i + 1
        while j < len(lines):
            closer = CODE_FENCE_LINE_RE.match(lines[j])
            if (
                closer
                and closer.group(1)[0] == marker
                and len(closer.group(1)) >= width
            ):
                break
            j += 1
        # An unclosed fence runs to EOF, which is how markdown renders it too.
        end = offsets[j] + len(lines[j]) if j < len(lines) else len(text)
        spans.append((offsets[i], end))
        i = j + 1

    def in_fence(pos: int) -> bool:
        return any(lo <= pos < hi for lo, hi in spans)

    for m in CODE_SPAN_RE.finditer(text):
        if not in_fence(m.start()):
            spans.append(m.span())
    return spans


def comment_spans(text: str) -> list[tuple[int, int]]:
    """``(start, end)`` of every HTML comment that is really a comment.

    Openers and closers inside code are skipped on BOTH sides, so a real comment
    is not truncated early by a ``-->`` someone quoted in backticks. An opener
    with no closer outside code is left alone entirely — markdown would render
    the rest of the document as text, and swallowing it is the one failure mode
    worse than not masking at all.
    """
    # Fast path, and the one that matters: no opener means no comment, and the
    # code scan below is the expensive half. Every shipped runbook takes it.
    if "<!--" not in text:
        return []

    protected = _protected_spans(text)

    def guarded(pos: int) -> bool:
        return any(lo <= pos < hi for lo, hi in protected)

    spans: list[tuple[int, int]] = []
    cursor = 0
    while True:
        start = text.find("<!--", cursor)
        if start < 0:
            return spans
        if guarded(start):
            cursor = start + 4
            continue
        probe = start + 4
        while True:
            end = text.find("-->", probe)
            if end < 0 or not guarded(end):
                break
            probe = end + 3
        if end < 0:
            return spans
        spans.append((start, end + 3))
        cursor = end + 3


def mask_html_comments(text: str) -> str:
    """Blank every HTML comment *in place*, preserving length and newlines.

    THE single decision about what a comment is; every consumer routes here.

    Masking rather than deleting is load-bearing twice over. For the
    ENUMERATION callers, the validator measures each Cause block against
    ``ContentChunker``'s size bounds and split-boundary pattern and the chunker
    sees raw markdown, so deleting would shrink the block the gate measures
    below the block retrieval actually chunks. And deleting JOINS the text
    either side: a comment spanning a line break splices the next line onto the
    previous one, which measurably made a Cause heading stop being a heading —
    dropped by the enumerator AND invisible to
    ``_flag_malformed_cause_headings``, the guard whose whole job is to turn a
    silent drop into an error. A same-length mask keeps every offset and line
    number identical to the raw text, so callers locate headings in the masked
    copy and slice the RAW text at them. On a document with no comments it is
    the identity.
    """
    spans = comment_spans(text)
    if not spans:
        return text
    out: list[str] = []
    previous = 0
    for start, end in spans:
        out.append(text[previous:start])
        out.append(COMMENT_BODY_CHAR_RE.sub(" ", text[start:end]))
        previous = end
    out.append(text[previous:])
    return "".join(out)


def causes_section(content: str) -> tuple[str, str]:
    """``(raw_body, masked_body)`` of the ``## Causes`` section, or ``("", "")``.

    The section is located in the MASKED document, not the raw one — a comment
    that opens BEFORE the ``## Causes`` heading would otherwise leave the whole
    section live, which measured as a full gate bypass: a well-formed runbook
    with its entire Causes section commented out returned ``passed=True`` and
    seeded both of its "causes". Because the mask preserves length, the span
    indexes into the raw text unchanged, so the raw body is exact.
    """
    match = CAUSES_SECTION_RE.search(mask_html_comments(content))
    if not match:
        return "", ""
    lo, hi = match.span(1)
    return content[lo:hi], match.group(1)


def iter_cause_blocks(content: str) -> list["CauseBlock"]:
    """Every strict ``### Cause X:`` block in ``## Causes`` — the ONE walk.

    The one place the head->terminus walk lives: the gate (``runbook_validator``)
    delegates to it. Folding the walk here is what stops the
    comment decision being re-made per call site: #1241 escaped a per-site
    repair twice — once at the chunk-stamping path, once at a comment opening
    before the section heading — because each site owned its own masking.

    Each block carries BOTH views, and which one a caller wants is a real
    choice: ``body`` is masked (what parsers must read) while ``raw_block`` is
    verbatim including comments (what the chunk-size gate must measure, because
    that is what the chunker sees).
    """
    raw_body, masked_body = causes_section(content)
    if not masked_body:
        return []
    heads = list(CAUSE_HEADING_RE.finditer(masked_body))
    blocks: list[CauseBlock] = []
    for index, head in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(masked_body)
        blocks.append(
            CauseBlock(
                letter=head.group(1),
                name=head.group(2).strip(),
                body=masked_body[head.end() : end],
                raw_block=raw_body[head.start() : end],
            )
        )
    return blocks


def parse_cause_subfields(cause_content: str, field_names: list[str]) -> dict[str, str]:
    """Parse ``**Field:**`` blocks out of a Cause body.

    Each field's value runs until the next ``**Field:**`` label (drawn from
    ``field_names``) or end of content. Callers pass the schema's sub-field set
    (``required + optional`` from :mod:`cause_grammar`) so the boundary set stays
    in lockstep with the validator's contract.

    HTML comments are MASKED first, before any label is looked for (#1241).
    Without that a ``**Statement:**`` written inside a comment is read as the
    real sub-field: measured, a hint comment spelling the labels as examples
    made a Cause whose three required sub-fields were all EMPTY pass the
    publication gate. Callers that came through ``iter_cause_blocks`` are
    already masked and this is then the identity; it stays here because the
    function is exported and mirrored, so a caller that is not must still be
    safe. Masked, not deleted, for the reason ``mask_html_comments`` gives —
    deletion splices the text either side together and eats quoted markup.
    """
    cause_content = mask_html_comments(cause_content)
    fields: dict[str, str] = {}
    for field in field_names:
        others = "|".join(re.escape(f"**{f}:**") for f in field_names if f != field)
        pattern = (
            rf"\*\*{re.escape(field)}:\*\*\s*(.*?)(?=(?:{others})|\Z)"
            if others
            else rf"\*\*{re.escape(field)}:\*\*\s*(.*?)\Z"
        )
        m = re.search(pattern, cause_content, re.DOTALL)
        if m:
            fields[field] = m.group(1).strip()
    return fields
