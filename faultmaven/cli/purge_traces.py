"""Count, then delete, the trace store's traces and spans that started before a
cutoff (#2700).

An operator removes, once, the prompts, responses and exception text traces
written before #2698 hold: no retention bound can be verified. It acts on the
configured backend, workspace and project, across enterprises (traces before
#2578 carry no tags). Runbook: ``docs/operations/case-deletion.md``.

Usage (``fm-purge-traces``, installed with the package)
--------------------------------------------------------
    fm-purge-traces --before 2026-11-18T00:00:00Z            # dry run: counts
    fm-purge-traces --before 2026-11-18T00:00:00Z --delete --confirm-project faultmaven

Exit codes
----------
| 0 | dry run counted, or the delete left nothing before the cutoff and no
      error text in the project |
| 1 | refused: no trace backend, content capture requested, no such project,
      ``--confirm-project`` missing or not the project; or a failure when
      nothing was deleted, or a 4xx other than 408/429: read the error before
      running it again |
| 2 | argparse usage error, a ``--before`` without a UTC offset among them |
| 3 | re-run needed: traces or spans before the cutoff remain after the wait, or
      the backend failed after a delete may have gone out, other than with a
      4xx besides 408/429. A re-run is safe |
| 4 | done, but traces or spans with error text remain in the project: the
      cutoff is earlier than the last process that wrote content |
"""

from __future__ import annotations

# First: the process outputs go through the log chain from here on (#2888).
from faultmaven.cli import _log_chain  # isort: skip

import argparse
import sys
from datetime import datetime

from faultmaven.config.tls_root_set import apply_root_set
from faultmaven.exceptions import ServiceException

#: argparse's ``description``. A literal: ``python -OO`` strips docstrings.
_SUMMARY = (
    "Count, then delete, the trace store's traces and spans that started before "
    "a cutoff, in the configured project."
)


def _cutoff(text: str) -> datetime:
    try:
        value = datetime.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an ISO-8601 time: {text!r}") from exc
    if value.utcoffset() is None:
        raise argparse.ArgumentTypeError(
            f"{text!r} has no UTC offset (Z or +HH:MM), so it names no instant"
        )
    return value


def _counts(label: str, count) -> str:
    return f"{label}: {count.traces} traces, {count.spans} spans"


def purge(cutoff: datetime, *, delete: bool, confirm_project: str | None) -> int:
    """Configure the trace SDK as every process does, then count or purge."""
    # Here, not at import: ``apply_root_set`` runs before any client is imported.
    from faultmaven.config.settings import get_settings
    from faultmaven.infrastructure.observability import opik_sdk, trace_erasure

    try:
        backend = opik_sdk.configure(get_settings())
    except opik_sdk.TraceContentCaptureRefused as exc:
        print(f"❌ Refusing: {exc}")
        return 1
    print(
        f"Trace backend: {backend.url}  workspace: {backend.workspace}  "
        f"project: {backend.project}"
    )
    if not backend.configured:
        print(f"❌ Refusing: no trace backend is configured ({backend.reason}).")
        return 1
    if delete and confirm_project != backend.project:
        print(
            f"❌ Refusing: --confirm-project is {confirm_project!r}, but the "
            f"project is {backend.project!r}."
        )
        return 1
    try:
        result = trace_erasure.purge_traces_before(
            cutoff, delete=delete, project_name=backend.project, progress=print
        )
    except ServiceException as exc:
        print(f"❌ {exc}")
        return 3 if exc.details.get("rerun") else 1
    if result.missing:
        print(
            f"❌ Refusing: the backend has no project named {result.project!r} in "
            f"workspace {backend.workspace!r}."
        )
        return 1
    print(_counts(f"Started before {result.cutoff}", result.before))
    if result.after is None or result.error_text is None:
        print("Dry run: nothing deleted. --delete --confirm-project deletes them.")
        return 0
    print(_counts("Left after the delete", result.after))
    print(_counts("With error text, anywhere in the project", result.error_text))
    if result.after != trace_erasure.TraceCount(0, 0):
        print("⚠️  Some remain: run the same command again.")
        return 3
    if result.error_text != trace_erasure.TraceCount(0, 0):
        print("⚠️  Error text remains: the cutoff is too early (see the runbook).")
        return 4
    print("✅ Nothing before the cutoff remains, and no error text in the project.")
    return 0


def main() -> None:
    """Console entrypoint (``fm-purge-traces``)."""
    _log_chain.install_command_logging()
    apply_root_set()
    parser = argparse.ArgumentParser(prog="fm-purge-traces", description=_SUMMARY)
    parser.add_argument(
        "--before",
        required=True,
        type=_cutoff,
        help="ISO-8601 time with a UTC offset; traces that started before it",
    )
    parser.add_argument(
        "--delete", action="store_true", help="Delete them (default: count only)"
    )
    parser.add_argument(
        "--confirm-project",
        metavar="PROJECT",
        help="The trace project, named back exactly. Required with --delete.",
    )
    args = parser.parse_args()
    if args.delete and not args.confirm_project:
        print(
            "Refusing to delete without --confirm-project: name the trace project "
            "exactly. Run without --delete to see it."
        )
        sys.exit(1)
    sys.exit(
        purge(args.before, delete=args.delete, confirm_project=args.confirm_project)
    )


if __name__ == "__main__":
    main()
