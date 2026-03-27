#!/usr/bin/env python3
"""Generate RSA key pair for OAuth JWT signing.

This script generates a 2048-bit RSA key pair for JWT token signing in OAuth flows.
The keys are output in PEM format ready to be added to environment variables.

Usage:
    python scripts/generate_oauth_keys.py

Output:
    - Prints private key (JWT_PRIVATE_KEY)
    - Prints public key (JWT_PUBLIC_KEY)
    - Provides instructions for adding to .env file
    - Verifies keys work with JWT library
"""

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


def generate_rsa_keypair(key_size: int = 2048) -> tuple[str, str]:
    """Generate RSA key pair for JWT signing.

    Args:
        key_size: Size of RSA key in bits (default: 2048)

    Returns:
        Tuple of (private_key_pem, public_key_pem)
    """
    try:
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError:
        print("❌ Error: cryptography library not installed")
        print("   Install with: pip install cryptography")
        sys.exit(1)

    # Generate private key
    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=key_size, backend=default_backend()
    )

    # Serialize private key to PEM format
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    # Get public key
    public_key = private_key.public_key()

    # Serialize public key to PEM format
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")

    return private_pem, public_pem


def verify_keys_with_jwt(private_key: str, public_key: str) -> bool:
    """Verify that keys work with PyJWT library.

    Args:
        private_key: RSA private key in PEM format
        public_key: RSA public key in PEM format

    Returns:
        True if keys work correctly, False otherwise
    """
    try:
        import jwt
    except ImportError:
        print("⚠️  Warning: PyJWT not installed, skipping verification")
        print("   Install with: pip install pyjwt")
        return False

    try:
        # Create test token
        test_payload = {
            "sub": "test_user",
            "username": "test",
            "exp": 9999999999,  # Far future
            "iat": 1000000000,
            "jti": "test_jti",
            "type": "access",
        }

        # Sign token with private key
        token = jwt.encode(test_payload, private_key, algorithm="RS256")

        # Verify token with public key
        decoded = jwt.decode(token, public_key, algorithms=["RS256"])

        # Check payload matches
        if decoded["sub"] == test_payload["sub"]:
            return True

        return False
    except Exception as e:
        print(f"❌ Verification failed: {e}")
        return False


def format_key_for_env(key: str) -> str:
    """Format key for .env file (escape newlines).

    Args:
        key: PEM-formatted key

    Returns:
        Key with newlines escaped for .env format
    """
    # Replace actual newlines with \\n for .env file format
    return key.replace("\n", "\\n")


def main():
    """Generate and display RSA key pair for OAuth."""
    print("🔑 Generating RSA Key Pair for OAuth JWT Signing")
    print("=" * 70)

    # Generate keys
    print("Generating 2048-bit RSA key pair...")
    private_key, public_key = generate_rsa_keypair(2048)
    print("✅ Keys generated successfully\n")

    # Verify keys
    print("Verifying keys with PyJWT...")
    if verify_keys_with_jwt(private_key, public_key):
        print("✅ Keys verified successfully\n")
    else:
        print("⚠️  Could not verify keys (PyJWT may not be installed)\n")

    # Display private key
    print("=" * 70)
    print("PRIVATE KEY (JWT_PRIVATE_KEY)")
    print("=" * 70)
    print(private_key)

    # Display public key
    print("=" * 70)
    print("PUBLIC KEY (JWT_PUBLIC_KEY)")
    print("=" * 70)
    print(public_key)

    # Display .env format
    print("=" * 70)
    print("ADD TO .env FILE")
    print("=" * 70)
    print("\n# OAuth JWT Keys (RS256)")
    print(f'JWT_PRIVATE_KEY="{format_key_for_env(private_key)}"')
    print(f'JWT_PUBLIC_KEY="{format_key_for_env(public_key)}"')
    print()

    # Display instructions
    print("=" * 70)
    print("INSTRUCTIONS")
    print("=" * 70)
    print("""
1. Copy the JWT_PRIVATE_KEY and JWT_PUBLIC_KEY lines above
2. Add them to your .env file in the FaultMaven root directory
3. Set OAUTH_ENABLED=true in your .env file
4. Set AUTH_MODE=oauth in your .env file
5. Restart the FaultMaven server

Example .env configuration:

    # Authentication
    AUTH_MODE=oauth
    OAUTH_ENABLED=true

    # OAuth JWT Keys
    JWT_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\\n..."
    JWT_PUBLIC_KEY="-----BEGIN PUBLIC KEY-----\\n..."

    # OAuth Settings
    OAUTH_ALLOWED_CLIENTS=["faultmaven-copilot"]
    DASHBOARD_URL=https://dashboard.faultmaven.ai

SECURITY NOTES:
- Keep the private key secret and never commit it to version control
- For production, store keys in a secure secret management system
- Rotate keys periodically (implement key rotation strategy)
- Consider using different keys for different environments
""")


if __name__ == "__main__":
    main()
