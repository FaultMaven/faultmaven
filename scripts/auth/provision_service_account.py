#!/usr/bin/env python3
"""Provision a service account an OAuth refresh-token credential (ADR-012 D10).

Under AUTH_MODE=oauth the passwordless dev-login endpoints are not mounted, so
the Slack agent — which authenticates via dev-login today — has no credential.
This script mints it one: an initial refresh token that the agent presents to
POST /api/v1/auth/oauth/token (grant_type=refresh_token) to obtain access
tokens, receiving a rotated refresh token each time.

The refresh window slides: every refresh issues a fresh token, so a running
agent never needs a human again. Re-run this script to recover if the agent
loses its rotated token or stays down past the window (JWT_REFRESH_TOKEN_EXPIRY,
default 7 days).

The token is printed ONCE and is not recoverable afterwards — nothing stores it
server-side. Treat the output as a secret.

Usage:
    # Interim single global Slack service account
    python scripts/auth/provision_service_account.py --username slack-agent

    # Capture straight into a file without it reaching the terminal/scrollback
    python scripts/auth/provision_service_account.py -u slack-agent --token-only > token.txt

In a Kubernetes deployment, run it in the API pod:
    kubectl exec -it deploy/faultmaven-api -- \
        python scripts/auth/provision_service_account.py -u slack-agent --token-only
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from faultmaven.config.settings import AuthMode, get_settings  # noqa: E402
from faultmaven.container import container  # noqa: E402
from faultmaven.modules.auth.domain.services.service_account_provisioning import (  # noqa: E402
    SERVICE_ACCOUNT_KIND,
    ServiceAccountProvisioningError,
    provision_service_account_credential,
)


async def provision(username: str, account_kind: str, token_only: bool) -> bool:
    """Provision the account and print its credential."""

    def status(*args) -> None:
        """Print progress to stderr so --token-only leaves stdout pure.

        Flushed on every call: the token goes to stdout and the surrounding
        banner to stderr, and unflushed buffers would reorder them.
        """
        print(*args, file=sys.stderr, flush=True)

    def emit_token(token: str) -> None:
        print(token, flush=True)

    settings = get_settings()
    if settings.auth.auth_mode != AuthMode.OAUTH:
        status(
            f"❌ AUTH_MODE is '{settings.auth.auth_mode.value}', not 'oauth'.\n"
            "   Service-account credentials exist because oauth mode drops "
            "dev-login.\n"
            "   In local mode the agent should keep using dev-login."
        )
        return False

    if not token_only:
        status("=" * 80)
        status("Provision Service Account Credential")
        status("=" * 80)

    status("\nInitializing...")
    await container.initialize()

    user_store = container.get_user_store()
    if not user_store:
        status("❌ Failed to get user store from container")
        return False

    # The RS256 generator registered for oauth mode — the same instance the
    # request path validates with.
    token_generator = getattr(container, "jwt_token_generator", None)

    try:
        credential = await provision_service_account_credential(
            username=username,
            user_store=user_store,
            token_generator=token_generator,
            account_kind=account_kind,
        )
    except ServiceAccountProvisioningError as e:
        status(f"❌ {e}")
        return False
    except Exception as e:  # pragma: no cover - defensive
        status(f"❌ Failed to provision '{username}': {e}")
        return False

    user = credential.user
    if credential.account_created:
        status(f"\n✅ Created service account '{user.username}'")
    else:
        status(f"\n✅ Found existing account '{user.username}'")
    if credential.account_kind_corrected:
        status(f"   Corrected account_kind → '{account_kind}'")

    if token_only:
        emit_token(credential.refresh_token)
        status("\n⚠️  Refresh token written to stdout. Store it as a secret.")
        return True

    expires = credential.expires_at.isoformat() if credential.expires_at else "unknown"
    status("")
    status("Account:")
    status(f"  User ID:      {user.user_id}")
    status(f"  Username:     {user.username}")
    status(f"  Account kind: {user.account_kind}")
    status(f"  Roles:        {user.roles}")
    status("")
    status("=" * 80)
    status("REFRESH TOKEN — shown once, not recoverable. Store it as a secret.")
    status("=" * 80)
    emit_token(credential.refresh_token)
    status("=" * 80)
    status(f"  Unused, this token expires: {expires}")
    status("")
    status("Next steps:")
    status("  1. Store it where the agent reads its credential from")
    status("     (faultmaven-slack-agent: FAULTMAVEN_REFRESH_TOKEN).")
    status("  2. The agent refreshes before expiry and persists each rotated")
    status("     token; no further human action is needed.")
    status("  3. Lost token or an outage longer than the refresh window?")
    status("     Re-run this script to issue a new one.")
    status("")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Mint an OAuth refresh-token credential for a service account",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--username",
        "-u",
        required=True,
        help="Service account username (interim: slack-agent)",
    )
    parser.add_argument(
        "--account-kind",
        "-k",
        default=SERVICE_ACCOUNT_KIND,
        help=f"ADR-012 account kind to enforce (default: {SERVICE_ACCOUNT_KIND})",
    )
    parser.add_argument(
        "--token-only",
        action="store_true",
        help="Print only the token on stdout (progress goes to stderr)",
    )
    args = parser.parse_args()

    success = asyncio.run(provision(args.username, args.account_kind, args.token_only))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
