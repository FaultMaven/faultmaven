"""No module-private name may survive into a template as a ``.format()`` key.

These templates are plain string literals rendered later with ``.format(**ctx)``,
so a ``{name}`` inside one is a RUNTIME context key, not a value. Writing
``{_SOME_CONSTANT}`` next to the f-string blocks in the same file reads as
interpolation and is not: the constant is never substituted, the key is not in
``ctx``, and the template either renders the brace text verbatim or raises
``KeyError`` — depending on which renderer reaches it first.

The distinction is invisible on inspection because both forms live in this one
module, so the rule is enforced rather than remembered: a definition-time value
is concatenated, never braced. Keys that ARE runtime context are lowercase by
convention; a leading underscore marks a module-private, which can only ever be
a definition-time value.
"""

from __future__ import annotations

import string

import pytest

from faultmaven.core.investigation.prompts import templates as t

pytestmark = pytest.mark.unit

_TEMPLATE_NAMES = [
    n for n in dir(t) if n.endswith("TEMPLATE") or n.endswith("INSTRUCTIONS")
]


def _format_keys(text: str) -> set[str]:
    return {f[1] for f in string.Formatter().parse(text) if f[1]}


def test_templates_exist_to_sweep():
    """Guard the guard: a renamed suffix would silently empty the sweep."""
    assert len(_TEMPLATE_NAMES) >= 5


@pytest.mark.parametrize("name", _TEMPLATE_NAMES)
def test_no_module_private_name_is_a_format_key(name):
    value = getattr(t, name)
    if not isinstance(value, str):
        pytest.skip(f"{name} is not a string")
    leaked = {k for k in _format_keys(value) if k.startswith("_")}
    assert not leaked, (
        f"{name} braces module-private name(s) {sorted(leaked)} — these are "
        "definition-time values and must be concatenated, not braced"
    )
