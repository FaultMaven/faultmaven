"""Tests for scripts/security_alert_sweep.py (#2120).

The sweep classifies every open Security-tab alert as tracked, exception, lag or
untracked, and /process-top-issues files every untracked group. Each test pins a
rule against the naive form that misread live data: a trailing period on a
``**Repo:**`` line, ``faultmaven`` as a substring of every repo name, ``CodeQL #1``
inside ``CodeQL #12``, a GHSA named in prose, a pnpm lock holding a second
vulnerable version, and ``gh --paginate`` printing one array per page.

``gh`` is replaced by ``FakeGh``, which answers as gh 2.46 does, so nothing here
reaches the network.
"""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "security_alert_sweep.py"
NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
CORE, COPILOT, SLACK = "faultmaven", "faultmaven-copilot", "faultmaven-slack-agent"
GHSA, REQ, LOCK = "GHSA-aaaa-bbbb-cccc", "requirements/dev.txt", "pnpm-lock.yaml"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("security_alert_sweep", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeGh:
    """``gh`` as 2.46 behaves: ``--paginate`` alone prints one JSON array per page,
    concatenated; with ``--jq '.[]'`` it prints one object per line across pages; a
    refused call exits 1 with ``gh: <message> (HTTP <code>)`` on stderr."""

    def __init__(self, repos):
        self.repos, self.calls = list(repos), []
        self.pages: dict[str, list[list[dict]]] = {}
        self.files: dict[tuple[str, str], str] = {}
        self.errors: dict[str, str] = {}

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["repo", "list"]:
            return 0, "".join(f"{name}\n" for name in self.repos), ""
        path = args[-1].split("?")[0].removeprefix("repos/FaultMaven/")
        if path in self.errors:
            return 1, '{"message": "..."}', f"gh: {self.errors[path]}\n"
        if "Accept: application/vnd.github.raw" in args:
            text = self.files.get(tuple(path.split("/contents/")))
            return (
                (1, "", "gh: Not Found (HTTP 404)") if text is None else (0, text, "")
            )
        pages = self.pages.get(path, [[]])[: None if "--paginate" in args else 1]
        if args[args.index("--jq") + 1 :][:1] == [".[]"]:
            return 0, "".join(json.dumps(o) + "\n" for page in pages for o in page), ""
        return 0, "".join(json.dumps(page) for page in pages), ""


def dependabot(
    repo, n, package="pillow", ghsa=GHSA, manifest=REQ, fixed="2.0.0", age=1
):
    created = (NOW - timedelta(days=age)).isoformat().replace("+00:00", "Z")
    package_info = {"ecosystem": "npm" if manifest == LOCK else "pip", "name": package}
    return {
        "number": n,
        "html_url": f"https://github.com/FaultMaven/{repo}/security/dependabot/{n}",
        "created_at": created,
        "dependency": {"manifest_path": manifest, "package": package_info},
        "security_advisory": {"ghsa_id": ghsa, "severity": "high"},
        "security_vulnerability": {
            "first_patched_version": {"identifier": fixed} if fixed else None
        },
    }


def codeql(repo, n):
    url = f"https://github.com/FaultMaven/{repo}/security/code-scanning/{n}"
    rule = {"id": "py/stack-trace-exposure", "security_severity_level": "medium"}
    return {
        "number": n,
        "html_url": url,
        "created_at": "2026-09-02T10:24:57Z",
        "rule": rule,
    }


def issue(n, body, title="an issue"):
    return {"number": n, "title": title, "body": body}


def fake(mod, repo, *alerts, issues=(), files=None, pulls=None):
    gh = FakeGh(mod.REPOS)
    kind = "dependabot" if "dependency" in alerts[0] else "code-scanning"
    gh.pages[f"{repo}/{kind}/alerts"] = [list(alerts)]
    gh.pages[f"{CORE}/issues"] = [list(issues)]
    gh.files.update(files or {})
    for pr_repo, prs in (pulls or {}).items():
        gh.pages[f"{pr_repo}/pulls"] = [prs]
    return gh


def table(report):
    """The report's table rows, as dicts."""
    keys = ("repo", "kind", "what", "advisory", "severity", "cls", "detail", "urls")
    rows = [line.strip("|").split(" | ") for line in report.splitlines()]
    rows = [r for r in rows if len(r) == len(keys)][1:]  # drop the header
    return [dict(zip(keys, (cell.strip() for cell in r), strict=True)) for r in rows]


def row(mod, repo, alert, **setup):
    """Sweep one alert and return its group's row."""
    (only,) = table(mod.sweep(fake(mod, repo, alert, **setup), NOW))
    return only


# --- Repo parsing -------------------------------------------------------------


@pytest.mark.parametrize(
    "body, repos",
    [
        (
            "**Repo: FaultMaven/faultmaven-website.** Filed here.",
            {"faultmaven-website"},
        ),
        (
            "**Repos: FaultMaven/fm-sre-simulator and FaultMaven/faultmaven-slack-agent.**",
            {"fm-sre-simulator", SLACK},
        ),
        ("No repo line; it mentions FaultMaven/faultmaven-copilot.", {CORE}),
    ],
)
def test_an_issue_names_its_repos_on_its_repo_line_or_is_core(mod, body, repos):
    assert mod.issue_tracker(issue(1, body)).repos == repos


def test_a_copilot_issue_tracks_a_copilot_alert_but_not_a_core_one(mod):
    """``faultmaven`` is a substring of every repo name: the repo test is set membership."""
    body = f"**Repo: FaultMaven/faultmaven-copilot.**\n\n| #1 | pillow | {GHSA} |"
    tracker = [issue(7, body)]
    assert row(mod, CORE, dependabot(CORE, 1), issues=tracker)["cls"] == "untracked"
    assert row(mod, COPILOT, dependabot(COPILOT, 1), issues=tracker)["detail"] == "#7"


# --- Tracked ------------------------------------------------------------------


@pytest.mark.parametrize(
    "body, cls",
    [
        (f"| Alert | Advisory |\n|---|---|\n| #1 | {GHSA} |", "tracked"),
        (f"**Alerts:** {GHSA} in requirements/dev.txt", "tracked"),
        (f"The OS store keeps the root {GHSA} revokes.", "untracked"),  # #2112
        (f"Out of scope: pillow {GHSA} (#1) is accepted.", "untracked"),
    ],
)
def test_a_ghsa_tracks_only_in_a_table_row_or_an_alerts_line(mod, body, cls):
    assert row(mod, CORE, dependabot(CORE, 1), issues=[issue(7, body)])["cls"] == cls


@pytest.mark.parametrize(
    "title, cls",
    [
        ("raw errors on the bind page (CodeQL #1)", "tracked"),
        ("CodeQL #12", "untracked"),
    ],
)
def test_a_scanning_key_matches_at_a_word_boundary(mod, title, cls):
    tracker = issue(9, "**Repo: FaultMaven/faultmaven-slack-agent.**", title)
    assert row(mod, SLACK, codeql(SLACK, 1), issues=[tracker])["cls"] == cls


@pytest.mark.parametrize("suffix, cls", [("", "tracked"), ("12", "untracked")])
def test_the_filed_form_tracks_its_alert_url_and_not_a_longer_one(mod, suffix, cls):
    url = dependabot(COPILOT, 1)["html_url"]
    body = f"**Repo: FaultMaven/faultmaven-copilot.**\n**Alerts:** {url}{suffix}\n"
    tracker = [issue(7, body)]
    assert row(mod, COPILOT, dependabot(COPILOT, 1), issues=tracker)["cls"] == cls


def test_an_open_pr_in_the_alerts_own_repo_tracks_it_by_url_only(mod):
    alert = dependabot(COPILOT, 3, manifest=LOCK)
    pr = {"number": 50, "title": "fix(deps)", "body": f"Fixes {alert['html_url']}"}
    assert (
        row(mod, COPILOT, alert, pulls={COPILOT: [pr]})["detail"]
        == "PR faultmaven-copilot#50"
    )

    alert = dependabot(CORE, 3)
    keyed = {"number": 51, "title": "Dependabot #3", "body": None}
    elsewhere = {"number": 52, "title": "x", "body": alert["html_url"]}
    pulls = {CORE: [keyed], COPILOT: [elsewhere]}
    assert row(mod, CORE, alert, pulls=pulls)["cls"] == "untracked"


# --- Lag ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "manifest, text",
    [
        (REQ, 'pillow[avif]==2.1.0 ; python_version >= "3.11"\n    # via x\n'),
        ("requirements/cloud.txt", "certifi==1.0\nPillow==2.0.0\n"),
        (
            "pyproject.toml",
            'dependencies = [\n    "Pillow==2.0.0",\n    "ruff==0.16.0",\n]',
        ),
    ],
)
def test_a_manifest_pinning_only_fixed_versions_is_lag(mod, manifest, text):
    alert = dependabot(CORE, 1, "Pillow", manifest=manifest)
    got = row(mod, CORE, alert, files={(CORE, manifest): text})
    assert (got["cls"], got["urls"]) == ("lag", "—")
    assert got["detail"].startswith("main resolves 2.")
    assert got["detail"].endswith(">= 2.0.0 · 1d old")


PNPM = """lockfileVersion: '9.0'

packages:

  '@babel/core@7.29.7':
    resolution: {integrity: sha512-x}

  dompurify@3.4.16:
    resolution: {integrity: sha512-y}
EXTRA
snapshots:

  dompurify@3.4.13(peer@1.0.0): {}

  next@15.5.20(react@18.3.1):
    dependencies: {}
"""
SECOND = "\n  dompurify@3.4.13:\n    resolution: {integrity: sha512-z}\n"


@pytest.mark.parametrize(
    "package, fixed, extra, cls",
    [
        ("dompurify", "3.4.16", "", "lag"),  # the 3.4.13 snapshots key is ignored
        ("dompurify", "3.4.16", SECOND, "untracked"),  # one instance below the fix
        ("@babel/core", "7.29.0", "", "lag"),  # a quoted, scoped key
        ("next", "15.5.27", "", "untracked"),  # only in snapshots: not resolved
    ],
)
def test_pnpm_lag_reads_every_packages_key_and_no_snapshots_key(
    mod, package, fixed, extra, cls
):
    alert = dependabot(COPILOT, 1, package, manifest=LOCK, fixed=fixed)
    files = {(COPILOT, LOCK): PNPM.replace("EXTRA", extra)}
    assert row(mod, COPILOT, alert, files=files)["cls"] == cls


@pytest.mark.parametrize(
    "manifest, text, fixed",
    [
        ("Gemfile.lock", "pillow==2.0.0\n", "2.0.0"),  # a manifest it does not read
        (REQ, "pillow==2.0.0\npillow==latest\n", "2.0.0"),  # an InvalidVersion
        (REQ, "pillow==9.0.0\n", None),  # no patched release
        (REQ, "pillow==1.9.9\n", "2.0.0"),
    ],
)
def test_anything_short_of_every_version_fixed_is_not_lag(mod, manifest, text, fixed):
    alert = dependabot(CORE, 1, manifest=manifest, fixed=fixed)
    assert row(mod, CORE, alert, files={(CORE, manifest): text})["cls"] == "untracked"


@pytest.mark.parametrize("age, cls", [(7, "lag"), (8, "untracked")])
def test_lag_older_than_seven_days_is_untracked_with_its_age(mod, age, cls):
    alert = dependabot(CORE, 1, age=age)
    got = row(mod, CORE, alert, files={(CORE, REQ): "pillow==2.0.0\n"})
    assert (got["cls"], f"{age}d old" in got["detail"]) == (cls, True)


# --- Exception ----------------------------------------------------------------


@pytest.mark.parametrize(
    "repo, cls", [(COPILOT, "exception"), ("faultmaven-dashboard", "untracked")]
)
def test_an_ignore_ghsas_entry_is_an_exception_in_its_own_repo_only(mod, repo, cls):
    config = json.dumps({"pnpm": {"auditConfig": {"ignoreGhsas": [GHSA]}}})
    alert = dependabot(repo, 1, "braces", manifest=LOCK, fixed=None)
    assert (
        row(mod, repo, alert, files={(COPILOT, "package.json"): config})["cls"] == cls
    )


EXCEPTIONS = """# Vulnerability Exceptions

## `lupa`
| CVE | [GHSA-1111-2222-3333](https://github.com/advisories/GHSA-1111-2222-3333) |

## Closed exceptions

- **`litellm`** ([GHSA-4444-5555-6666](https://github.com/advisories/GHSA-4444-5555-6666)).
"""


@pytest.mark.parametrize(
    "repo, ghsa, cls",
    [
        (CORE, "GHSA-1111-2222-3333", "exception"),
        (CORE, "GHSA-4444-5555-6666", "untracked"),  # under ## Closed exceptions
        ("faultmaven-kb-toolkit", "GHSA-1111-2222-3333", "untracked"),  # core's only
    ],
)
def test_the_exceptions_doc_counts_above_closed_exceptions_for_core_only(
    mod, repo, ghsa, cls
):
    files = {(CORE, mod.EXCEPTIONS_DOC): EXCEPTIONS}
    assert row(mod, repo, dependabot(repo, 1, ghsa=ghsa), files=files)["cls"] == cls


# --- Fetch --------------------------------------------------------------------


def test_every_page_of_issues_is_read(mod):
    gh = fake(mod, CORE, dependabot(CORE, 1))
    page_one = [issue(n, "unrelated") for n in range(200, 300)]
    gh.pages[f"{CORE}/issues"] = [page_one, [issue(150, f"| #1 | {GHSA} |")]]
    assert [r["detail"] for r in table(mod.sweep(gh, NOW))] == ["#150"]


def test_failed_fetches_are_counted_and_the_other_repos_still_classify(mod):
    gh = fake(mod, CORE, dependabot(CORE, 1))
    gh.errors.update(
        {
            "fm-sre-simulator/code-scanning/alerts": "Code Security must be enabled (HTTP 403)",
            "fm-sre-simulator/secret-scanning/alerts": "Secret scanning is disabled (HTTP 404)",
            f"{COPILOT}/dependabot/alerts": "Resource not accessible by integration (HTTP 403)",
        }
    )
    report = mod.sweep(gh, NOW)
    lines = report.splitlines()
    assert lines[:3] == [
        f"unavailable: {COPILOT} dependabot: gh: Resource not accessible by integration (HTTP 403)",
        "disabled: code-scanning in fm-sre-simulator",
        "disabled: secret-scanning in fm-sre-simulator",
    ]
    assert [r["cls"] for r in table(report)] == ["untracked"]
    assert lines[-1] == (
        "alerts 1: tracked 0 · exception 0 · lag 0 · untracked 1 (1 groups)"
        " · disabled 2 · unavailable 1"
    )


# --- Groups, scope and output -------------------------------------------------


def test_a_group_takes_its_worst_class_and_lists_only_the_regressed_alert(mod):
    manifests = ("requirements/cloud.txt", REQ, "requirements/test.txt")
    alerts = [dependabot(CORE, n, manifest=m) for n, m in enumerate(manifests, 1)]
    pins = ("pillow==2.0.0\n", "pillow==2.0.0\n", "pillow==1.0.0\n")
    files = {(CORE, m): pin for m, pin in zip(manifests, pins, strict=True)}
    report = mod.sweep(fake(mod, CORE, *alerts, files=files), NOW)
    (group,) = table(report)
    assert (group["cls"], group["urls"]) == ("untracked", alerts[2]["html_url"])
    assert report.splitlines()[-1].startswith(
        "alerts 3: tracked 0 · exception 0 · lag 2 · untracked 1 (1 groups)"
    )


def test_a_repo_outside_the_map_is_counted_on_one_line_and_not_classified(mod):
    outside = "fm-agent-service"
    gh = fake(mod, outside, dependabot(outside, 1), dependabot(outside, 2))
    gh.repos.append(outside)
    gh.errors[f"{outside}/code-scanning/alerts"] = "Code Security must be enabled"
    report = mod.sweep(gh, NOW)
    expected = (
        f"outside the map: {outside} dependabot 2 code-scanning disabled — see #2129"
    )
    assert expected in report.splitlines()
    assert table(report) == []
    assert report.splitlines()[-1].startswith("alerts 0:")


def test_the_sweep_only_reads(mod):
    gh = fake(mod, CORE, dependabot(CORE, 1))
    mod.sweep(gh, NOW)
    writes = {"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"}
    assert all(call[0] == "api" or call[:2] == ["repo", "list"] for call in gh.calls)
    assert not [call for call in gh.calls if writes & set(call)]
