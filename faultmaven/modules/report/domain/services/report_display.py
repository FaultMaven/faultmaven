"""Display normalization for STORED report content (#1097 follow-up).

#1097 split the conclusion's engine-internal notation from its user-facing prose
and normalized the two fields at the read, on the premise that terminal cases
never recompute. That premise is right about the *fields* and wrong about the
*report*: a resolution summary is not rendered on read — it is generated once at
the terminal transition and persisted as markdown in ``reports.content``, and
every read path serves that column verbatim. So the surface a user actually
looks at (the Dashboard's Report tab) kept showing the leak on every case
resolved before the fix, including the one the issue was filed on.

This normalizes those two lines at the read, reusing the SAME field normalizers
rather than re-deriving the detection — the report is markdown, but the two
values inside it are the very fields those functions already own.

Deliberately NOT a backfill. Regenerating a stored summary would rewrite a
historical record with everything the generator has learned since (new sections,
different evidence selection), which is a far larger change than removing two
pieces of notation. Rewriting is also self-limiting: a report generated after
#1097 can never match, so this only ever touches legacy rows.

The case TRANSCRIPT is deliberately untouched. The summary is also embedded in
the terminal assistant message, and that is a record of what was said at the
time; editing it would be falsifying the conversation, not fixing a rendering.
"""

from typing import Optional

from faultmaven.modules.case.contracts import (
    established_by_for_display,
    mechanism_for_display,
)

# The report's own rendered labels (report_generation_service). Matching on
# these rather than on free text keeps the rewrite scoped to the two lines the
# generator produced, so nothing in a user's evidence or an LLM's prose can be
# caught by it.
_ESTABLISHED_PREFIX = "_Established by: "
_ESTABLISHED_SUFFIX = "._"
_MECHANISM_PREFIX = "**How it produced the symptom:** "


def normalize_stored_report_content(content: Optional[str]) -> Optional[str]:
    """Rewrite the two pre-#1097 lines in a stored report, or return it as-is.

    Line-scoped and label-anchored: only a line the generator itself wrote as
    the provenance or the mechanism is considered, and only its VALUE is passed
    to the field normalizer that owns it. A line either normalizer leaves
    unchanged is left byte-identical, so a current report round-trips exactly.
    """
    if not content:
        return content

    lines = content.split("\n")
    changed = False
    for i, line in enumerate(lines):
        if line.startswith(_ESTABLISHED_PREFIX) and line.endswith(_ESTABLISHED_SUFFIX):
            value = line[len(_ESTABLISHED_PREFIX) : -len(_ESTABLISHED_SUFFIX)]
            fixed = established_by_for_display(value)
            if fixed != value:
                lines[i] = f"{_ESTABLISHED_PREFIX}{fixed}{_ESTABLISHED_SUFFIX}"
                changed = True
        elif line.startswith(_MECHANISM_PREFIX):
            value = line[len(_MECHANISM_PREFIX) :]
            fixed = mechanism_for_display(value)
            if fixed != value:
                lines[i] = f"{_MECHANISM_PREFIX}{fixed}"
                changed = True

    return "\n".join(lines) if changed else content
