"""Tests for scripts/security_alert_sweep.py (#2120, scheduled audits #2159).

Each test pins one rule against the naive form that misread live data or a review
probe: a URL in prose, a failed fetch read as "nothing there", a 502 read as a
missing file, npm prereleases and PEP 440 local versions, an advisory's other
ranges, a U+0085 inside a JSON string, and private detail, posture or a secret
type in a paste of the output; a dispatch or a run in progress read as the
schedule's result. ``FakeGh`` answers as gh 2.46 does, offline.
"""

import importlib.util
import json
import urllib.parse
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


def iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


class FakeGh:
    """``gh`` as 2.46 behaves: ``--paginate`` alone prints one JSON array per page,
    concatenated; ``--jq '.[]'`` prints one object per line across pages, leaving
    non-ASCII (U+0085 included) unescaped; a refused call exits 1 with
    ``gh: <message> (HTTP <code>)`` on stderr; ``--jq`` prints an empty line for a
    missing value (a path no commit touches). A workflow's ``/runs`` applies
    GitHub's ``event``, ``status`` and ``per_page`` filters, newest first."""

    def __init__(self, repos):
        self.visibility = {name: "PUBLIC" for name in repos}
        self.pages: dict[str, list[list[dict]]] = {}
        self.files: dict[tuple[str, str], str] = {}
        self.commits: dict[tuple[str, str], str] = {}  # default: a day ago
        self.runs: dict[str, list[dict]] = {}  # default: a success a day ago
        self.states: dict[str, str] = {}  # a workflow's state; default: active
        self.errors: dict[str, str] = {}
        self.calls: list[list[str]] = []

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["repo", "list"]:
            if "repo list" in self.errors:
                return 1, "", f"gh: {self.errors['repo list']}\n"
            listing = [{"name": n, "visibility": v} for n, v in self.visibility.items()]
            return 0, json.dumps(listing), ""
        path, _, query = args[-1].removeprefix("repos/FaultMaven/").partition("?")
        if path in self.errors:
            return 1, '{"message": "..."}', f"gh: {self.errors[path]}\n"
        if path.endswith("/commits"):
            manifest = urllib.parse.parse_qs(query)["path"][0]
            default = iso(NOW - timedelta(days=1))
            return (
                0,
                self.commits.get((path[: -len("/commits")], manifest), default) + "\n",
                "",
            )
        if path.endswith(".yml"):
            state = self.states.get(path.split("/")[0], "active")
            return 0, json.dumps({"state": state}), ""
        if path.endswith("/runs"):
            want = urllib.parse.parse_qs(query)
            runs = [
                r
                for r in self.runs.get(path.split("/")[0], [wf_run()])
                if [r["event"]] == want["event"]
                and [r["status"]] == want.get("status", [r["status"]])
            ]
            page = {"workflow_runs": runs[: int(want["per_page"][0])]}
            return 0, json.dumps(page), ""
        if "Accept: application/vnd.github.raw" in args:
            text = self.files.get(tuple(path.split("/contents/")))
            return (
                (1, "", "gh: Not Found (HTTP 404)") if text is None else (0, text, "")
            )
        pages = self.pages.get(path, [[]])[: None if "--paginate" in args else 1]
        if args[args.index("--jq") + 1 :][:1] == [".[]"]:
            lines = (json.dumps(o, ensure_ascii=False) + "\n" for p in pages for o in p)
            return 0, "".join(lines), ""
        return 0, "".join(json.dumps(page) for page in pages), ""


def dependabot(repo, n, package="pillow", ghsa=GHSA, manifest=REQ, ranges=("< 2.0.0",)):
    package_info = {"ecosystem": "npm" if manifest == LOCK else "pip", "name": package}
    vulnerable = [
        {"package": package_info, "vulnerable_version_range": r} for r in ranges
    ]
    return {
        "number": n,
        "html_url": f"https://github.com/FaultMaven/{repo}/security/dependabot/{n}",
        "dependency": {"manifest_path": manifest, "package": package_info},
        "security_advisory": {
            "ghsa_id": ghsa,
            "severity": "high",
            "vulnerabilities": vulnerable,
        },
        "security_vulnerability": {"first_patched_version": {"identifier": "2.0.0"}},
    }


def secret(repo, n):
    url = f"https://github.com/FaultMaven/{repo}/security/secret-scanning/{n}"
    kind = {"secret_type": "openai_api_key", "secret_type_display_name": "OpenAI Key"}
    return {"number": n, "html_url": url, **kind}


def issue(n, body):
    return {"number": n, "title": "an issue", "body": body}


def fake(mod, repo, *alerts, issues=(), files=None, pulls=None, kind="dependabot"):
    gh = FakeGh(mod.REPOS)
    gh.pages[f"{repo}/{kind}/alerts"] = [list(alerts)]
    gh.pages[f"{CORE}/issues"] = [list(issues)]
    gh.files.update(files or {})
    for pr_repo, prs in (pulls or {}).items():
        gh.pages[f"{pr_repo}/pulls"] = [prs]
    return gh


def table(report):
    """The report's table rows, as dicts."""
    keys = ("repo", "kind", "what", "advisory", "severity", "cls", "detail", "urls")
    rows = [line.strip("|").split(" | ") for line in report.split("\n")]
    rows = [r for r in rows if len(r) == len(keys)][1:]  # drop the header
    return [dict(zip(keys, (cell.strip() for cell in r), strict=True)) for r in rows]


def row(mod, gh):
    """Sweep and return the one row."""
    (only,) = table(mod.sweep(gh, NOW))
    return only


def cls_of(mod, repo, alert, **setup):
    return row(mod, fake(mod, repo, alert, **setup))["cls"]


def summary(report, prefix="alerts "):
    """The totals line (or the ``queue:`` line) of a report."""
    return next(line for line in report.split("\n") if line.startswith(prefix))


# --- Tracked ------------------------------------------------------------------

URL = "https://github.com/FaultMaven/faultmaven/security/dependabot/1"


@pytest.mark.parametrize(
    "body, cls",
    [
        (f"**Repo: FaultMaven/faultmaven.**\n**Alerts:** {URL} and more", "tracked"),
        (f"Some prose.\n**Alerts:** {URL}\r\n", "tracked"),
        (f"**Alerts:** {URL}2", "untracked"),  # alert #12, not #1
        (f"The alert {URL} is out of scope.", "untracked"),
        (f"| #1 | pillow | {URL} |", "untracked"),
        (f"> **Alerts:** {URL}", "untracked"),  # quoted from elsewhere
        (f"  **Alerts:** {URL}", "untracked"),  # indented: not a line starting it
        (f"Next\u0085line\u2028here.\n**Alerts:** {URL}", "tracked"),  # one JSON line
        (f"Prose\u0085**Alerts:** {URL}", "untracked"),  # U+0085 is not a line break
        (f"| Alert | Advisory |\n|---|---|\n| #1 | {GHSA} |", "untracked"),
        (f"Dependabot #1 is fixed by {GHSA}.", "untracked"),
    ],
)
def test_only_an_alerts_line_carrying_the_url_tracks(mod, body, cls):
    assert cls_of(mod, CORE, dependabot(CORE, 1), issues=[issue(7, body)]) == cls


def test_a_core_pr_in_the_issue_list_does_not_track_as_an_issue(mod):
    pr = {**issue(8, f"**Alerts:** {URL}"), "pull_request": {"url": "..."}}
    assert cls_of(mod, CORE, dependabot(CORE, 1), issues=[pr]) == "untracked"


@pytest.mark.parametrize(
    "repo, title, body, detail",
    [
        (CORE, "fix(deps): pillow", f"Fixes {URL}.", "PR faultmaven#50"),
        (CORE, f"Fix {URL}", None, "PR faultmaven#50"),
        (COPILOT, "x", URL, "—"),  # another repo's PR
    ],
)
def test_an_open_pr_in_the_alerts_own_repo_carrying_the_url_tracks(
    mod, repo, title, body, detail
):
    pulls = {repo: [{"number": 50, "title": title, "body": body}]}
    assert (
        row(mod, fake(mod, CORE, dependabot(CORE, 1), pulls=pulls))["detail"] == detail
    )


# --- Unknown ------------------------------------------------------------------


@pytest.mark.parametrize(
    "failing, pin",
    [
        (f"{CORE}/issues", "1.0.0"),
        (f"{CORE}/pulls", "1.0.0"),
        (f"{CORE}/contents/{REQ}", "2.0.0"),
        (f"{CORE}/contents/package.json", "2.0.0"),
        (f"{CORE}/commits", "2.0.0"),  # the manifest's last commit, for the lag bound
    ],
)
def test_a_source_that_failed_makes_the_alert_unknown(mod, failing, pin):
    gh = fake(mod, CORE, dependabot(CORE, 1), files={(CORE, REQ): f"pillow=={pin}\n"})
    gh.errors[failing] = "HTTP 502: Bad Gateway (https://api.github.com/...)"
    report = mod.sweep(gh, NOW)
    got = table(report)[0]
    assert (got["cls"], got["urls"]) == ("unknown", URL)
    assert report.split("\n")[0].startswith("unavailable: faultmaven ")
    assert " · unknown 1 · disabled 0 · unavailable 1" in summary(report)


def test_a_missing_file_is_not_a_failure(mod):
    report = mod.sweep(fake(mod, CORE, dependabot(CORE, 1)), NOW)  # every file 404s
    assert table(report)[0]["cls"] == "untracked"
    assert summary(report).endswith(" · unknown 0 · disabled 0 · unavailable 0")


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
    alert = dependabot(CORE, 1, "pillow", manifest=manifest)
    advisory = {"ecosystem": "pip", "name": "Pillow"}  # the advisory's own spelling
    alert["security_advisory"]["vulnerabilities"][0]["package"] = advisory
    got = row(mod, fake(mod, CORE, alert, files={(CORE, manifest): text}))
    assert (got["cls"], got["urls"]) == ("lag", "—")
    assert got["detail"].startswith("main resolves 2.")
    assert got["detail"].endswith(
        ", outside every vulnerable range · manifest changed 1.0d ago"
    )


PYPROJECT = """[project]
dependencies = ["pillow>=1.0; python_version < '3.12'"]

[project.optional-dependencies]
images = ["Pillow[avif]==2.0.0"]
"""


def test_a_pyproject_needs_every_declaration_exactly_pinned(mod):
    alert = dependabot(CORE, 1, manifest="pyproject.toml")
    files = {(CORE, "pyproject.toml"): PYPROJECT}
    assert cls_of(mod, CORE, alert, files=files) == "untracked"


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
PRERELEASE = "\n  dompurify@3.4.16-0:\n    resolution: {integrity: sha512-z}\n"


@pytest.mark.parametrize(
    "package, extra, cls",
    [
        ("dompurify", "", "lag"),  # the 3.4.13 snapshots key is ignored
        ("dompurify", SECOND, "untracked"),  # one instance below the fix
        ("dompurify", PRERELEASE, "untracked"),  # PEP 440: 3.4.16-0 is a post-release
        ("@babel/core", "", "lag"),  # a quoted, scoped key
        ("next", "", "untracked"),  # only in snapshots: not resolved
    ],
)
def test_pnpm_lag_reads_every_packages_key_and_no_snapshots_key(
    mod, package, extra, cls
):
    ranges = ("< 3.4.16",) if package == "dompurify" else ("< 7.0.0",)
    alert = dependabot(COPILOT, 1, package, manifest=LOCK, ranges=ranges)
    files = {(COPILOT, LOCK): PNPM.replace("EXTRA", extra)}
    assert cls_of(mod, COPILOT, alert, files=files) == cls


@pytest.mark.parametrize(
    "ranges, version, cls",
    [
        (("< 1.2.5", ">= 2.0.0, < 2.1.3"), "2.0.0", "untracked"),  # in the second range
        (("< 1.2.5", ">= 2.0.0, < 2.1.3"), "2.1.3", "lag"),
        (("<= 2.0.0",), "2.0.0", "untracked"),
        (("> 1.0.0, <= 1.5.0",), "1.0.0", "lag"),
        (("= 2.0.0",), "2.0.0", "untracked"),
        (("= 2.0.0",), "2.0.1", "lag"),
        (("< 2.0.0", "garbage"), "3.0.0", "untracked"),
        (("< 2.0.0-rc.1",), "3.0.0", "untracked"),
        (("<= 2.5.1",), "2.5.1+cpu", "untracked"),  # PEP 440 sorts it above 2.5.1
        ((), "3.0.0", "untracked"),
    ],
)
def test_lag_needs_every_version_outside_every_range(mod, ranges, version, cls):
    alert = dependabot(CORE, 1, ranges=ranges)
    other = {"ecosystem": "pip", "name": "not-pillow"}
    alert["security_advisory"]["vulnerabilities"].append(
        {"package": other, "vulnerable_version_range": "< 99.0"}
    )
    files = {(CORE, REQ): f"pillow=={version}\n"}
    assert cls_of(mod, CORE, alert, files=files) == cls


@pytest.mark.parametrize(
    "manifest, text",
    [
        ("Gemfile.lock", "pillow==3.0.0\n"),  # a manifest it does not read
        (REQ, "pillow==3.0.0\npillow==latest\n"),  # an InvalidVersion
        (REQ, "pillow>=1.0\npillow==3.0.0\n"),  # a ranged line beside an exact one
        (REQ, "  pillow>=1.0\npillow==3.0.0\n"),  # the same, the ranged line indented
    ],
)
def test_an_unreadable_manifest_or_version_is_not_lag(mod, manifest, text):
    alert = dependabot(CORE, 1, manifest=manifest)
    assert cls_of(mod, CORE, alert, files={(CORE, manifest): text}) == "untracked"


def test_no_patched_release_is_not_lag(mod):
    alert = dependabot(CORE, 1, ranges=("< 1.0.0",))
    alert["security_vulnerability"]["first_patched_version"] = None
    files = {(CORE, REQ): "pillow==3.0.0\n"}
    assert cls_of(mod, CORE, alert, files=files) == "untracked"


@pytest.mark.parametrize(
    "age, cls, shown",
    [
        (timedelta(days=7), "lag", "7.0d"),
        (timedelta(days=7, hours=1), "untracked", "7.0d"),
    ],
)
def test_lag_counts_seven_days_from_the_manifests_last_commit(mod, age, cls, shown):
    gh = fake(mod, CORE, dependabot(CORE, 1), files={(CORE, REQ): "pillow==2.0.0\n"})
    gh.commits[(CORE, REQ)] = iso(NOW - age)
    got = row(mod, gh)
    assert (got["cls"], f"manifest changed {shown} ago" in got["detail"]) == (cls, True)


# --- Exception ----------------------------------------------------------------


@pytest.mark.parametrize(
    "repo, cls", [(COPILOT, "exception"), ("faultmaven-dashboard", "untracked")]
)
def test_an_ignore_ghsas_entry_is_an_exception_in_its_own_repo_only(mod, repo, cls):
    config = json.dumps({"pnpm": {"auditConfig": {"ignoreGhsas": [GHSA]}}})
    alert = dependabot(repo, 1, "braces", manifest=LOCK)
    assert cls_of(mod, repo, alert, files={(COPILOT, "package.json"): config}) == cls


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
    assert cls_of(mod, repo, dependabot(repo, 1, ghsa=ghsa), files=files) == cls


# --- Redaction ----------------------------------------------------------------


def test_a_private_repos_row_names_no_package_advisory_severity_or_version(mod):
    repo = "faultmaven-kb-toolkit"  # its packages here are invented
    alerts = [
        dependabot(repo, 1, "examplepkg"),
        dependabot(repo, 2, "examplepkg", "GHSA-x"),
    ]
    gh = fake(mod, repo, *alerts, files={(repo, REQ): "examplepkg==2.0.0\n"})
    gh.visibility[repo] = "PRIVATE"
    gh.commits[(repo, REQ)] = iso(NOW - timedelta(days=30))  # stale: names the version
    gh.pages[f"{CORE}/issues"] = [[issue(7, f"**Alerts:** {alerts[0]['html_url']}")]]
    report = mod.sweep(gh, NOW)
    rows = table(report)
    assert [(r["what"], r["advisory"], r["severity"]) for r in rows] == [
        ("(private)",) * 3
    ] * 2
    assert [(r["cls"], r["detail"]) for r in rows] == [
        ("untracked", "(private)"),
        ("tracked", "#7"),
    ]
    assert rows[0]["urls"] == alerts[1]["html_url"]
    for detail in ("examplepkg", "GHSA", "2.0.0", "high"):
        assert detail not in report


def test_a_failed_repo_list_is_unavailable_and_every_row_reads_private(mod):
    gh = fake(mod, CORE, dependabot(CORE, 1))
    gh.errors["repo list"] = "HTTP 502: Bad Gateway"
    report = mod.sweep(gh, NOW)
    assert row(mod, gh)["what"] == "(private)"
    lines = report.split("\n")
    assert lines[0] == "unavailable: FaultMaven repo list: gh: HTTP 502: Bad Gateway"
    assert summary(report).endswith(" · unavailable 1")


@pytest.mark.parametrize("visibility", ["PUBLIC", "PRIVATE"])
def test_no_row_names_a_secret_type(mod, visibility):
    gh = fake(mod, CORE, secret(CORE, 17), kind="secret-scanning")
    gh.visibility[CORE] = visibility
    report = mod.sweep(gh, NOW)
    got = row(mod, gh)
    assert (got["kind"], got["cls"]) == ("secret-scanning", "untracked")
    assert got["detail"] == "raise with the owner in session"
    assert "openai" not in report.lower()


# --- Fetch, groups and scope --------------------------------------------------


def test_every_page_of_issues_is_read(mod):
    gh = fake(mod, CORE, dependabot(CORE, 1))
    page_one = [issue(n, "unrelated") for n in range(200, 300)]
    gh.pages[f"{CORE}/issues"] = [page_one, [issue(150, f"**Alerts:** {URL}")]]
    assert row(mod, gh)["detail"] == "#150"


def test_failed_fetches_are_counted_and_the_other_repos_still_classify(mod):
    gh = fake(mod, CORE, dependabot(CORE, 1))
    gh.errors.update(
        {
            "faultmaven-website/code-scanning/alerts": "Code Security must be enabled (HTTP 403)",
            f"{COPILOT}/secret-scanning/alerts": "Secret scanning is disabled (HTTP 404)",
            f"{COPILOT}/dependabot/alerts": "Resource not accessible by integration (HTTP 403)",
        }
    )
    report = mod.sweep(gh, NOW)
    lines = report.split("\n")
    assert lines[:3] == [
        f"unavailable: {COPILOT} dependabot: gh: Resource not accessible by integration (HTTP 403)",
        "disabled: code-scanning in faultmaven-website",
        "disabled: secret-scanning in faultmaven-copilot",  # recorded first, printed second
    ]
    assert [r["cls"] for r in table(report)] == ["untracked"]
    assert summary(report) == (
        "alerts 1: tracked 0 · exception 0 · lag 0 · untracked 1 (1 groups)"
        " · unknown 0 · disabled 2 · unavailable 1"
    )


def test_a_group_takes_its_worst_class_and_lists_only_what_needs_action(mod):
    manifests = ("a/requirements.txt", REQ, "b/requirements.txt", "c/requirements.txt")
    alerts = [dependabot(CORE, n, manifest=m) for n, m in enumerate(manifests, 1)]
    files = {(CORE, m): "pillow==2.0.0\n" for m in manifests[:2]}
    files[(CORE, manifests[2])] = "pillow==1.0.0\n"
    gh = fake(mod, CORE, *alerts, files=files)
    report = mod.sweep(gh, NOW)
    (group,) = table(report)
    assert (group["cls"], group["urls"]) == (
        "untracked",
        " ".join(a["html_url"] for a in alerts[2:]),
    )
    gh = fake(mod, CORE, *alerts, files=files)
    gh.errors[f"{CORE}/contents/{manifests[3]}"] = "HTTP 502: Bad Gateway"
    assert row(mod, gh)["cls"] == "unknown"  # worse than untracked
    assert summary(report).startswith(
        "alerts 4: tracked 0 · exception 0 · lag 2 · untracked 2 (1 groups)"
    )


def test_a_repo_outside_the_map_is_counted_on_one_line_and_not_classified(mod):
    outside = "fm-agent-service"
    gh = fake(mod, outside, dependabot(outside, 1), dependabot(outside, 2))
    gh.visibility[outside] = "PUBLIC"
    gh.errors[f"{outside}/code-scanning/alerts"] = "no analysis found (HTTP 404)"
    report = mod.sweep(gh, NOW)
    lines = report.split("\n")
    assert (
        f"outside the map: {outside} dependabot 2 code-scanning unavailable — see #2129"
        in lines
    )
    assert (
        lines[0]
        == f"unavailable: {outside} code-scanning: gh: no analysis found (HTTP 404)"
    )
    assert table(report) == []
    assert summary(report).startswith("alerts 0:")
    assert summary(report).endswith(" · unavailable 1")


def test_no_private_repos_posture_or_outside_name_appears(mod):
    """Fixture visibility and posture are invented; no real repo's configuration."""
    gh = fake(mod, CORE, dependabot(CORE, 1))
    mapped, outside = ("faultmaven-dashboard", SLACK), ("priv-a", "priv-b", "priv-c")
    gh.visibility.update({repo: "PRIVATE" for repo in mapped + outside})
    off, off_secrets = "Code Security must be enabled", "Secret scanning is disabled"
    for repo in mapped:
        gh.errors[f"{repo}/code-scanning/alerts"] = off
        gh.errors[f"{repo}/secret-scanning/alerts"] = off_secrets
    gh.errors[f"{COPILOT}/secret-scanning/alerts"] = off_secrets
    gh.errors["priv-a/code-scanning/alerts"] = off
    gh.pages["priv-b/code-scanning/alerts"] = [[{"number": 1}]]
    gh.errors["priv-c/dependabot/alerts"] = "HTTP 502: Bad Gateway (https://x/priv-c)"
    report = mod.sweep(gh, NOW)
    assert report.split("\n")[:2] == [
        "unavailable: a private repo outside the map: HTTP 502",
        "disabled: secret-scanning in faultmaven-copilot",
    ]
    assert "outside the map: 3 private repos, alerts unavailable" in report.split("\n")
    assert [repo for repo in mapped + outside if repo in report] == []
    assert summary(report).endswith(" · disabled 1 · unavailable 1")
    del gh.errors["priv-c/dependabot/alerts"]
    assert "outside the map: 3 private repos, alerts 1" in mod.sweep(gh, NOW)


QUEUE = "queue: alerts 1 (secret scanning excluded): untracked groups "


@pytest.mark.parametrize(
    "issues_fail, expected",
    [(False, "1, unknown 0, unavailable 1"), (True, "0, unknown 1, unavailable 2")],
)
def test_the_queue_line_leaves_secret_scanning_out_of_every_count(
    mod, issues_fail, expected
):
    reports = []
    for secrets in (False, True):
        gh = fake(mod, CORE, dependabot(CORE, 1))
        gh.errors.update({f"{CORE}/issues": "HTTP 502"} if issues_fail else {})
        gh.errors[f"{SLACK}/pulls"] = "HTTP 502"  # its only alerts are secrets, if any
        if secrets:
            gh.pages[f"{CORE}/secret-scanning/alerts"] = [[secret(CORE, 17)]]
            gh.pages[f"{SLACK}/secret-scanning/alerts"] = [[secret(SLACK, 3)]]
            gh.errors[f"{COPILOT}/secret-scanning/alerts"] = "HTTP 502: Bad Gateway"
        reports.append(mod.sweep(gh, NOW))
    last = QUEUE + expected + ", scheduled audits failing 0"
    assert [report.split("\n")[-1] for report in reports] == [last] * 2
    pulls = f"unavailable: {SLACK} pulls: gh: HTTP 502"
    assert [pulls in report.split("\n") for report in reports] == [True, True]
    assert summary(reports[0]) != summary(reports[1])


BAD_GATEWAY = (
    "HTTP 502: Bad Gateway (https://api.github.com/repos/FaultMaven/r/contents/x/y)"
)
RESET = "error reading requirements/dev.txt: connection reset"


@pytest.mark.parametrize(
    "visibility, failing, stderr, expected",
    [
        ("PRIVATE", f"contents/{REQ}", BAD_GATEWAY, "(private) file fetch: HTTP 502"),
        ("PRIVATE", "commits", RESET, "(private) file fetch: fetch failed"),
        ("PUBLIC", f"contents/{REQ}", BAD_GATEWAY, f"{REQ}: gh: {BAD_GATEWAY}"),
    ],
)
def test_a_private_repos_failed_fetch_names_no_path(
    mod, visibility, failing, stderr, expected
):
    repo = "faultmaven-kb-toolkit"
    gh = fake(mod, repo, dependabot(repo, 1), files={(repo, REQ): "pillow==2.0.0\n"})
    gh.visibility[repo] = visibility
    gh.errors[f"{repo}/{failing}"] = stderr
    report = mod.sweep(gh, NOW)
    assert report.split("\n")[0] == f"unavailable: {repo} {expected}"
    assert ("requirements" in report) == (visibility == "PUBLIC")


# --- Scheduled audits (#2159) -------------------------------------------------

DASHBOARD, WEBSITE = "faultmaven-dashboard", "faultmaven-website"
WORKFLOW = "actions/workflows/scheduled-audit.yml"
AUDIT_FILE = ".github/workflows/scheduled-audit.yml"
RUNS = f"{WORKFLOW}/runs"
NOT_FOUND = "Not Found (HTTP 404)"  # a failed read, never "not there"


def wf_run(conclusion="success", days=1.0, status="completed", event="schedule"):
    return {
        "event": event,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "created_at": iso(NOW - timedelta(days=days)),
    }


def scheduled(report):
    return [line for line in report.split("\n") if line.startswith("scheduled audit:")]


@pytest.mark.parametrize(
    "runs, shown, failing",
    [
        ([wf_run()], "success 1.00d ago", 0),
        ([wf_run("failure", 0.5)], "failure 0.50d ago", 1),
        ([wf_run("cancelled")], "cancelled 1.00d ago", 1),  # any non-success
        ([wf_run(days=2)], "success 2.00d ago", 0),
        ([wf_run(days=2.03)], "success 2.03d ago, stale", 1),
        ([wf_run("failure", 3)], "failure 3.00d ago, stale", 1),  # counted once
        # A run in progress hides no earlier result, and is not the schedule's yet.
        ([wf_run(status="in_progress"), wf_run("failure")], "failure 1.00d ago", 1),
        ([wf_run(status="queued")], "none yet", 0),
        ([wf_run(event="workflow_dispatch"), wf_run(event="push")], "none yet", 0),
        ([], "none yet", 0),
        ([{**wf_run(), "created_at": None}], "unavailable: unreadable response", 1),
        (NOT_FOUND, f"unavailable: gh: {NOT_FOUND}", 1),
    ],
)
def test_a_repos_line_reads_its_newest_completed_scheduled_audit(
    mod, runs, shown, failing
):
    gh = fake(mod, CORE, dependabot(CORE, 1))
    if isinstance(runs, str):
        gh.errors[f"{DASHBOARD}/{RUNS}"] = runs
    else:
        gh.runs[DASHBOARD] = runs
    report = mod.sweep(gh, NOW)
    assert scheduled(report) == [
        f"scheduled audit: {DASHBOARD} {shown}",
        f"scheduled audit: {COPILOT} success 1.00d ago",
        f"scheduled audit: {WEBSITE} success 1.00d ago",
    ]
    assert summary(report, "queue: ").endswith(f", scheduled audits failing {failing}")


def dashboard(mod, gh):
    """The dashboard's line, after its name, and the queue's failing count."""
    report = mod.sweep(gh, NOW)
    line = scheduled(report)[0].removeprefix(f"scheduled audit: {DASHBOARD} ")
    return line, int(summary(report, "queue: ").rsplit(" ", 1)[1])


@pytest.mark.parametrize(
    "runs, days, error, shown, failing",
    [
        ([], None, None, "not on main", 1),
        # Today, before the merge: a PR run registered the workflow, main lacks it.
        ([wf_run(event="pull_request")], None, None, "not on main", 1),
        ([], 3, None, "none yet, stale", 1),  # a schedule that never fires
        ([], 2, None, "none yet", 0),  # the same bound as a stale run
        ([], 1, None, "none yet", 0),
        ([], 1, "HTTP 502: Bad Gateway", "unavailable: gh: HTTP 502: Bad Gateway", 1),
        ([wf_run()], None, None, "success 1.00d ago", 0),  # read only for none yet
    ],
)
def test_none_yet_is_aged_from_the_files_last_commit_on_main(
    mod, runs, days, error, shown, failing
):
    gh = fake(mod, CORE, dependabot(CORE, 1))
    gh.runs[DASHBOARD] = runs
    committed = "" if days is None else iso(NOW - timedelta(days=days))
    gh.commits[(DASHBOARD, AUDIT_FILE)] = committed
    gh.errors.update({f"{DASHBOARD}/commits": error} if error else {})
    assert dashboard(mod, gh) == (shown, failing)


@pytest.mark.parametrize(
    "state, runs, shown, failing",
    [
        ("active", [wf_run()], "success 1.00d ago", 0),
        ("disabled_manually", [wf_run()], "success 1.00d ago, disabled_manually", 1),
        (
            "disabled_inactivity",
            [wf_run(days=61)],
            "success 61.00d ago, stale, disabled_inactivity",
            1,
        ),
        ("disabled_manually", [], "none yet, disabled_manually", 1),
        (None, [wf_run()], "unavailable: gh: HTTP 502: Bad Gateway", 1),
    ],
)
def test_a_workflow_not_active_adds_its_state_and_fails_whatever_the_run_says(
    mod, state, runs, shown, failing
):
    gh = fake(mod, CORE, dependabot(CORE, 1))
    gh.runs[DASHBOARD], gh.states[DASHBOARD] = runs, state or "active"
    bad = {f"{DASHBOARD}/{WORKFLOW}": "HTTP 502: Bad Gateway"}
    gh.errors.update({} if state else bad)
    assert dashboard(mod, gh) == (shown, failing)


def test_failing_audits_move_only_the_queue_lines_new_count(mod):
    gh = fake(mod, CORE, dependabot(CORE, 1))
    gh.errors.update({f"{repo}/{RUNS}": NOT_FOUND for repo in mod.AUDITED})
    report = mod.sweep(gh, NOW)
    assert len(scheduled(report)) == 3
    assert summary(report).endswith(" · unknown 0 · disabled 0 · unavailable 0")
    assert summary(report, "queue: ").endswith(
        "unknown 0, unavailable 0, scheduled audits failing 3"
    )


def test_a_repo_not_known_public_is_counted_and_neither_read_nor_named(mod):
    gh = fake(mod, CORE, dependabot(CORE, 1))
    gh.visibility[DASHBOARD] = "PRIVATE"
    gh.runs[DASHBOARD] = [wf_run("failure")]
    report = mod.sweep(gh, NOW)
    assert scheduled(report) == [
        f"scheduled audit: {COPILOT} success 1.00d ago",
        f"scheduled audit: {WEBSITE} success 1.00d ago",
        "scheduled audit: 1 repo not known public",
    ]
    assert DASHBOARD not in report and "failure" not in report
    assert summary(report, "queue: ").endswith(", scheduled audits failing 1")
    gh.errors["repo list"] = "HTTP 502: Bad Gateway"  # no repo is known public
    hidden = ["scheduled audit: 3 repos not known public"]
    assert scheduled(mod.sweep(gh, NOW)) == hidden
    reads = [call[-1] for call in gh.calls if "scheduled-audit" in call[-1]]
    assert reads and not [read for read in reads if DASHBOARD in read]


def test_the_sweep_only_reads(mod):
    gh = fake(mod, CORE, dependabot(CORE, 1), files={(CORE, REQ): "pillow==2.0.0\n"})
    mod.sweep(gh, NOW)
    writes = {"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"}
    assert all(call[0] == "api" or call[:2] == ["repo", "list"] for call in gh.calls)
    assert not [call for call in gh.calls if writes & set(call)]
