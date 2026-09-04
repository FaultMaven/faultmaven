"""The confirmation gate every writing operator command shares.

Three commands had written this out verbatim — ``fm-remove-org-member``,
``fm-reassign-cases`` and ``fm-set-turn-cap`` — and a fourth would have made it
four. It is extracted here rather than left as a convention because the two
rules it encodes are the kind that rot silently when copied:

* **``--dry-run`` with ``--yes`` is a usage error, not a preference.** The two
  invocations differ by one flag, so an operator editing the previous command
  can end up with both. Silently taking the dry-run branch would exit 0 and read
  as "it was written" when nothing was — the one failure that looks exactly like
  success.
* **Neither flag refuses.** A run with no decision is an operator who has not
  made one, and every caller of this writes something an operator would want
  back. The refusal happens here, before any container initialisation or
  database connection, so it costs nothing and cannot half-run.

Each command still supplies its own sentence about what the write *does*: the
shape is shared, the consequence is not.
"""

from __future__ import annotations

import argparse
import sys


def require_confirmation(
    parser: argparse.ArgumentParser, args: argparse.Namespace, consequence: str
) -> None:
    """Refuse unless the operator has chosen ``--dry-run`` or ``--yes``.

    Args:
        parser: The command's parser, so a usage error exits 2 through argparse
            rather than through a second exit-code convention of our own.
        args: Parsed arguments carrying ``dry_run`` and ``yes``.
        consequence: One sentence naming what the write changes, printed on the
            refusal. The caller owns it because only the caller knows.

    Exits 2 on the mutually-exclusive pair, 1 on no decision, and returns
    normally when exactly one of the two was given.
    """
    if args.dry_run and args.yes:
        parser.error(
            "--dry-run and --yes are mutually exclusive: pass --dry-run to "
            "preview, --yes to write."
        )
    if not args.dry_run and not args.yes:
        print(
            f"❌ Refusing to run without --yes. {consequence}\n"
            "   Use --dry-run first to see what would change."
        )
        sys.exit(1)
