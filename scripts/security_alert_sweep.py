#!/usr/bin/env python3
"""List every open Security-tab alert across the FaultMaven repositories and classify it.

``/process-top-issues`` runs this in step 2 (*Sort*) each round (#2120). It is
read-only: it files, comments on and dismisses nothing. Each open Dependabot,
code-scanning and secret-scanning alert in ``REPOS`` is, first match wins:

- ``exception`` (Dependabot): its GHSA is in the repo's ``package.json``
  ``pnpm.auditConfig.ignoreGhsas`` or, for core, in ``EXCEPTIONS_DOC`` above
  its ``## Closed exceptions`` heading;
- ``lag`` (Dependabot): every version the manifest resolves lies outside every
  vulnerable range the advisory lists for the package, and the manifest's last
  commit is at most ``LAG_DAYS`` old;
- ``tracked``: a line starting ``**Alerts:**`` in an open core issue, or an
  open PR in the alert's own repo, carries the alert's URL;
- ``unknown``: a source one of those answers needs could not be read;
- ``untracked``: everything else.

Files and commits are read from each repo's default branch. A fetch refused
because the feature is off is ``disabled`` (reported for public repos only);
any other failure is printed as ``unavailable`` and counted, and never read as
"nothing there". Rows for a repo that is not public print no package, advisory
or severity, no row names a secret type, and a private repo outside the map is
never named, so a paste of the output leaks none of them. The last line,
``queue:``, leaves secret scanning out of every count, for the public Queue.

Each repo in ``AUDITED`` also gets a ``scheduled audit:`` line (#2159): the
conclusion and age of the newest completed ``schedule`` run of its daily
lockfile audit; without one, ``none yet``, aged from the workflow file's last
commit on the default branch, or ``not on main``. Past ``AUDIT_STALE`` either
age adds ``stale``, and a workflow GitHub does not report ``active`` adds its
``state``. ``queue:`` counts every line but a fresh ``success`` or ``none yet``,
a failed read (``unavailable``) included. A repo not known to be public is not
read and only counted.

Usage: ``.venv/bin/python scripts/security_alert_sweep.py``
"""

import json
import operator
import re
import subprocess
import sys
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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
KINDS = ("dependabot", "code-scanning", "secret-scanning")
DISABLED = ("Code Security must be enabled", "Secret scanning is disabled")
LAG_DAYS = timedelta(days=7)
OPEN = "state=open&per_page=100"
EXCEPTIONS_DOC = "docs/operations/security/vulnerability-exceptions.md"
CLASSES = ("lag", "exception", "tracked", "untracked", "unknown")  # worst last
REPO_LIST = f"repo list {ORG} --no-archived --limit 1000 --json name,visibility"
RAW = ["api", "-H", "Accept: application/vnd.github.raw"]
PRIVATE, SECRET_NOTE = "(private)", "raise with the owner in session"
OPS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}
OPS["="] = operator.eq
AUDITED = ("faultmaven-dashboard", "faultmaven-copilot", "faultmaven-website")
AUDIT_FILE = ".github/workflows/scheduled-audit.yml"
AUDIT_WORKFLOW = "actions/workflows/scheduled-audit.yml"
# Completed runs only: a run still in progress hides no earlier result.
AUDIT_RUNS = f"{AUDIT_WORKFLOW}/runs?event=schedule&status=completed&per_page=1"
AUDIT_COMMITS = f"commits?path={AUDIT_FILE}&per_page=1"  # on the default branch
AUDIT_STALE = timedelta(days=2)  # a schedule disabled, never firing, or stopped

# A requirement line in a .txt manifest: name, extras, then the rest of the line.
REQ_LINE = re.compile(r"^[ \t]*([A-Za-z0-9][\w.-]*)[ \t]*(\[[^\]]*\])?(.*)$", re.M)
# A quoted requirement in pyproject.toml: name, extras, then the rest of the string.
REQUIREMENT = re.compile(
    r"""(["'])[ \t]*([A-Za-z0-9][\w.-]*)[ \t]*(\[[^\]]*\])?((?:(?!\1).)*)\1"""
)
EXACT_PIN = re.compile(r"^\s*==\s*([^\s;,]+)\s*(?:;.*)?$")
PNPM_KEY = re.compile(r"""^  (['"]?)(@?[^@'"\s]+)@([^'"\s]+?)\1:(?:\s|$)""")
CONSTRAINT = re.compile(r"^(<=|>=|<|>|=)\s*(\S+)$")

Runner = Callable[[list[str]], tuple[int, str, str]]


def gh(args: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


class FetchError(Exception):
    def __init__(self, stderr: str):
        self.disabled = any(message in stderr for message in DISABLED)
        self.line = stderr.strip().split("\n")[0] or "no output"
        super().__init__(self.line)


class Unreadable(Exception):
    """A source an alert's class depends on could not be read: the class is unknown."""


def fetch(run: Runner, path: str) -> list[dict]:
    """Every element of a list endpoint: ``--jq '.[]'`` prints one per line, all pages.

    Split on ``\\n`` only: ``splitlines`` also splits on U+0085 inside a JSON string.
    """
    code, out, err = run(["api", "--paginate", "--jq", ".[]", path])
    if code:
        raise FetchError(err)
    return [json.loads(line) for line in out.split("\n") if line.strip()]


def fetch_object(run: Runner, path: str) -> dict:
    """One object endpoint, unpaginated."""
    code, out, err = run(["api", path])
    if code:
        raise FetchError(err)
    return json.loads(out)


def alerts_path(repo: str, kind: str) -> str:
    return f"repos/{ORG}/{repo}/{kind}/alerts?{OPEN}"


def norm(name: str) -> str:
    """PEP 503 normalisation, so ``Pillow`` and ``pillow`` are one package."""
    return re.sub(r"[-_.]+", "-", name).lower()


def same_package(a: dict, b: dict) -> bool:
    if a["ecosystem"] != b["ecosystem"]:
        return False
    pip = a["ecosystem"] == "pip"
    return norm(a["name"]) == norm(b["name"]) if pip else a["name"] == b["name"]


def resolved_versions(manifest_path: str, text: str, package: str) -> list[str]:
    """Every version the manifest resolves ``package`` at; [] when it cannot say."""
    base = manifest_path.rsplit("/", 1)[-1]
    if base.endswith(".txt") or base == "pyproject.toml":
        # Every declaration of the package must be an exact pin.
        if base.endswith(".txt"):
            found = [(name, rest) for name, _, rest in REQ_LINE.findall(text)]
        else:
            found = [(name, rest) for _, name, _, rest in REQUIREMENT.findall(text)]
        pins = [EXACT_PIN.match(rest) for n, rest in found if norm(n) == norm(package)]
        return [pin.group(1) for pin in pins] if all(pins) else []
    versions, section = [], ""
    for line in text.split("\n") if base == "pnpm-lock.yaml" else []:
        if line and not line[0].isspace():
            section = line.rstrip()  # only `packages:` keys; `snapshots:` has peers
        elif section == "packages:" and (key := PNPM_KEY.match(line)):
            versions += [key.group(3)] if key.group(2) == package else []
    return versions


def in_range(version: Version, spec: str) -> bool:
    """Whether ``version`` meets every constraint of ``spec`` (``>= 1.0, < 2.0``)."""
    for part in spec.split(","):
        match = CONSTRAINT.match(part.strip())
        if not match or "-" in match.group(2):
            raise InvalidVersion(part)
        if not OPS[match.group(1)](version, Version(match.group(2))):
            return False
    return True


def outside_every_range(versions: list[str], ranges: list[str]) -> bool:
    """False for no versions, no ranges, an npm prerelease (``-``), a PEP 440 local
    version (``+``) or anything unparseable."""
    if not versions or not ranges or any(c in v for v in versions for c in "-+"):
        return False
    try:
        return not any(in_range(Version(v), r) for v in versions for r in ranges)
    except InvalidVersion:
        return False


@dataclass
class Alert:
    repo: str
    kind: str
    item: dict
    what: str
    advisory: str
    severity: str
    cls: str = ""
    detail: str = ""

    @property
    def url(self) -> str:
        return self.item["html_url"]


def to_alert(repo: str, kind: str, item: dict) -> Alert:
    what = advisory = severity = "—"  # a secret's type is never read
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
    return Alert(repo, kind, item, what, advisory, severity)


def carries(text: str, url: str) -> bool:
    return re.search(re.escape(url) + r"(?!\w)", text) is not None


def alert_lines(body: str) -> list[str]:
    """The only issue lines that track an alert: those starting ``**Alerts:**``."""
    return [line for line in body.split("\n") if line.startswith("**Alerts:**")]


class Sweep:
    def __init__(self, run: Runner, now: datetime):
        self.run, self.now = run, now
        self.files: dict[tuple[str, str], tuple[bool, str | None]] = {}
        self.ages: dict[tuple[str, str], timedelta | None] = {}
        self.prs: dict[str, list[tuple[str, str]] | None] = {}
        self.issues: list[tuple[str, str]] | None = None
        self.public: set[str] = set()
        self.disabled: dict[str, list[str]] = {}
        self.unavailable: list[tuple[bool, str]] = []  # (from secret scanning, line)

    def note(self, repo: str, what: str, line: str, private_as: str = "") -> None:
        """Record a failed fetch. For a repo not known to be public the line names
        the kind (or ``file fetch``) and only ``HTTP <nnn>``, else ``fetch failed``;
        a private repo outside the map is not named at all."""
        secret = what == "secret-scanning"
        if repo in self.public:
            self.unavailable.append((secret, f"unavailable: {repo} {what}: {line}"))
            return
        status = re.search(r"HTTP \d{3}", line)
        reason = status.group(0) if status else "fetch failed"
        named = f"{repo} {PRIVATE} {private_as or what}"
        named = named if repo in REPOS else "a private repo outside the map"
        self.unavailable.append((secret, f"unavailable: {named}: {reason}"))

    def file(self, repo: str, path: str) -> str | None:
        """A default-branch file; None when it is missing (404); else Unreadable."""
        if (repo, path) not in self.files:
            code, out, err = self.run([*RAW, f"repos/{ORG}/{repo}/contents/{path}"])
            failed = bool(code) and "(HTTP 404)" not in err
            if failed:
                self.note(repo, path, FetchError(err).line, private_as="file fetch")
            self.files[(repo, path)] = (failed, None if code else out)
        failed, text = self.files[(repo, path)]
        if failed:
            raise Unreadable(path)
        return text

    def manifest_age(self, repo: str, path: str) -> timedelta:
        """Time since the manifest's last commit on the default branch."""
        if (repo, path) not in self.ages:
            query = f"path={urllib.parse.quote(path, safe='/')}&per_page=1"
            jq = ["--jq", ".[0].commit.committer.date"]
            code, out, err = self.run(
                ["api", *jq, f"repos/{ORG}/{repo}/commits?{query}"]
            )
            try:
                when = datetime.fromisoformat(out.strip().replace("Z", "+00:00"))
                self.ages[(repo, path)] = None if code else self.now - when
            except (ValueError, TypeError):
                self.ages[(repo, path)] = None
            if self.ages[(repo, path)] is None:
                reason = (
                    FetchError(err).line if code else f"no commit date ({out.strip()})"
                )
                self.note(repo, f"commits {path}", reason, private_as="file fetch")
        age = self.ages[(repo, path)]
        if age is None:
            raise Unreadable(path)
        return age

    def exception_detail(self, alert: Alert) -> str:
        ghsa = alert.advisory.lower()
        try:
            config = json.loads(self.file(alert.repo, "package.json") or "{}")
            if ghsa in {
                g.lower() for g in config["pnpm"]["auditConfig"]["ignoreGhsas"]
            }:
                return "pnpm.auditConfig.ignoreGhsas"
        except (ValueError, KeyError, TypeError, AttributeError):
            pass
        doc = self.file(CORE, EXCEPTIONS_DOC) if alert.repo == CORE else None
        closed = re.compile(r"^## Closed exceptions", re.M)
        open_part = closed.split(doc or "", maxsplit=1)[0].lower()
        return EXCEPTIONS_DOC.rsplit("/", 1)[-1] if ghsa in open_part else ""

    def lag_detail(self, alert: Alert) -> tuple[str, timedelta] | None:
        """``(main resolves <versions> ..., manifest age)`` when it is fixed on main."""
        if not alert.item["security_vulnerability"].get("first_patched_version"):
            return None
        path = alert.item["dependency"]["manifest_path"]
        package = alert.item["dependency"]["package"]
        text = self.file(alert.repo, path)
        versions = resolved_versions(path, text, package["name"]) if text else []
        vulnerable = alert.item["security_advisory"].get("vulnerabilities") or []
        ranges = [
            v["vulnerable_version_range"]
            for v in vulnerable
            if same_package(v["package"], package)
        ]
        if not outside_every_range(versions, ranges):
            return None
        resolved = ", ".join(sorted(set(versions)))
        age = self.manifest_age(alert.repo, path)
        return f"main resolves {resolved}, outside every vulnerable range", age

    def repo_prs(self, repo: str) -> list[tuple[str, str]] | None:
        if repo not in self.prs:
            try:
                pulls = fetch(self.run, f"repos/{ORG}/{repo}/pulls?{OPEN}")
                self.prs[repo] = [
                    (f"PR {repo}#{p['number']}", f"{p.get('title')}\n{p.get('body')}")
                    for p in pulls
                ]
            except FetchError as exc:
                self.note(repo, "pulls", exc.line)
                self.prs[repo] = None
        return self.prs[repo]

    def classify(self, alert: Alert) -> None:
        try:
            alert.cls, alert.detail = self.verdict(alert)
        except Unreadable:
            alert.cls, alert.detail = "unknown", "a source is unavailable (above)"

    def verdict(self, alert: Alert) -> tuple[str, str]:
        stale = ""
        if alert.kind == "dependabot":
            if detail := self.exception_detail(alert):
                return "exception", detail
            if lag := self.lag_detail(alert):
                resolved, age = lag
                days = f"{age / timedelta(days=1):.1f}d"
                if age <= LAG_DAYS:
                    return "lag", f"{resolved} · manifest changed {days} ago"
                stale = f"{resolved}, but the manifest changed {days} ago"
        prs = self.repo_prs(alert.repo)
        if self.issues is None or prs is None:
            raise Unreadable("trackers")
        found = [label for label, text in self.issues + prs if carries(text, alert.url)]
        return ("tracked", ", ".join(found)) if found else ("untracked", stale)

    def collect(self, repo: str, kind: str) -> list[dict]:
        try:
            return fetch(self.run, alerts_path(repo, kind))
        except FetchError as exc:
            if not exc.disabled:
                self.note(repo, kind, exc.line)
            elif repo in self.public:  # a private repo's posture leaves no trace
                self.disabled.setdefault(kind, []).append(repo)
            return []

    def outside_counts(self, repo: str) -> dict[str, int | str]:
        counts: dict[str, int | str] = {}
        for kind in ("dependabot", "code-scanning"):
            try:
                counts[kind] = len(fetch(self.run, alerts_path(repo, kind)))
            except FetchError as exc:
                if not exc.disabled:
                    self.note(repo, kind, exc.line)
                counts[kind] = "disabled" if exc.disabled else "unavailable"
        return counts

    def outside_lines(self, repos: list[str]) -> list[str]:
        """One line per public repo; the private ones fold into one unnamed total."""
        lines = []
        for repo in (r for r in repos if r in self.public):
            shown = " ".join(f"{k} {n}" for k, n in self.outside_counts(repo).items())
            lines.append(f"outside the map: {repo} {shown} — see #2129")
        if hidden := [r for r in repos if r not in self.public]:
            counts = [n for r in hidden for n in self.outside_counts(r).values()]
            total = sum(n for n in counts if isinstance(n, int))
            alerts = "unavailable" if "unavailable" in counts else total
            plural = "s" if len(hidden) != 1 else ""
            lines.append(
                f"outside the map: {len(hidden)} private repo{plural}, alerts {alerts}"
            )
        return lines

    def audit_committed(self, path: str) -> str:
        """The audit file's last commit date on the default branch, or "" for none:
        gh's ``--jq`` prints an empty line for a missing value."""
        jq = ["--jq", ".[0].commit.committer.date"]
        code, out, err = self.run(["api", *jq, path + AUDIT_COMMITS])
        if code:
            raise FetchError(err)
        return out.strip()

    def audit_line(self, repo: str) -> tuple[bool, str]:
        """``(failing, line)``. GitHub registers a workflow at its first run on any
        branch, so ``none yet`` is aged from the file's last commit on main."""
        head, path = f"scheduled audit: {repo}", f"repos/{ORG}/{repo}/"
        try:
            state = fetch_object(self.run, path + AUDIT_WORKFLOW)["state"]
            if not isinstance(state, str):
                raise TypeError(state)
            runs = fetch_object(self.run, path + AUDIT_RUNS)["workflow_runs"]
            conclusion = runs[0]["conclusion"] if runs else None
            when = runs[0]["created_at"] if runs else self.audit_committed(path)
            age = None
            if when or runs:
                age = self.now - datetime.fromisoformat(when.replace("Z", "+00:00"))
        except FetchError as exc:  # a 404 too: a failed read is never "not there"
            return True, f"{head} unavailable: {exc.line}"
        except (ValueError, KeyError, TypeError, AttributeError):
            return True, f"{head} unavailable: unreadable response"
        flags = ["stale"] if age is not None and age > AUDIT_STALE else []
        flags += [] if state == "active" else [state]
        if age is None:  # no commit on the default branch touches the file
            shown = "not on main"
        elif runs:
            shown = f"{conclusion} {age / timedelta(days=1):.2f}d ago"
        else:
            shown = "none yet"
        failing = bool(flags) or age is None or (bool(runs) and conclusion != "success")
        return failing, ", ".join([f"{head} {shown}", *flags])

    def audit_lines(self) -> tuple[int, list[str]]:
        """``(failing, lines)``. A repo not known to be public is not read: it folds
        into one unnamed line and counts as failing, so no paste shows its runs."""
        read = [self.audit_line(repo) for repo in AUDITED if repo in self.public]
        lines = [line for _, line in read]
        if hidden := len(AUDITED) - len(read):
            plural = "s" if hidden != 1 else ""
            lines.append(f"scheduled audit: {hidden} repo{plural} not known public")
        return sum(failing for failing, _ in read) + hidden, lines

    def disabled_line(self, kind: str, repos: list[str]) -> str:
        listed = (
            f"{', '.join(repos[:-1])} and {repos[-1]}" if len(repos) > 1 else repos[0]
        )
        return f"disabled: {kind} in {listed}"

    def row(self, members: list[Alert]) -> tuple[int, tuple[str, ...]]:
        first = members[0]
        cls = max((a.cls for a in members), key=CLASSES.index)
        shown = [a for a in members if a.cls != "lag"] or members
        details = ", ".join(dict.fromkeys(a.detail for a in shown if a.detail))
        cells = [first.repo, first.kind, first.what, first.advisory, first.severity]
        if first.repo not in self.public:  # a private or unlisted repo
            cells[2:] = [PRIVATE] * 3
            tracked = [a.detail for a in shown if a.cls == "tracked"]
            details = ", ".join(dict.fromkeys(tracked)) or PRIVATE
        if first.kind == "secret-scanning":
            details = SECRET_NOTE
        urls = " ".join(a.url for a in shown if a.cls in ("untracked", "unknown"))
        return -CLASSES.index(cls), (*cells, cls, details or "—", urls or "—")

    def report(self) -> str:
        code, out, err = self.run(REPO_LIST.split())
        try:
            listed = (
                {} if code else {r["name"]: r["visibility"] for r in json.loads(out)}
            )
        except (ValueError, KeyError, TypeError):
            listed, code = {}, 1
        if code:
            line = f"unavailable: {ORG} repo list: {FetchError(err).line}"
            self.unavailable.append((False, line))
        self.public = {name for name, seen in listed.items() if seen == "PUBLIC"}
        try:
            issues = fetch(self.run, f"repos/{ORG}/{CORE}/issues?{OPEN}")
            self.issues = [
                (f"#{i['number']}", "\n".join(alert_lines(i.get("body") or "")))
                for i in issues
                if "pull_request" not in i
            ]
        except FetchError as exc:
            self.note(CORE, "issues", exc.line)
        for repo in REPOS:  # every one, so no alert (a secret's) decides what is read
            self.repo_prs(repo)
        alerts = [
            to_alert(repo, kind, item)
            for repo in REPOS
            for kind in KINDS
            for item in self.collect(repo, kind)
        ]
        for alert in alerts:
            self.classify(alert)
        outside = self.outside_lines(sorted(set(listed) - set(REPOS)))
        audits_failing, audits = self.audit_lines()

        groups: dict[tuple[str, str, str, str], list[Alert]] = {}
        for a in alerts:
            groups.setdefault((a.repo, a.kind, a.what, a.advisory), []).append(a)
        rows = sorted(self.row(members) for members in groups.values())
        count = {c: sum(a.cls == c for a in alerts) for c in CLASSES}
        untracked_groups = sum(1 for _, cells in rows if cells[5] == "untracked")
        lines = [line for _, line in self.unavailable]
        lines += [
            self.disabled_line(k, self.disabled[k]) for k in KINDS if k in self.disabled
        ]
        lines += [
            "",
            "| repo | kind | package or rule | advisory | severity | class | tracked by / detail | untracked or unknown alerts |",
            "|---|---|---|---|---|---|---|---|",
            *("| " + " | ".join(cells) + " |" for _, cells in rows),
            "",
            *outside,
            *audits,
        ]
        lines.append(
            f"alerts {len(alerts)}: tracked {count['tracked']} · exception {count['exception']}"
            f" · lag {count['lag']} · untracked {count['untracked']} ({untracked_groups} groups)"
            f" · unknown {count['unknown']} · disabled {sum(map(len, self.disabled.values()))}"
            f" · unavailable {len(self.unavailable)}"
        )
        # Safe to post on the public Queue: secret scanning is left out of every count.
        shared = [a for a in alerts if a.kind != "secret-scanning"]
        groups_u = sum(
            1 for _, c in rows if c[5] == "untracked" and c[1] != "secret-scanning"
        )
        unknown = sum(a.cls == "unknown" for a in shared)
        failed = sum(1 for secret, _ in self.unavailable if not secret)
        lines.append(
            f"queue: alerts {len(shared)} (secret scanning excluded): untracked groups"
            f" {groups_u}, unknown {unknown}, unavailable {failed},"
            f" scheduled audits failing {audits_failing}"
        )
        return "\n".join(lines)


def sweep(run: Runner = gh, now: datetime | None = None) -> str:
    return Sweep(run, now or datetime.now(timezone.utc)).report()


def main() -> int:
    print(sweep())
    return 0  # a report, not a gate


if __name__ == "__main__":
    sys.exit(main())
