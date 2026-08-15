"""First-party clients skip the consent screen; everything else does not.

Consent is a trust-boundary question — it lets a user refuse a THIRD PARTY
access to their data. The browser extension is FaultMaven's own client, and the
cases it asks to read are the ones the user wrote through it. Prompting there
informs nobody, and a consent screen that never means anything trains users to
click past the one that eventually does.

The skip has two halves and only one of them proves anything. ``client_id`` is
caller-supplied, so an impostor extension claims ``faultmaven-copilot`` as
easily as the real one does — and the consent screen never caught that either,
because it renders the client *name*. What an impostor cannot do is receive the
code at our extension's redirect: the browser derives that host from the
extension's own id. So the skip turns on the redirect, and on the deployment
having pinned which redirect is ours.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from faultmaven.config.settings import AuthSettings
from faultmaven.modules.auth.api.oauth import _is_first_party

#: The published id a deployment would pin. Any concrete 32-char a-p id does.
PINNED_ID = "abcdefghijklmnopabcdefghijklmnop"
PINNED_REDIRECT = f"https://{PINNED_ID}.chromiumapp.org/"
PINNED_PATTERN = rf"^https://{PINNED_ID}\.chromiumapp\.org/?$"


def _settings(first_party, first_party_redirects=(PINNED_PATTERN,)):
    return SimpleNamespace(
        auth=SimpleNamespace(
            oauth_first_party_clients=list(first_party),
            oauth_first_party_redirect_patterns=list(first_party_redirects),
        )
    )


def _declared_default(field_name):
    """The default as DECLARED, not as this runner's environment resolves it.

    ``AuthSettings()`` reads ``os.environ`` (``env_prefix`` is empty and there is
    no env_file), so instantiating it here would test whoever exported
    ``OAUTH_REDIRECT_URI_PATTERNS`` last — which is precisely the variable the
    field's own docstring tells deployments to set.
    """
    return AuthSettings.model_fields[field_name].default


# --------------------------------------------------------------------------- #
# The client half: necessary, never sufficient
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_shipped_client_at_pinned_redirect_is_first_party():
    assert _is_first_party(
        "faultmaven-copilot", PINNED_REDIRECT, _settings(["faultmaven-copilot"])
    )


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
    """Anything looser than exact membership — a prefix test, a case-insensitive
    compare — would let an attacker widen the first half by naming itself
    convincingly. It reaches the redirect check either way, but the narrowing
    has to hold on its own terms."""
    assert not _is_first_party(
        client_id, PINNED_REDIRECT, _settings(["faultmaven-copilot"])
    )


@pytest.mark.unit
def test_empty_client_allowlist_prompts_for_everything():
    """The list narrows the screen; it must never be the thing that disables it."""
    assert not _is_first_party("faultmaven-copilot", PINNED_REDIRECT, _settings([]))


# --------------------------------------------------------------------------- #
# The redirect half: the one that carries the proof
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.security
def test_named_client_at_an_unpinned_redirect_still_gets_consent():
    """The attack the client half cannot see.

    A hostile extension presents our ``client_id`` with its OWN
    launchWebAuthFlow redirect. The redirect is a legitimate one — the
    deployment-wide allowlist admits any extension id so dev builds work — but
    it is not the id this deployment pinned, so the prompt still renders.
    """
    attacker_redirect = "https://ponmlkjihgfedcbaponmlkjihgfedcba.chromiumapp.org/"
    assert not _is_first_party(
        "faultmaven-copilot", attacker_redirect, _settings(["faultmaven-copilot"])
    )


@pytest.mark.unit
@pytest.mark.security
def test_unpinned_deployment_prompts_for_everything():
    """The shipped default: no pinned redirect, so nothing skips consent.

    A shipped default cannot know the published extension id, and an
    id-agnostic pattern would hand the skip to any extension that asked — with
    nothing rendered to notice it by. Consent-as-shipped is the pre-existing
    behaviour, so this costs an unconfigured deployment nothing.
    """
    assert _declared_default("oauth_first_party_redirect_patterns") == []
    assert not _is_first_party(
        "faultmaven-copilot",
        PINNED_REDIRECT,
        _settings(["faultmaven-copilot"], first_party_redirects=[]),
    )


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.parametrize(
    ("uri", "allowed"),
    [
        (PINNED_REDIRECT, True),
        (f"https://{PINNED_ID}.chromiumapp.org", True),
        # A different extension's own redirect — allowed by the deployment-wide
        # list, and the whole reason the consent decision needs its own.
        ("https://ponmlkjihgfedcbaponmlkjihgfedcba.chromiumapp.org/", False),
        ("https://evil.chromiumapp.org.attacker.test/", False),
        (f"https://{PINNED_ID}.chromiumapp.org/steal", False),
        (f"http://{PINNED_ID}.chromiumapp.org/", False),
        # The retired in-extension callback page, served by the extension
        # itself rather than derived by the browser.
        (f"chrome-extension://{PINNED_ID}/callback.html", False),
    ],
)
def test_pinned_pattern_admits_only_the_pinned_extension(uri, allowed):
    assert (
        _is_first_party("faultmaven-copilot", uri, _settings(["faultmaven-copilot"]))
        is allowed
    ), uri


# --------------------------------------------------------------------------- #
# Deployment-wide redirect allowlist: browser-derived hosts only
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.parametrize(
    ("uri", "allowed"),
    [
        (PINNED_REDIRECT, True),
        (f"https://{PINNED_ID}.chromiumapp.org", True),
        # Not an extension-derived host: the whole point of the pattern.
        ("https://evil.chromiumapp.org.attacker.test/", False),
        ("https://chromiumapp.org/", False),
        (f"http://{PINNED_ID}.chromiumapp.org/", False),
        # Extension ids are a-p only; anything else is not one.
        ("https://zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz.chromiumapp.org/", False),
        (f"https://{PINNED_ID}.chromiumapp.org/steal", False),
        # Firefox's identity redirect: a 40-hex digest, different host.
        ("https://" + "a1b2c3d4" * 5 + ".extensions.allizom.org/", True),
        ("https://short.extensions.allizom.org/", False),
    ],
)
def test_default_redirect_patterns(uri, allowed):
    """``launchWebAuthFlow`` redirects here, and the browser derives the host
    from the extension's own id — so unlike ``chrome-extension://``, one
    extension cannot claim another's redirect."""
    patterns = _declared_default("oauth_redirect_uri_patterns")
    assert any(re.match(p, uri) for p in patterns) is allowed, uri


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.parametrize(
    "uri",
    [
        f"chrome-extension://{PINNED_ID}/callback.html",
        "moz-extension://12345678-1234-1234-1234-123456789abc/callback.html",
    ],
)
def test_in_extension_callback_pages_are_no_longer_admitted(uri):
    """The copilot calls ``identity.getRedirectURL()`` and no longer serves a
    callback page. Leaving these in the shipped default kept every unconfigured
    deployment accepting a redirect form an extension serves for itself, which
    is the form that carries no proof of who is receiving the code."""
    patterns = _declared_default("oauth_redirect_uri_patterns")
    assert not any(re.match(p, uri) for p in patterns), uri
