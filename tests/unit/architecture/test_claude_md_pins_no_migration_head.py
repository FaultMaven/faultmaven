"""``CLAUDE.md`` must not pin an alembic revision id (#1246).

The file is what every agent and contributor reads first, and its
hand-maintained counts drifted repeatedly — the migration head worst of all,
because it is wrong the moment any lane merges a migration. It was stale by
nine revisions when #1246 was filed and drifted again (047, 048) while the
issue sat open.

A stale head is not cosmetic: a lane that parents a new migration onto the
revision named in this file parents onto one that is no longer the head. Two
concurrent lanes hit exactly that during the release-blocker campaign and
avoided it only because they were told to run ``alembic heads`` rather than
read the doc.

So the durable fix is that the number is not written down at all, and this
pins it. Correcting the value instead would re-open #1246 on the next
migration.
"""

import re
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_CLAUDE_MD = _PROJECT_ROOT / "CLAUDE.md"
_VERSIONS = _PROJECT_ROOT / "alembic" / "versions"

_REVISION_DECL = re.compile(r'^revision: str = "([0-9a-f]+)"', re.MULTILINE)
_DOWN_NONE = re.compile(r"^down_revision[^=]*=\s*None", re.MULTILINE)


def _known_revisions() -> set[str]:
    """Every revision id the migration chain actually declares."""
    found: set[str] = set()
    for path in _VERSIONS.glob("*.py"):
        found.update(_REVISION_DECL.findall(path.read_text(encoding="utf-8")))
    return found


def _root_revisions() -> set[str]:
    """Revisions with no parent — the chain's immutable baseline.

    Derived rather than hand-listed, so this exemption cannot itself drift
    into the thing it exempts. A root revision is the one id in the chain that
    is stable by construction: nothing can be inserted before it, so naming it
    in prose ("Baseline ``001_clean_baseline`` (revision ...)") stays true for
    the life of the chain. A HEAD has the opposite property — it changes on
    every migration merge — which is the whole of #1246.
    """
    roots: set[str] = set()
    for path in _VERSIONS.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if _DOWN_NONE.search(text):
            roots.update(_REVISION_DECL.findall(text))
    return roots


def _pinnable_revisions() -> set[str]:
    """Revisions it is a defect to name in CLAUDE.md: everything but a root."""
    return _known_revisions() - _root_revisions()


@pytest.mark.unit
@pytest.mark.architecture
def test_the_migration_chain_declares_revisions_we_can_detect() -> None:
    """Positive control: the detector finds real revisions.

    Without this, an empty ``_known_revisions()`` would make the guard below
    pass vacuously for every possible CLAUDE.md — the failure mode #1246 is
    itself an instance of.
    """
    revisions = _known_revisions()
    assert len(revisions) > 30, f"expected the full chain, found {len(revisions)}"


@pytest.mark.unit
@pytest.mark.architecture
def test_the_detector_would_catch_a_pinned_revision() -> None:
    """Positive control: a document that DOES pin one is reported."""
    a_real_revision = sorted(_pinnable_revisions())[0]
    synthetic = f"Current head: `{a_real_revision}`.\n"
    assert _pinned_in(synthetic) == {a_real_revision}


def _pinned_in(text: str) -> set[str]:
    return {rev for rev in _pinnable_revisions() if rev in text}


@pytest.mark.unit
@pytest.mark.architecture
def test_claude_md_pins_no_alembic_revision() -> None:
    """The guard: CLAUDE.md names no revision id, current or stale."""
    pinned = _pinned_in(_CLAUDE_MD.read_text(encoding="utf-8"))
    assert not pinned, (
        f"CLAUDE.md pins alembic revision(s) {sorted(pinned)}. Do not write a "
        "revision id here — it is stale on the next migration merge and a lane "
        "that parents onto it parents onto a non-head. Point at `alembic heads` "
        "instead (#1246)."
    )


@pytest.mark.unit
@pytest.mark.architecture
def test_exactly_one_root_revision_anchors_the_chain() -> None:
    """Positive control for the exemption itself.

    If this ever finds zero roots the exemption is empty and the guard tightens
    silently; if it finds several the chain has forked and "the baseline" is no
    longer a single stable id worth naming in prose.
    """
    assert len(_root_revisions()) == 1, sorted(_root_revisions())
