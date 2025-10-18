"""
STRUCTURED_CONFIG Extractor

Parses configuration files (YAML, JSON, TOML, INI, .env) and extracts
key settings while redacting secrets. No LLM calls required.
"""

import re
import json
from typing import Dict, Any, List, Optional


class StructuredConfigExtractor:
    """Parse and sanitize configuration files (0 LLM calls)"""

    # Patterns that indicate secrets (case-insensitive)
    SECRET_PATTERNS = [
        r'password',
        r'passwd',
        r'secret',
        r'api[_-]?key',
        r'token',
        r'credentials?',
        r'auth',
        r'private[_-]?key',
        r'access[_-]?key',
    ]

    # Values that should be redacted
    SECRET_VALUE_PATTERNS = [
        r'^[a-zA-Z0-9]{20,}$',  # Long alphanumeric strings (likely tokens)
        r'^sk-[a-zA-Z0-9]+$',  # OpenAI-style keys
        r'^[A-Z0-9]{32,}$',  # All-caps hex strings
    ]

    MAX_OUTPUT_LINES = 500  # Safety limit

    @property
    def strategy_name(self) -> str:
        return "direct"

    @property
    def llm_calls_used(self) -> int:
        return 0

    def extract(self, content: str) -> str:
        """
        Extract and sanitize configuration

        Steps:
        1. Detect format (YAML, JSON, TOML, INI, .env)
        2. Parse content
        3. Redact secrets
        4. Format output
        """
        # Try to detect format and parse
        config_data = self._parse_config(content)

        if config_data is None:
            # Couldn't parse - treat as key=value pairs
            config_data = self._parse_key_value_pairs(content)

        # Redact secrets
        sanitized = self._redact_secrets(config_data)

        # Format output
        return self._format_config(sanitized)

    def _parse_config(self, content: str) -> Optional[Dict[str, Any]]:
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

    def _parse_key_value_pairs(self, content: str) -> Dict[str, Any]:
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

        for line in content.split('\n'):
            line = line.strip()

            # Skip empty lines and comments
            if not line or line.startswith('#') or line.startswith(';'):
                continue

            # Section header [section]
            if line.startswith('[') and line.endswith(']'):
                current_section = line[1:-1]
                result[current_section] = {}
                continue

            # Key=value pair
            if '=' in line:
                key, _, value = line.partition('=')
                key = key.strip()
                value = value.strip().strip('"').strip("'")

                if current_section:
                    result[current_section][key] = value
                else:
                    result[key] = value

        return result

    def _redact_secrets(self, data: Any, path: str = "") -> Any:
        """
        Recursively redact secrets from config data

        Args:
            data: Config data (dict, list, or primitive)
            path: Current key path (for nested configs)

        Returns:
            Sanitized data
        """
        if isinstance(data, dict):
            return {
                key: self._redact_secrets(value, f"{path}.{key}" if path else key)
                for key, value in data.items()
            }
        elif isinstance(data, list):
            return [self._redact_secrets(item, f"{path}[{i}]") for i, item in enumerate(data)]
        elif isinstance(data, str):
            # Check if key name suggests secret
            if self._is_secret_key(path):
                return "[REDACTED]"
            # Check if value looks like a secret
            if self._is_secret_value(data):
                return "[REDACTED]"
            return data
        else:
            return data

    def _is_secret_key(self, key_path: str) -> bool:
        """Check if key name suggests it's a secret"""
        key_lower = key_path.lower()
        return any(re.search(pattern, key_lower) for pattern in self.SECRET_PATTERNS)

    def _is_secret_value(self, value: str) -> bool:
        """Check if value looks like a secret"""
        # Don't redact short values
        if len(value) < 16:
            return False

        return any(re.match(pattern, value) for pattern in self.SECRET_VALUE_PATTERNS)

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

        # Safety check: truncate if too long
        output_lines = output.split('\n')
        if len(output_lines) > self.MAX_OUTPUT_LINES:
            truncated = '\n'.join(output_lines[:self.MAX_OUTPUT_LINES])
            truncated += f"\n\n... [Truncated {len(output_lines) - self.MAX_OUTPUT_LINES} lines]"
            return truncated

        return output
