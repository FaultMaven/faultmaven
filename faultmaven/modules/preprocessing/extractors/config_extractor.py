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

import tomli
import yaml

from faultmaven.modules.preprocessing.extractors.protocol import ExtractResult
from faultmaven.modules.preprocessing.extractors.utils import (
    EMPTY_CONTENT_RESPONSE,
    UNPARSED_META_KEY,
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


class _RepeatedKey(ValueError):
    """A key repeats within one mapping. JSON and YAML parsers keep only its
    last value, so the parse would drop the others without a word (#2791).
    TOML parsers refuse a repeated key themselves."""

    def __init__(self, key: object) -> None:
        super().__init__(key)
        self.key = key


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``json.loads``' ``object_pairs_hook``: the object, or ``_RepeatedKey``."""
    mapping: dict[str, Any] = {}
    for key, value in pairs:
        if key in mapping:
            raise _RepeatedKey(key)
        mapping[key] = value
    return mapping


_YAML_MERGE_TAG = "tag:yaml.org,2002:merge"


class _UniqueKeyLoader(yaml.SafeLoader):
    """``yaml.SafeLoader`` that raises ``_RepeatedKey`` instead of keeping a
    repeated key's last value.

    The check walks the composed node tree before anything is constructed:
    constructing a mapping that merges another (``<<``) flattens the merged
    node in place, after which a key it inherits sits beside the key that
    overrides it, which YAML allows. Only a mapping's own explicit keys are
    compared, by their constructed value (so ``yes`` and ``true`` collide, as
    they do in the result). A non-scalar key is left to SafeLoader, which
    refuses it.
    """

    def construct_document(self, node):
        self._refuse_repeated_keys(node)
        return super().construct_document(node)

    def _refuse_repeated_keys(self, root) -> None:
        visited: set[int] = set()
        stack = [root]
        while stack:
            node = stack.pop()
            if id(node) in visited:
                continue
            visited.add(id(node))
            if isinstance(node, yaml.SequenceNode):
                stack.extend(node.value)
            elif isinstance(node, yaml.MappingNode):
                seen: set = set()
                for key_node, value_node in node.value:
                    stack.append(value_node)
                    if key_node.tag == _YAML_MERGE_TAG or not isinstance(
                        key_node, yaml.ScalarNode
                    ):
                        continue
                    key = self.construct_object(key_node)
                    if key in seen:
                        raise _RepeatedKey(key)
                    seen.add(key)


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

        try:
            parsed = self._parse_config(content)
        except _RepeatedKey as repeated:
            return ExtractResult(
                file_extract=f"[Repeated key {repeated.key!r}: not parsed]",
                file_meta={
                    UNPARSED_META_KEY: (
                        f"the key {repeated.key!r} repeats within one mapping, "
                        f"and a parser keeps only its last value"
                    ),
                    "size_bytes": len(content.encode("utf-8", errors="replace")),
                },
            )

        unparsed = None
        if parsed is not None:
            config_data, format_detected = parsed
        else:
            # Couldn't parse - treat as key=value pairs
            config_data, read, total, repeated = self._parse_key_value_pairs(content)
            format_detected = "key-value"
            # The fallback is a reading of the content only where most of it
            # is key=value pairs; elsewhere (``kubectl describe``) it keeps
            # the few lines that hold an ``=`` and drops the rest (#2791).
            if read * 2 < total:
                unparsed = (
                    f"only {read} of {total} lines are key=value pairs "
                    f"or [section] headers"
                )
            elif repeated:
                unparsed = (
                    f"the {repeated} repeats, and a key=value reading keeps "
                    f"only one value per key"
                )

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
                **({UNPARSED_META_KEY: unparsed} if unparsed else {}),
            },
        )

    def _count_keys(self, data: Any) -> int:
        """Count total keys recursively."""
        if isinstance(data, dict):
            return len(data) + sum(self._count_keys(v) for v in data.values())
        if isinstance(data, list):
            return sum(self._count_keys(item) for item in data)
        return 0

    def _parse_config(self, content: str) -> tuple[Any, str] | None:
        """
        Try to parse as structured config

        Returns the parsed data and its format, or None if no parser reads
        the content. Raises ``_RepeatedKey`` when JSON or YAML that would
        parse repeats a key in one mapping.
        """
        # Try JSON first
        try:
            return json.loads(content, object_pairs_hook=_unique_pairs), "json"
        except _RepeatedKey:
            raise
        except (ValueError, RecursionError):  # JSONDecodeError subclasses it
            pass

        try:
            result = yaml.load(content, Loader=_UniqueKeyLoader)
            # YAML might parse plain text as a string - we need a dict
            if isinstance(result, dict):
                return result, "yaml"
        except _RepeatedKey:
            raise
        except (yaml.YAMLError, AttributeError, ValueError, RecursionError):
            # ValueError: a scalar its constructor refuses (``2026-13-45`` as a
            # date); RecursionError: nesting past the interpreter's limit.
            # Either way YAML does not read it, so the parsers below try.
            pass

        # Try TOML: tomli, not the stdlib tomllib, which reads only TOML 1.0 on 3.11
        try:
            return tomli.loads(content), "toml"
        except (ValueError, RecursionError):
            # TOMLDecodeError subclasses ValueError, as does an integer past
            # CPython's digit limit; RecursionError is tomli's nesting limit.
            pass

        return None

    def _parse_key_value_pairs(
        self, content: str
    ) -> tuple[dict[str, Any], int, int, str | None]:
        """
        Parse as simple key=value pairs (.env, .ini style)

        Handles:
        - KEY=value
        - KEY="value"
        - [section] headers
        - # comments

        Returns the pairs, how many lines it read (pairs and headers), how
        many lines are neither blank nor comments, and the first key or
        section that repeats (``None`` when none does): this reading keeps one
        value per key, so a repeat (a systemd unit's several ``Environment=``,
        an ``.env`` that sets a name twice, a section opened twice) loses the
        others (#2791).
        """
        result = {}
        current_section = None
        read = total = 0
        repeated: str | None = None

        for line in content.split("\n"):
            line = line.strip()

            # Skip empty lines and comments
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            total += 1

            # Section header [section]
            if line.startswith("[") and line.endswith("]"):
                read += 1
                current_section = line[1:-1]
                if current_section in result:
                    repeated = repeated or f"section [{current_section}]"
                result.setdefault(current_section, {})
                continue

            # Key=value pair
            if "=" in line:
                read += 1
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")

                target = result[current_section] if current_section else result
                if key in target:
                    repeated = repeated or f"key {key!r}"
                target[key] = value

        return result, read, total, repeated

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
