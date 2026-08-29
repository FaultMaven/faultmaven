"""The turn's file uploads, as the two metadata keys that report them.

One derivation, for every path a turn can take (#1229).

#1224 made ``novel_files_uploaded`` live but derived it inside
``MilestoneEngine._process_response_structured`` — the engine's generation
path. Every other path that carries the same attachments dropped the signal:
the engine's deterministic early-return branches, its terminal short-circuit,
and the SERVICE-routed intent handlers in ``InvestigationService`` that answer
without calling the engine at all. An identical file therefore counted as
progress on one path and was invisible on another, and the two degradation
warnings below — the whole observability half of #1224 — could only ever fire
on the one path that owned the derivation.

Deliberately a free function over ``(case_id, current_turn, attachments)``
rather than a method over ``Case``: the engine and the agent service both need
it, and keeping it free of the ``Case`` model lets the service import it
without dragging the engine module in.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["report_turn_uploads"]


def report_turn_uploads(
    case_id: str,
    current_turn: int,
    attachments: list[dict[str, Any]] | None,
) -> dict[str, list[str]]:
    """Return ``{}`` / ``{"files_uploaded": [...]}`` /
    ``{..., "novel_files_uploaded": [...]}`` for the turn's attachments.

    Empty when nothing was attached, so a caller can ``update()`` a metadata
    dict with it unconditionally and the keys stay ABSENT on a turn with no
    uploads — which is what every consumer already expects.

    The service owns the rows: it persists each ``UploadedFile`` and appends it
    to the same ``Case`` object before the engine runs. Nothing here creates
    anything; it only reads what arrived and records it. See the tombstone at
    ``_create_uploaded_file_from_attachment``'s former site (#1210).
    """
    report: dict[str, list[str]] = {}
    if not attachments:
        return report

    for attachment in attachments:
        file_id = attachment.get("file_id")
        if not file_id:
            logger.warning(
                "Attachment metadata carries no file_id; not reported "
                "on turn %s of case %s",
                current_turn,
                case_id,
            )
            continue
        # Every attachment ON the turn, deduped re-submissions included.
        report.setdefault("files_uploaded", []).append(file_id)
        # #1136's stall-net arm: data the case did not already hold.
        #
        # Threaded in as a TRI-STATE by
        # ``investigation_service._engine_attachment_metadata``, because the
        # engine cannot recover any of it: the authoritative row is on
        # ``case.uploaded_files`` before this runs, so the old
        # ``file_id not in {f.file_id for f in case.uploaded_files}`` was False
        # for a brand-new upload as much as for a deduped one, and the arm was
        # dead on every turn (#1210).
        #
        # True = dedup ran and found nothing. False = it found the bytes.
        # None (or absent) = it never ran, so nobody knows — and "unknown" is
        # scored as NOT novel, the same conservative direction as the other
        # ``novel_*`` arms. Logged either way: a silent False is exactly the
        # failure mode #1210 was, and a silent True on an undetermined signal
        # is that failure inverted, arming the stall net on a re-submission.
        is_novel = attachment.get("is_novel")
        if is_novel is None:
            logger.warning(
                "Attachment %s carries no novelty signal (undetermined or "
                "absent); treating as not novel on turn %s of case %s. "
                "#1136's upload progress arm cannot arm for this turn.",
                file_id,
                current_turn,
                case_id,
            )
        if is_novel:
            report.setdefault("novel_files_uploaded", []).append(file_id)

    return report
