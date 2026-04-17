"""Shared test helpers for extractor tests."""

from faultmaven.modules.preprocessing.extractors.utils import COVERAGE_SEPARATOR


def strip_coverage(result: str) -> str:
    """Return extractor output with coverage metadata removed."""
    if COVERAGE_SEPARATOR in result:
        return result.split(COVERAGE_SEPARATOR)[0]
    return result


def get_coverage(result: str) -> dict[str, str]:
    """Parse coverage metadata into a dict of key-value pairs.

    Returns an empty dict if no coverage metadata is present.
    """
    if COVERAGE_SEPARATOR not in result:
        return {}
    metadata_section = result.split(COVERAGE_SEPARATOR)[1]
    pairs: dict[str, str] = {}
    for line in metadata_section.strip().split("\n"):
        if ": " in line:
            key, _, value = line.partition(": ")
            pairs[key.strip()] = value.strip()
    return pairs
