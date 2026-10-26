"""__init__.py

Purpose: Package initialization file

Core Design Principles:
--------------------------------------------------------------------------------
• Privacy-First: Sanitize all external-bound data
• Resilience: Implement retries and fallbacks
• Extensibility: Use interfaces for pluggable components
• Observability: Add tracing spans for key operations
"""

import sys

# The oldest CPython FaultMaven runs on: pyproject.toml's requires-python floor,
# which test_packaging holds this to. pip and uv check that floor only when they
# install, so an editable install made under an older floor, or a run from the
# source tree, gets here unchecked. RuntimeError, not ImportError: an
# `except ImportError` or pytest.importorskip would turn a refusal into a skip.
# The message gives the floor's reason; a new floor brings its own.
MINIMUM_PYTHON = (3, 11, 5)

if sys.version_info[:3] < MINIMUM_PYTHON:
    raise RuntimeError(
        f"FaultMaven requires Python {'.'.join(map(str, MINIMUM_PYTHON))} or later; "
        f"{sys.executable or 'this interpreter'} is Python "
        f"{'.'.join(map(str, sys.version_info[:3]))}. 3.11.0-3.11.4 mis-match "
        "the possessive regular expressions the secret redactor relies on "
        "(python/cpython#100061, #106052) and leave secrets in clear. Run "
        "FaultMaven on a newer Python."
    )
