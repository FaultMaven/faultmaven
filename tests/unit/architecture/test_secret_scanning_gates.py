"""The two dotenv leak gates must stay wide.

Live provider keys reached this repository's history in `.env-back` and
`.env.backup`. Neither gate stopped them: `.gitignore` covered only the exact
name `.env`, and the `check-api-keys` pre-commit hook selected files by
extension, so `.env-back` was neither ignored nor scanned.

Both gates are now pattern-based, and a pattern is easy to narrow by accident
while tidying. These tests pin the width, so a re-narrowing fails here instead
of at the next leak.

`git check-ignore` is invoked with `--no-index`: without it git answers "not
ignored" for anything already tracked, which would make the tracked-file
assertion below pass no matter what the rules say.
"""

import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
PRE_COMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"

# Dotenv names a developer can plausibly produce: the two that actually leaked,
# the editor/shell backup forms, and one nested to prove depth is covered.
LEAK_PRONE_DOTENV_NAMES = [
    ".env",
    ".env-back",
    ".env.backup",
    ".env.bak",
    ".env.old",
    ".env.save",
    ".env.local",
    ".env~",
    "config/.env-back",
]


def _check_api_keys_files_pattern() -> re.Pattern:
    """The `files:` selector of the check-api-keys hook, as pre-commit applies it."""
    config = yaml.safe_load(PRE_COMMIT_CONFIG.read_text())
    hooks = [
        hook
        for repo in config["repos"]
        for hook in repo["hooks"]
        if hook.get("id") == "check-api-keys"
    ]
    assert len(hooks) == 1, f"expected exactly one check-api-keys hook, got {hooks}"
    return re.compile(hooks[0]["files"])


def _is_ignored(path: str) -> bool:
    """True if .gitignore would ignore `path`, index membership disregarded."""
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", "--", path],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    assert result.returncode in (0, 1), (
        f"git check-ignore failed for {path!r}: "
        f"rc={result.returncode} {result.stderr.decode(errors='replace')}"
    )
    return result.returncode == 0


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.parametrize("name", LEAK_PRONE_DOTENV_NAMES)
def test_gitignore_denies_dotenv_variants(name: str):
    assert _is_ignored(name), f"{name} is committable — .gitignore has been narrowed"


@pytest.mark.unit
@pytest.mark.security
def test_gitignore_keeps_the_template_trackable():
    """The `!.env.example` exemption is what makes `.env*` usable; keep it."""
    assert not _is_ignored(".env.example")
    assert not _is_ignored("deploy/.env.example")


@pytest.mark.unit
@pytest.mark.security
def test_gitignore_hides_no_tracked_file():
    """A broad `.env*` must not shadow anything the repo actually ships."""
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout
    assert tracked, "git ls-files returned nothing — the check would be vacuous"
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin", "-z"],
        cwd=REPO_ROOT,
        input=tracked,
        capture_output=True,
    )
    assert ignored.returncode in (0, 1), ignored.stderr.decode(errors="replace")
    hidden = [p for p in ignored.stdout.decode().split("\0") if p]
    assert hidden == [], f"tracked files are now ignored: {hidden}"


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.parametrize("name", LEAK_PRONE_DOTENV_NAMES)
def test_check_api_keys_scans_dotenv_variants(name: str):
    """Extension matching alone misses every dotenv name but `.env` itself."""
    pattern = _check_api_keys_files_pattern()
    assert pattern.search(name), f"{name} would skip the API-key scan"


@pytest.mark.unit
@pytest.mark.security
def test_check_api_keys_still_scans_source_and_docs():
    """Widening the selector must not have replaced its original coverage."""
    pattern = _check_api_keys_files_pattern()
    for name in (
        "faultmaven/main.py",
        "README.md",
        "docker-compose.yml",
        ".env.example",
    ):
        assert pattern.search(name), f"{name} is no longer scanned"
    assert not pattern.search("LICENSE")
