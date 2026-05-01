"""
STRUCTURED_CONFIG Extractor

Parses configuration files (YAML, JSON, TOML, INI, .env) and extracts
key settings while redacting secrets. No LLM calls required.

Uses detect-secrets for enhanced secret detection when available,
falling back to regex patterns.
"""

import json
import re
from typing import Any

from faultmaven.modules.preprocessing.extractors.protocol import ExtractResult
from faultmaven.modules.preprocessing.extractors.utils import (
    EMPTY_CONTENT_RESPONSE,
    has_content,
    truncate_output,
)

try:
    from detect_secrets import settings as _ds_settings
    from detect_secrets.core.scan import scan_line as _ds_scan_line

    DETECT_SECRETS_AVAILABLE = True
except ImportError:
    DETECT_SECRETS_AVAILABLE = False

# detect-secrets plugin config: only structured-pattern detectors (no entropy-based)
_DS_PLUGINS = {
    "plugins_used": [
        {"name": "ArtifactoryDetector"},
        {"name": "AWSKeyDetector"},
        {"name": "AzureStorageKeyDetector"},
        {"name": "BasicAuthDetector"},
        {"name": "CloudantDetector"},
        {"name": "DiscordBotTokenDetector"},
        {"name": "GitHubTokenDetector"},
        {"name": "GitLabTokenDetector"},
        {"name": "IbmCloudIamDetector"},
        {"name": "IbmCosHmacDetector"},
        {"name": "JwtTokenDetector"},
        {"name": "MailchimpDetector"},
        {"name": "NpmDetector"},
        {"name": "OpenAIDetector"},
        {"name": "SendGridDetector"},
        {"name": "SlackDetector"},
        {"name": "SoftlayerDetector"},
        {"name": "SquareOAuthDetector"},
        {"name": "StripeDetector"},
        {"name": "TelegramBotTokenDetector"},
        {"name": "TwilioKeyDetector"},
    ]
}


class StructuredConfigExtractor:
    """Parse and sanitize configuration files (0 LLM calls)"""

    # Key-name patterns that indicate the VALUE is a secret.
    # Matched against the TERMINAL segment (last component after '.') of the
    # dotted key path. Each pattern uses anchoring so that the secret indicator
    # must appear as the ENTIRE key or as a SUFFIX preceded by _ or -.
    #
    # Examples:  password ✓  db_password ✓  AUTH_TYPE ✗  token_type ✗
    SECRET_KEY_PATTERNS = [
        r"(?:^|[_-])password$",
        r"(?:^|[_-])passwd$",
        r"(?:^|[_-])secret$",
        r"(?:^|[_-])token$",  # auth_token YES, token_type NO
        r"(?:^|[_-])credentials?$",
        r"^api[_-]?key$",
        r"^access[_-]?key$",
        r"^secret[_-]?key$",
        r"^private[_-]?key$",
        r"(?:^|[_-])signing[_-]?key$",
        r"(?:^|[_-])encryption[_-]?key$",
    ]

    # Values that are obviously NOT secrets even if the key matches.
    # Booleans, auth-mode enums, algorithm names, small integers.
    _NON_SECRET_VALUE_RE = re.compile(
        r"^("
        r"true|false|yes|no|on|off|none|null|"
        r"bearer|basic|digest|oauth2?|oidc|saml|ldap|local|hmac|"
        r"hs256|hs384|hs512|rs256|rs384|rs512|es256|es384|es512|"
        r"required|optional|enabled|disabled|"
        r"\d{1,5}"
        r")$",
        re.IGNORECASE,
    )

    # Values that should be redacted regardless of key name.
    SECRET_VALUE_PATTERNS = [
        r"^[a-zA-Z0-9]{20,}$",  # Long alphanumeric strings (likely tokens)
        r"^sk-[a-zA-Z0-9]+$",  # OpenAI-style keys
        r"^[A-Z0-9]{32,}$",  # All-caps hex strings
    ]

    @property
    def strategy_name(self) -> str:
        return "direct"

    @property
    def llm_calls_used(self) -> int:
        return 0

    def extract(self, content: str) -> ExtractResult:
        """
        Extract and sanitize configuration

        Steps:
        1. Detect format (YAML, JSON, TOML, INI, .env)
        2. Parse content
        3. Redact secrets
        4. Format output
        """
        content = content.lstrip("\ufeff")
        if len(content) > 50_000_000:
            return ExtractResult(
                file_extract="[File exceeds 50MB maximum size limit for extraction]"
            )

        if not has_content(content):
            return ExtractResult(file_extract=EMPTY_CONTENT_RESPONSE)

        # Detect format
        format_detected = self._detect_format(content)

        # Try to detect format and parse
        config_data = self._parse_config(content)

        if config_data is None:
            # Couldn't parse - treat as key=value pairs
            config_data = self._parse_key_value_pairs(content)
            format_detected = "key-value"

        # Count keys before redaction
        top_keys = list(config_data.keys()) if isinstance(config_data, dict) else []
        total_keys = self._count_keys(config_data)

        # Redact secrets
        sanitized, redaction_count = self._redact_secrets_counted(config_data)

        # Fully redacted check
        fully_redacted = total_keys > 0 and redaction_count == total_keys

        # Format output
        if fully_redacted:
            result = "[WARNING: Fully Redacted Config - No structural metadata available]\n\n"
        else:
            result = self._format_config(sanitized)

        return ExtractResult(
            file_extract=result,
            file_meta={
                "format": format_detected,
                "fully_redacted": fully_redacted if fully_redacted else None,
                "top_level_keys": (
                    ", ".join(top_keys[:10]) if not fully_redacted else None
                ),
                "total_keys": total_keys,
                "secrets_redacted": redaction_count,
                "size_bytes": len(content.encode("utf-8", errors="replace")),
            },
        )

    def _detect_format(self, content: str) -> str:
        """Detect config format without parsing."""
        try:
            json.loads(content)
            return "json"
        except (json.JSONDecodeError, ValueError):
            pass
        try:
            import yaml

            result = yaml.safe_load(content)
            if isinstance(result, dict):
                return "yaml"
        except (ImportError, Exception):
            pass
        try:
            import tomli

            tomli.loads(content)
            return "toml"
        except (ImportError, Exception):
            pass
        return "ini/env"

    def _count_keys(self, data: Any) -> int:
        """Count total keys recursively."""
        if isinstance(data, dict):
            return len(data) + sum(self._count_keys(v) for v in data.values())
        if isinstance(data, list):
            return sum(self._count_keys(item) for item in data)
        return 0

    def _parse_config(self, content: str) -> dict[str, Any] | None:
        """
        Try to parse as structured config

        Returns parsed dict or None if parsing fails
        """
        # Try JSON first
        try:
            return json.loads(content)
        except (json.JSONDecodeError, ValueError):
            pass

        # Try YAML (if available)
        try:
            import yaml

            result = yaml.safe_load(content)
            # YAML might parse plain text as a string - we need a dict
            if isinstance(result, dict):
                return result
        except (ImportError, yaml.YAMLError, AttributeError):
            pass

        # Try TOML (if available)
        try:
            import tomli

            return tomli.loads(content)
        except (ImportError, Exception):
            pass

        return None

    def _parse_key_value_pairs(self, content: str) -> dict[str, Any]:
        """
        Parse as simple key=value pairs (.env, .ini style)

        Handles:
        - KEY=value
        - KEY="value"
        - [section] headers
        - # comments
        """
        result = {}
        current_section = None

        for line in content.split("\n"):
            line = line.strip()

            # Skip empty lines and comments
            if not line or line.startswith("#") or line.startswith(";"):
                continue

            # Section header [section]
            if line.startswith("[") and line.endswith("]"):
                current_section = line[1:-1]
                result[current_section] = {}
                continue

            # Key=value pair
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")

                if current_section:
                    result[current_section][key] = value
                else:
                    result[key] = value

        return result

    def _redact_secrets_counted(self, data: Any, path: str = "") -> tuple[Any, int]:
        """Recursively redact secrets from config data, counting redactions.

        Returns:
            (sanitized_data, redaction_count)
        """
        if isinstance(data, dict):
            result = {}
            count = 0
            for key, value in data.items():
                new_path = f"{path}.{key}" if path else key
                sanitized, c = self._redact_secrets_counted(value, new_path)
                result[key] = sanitized
                count += c
            return result, count
        elif isinstance(data, list):
            result = []
            count = 0
            for i, item in enumerate(data):
                sanitized, c = self._redact_secrets_counted(item, f"{path}[{i}]")
                result.append(sanitized)
                count += c
            return result, count
        elif isinstance(data, str):
            if self._is_secret_key(path, value=data):
                return "[REDACTED]", 1
            if self._is_secret_value(data, key_path=path):
                return "[REDACTED]", 1
            return data, 0
        else:
            return data, 0

    def _is_secret_key(self, key_path: str, value: str = "") -> bool:
        """Check if key name suggests it holds a secret value.

        Matches against the terminal segment of the dotted key path
        (the last component after '.') to avoid false positives from
        parent key names like 'auth' in 'auth.method'.

        Also skips redaction when the value is an obvious non-secret
        (boolean, enum, small integer).
        """
        # Extract terminal key segment
        terminal_key = key_path.rsplit(".", 1)[-1].lower()

        is_match = any(
            re.search(pattern, terminal_key) for pattern in self.SECRET_KEY_PATTERNS
        )

        if not is_match:
            return False

        # Even if key matches, skip redaction for obviously non-secret values
        if value and self._NON_SECRET_VALUE_RE.match(value.strip()):
            return False

        return True

    def _is_secret_value(self, value: str, key_path: str = "") -> bool:
        """Check if value looks like a secret.

        Uses regex patterns as fast first-pass, then detect-secrets
        as second-pass for structured API token detection.
        """
        # Don't redact short values
        if len(value) < 16:
            return False

        # First pass: regex patterns
        if any(re.match(pattern, value) for pattern in self.SECRET_VALUE_PATTERNS):
            return True

        # Second pass: detect-secrets (structured token detectors only)
        if DETECT_SECRETS_AVAILABLE:
            try:
                with _ds_settings.transient_settings(_DS_PLUGINS):
                    synthetic_line = f"{key_path}={value}" if key_path else value
                    results = list(_ds_scan_line(synthetic_line))
                    if results:
                        return True
            except Exception:
                pass

        return False

    def _format_config(self, data: Any, indent: int = 0) -> str:
        """
        Format config data as readable text

        Args:
            data: Sanitized config data
            indent: Current indentation level

        Returns:
            Formatted string
        """
        lines = []
        prefix = "  " * indent

        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, (dict, list)):
                    lines.append(f"{prefix}{key}:")
                    lines.append(self._format_config(value, indent + 1))
                else:
                    lines.append(f"{prefix}{key}: {value}")
        elif isinstance(data, list):
            for i, item in enumerate(data):
                if isinstance(item, (dict, list)):
                    lines.append(f"{prefix}[{i}]:")
                    lines.append(self._format_config(item, indent + 1))
                else:
                    lines.append(f"{prefix}- {item}")
        else:
            return f"{prefix}{data}"

        output = "\n".join(lines)

        return truncate_output(output)
