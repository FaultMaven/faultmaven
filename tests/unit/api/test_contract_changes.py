"""The contract changelog is data, and the version cannot move without an entry.

``CONTRACT_CHANGES`` replaced a comment changelog that two clients scraped with
a regex (#2411). ``validate_contract_changes`` runs on it at import; these
cases feed it synthetic tuples, so each rule is shown to refuse, naming the
entry, and the real history is shown to pass.
"""

import json
from pathlib import Path

import pytest

from faultmaven.api.contract_version import (
    API_CONTRACT_VERSION,
    CONTRACT_CHANGES,
    ContractChange,
    validate_contract_changes,
)

pytestmark = pytest.mark.unit

NOTES = Path(__file__).resolve().parents[3] / "docs/reference/api/contract-notes.json"


def _c(version: str, level: str, title: str = "Something moved.") -> ContractChange:
    return ContractChange(version=version, level=level, title=title, body="")


_MAJOR_1 = _c("1.0.0", "MAJOR")
_REFUSED = {
    "duplicate": (
        (_c("1.1.0", "MINOR"), _c("1.1.0", "MINOR"), _MAJOR_1),
        r"1\.1\.0: the version is listed twice",
    ),
    "out-of-order": (
        (_MAJOR_1, _c("1.1.0", "MINOR")),
        r"1\.0\.0: listed above 1\.1\.0",
    ),
    "minor-on-a-major-bump": (
        (_c("2.0.0", "MINOR"), _c("1.4.0", "MINOR")),
        r"2\.0\.0: labelled MINOR",
    ),
    "patch-on-a-minor-bump": (
        (_c("1.5.0", "PATCH"), _c("1.4.0", "MINOR")),
        r"1\.5\.0: labelled PATCH",
    ),
    "skipped-number": (
        (_c("14.12.0", "MINOR"), _c("14.10.0", "MINOR")),
        r"14\.12\.0: labelled MINOR, but a MINOR bump from 14\.10\.0 is 14\.11\.0",
    ),
    "empty-title": (
        (_c("1.1.0", "MINOR", "  "), _MAJOR_1),
        r"1\.1\.0: the title is empty",
    ),
    "not-semver": (
        (_c("1.1", "MINOR"),),
        r"'1\.1': the version is not MAJOR\.MINOR\.PATCH",
    ),
}


@pytest.mark.parametrize("changes, names", _REFUSED.values(), ids=_REFUSED.keys())
def test_a_broken_changelog_is_refused_naming_the_entry(changes, names):
    with pytest.raises(ValueError, match=names):
        validate_contract_changes(changes)


def test_the_published_history_passes():
    validate_contract_changes(CONTRACT_CHANGES)


def test_the_version_is_the_newest_entry():
    assert API_CONTRACT_VERSION == CONTRACT_CHANGES[0].version


def test_the_committed_notes_carry_the_version_and_every_entry():
    notes = json.loads(NOTES.read_text(encoding="utf-8"))
    assert notes["contractVersion"] == API_CONTRACT_VERSION
    # 60 when the changelog became data; it only grows from there.
    assert len(notes["entries"]) == len(CONTRACT_CHANGES) >= 60
