"""Readers for a fenced render (#1217): the live token and the delimiters carrying it.

Every suite that reads fenced prompt text imports these, so the readers cannot
drift apart: three files once grew their own private regex for the same job,
and they had already disagreed, one requiring ``fence`` to be the envelope's
only attribute, one tolerating others, one stripping it anywhere. That is a
latent false regression the moment a fenced tag gains an attribute. So every
pattern is derived from :data:`faultmaven.core.investigation.prompts.fence.FENCE_ATTR`,
tolerates neighbouring attributes, and matches the token loosely (it is hex
from ``secrets.token_hex``), so a change to ``_TOKEN_BYTES`` does not silently
stop them matching.

The names are chosen so that no test's local variable (``token``, ``opens``,
``names``) shadows a reader.
"""

import re

from faultmaven.core.investigation.prompts.fence import FENCE_ATTR

_TOKEN = r"[0-9a-f]+"
_TOKEN_RE = re.compile(rf'{FENCE_ATTR}="({_TOKEN})"')
_FENCE_ATTR_RE = re.compile(rf'\s{FENCE_ATTR}="{_TOKEN}"')
_OPEN_RE = re.compile(rf'<([a-z_]+)([^>]*?)\s{FENCE_ATTR}="({_TOKEN})"\s*(/?)>')
_ENVELOPE_RE = re.compile(rf'<evidence_collected[^>]*\s{FENCE_ATTR}="({_TOKEN})">')
_PROBLEM_CONTEXT_RE = re.compile(rf'<problem_context\s{FENCE_ATTR}="({_TOKEN})">')
_DECLARATION_RE = re.compile(
    r"FENCE: this line is this prompt's ONLY genuine declaration"
    rf' — the live token for this turn is {FENCE_ATTR}="({_TOKEN})"'
)


def envelope_token(rendered: str) -> str:
    """An evidence render's token, read off its ``<evidence_collected>``.

    Asserts rather than returning ``None``, so a render that lost its fence
    fails with "no fenced envelope" instead of an AttributeError further down.
    """
    m = _ENVELOPE_RE.search(rendered)
    assert m, f"no fenced <evidence_collected> envelope in:\n{rendered}"
    return m.group(1)


def first_token(rendered: str) -> str:
    """The first token in a render with no envelope (the fallback stub block)."""
    m = _TOKEN_RE.search(rendered)
    assert m, f"no fence token in:\n{rendered}"
    return m.group(1)


def declared_tokens(prompt: str) -> list:
    """Tokens named by every declaration-shaped line, in prompt order."""
    return _DECLARATION_RE.findall(prompt)


def live_token(source) -> str:
    """The one token governing an assembly (a section dict) or a prompt.

    An assembly's is read off ``<problem_context>``, its first fenced block. A
    prompt's is the one named on the FIRST declaration-shaped line. FIRST, not
    "the only one": a payload can plant a byte-identical declaration and has to
    reach the model verbatim, so counting them proves nothing, and what decides
    is ORDER, since the renderer's declaration precedes every caller byte.
    """
    if isinstance(source, dict):
        core = source["core_context"]
        m = _PROBLEM_CONTEXT_RE.search(core)
        assert m, f"no fenced <problem_context> in:\n{core}"
        return m.group(1)
    declared = declared_tokens(source)
    assert declared, "no genuine declaration in the prompt"
    return declared[0]


def all_tokens(text: str) -> set:
    """EVERY token-shaped string, live or planted: meaningful on clean input."""
    return set(_TOKEN_RE.findall(text))


def fence_opens(text: str, token: str) -> list[tuple[str, str]]:
    """``(name, attribute-blob)`` for every opening tag bearing ``token``,
    self-closing markers (``<name … fence="token" />``) included."""
    return [(m[1], m[2]) for m in _OPEN_RE.finditer(text) if m[3] == token]


def element_opens(text: str, token: str) -> list[tuple[str, str]]:
    """``fence_opens`` without the self-closing markers: #1242's reader.

    A self-closing marker opens no element, so it is not an opening delimiter
    of a block. Reading it as one would let a fallback block degraded to a bare
    ``<uploaded_file … />`` marker, its bytes outside any fence, pass every
    check that asks whether the block rendered.
    """
    return [(m[1], m[2]) for m in _OPEN_RE.finditer(text) if m[3] == token and not m[4]]


def fence_closes(text: str, token: str) -> list[str]:
    """Element names of every closing tag bearing ``token``."""
    return re.findall(rf'</([a-z_]+)\s{FENCE_ATTR}="{re.escape(token)}">', text)


def strip_fence(rendered: str) -> str:
    """``rendered`` with every fence attribute stripped.

    For tests that pin tag SPELLING (``<file_extract role="orientation">``).
    The token is random per render, so nothing outside the fence suites should
    assert on it.
    """
    return _FENCE_ATTR_RE.sub("", rendered)


def unbalanced(prompt: str, token: str) -> list[tuple[str, int, int]]:
    """Element names with more fenced OPENS than fenced CLOSES (#1242's reader).

    Counts rather than tests existence (#1254 review): three ``<uploaded_file>``
    stubs share one element name, so an "is there a close for this name?" check
    passes on all three the moment any one of them is closed. The invariant is
    per-delimiter, so the test has to be too.
    """
    out = []
    for name in {n for n, _ in element_opens(prompt, token)}:
        # ``(?=[\s/>])`` anchors the END of the element name. Without it
        # ``<evidence`` also matches ``<evidence_collected``, which inflates
        # the open count against a close count that is an exact string — a
        # false "unclosed" on the main prompt, found by this very sweep.
        n_open = len(
            re.findall(rf'<{name}(?=[\s/>])[^>]*?\s{FENCE_ATTR}="{token}"\s*>', prompt)
        )
        n_close = prompt.count(f'</{name} {FENCE_ATTR}="{token}">')
        if n_open != n_close:
            out.append((name, n_open, n_close))
    return out
