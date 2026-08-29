"""Shared readers for the prompt fence (#1217).

Three test files grew their own private regex for the same job and had already
drifted apart — one required ``fence`` to be the envelope's only attribute, one
tolerated others, one stripped it anywhere. That is a latent false regression
the moment a fenced tag gains an attribute, so the readers live here and are
derived from :data:`faultmaven.core.investigation.prompts.fence.FENCE_ATTR`
rather than from a literal spelled out per file.

Exposed as one ``fence_read`` fixture rather than importable functions: this
directory is not a package, so a dotted import of a sibling module would not
resolve.
"""

import re
from types import SimpleNamespace

import pytest

from faultmaven.core.investigation.prompts.fence import FENCE_ATTR

#: The token is hex from ``secrets.token_hex``; kept loose so a change to
#: ``_TOKEN_BYTES`` does not silently stop these matching.
_TOKEN = r"[0-9a-f]+"

#: A fence attribute anywhere — deliberately tolerant of neighbours, so a tag
#: that gains an attribute later does not fall out of these readers.
FENCE_ATTR_RE = re.compile(rf'\s{FENCE_ATTR}="{_TOKEN}"')
_ENVELOPE_RE = re.compile(rf'<evidence_collected[^>]*\s{FENCE_ATTR}="({_TOKEN})">')


def _token(rendered: str) -> str:
    """This render's fence token, read off the ``<evidence_collected>`` envelope.

    Asserts rather than returning ``None``, so a render that lost its fence
    fails with "no fenced envelope" instead of an AttributeError further down.
    """
    m = _ENVELOPE_RE.search(rendered)
    assert m, f"no fenced <evidence_collected> envelope in:\n{rendered}"
    return m.group(1)


def _any_token(rendered: str) -> str:
    """The first fence token in a render with no ``<evidence_collected>``.

    For the fallback stub block, which mints its own fence and has no envelope.
    """
    m = re.search(rf'{FENCE_ATTR}="({_TOKEN})"', rendered)
    assert m, f"no fence token in:\n{rendered}"
    return m.group(1)


def _opens(rendered: str, token: str) -> list[tuple[str, str]]:
    """``(name, attribute-blob)`` for every opening tag bearing the live fence."""
    pattern = re.compile(
        rf'<([a-z_]+)([^>]*?)\s{FENCE_ATTR}="{re.escape(token)}"\s*/?>'
    )
    return [(m.group(1), m.group(2)) for m in pattern.finditer(rendered)]


def _closes(rendered: str, token: str) -> list[str]:
    pattern = re.compile(rf'</([a-z_]+)\s{FENCE_ATTR}="{re.escape(token)}">')
    return pattern.findall(rendered)


def _unfenced(rendered: str) -> str:
    """``rendered`` with every fence attribute stripped.

    For tests that pin tag SPELLING (``<file_extract role="orientation">``).
    The token is random per render, so nothing outside the fence suites should
    assert on it.
    """
    return FENCE_ATTR_RE.sub("", rendered)


@pytest.fixture
def fence_read():
    """Readers for a fenced render — ``token``, ``any_token``, ``opens``,
    ``closes``, ``unfenced``."""
    return SimpleNamespace(
        token=_token,
        any_token=_any_token,
        opens=_opens,
        closes=_closes,
        unfenced=_unfenced,
    )
