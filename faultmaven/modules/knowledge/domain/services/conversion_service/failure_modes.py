"""Pure helpers over a batch of failure modes: forcing the frontmatter id
onto a generated runbook, and partitioning a job's failure modes into
survivors and refused duplicates before any conversion runs."""

from typing import Dict, List, Tuple

from faultmaven.modules.knowledge.domain.models.conversion import (
    ConversionError,
    FailureModeAnalysis,
    generate_runbook_id,
)
from faultmaven.utils.frontmatter import match_frontmatter


def _force_frontmatter_id(content: str, runbook_id: str) -> str:
    """Rewrite the frontmatter ``id`` field to ``runbook_id``.

    The conversion prompt instructs the LLM to use a specific kebab-case id,
    but some models emit the failure-mode title (or other text) as the id
    anyway, which then fails ``RunbookValidator`` for case-derived runbooks
    whose titles contain capitals or periods. This helper guarantees the
    frontmatter agrees with the filename + DB row by force-replacing the
    line. If the LLM omitted ``id`` entirely, we insert one as the first
    frontmatter field.
    """
    import re as _re

    fm_match = match_frontmatter(content)
    if not fm_match:
        # No frontmatter — caller's downstream validator will catch this;
        # we don't synthesize one here.
        return content
    # The shared grammar captures only the YAML body, so the delimiter lines
    # come from its offsets. head + body + tail is the whole block, byte for
    # byte, which is what lets the reassembly below stay a pure substitution.
    body = fm_match.group(1)
    head = content[: fm_match.start(1)]
    tail = content[fm_match.end(1) : fm_match.end()]
    if _re.search(r"^id:\s*.+$", body, _re.MULTILINE):
        new_body = _re.sub(
            r"^id:\s*.+$", f"id: {runbook_id}", body, count=1, flags=_re.MULTILINE
        )
    else:
        new_body = f"id: {runbook_id}\n{body}"
    return head + new_body + tail + content[fm_match.end() :]


def _partition_failure_modes(
    failure_modes: List[FailureModeAnalysis],
) -> Tuple[List[FailureModeAnalysis], List[ConversionError]]:
    """Decide which of one job's failure modes can yield a runbook, and why not.

    Two keys, applied in order, both meaning "an earlier mode already covers
    this one". They are in ONE function because they are one decision — which
    modes survive — and because splitting them is what let the second key's
    outcome be reported while the first key's stayed silent:

    1. ``(service, sorted(symptom_class))`` — the pre-existing collapse. It is
       deliberately coarse: an analysis routinely emits near-duplicate modes for
       one document. **Kept as a product behaviour, not endorsed as a key** —
       it also collapses modes that mint DIFFERENT ids and are therefore
       genuinely different runbooks (``"PostgreSQL Lag A"`` / ``"Lag B"`` under
       one service and symptom class), which is a real loss. What changed here
       is that it no longer collapses them **silently**: before #1258's follow-up
       it dropped them with no error, no warning, and a COMPLETED status, so a
       document that produced one runbook out of two looked like a clean run.
       Now every dropped mode is accounted for by its own ``ConversionError``,
       exactly like key 2, which also makes the job's status PARTIAL — the
       truthful answer for a document that yielded fewer runbooks than it
       analysed into.
    2. The minted ``runbook_id`` (#1258). Migration 046 admits one live draft
       per ``(enterprise_id, runbook_id)``, and nothing is committed while a
       multi-mode conversion runs, so two modes minting one id were invisible to
       both ``refuse_if_draft_slot_taken`` and ``_raise_if_runbook_id_taken``.
       They wrote both runbooks to a single file (both ids resolve to one
       ``draft_filename``) and then failed the commit with a bare
       ``IntegrityError`` — a 500 that says nothing, after silent data loss.
       Measured on ``e1cf27371``: ``"Redis OOM"`` and ``"redis oom"`` under
       service ``"redis"`` both mint ``redis-redis-oom``.

    Called BEFORE any conversion runs, which is what makes key 2 a fix rather
    than a nicer error: ``runbook_id_from_parts`` is a pure function of
    ``(service, title)``, so the whole batch is knowable up front and a losing
    mode never spends an LLM call or touches the filesystem. It also cannot race
    — the parallel branch dispatches with ``asyncio.gather``, so a check made
    inside the per-mode coroutine would be concurrent readers of one
    unsynchronised set.

    **Key 2 refuses rather than disambiguating.** Minting a distinct id for the
    second mode — the way the empty-slug branch appends a hash — cannot be made
    deterministic, and determinism is not optional (the disk scan reconciles a
    file to its row by this id). A disambiguator must be a function of something
    that DIFFERS between the two modes; but "collide" means their
    ``(service, title)`` are identical after normalisation, and every other
    field on a ``FailureModeAnalysis`` is LLM output for this one analysis pass,
    so hashing any of them gives a different id on a re-run. An ordinal suffix
    is worse still: it keys on position in ``analysis.failure_modes``, the
    model's own ordering. And even given a stable disambiguator, minting one
    would persist two runbooks that every normalised signal says are the same
    failure mode — exactly the indistinguishable pair migration 046 exists to
    reject, recreated one hash apart.

    **Key 2 is shape-agnostic on purpose.** It compares minted IDS, not titles,
    so it covers every shape ``_slug`` collapses without enumerating any:
    case, punctuation, underscore/hyphen, whitespace runs, leading/trailing
    trim, tabs and control characters, NBSP and zero-width joiners, emoji,
    accents and non-latin scripts, full-width forms; the ``service``/``title``
    join, which makes the delimiter part of the data (``("redis-cache", "OOM")``
    and ``("redis", "cache OOM")`` both mint ``redis-cache-oom``); and the
    over-length branch, both when two titles differ only where the slug rule
    normalises and when two distinct long slugs land on the same 4-hex
    disambiguator. A future tightening of the slug rule is covered for free. The
    mint itself is deliberately untouched: it is a PERSISTED id, and re-minting
    it would orphan rows that already exist — the boundary #1230 and #1243 drew.

    Keying on the id ALONE is safe because ``draft_filename`` is injective over
    the ids this mint produces: they match ``^[a-z0-9]+(-[a-z0-9]+)*$`` and are
    bounded by ``_MAX_RUNBOOK_ID_CHARS``, which is ``<= _MAX_SLUG_CHARS``, so
    ``draft_filename`` neither re-slugs nor truncates them. That is a property
    of two constants that are deliberately SEPARATE, so it is pinned by a test
    rather than trusted; a second index over filenames here would be dead code
    whose failure mode is looking like coverage.
    """
    survivors: List[FailureModeAnalysis] = []
    errors: List[ConversionError] = []
    claimed_coarse: Dict[Tuple[str, Tuple[str, ...]], FailureModeAnalysis] = {}
    claimed_id: Dict[str, FailureModeAnalysis] = {}

    for fm in failure_modes:
        coarse_key = (fm.service, tuple(sorted(fm.symptom_class)))
        # ``is not None``, never truthiness: the value is a model instance and
        # ``FailureModeAnalysis`` does not promise to be truthy. Same rule
        # ``_find_live_draft_owning`` states for its own id filter, and for the
        # same reason — a falsy-but-present value would read as "free slot".
        holder = claimed_coarse.get(coarse_key)
        if holder is not None:
            errors.append(
                ConversionError(
                    failure_mode_id=fm.id,
                    error=(
                        f"Failure mode {fm.id!r} ({fm.title!r}) was collapsed "
                        f"into {holder.id!r} ({holder.title!r}): both describe "
                        f"service {fm.service!r} with symptom class "
                        f"{sorted(fm.symptom_class)!r}, and this conversion "
                        f"produces one runbook per (service, symptom class). "
                        f"Give them distinct symptom classes in the source "
                        f"document if they are genuinely different failures."
                    ),
                    retryable=False,
                )
            )
            continue

        # The same pure function ``_convert_single_failure_mode`` calls, so the
        # id decided here and the id it mints for itself agree by construction
        # rather than by a parameter that could drift.
        runbook_id = generate_runbook_id(fm)
        holder = claimed_id.get(runbook_id)
        if holder is not None:
            errors.append(
                ConversionError(
                    failure_mode_id=fm.id,
                    error=(
                        f"Runbook id {runbook_id!r} was already claimed in this "
                        f"conversion by failure mode {holder.id!r} "
                        f"(service {holder.service!r}, title {holder.title!r}). "
                        f"This failure mode (service {fm.service!r}, title "
                        f"{fm.title!r}) mints the same id, because the id is "
                        f"derived from service and title with case, punctuation, "
                        f"whitespace and non-latin characters normalised away — "
                        f"so the two would produce one runbook, not two. Give "
                        f"them distinct titles in the source document, or merge "
                        f"them into a single failure mode."
                    ),
                    # Nothing about retrying frees the id — the same wording and
                    # the same reason as the committed-duplicate branch in
                    # ``_convert_single_failure_mode``.
                    retryable=False,
                )
            )
            continue

        claimed_coarse[coarse_key] = fm
        claimed_id[runbook_id] = fm
        survivors.append(fm)

    return survivors, errors
