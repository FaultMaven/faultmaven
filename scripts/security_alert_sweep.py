#!/usr/bin/env python3
"""List every open Security-tab alert across the FaultMaven repositories and classify it.

``/process-top-issues`` runs this in step 2 (*Sort*) each round and files every
``untracked`` group as a core issue naming the repo (#2120). It is read-only:
it files, comments on and dismisses nothing. Each open Dependabot,
code-scanning and secret-scanning alert in ``REPOS`` is, first match wins:

- ``exception`` (Dependabot): its GHSA is in the repo's ``package.json``
  ``pnpm.auditConfig.ignoreGhsas`` or, for core, in ``EXCEPTIONS_DOC`` above
  its ``## Closed exceptions`` heading;
- ``lag`` (Dependabot): the manifest resolves the package only at versions at
  or above the first patched one, and the alert is at most ``LAG_DAYS`` old;
- ``tracked``: an open core issue whose ``**Repo: FaultMaven/<r>.**`` /
  ``**Repos: … and ….**`` line names the alert's repo (core when it has none)
  carries the alert's URL, its ``Dependabot #<n>`` / ``CodeQL #<n>`` /
  ``secret scanning #<n>`` key, or its GHSA in a table row or an
  ``**Alerts:**`` line; or an open PR in the alert's repo carries its URL;
- ``untracked``: everything else.

Files are read from each repo's default branch. A fetch refused because the
feature is off is ``disabled``; any other failure is ``unavailable``, printed
and counted, and never read as zero alerts.

Usage: ``.venv/bin/python scripts/security_alert_sweep.py``
"""

import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from packaging.version import InvalidVersion, Version

ORG, CORE = "FaultMaven", "faultmaven"
# The Repository Map in the workspace CLAUDE.md (/home/swhouse/product/.claude/CLAUDE.md).
# Unarchived repos outside it are only counted, pending the scope ruling in #2129.
REPOS = (
    "faultmaven",
    "faultmaven-cloud",
    "faultmaven-copilot",
    "faultmaven-dashboard",
    "faultmaven-slack-agent",
    "faultmaven-website",
    "faultmaven-enterprise-infra",
    "faultmaven-kb-toolkit",
    "fm-sre-simulator",
    "faultmaven-doc-internal",
)
KEY_WORD = {
    "dependabot": "Dependabot",
    "code-scanning": "CodeQL",
    "secret-scanning": "secret scanning",
}
DISABLED = ("Code Security must be enabled", "Secret scanning is disabled")
LAG_DAYS = 7
OPEN = "state=open&per_page=100"
EXCEPTIONS_DOC = "docs/operations/security/vulnerability-exceptions.md"
CLASSES = ("lag", "exception", "tracked", "untracked")  # worst last
REPO_LIST = f"repo list {ORG} --no-archived --limit 1000 --json name --jq .[].name"

REPO_LINE = re.compile(r"^\*\*Repos?:\s+([^*\n]+)\*\*", re.M)
REPO_NAME = re.compile(rf"{ORG}/([\w.-]*[\w-])")
TXT_PIN = re.compile(r"^([\w.-]+)(?:\[[^\]]*\])?==([^\s;]+)[ \t]*(?:;.*)?$", re.M)
TOML_PIN = re.compile(r"""(["'])([\w.-]+)(?:\[[^\]]*\])?==([^\s;"']+)[^"'\n]*\1""")
PNPM_KEY = re.compile(r"""^  (['"]?)(@?[^@'"\s]+)@([^'"\s]+?)\1:(?:\s|$)""")

Runner = Callable[[list[str]], tuple[int, str, str]]


def gh(args: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


class FetchError(Exception):
    def __init__(self, stderr: str):
        self.disabled = any(message in stderr for message in DISABLED)
        self.line = (stderr.strip().splitlines() or ["no output"])[0]
        super().__init__(self.line)


def fetch(run: Runner, path: str) -> list[dict]:
    """Every element of a list endpoint: ``--jq '.[]'`` prints one per line, all pages."""
    code, out, err = run(["api", "--paginate", "--jq", ".[]", path])
    if code:
        raise FetchError(err)
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def alerts_path(repo: str, kind: str) -> str:
    return f"repos/{ORG}/{repo}/{kind}/alerts?{OPEN}"


def norm(name: str) -> str:
    """PEP 503 normalisation, so ``Pillow`` and ``pillow`` are one package."""
    return re.sub(r"[-_.]+", "-", name).lower()


def resolved_versions(manifest_path: str, text: str, package: str) -> list[str]:
    """Every version the manifest resolves ``package`` at; [] for one this cannot read."""
    base = manifest_path.rsplit("/", 1)[-1]
    if base.endswith(".txt"):
        pins = TXT_PIN.findall(text)
        return [ver for name, ver in pins if norm(name) == norm(package)]
    if base == "pyproject.toml":
        pins = TOML_PIN.findall(text)
        return [ver for _, name, ver in pins if norm(name) == norm(package)]
    versions, section = [], ""
    for line in text.splitlines() if base == "pnpm-lock.yaml" else []:
        if line and not line[0].isspace():
            section = line.rstrip()  # only `packages:` keys; `snapshots:` has peers
        elif section == "packages:" and (key := PNPM_KEY.match(line)):
            versions += [key.group(3)] if key.group(2) == package else []
    return versions


@dataclass
class Tracker:
    label: str
    repos: set[str]
    text: str  # where a URL counts
    rows: str = ""  # where a GHSA counts: table rows and **Alerts:** lines
    keys: bool = True  # whether `Dependabot #<n>`-style keys count


def issue_tracker(issue: dict) -> Tracker:
    body = issue.get("body") or ""
    named = REPO_LINE.search(body)
    repos = set(REPO_NAME.findall(named.group(1))) if named else {CORE}
    rows = [
        line.strip()
        for line in body.splitlines()
        if re.match(r"\s*\|.*\|\s*$", line) or line.lstrip().startswith("**Alerts:**")
    ]
    text = f"{issue.get('title') or ''}\n{body}"
    return Tracker(f"#{issue['number']}", repos, text, "\n".join(rows).lower())


@dataclass
class Alert:
    repo: str
    kind: str
    item: dict
    what: str
    advisory: str
    severity: str
    age: int
    cls: str = ""
    detail: str = ""

    @property
    def url(self) -> str:
        return self.item["html_url"]


def to_alert(repo: str, kind: str, item: dict, now: datetime) -> Alert:
    created = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
    advisory = severity = "—"
    if kind == "dependabot":
        package = item["dependency"]["package"]
        pip = package.get("ecosystem") == "pip"
        what = norm(package["name"]) if pip else package["name"]
        advisory = item["security_advisory"]["ghsa_id"]
        severity = item["security_advisory"].get("severity") or "—"
    elif kind == "code-scanning":
        rule = item.get("rule") or {}
        what = rule.get("id") or "?"
        severity = rule.get("security_severity_level") or rule.get("severity") or "—"
    else:
        what = item.get("secret_type_display_name") or item.get("secret_type") or "?"
    return Alert(repo, kind, item, what, advisory, severity, (now - created).days)


def names_alert(tracker: Tracker, alert: Alert) -> bool:
    if re.search(re.escape(alert.url) + r"(?!\w)", tracker.text):
        return True
    key = rf"\b{KEY_WORD[alert.kind]} #{alert.item['number']}\b"
    if tracker.keys and re.search(key, tracker.text, re.I):
        return True
    return alert.kind == "dependabot" and alert.advisory.lower() in tracker.rows


class Sweep:
    def __init__(self, run: Runner, now: datetime):
        self.run, self.now = run, now
        self.files: dict[tuple[str, str], str | None] = {}
        self.prs: dict[str, list[Tracker]] = {}
        self.issues: list[Tracker] = []
        self.disabled: dict[str, list[str]] = {}
        self.unavailable: list[str] = []

    def file(self, repo: str, path: str) -> str | None:
        """A file on the repo's default branch, or None when it cannot be read."""
        if (repo, path) not in self.files:
            raw = ["api", "-H", "Accept: application/vnd.github.raw"]
            code, out, _ = self.run([*raw, f"repos/{ORG}/{repo}/contents/{path}"])
            self.files[(repo, path)] = None if code else out
        return self.files[(repo, path)]

    def exception_detail(self, alert: Alert) -> str:
        ghsa = alert.advisory.lower()
        try:
            config = json.loads(self.file(alert.repo, "package.json") or "{}")
            ignored = config["pnpm"]["auditConfig"]["ignoreGhsas"]
            if ghsa in {g.lower() for g in ignored}:
                return "pnpm.auditConfig.ignoreGhsas"
        except (ValueError, KeyError, TypeError, AttributeError):
            pass
        doc = self.file(CORE, EXCEPTIONS_DOC) if alert.repo == CORE else None
        closed = re.compile(r"^## Closed exceptions", re.M)
        open_part = closed.split(doc or "", maxsplit=1)[0].lower()
        return EXCEPTIONS_DOC.rsplit("/", 1)[-1] if ghsa in open_part else ""

    def lag_detail(self, alert: Alert) -> str:
        """``main resolves <versions> >= <fixed>`` when every version is fixed, else ''."""
        patched = alert.item["security_vulnerability"].get("first_patched_version")
        fixed = (patched or {}).get("identifier")
        path = alert.item["dependency"]["manifest_path"]
        text = self.file(alert.repo, path) if fixed else None
        name = alert.item["dependency"]["package"]["name"]
        versions = resolved_versions(path, text, name) if text else []
        try:
            if not versions or any(Version(v) < Version(fixed) for v in versions):
                return ""
        except InvalidVersion:
            return ""
        return f"main resolves {', '.join(sorted(set(versions)))} >= {fixed}"

    def repo_prs(self, repo: str) -> list[Tracker]:
        if repo not in self.prs:
            try:
                pulls = fetch(self.run, f"repos/{ORG}/{repo}/pulls?{OPEN}")
            except FetchError as exc:
                self.unavailable.append(f"unavailable: {repo} pulls: {exc.line}")
                pulls = []
            self.prs[repo] = []
            # Only a URL counts in a PR: `Dependabot #<n>` keys and GHSAs do not.
            for p in pulls:
                text = f"{p.get('title') or ''}\n{p.get('body') or ''}"
                label = f"PR {repo}#{p['number']}"
                self.prs[repo].append(Tracker(label, {repo}, text, keys=False))
        return self.prs[repo]

    def classify(self, alert: Alert) -> None:
        stale = ""
        if alert.kind == "dependabot":
            if detail := self.exception_detail(alert):
                alert.cls, alert.detail = "exception", detail
                return
            if (lag := self.lag_detail(alert)) and alert.age <= LAG_DAYS:
                alert.cls, alert.detail = "lag", f"{lag} · {alert.age}d old"
                return
            stale = f"{lag}, but the alert is {alert.age}d old" if lag else ""
        candidates = self.issues + self.repo_prs(alert.repo)
        found = [
            t.label
            for t in candidates
            if alert.repo in t.repos and names_alert(t, alert)
        ]
        alert.cls = "tracked" if found else "untracked"
        alert.detail = ", ".join(found) if found else stale

    def collect(self, repo: str, kind: str) -> list[dict]:
        try:
            return fetch(self.run, alerts_path(repo, kind))
        except FetchError as exc:
            if exc.disabled:
                self.disabled.setdefault(kind, []).append(repo)
            else:
                self.unavailable.append(f"unavailable: {repo} {kind}: {exc.line}")
            return []

    def outside_line(self, repo: str) -> str:
        counts = []
        for kind in ("dependabot", "code-scanning"):
            try:
                counts.append(f"{kind} {len(fetch(self.run, alerts_path(repo, kind)))}")
            except FetchError as exc:
                state = "disabled" if exc.disabled else f"unavailable ({exc.line})"
                counts.append(f"{kind} {state}")
        return f"outside the map: {repo} {' '.join(counts)} — see #2129"

    def report(self) -> str:
        try:
            issues = fetch(self.run, f"repos/{ORG}/{CORE}/issues?{OPEN}")
            self.issues = [issue_tracker(i) for i in issues if "pull_request" not in i]
        except FetchError as exc:
            self.unavailable.append(f"unavailable: {CORE} issues: {exc.line}")
        alerts = [
            to_alert(repo, kind, item, self.now)
            for repo in REPOS
            for kind in KEY_WORD
            for item in self.collect(repo, kind)
        ]
        for alert in alerts:
            self.classify(alert)
        code, out, err = self.run(REPO_LIST.split())
        if code:
            self.unavailable.append(
                f"unavailable: {ORG} repo list: {FetchError(err).line}"
            )
        unmapped = [] if code else sorted(set(out.split()) - set(REPOS))
        outside = [self.outside_line(repo) for repo in unmapped]

        groups: dict[tuple[str, str, str, str], list[Alert]] = {}
        for a in alerts:
            groups.setdefault((a.repo, a.kind, a.what, a.advisory), []).append(a)
        rows = []
        for (repo, kind, what, advisory), members in groups.items():
            cls = max((a.cls for a in members), key=CLASSES.index)
            shown = [a for a in members if a.cls != "lag"] or members
            detail = ", ".join(dict.fromkeys(a.detail for a in shown if a.detail))
            urls = " ".join(a.url for a in shown if a.cls == "untracked")
            cells = (repo, kind, what, advisory, members[0].severity, cls)
            cells += (detail or "—", urls or "—")
            rows.append((-CLASSES.index(cls), cells))

        count = {c: sum(a.cls == c for a in alerts) for c in CLASSES}
        untracked_groups = sum(1 for _, cells in rows if cells[5] == "untracked")
        lines = [*self.unavailable]
        lines += [
            f"disabled: {kind} in {', '.join(r)}" for kind, r in self.disabled.items()
        ]
        lines += [
            "",
            "| repo | kind | package or rule | advisory | severity | class | tracked by / detail | untracked alerts |",
            "|---|---|---|---|---|---|---|---|",
            *("| " + " | ".join(cells) + " |" for _, cells in sorted(rows)),
            "",
            *outside,
        ]
        lines.append(
            f"alerts {len(alerts)}: tracked {count['tracked']} · exception {count['exception']}"
            f" · lag {count['lag']} · untracked {count['untracked']} ({untracked_groups} groups)"
            f" · disabled {sum(map(len, self.disabled.values()))} · unavailable {len(self.unavailable)}"
        )
        return "\n".join(lines)


def sweep(run: Runner = gh, now: datetime | None = None) -> str:
    return Sweep(run, now or datetime.now(timezone.utc)).report()


def main() -> int:
    print(sweep())
    return 0  # a report, not a gate


if __name__ == "__main__":
    sys.exit(main())
