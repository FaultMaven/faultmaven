"""First-party clients skip the consent screen; everything else does not.

Consent is a trust-boundary question — it lets a user refuse a THIRD PARTY
access to their data. The browser extension is FaultMaven's own client, and the
cases it asks to read are the ones the user wrote through it. Prompting there
informs nobody, and a consent screen that never means anything trains users to
click past the one that eventually does.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from faultmaven.modules.auth.api.oauth import _is_first_party


def _settings(first_party):
    return SimpleNamespace(auth=SimpleNamespace(oauth_first_party_clients=first_party))


@pytest.mark.unit
def test_shipped_client_is_first_party():
    assert _is_first_party("faultmaven-copilot", _settings(["faultmaven-copilot"]))


@pytest.mark.unit
@pytest.mark.parametrize(
    "client_id",
    [
        "faultmaven-copilot-evil",
        "evil-faultmaven-copilot",
        "faultmaven-",
        "FAULTMAVEN-COPILOT",
        "",
    ],
)
def test_membership_is_exact_not_prefix_or_fuzzy(client_id):
    """``client_id`` is caller-supplied. Anything looser than exact membership —
    a prefix test, a case-insensitive compare — lets an attacker skip the prompt
    by naming itself convincingly."""
    assert not _is_first_party(client_id, _settings(["faultmaven-copilot"]))


@pytest.mark.unit
def test_empty_allowlist_prompts_for_everything():
    """The list narrows the screen; it must never be the thing that disables it."""
    assert not _is_first_party("faultmaven-copilot", _settings([]))


# --------------------------------------------------------------------------- #
# Redirect allowlist: the chromiumapp form is the one that cannot be spoofed
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.parametrize(
    ("uri", "allowed"),
    [
        ("https://abcdefghijklmnopabcdefghijklmnop.chromiumapp.org/", True),
        ("https://abcdefghijklmnopabcdefghijklmnop.chromiumapp.org", True),
        # Not an extension-derived host: the whole point of the pattern.
        ("https://evil.chromiumapp.org.attacker.test/", False),
        ("https://chromiumapp.org/", False),
        ("http://abcdefghijklmnopabcdefghijklmnop.chromiumapp.org/", False),
        # Extension ids are a-p only; anything else is not one.
        ("https://zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz.chromiumapp.org/", False),
        ("https://abcdefghijklmnopabcdefghijklmnop.chromiumapp.org/steal", False),
        # Firefox's identity redirect: a 40-hex digest, different host.
        ("https://" + "a1b2c3d4" * 5 + ".extensions.allizom.org/", True),
        ("https://short.extensions.allizom.org/", False),
    ],
)
def test_chromiumapp_redirect_pattern(uri, allowed):
    """``launchWebAuthFlow`` redirects here, and the browser derives the host
    from the extension's own id — so unlike ``chrome-extension://``, one
    extension cannot claim another's redirect."""
    import re

    from faultmaven.config.settings import AuthSettings

    patterns = AuthSettings().oauth_redirect_uri_patterns
    matched = any(re.match(p, uri) for p in patterns)
    assert matched is allowed, uri
