"""Turning a resolved case into a runbook draft: the dedup scope resolver, the conversion call, and the case-level draft check that gates it."""

import asyncio
import logging
from typing import Any

from faultmaven.core.investigation.milestone_engine.regeneration import (
    _remaining_regens_for,
)
from faultmaven.core.investigation.milestone_engine.turn_commit import TurnCommitPlan
from faultmaven.modules.case.contracts import (
    Case,
    MessageRowKind,
    append_message_row,
)

from .terminal_replies import (
    _generate_runbook_anyway_suggestion,
    _resolved_suggestions,
)

logger = logging.getLogger(__name__)

#: Strong references to the conversion tasks in flight. The event loop keeps
#: only a weak reference to a task, and a conversion task spends its first
#: moments parked on its turn's commit gate, so without this a task could be
#: collected before it ever ran. Each removes itself when done.
_CONVERSION_TASKS: set["asyncio.Task[None]"] = set()


class RunbookCreator:
    """Creates a runbook draft from a resolved case, deduplicating against existing runbooks in the caller's visible scope before converting."""

    def __init__(self, *, case_locks, deps) -> None:
        self._case_locks = case_locks
        self.deps = deps

    async def case_has_runbook_draft(self, case: "Case") -> bool:
        """Whether a runbook draft has already been generated for this case.

        Drives the "hide once used" gate on the Generate-runbook affordance:
        each case gets at most one chat-side generation. Re-rolls happen in
        the Dashboard Drafts editor, not via repeated chat clicks.

        Returns False (i.e. "show the affordance") on any lookup failure or
        when the conversion service isn't wired — preserves the legacy
        behaviour of always offering the affordance when state is unknown,
        which is the safer default for a forward action.
        """
        conversion_service = self.deps.conversion_service
        if conversion_service is None:
            return False
        try:
            drafts = await conversion_service.list_drafts_for_case(case.case_id)
        except Exception:
            return False
        return any(d for d in drafts)

    async def handle_runbook_creation(
        self,
        case: "Case",
        metadata: dict[str, Any],
        *,
        plan: TurnCommitPlan,
        dedup_confirmed: bool = False,
    ) -> dict[str, Any]:
        """Evaluate readiness + dedup, then create runbook draft (fire-and-forget).

        The conversion is spawned here but WAITS on a gate in ``plan``
        (``plan.gate()``) before it does anything: the gate is released once
        this turn has committed and cancelled if it does not (#1882). So a
        turn that fails starts no conversion, and its retry is not told
        "already exists" for a draft the failed turn produced — and of two
        clicks racing on one case, the one whose commit loses (409) starts
        nothing.

        Only RESOLVED cases reach this path — runbooks codify complete
        troubleshooting scenarios (root cause + verified solution).
        Eligibility is gated by the caller (`_process_terminal_turn`).

        Flow:
        1. Check content readiness (assess_runbook_readiness via evaluate_runbook_suggestion)
        2. Check deduplication. A SIMILAR_FOUND verdict STOPS the turn: the
           candidate is named and nothing is created until the user chooses
           (the "generate anyway" affordance routes back here with
           ``dedup_confirmed=True``, which waives this stop and only this
           stop). Dedup-failure caveats do not stop — the case is
           runbook-worthy and only the duplicate check is uncertain, so
           creation proceeds with the caveat stated (#944).
        3. If eligible: call ConversionService.convert_from_case() in background
        4. Return immediately with a message directing user to Dashboard Drafts

        Args:
            dedup_confirmed: True only on the explicit "generate anyway"
                confirmation payload — the user has already been shown the
                similar-runbook candidate on the previous turn and chosen to
                proceed.
        """
        from faultmaven.core.investigation.seeded_provenance import (
            confirmed_root_seed_origin,
        )
        from faultmaven.core.investigation.terminal_transitions import (
            RunbookSuggestion,
            evaluate_runbook_suggestion,
        )

        # Step 0: Provenance-based uniqueness (Phase 5.2b), LEGACY rows only —
        # the seeder that stamped this provenance is gone (fm#1295, see
        # ``seeded_provenance``). A case resolved by validating a cause it had
        # planted from an existing runbook needs no
        # new runbook — it would duplicate that one. This is the cheap SYNC tier
        # ABOVE the async embedding-similarity dedup (Step 2, which stops and
        # names a ≥70% match for the user to decide on):
        # a direct, certain "you applied runbook X" signal, so we short-circuit
        # with the covering runbook named before spending an embedding search.
        # (The offer gate already suppresses the affordance for these cases; this
        # covers the residual typed-exact-payload path and names the runbook.)
        # A knowledge-lifecycle decision, not a safety gate — the manual
        # POST /knowledge/runbooks/create path stays open.
        seed_origin = confirmed_root_seed_origin(case)
        if seed_origin:
            title = None
            if self.deps.knowledge_service and hasattr(
                self.deps.knowledge_service, "get_runbook_title"
            ):
                title = await self.deps.knowledge_service.get_runbook_title(seed_origin)
            named = f"**{title}**" if title else "an existing runbook"
            return {
                "agent_response": (
                    f"This case was resolved by applying {named}, so it is already "
                    "covered — no new runbook is needed. You can view or update it "
                    "from the Dashboard Knowledge Base."
                ),
                "suggested_follow_ups": [],
                "case_updated": case,
                "metadata": metadata,
            }

        # Step 1+2: Evaluate readiness and deduplication. The KB is injected
        # explicitly (constructor param) — the old probe here,
        # ``hasattr(self.knowledge_service, "runbook_kb")``, was permanently
        # False (no such attribute on any IKnowledgeService), so the engine
        # always passed None and dedup never ran (fm#1030). None stays
        # legitimate: without ChromaDB, evaluate_runbook_suggestion takes its
        # honest "did not run" caveat.
        suggestion = await evaluate_runbook_suggestion(
            case,
            self.deps.runbook_kb,
            scope_resolver=self._runbook_dedup_scope_resolver(case),
        )

        if suggestion.verdict == RunbookSuggestion.NOT_READY:
            return {
                "agent_response": suggestion.message,
                "suggested_follow_ups": [],
                "case_updated": case,
                "metadata": metadata,
            }

        # A similar runbook was found: STOP and let the user choose, unless
        # they already have. Surfacing a likely duplicate and then creating
        # it anyway on the same turn would make the question rhetorical and
        # defeat the point of checking — preventing duplicate runbooks is
        # what dedup is FOR. This is not a coverage claim (best-chunk-max
        # measures overlap, not equivalence — the message says so); the
        # "generate anyway" affordance makes the choice answerable on the
        # next turn, and the Dashboard KB link covers the review path.
        if (
            suggestion.verdict == RunbookSuggestion.SIMILAR_FOUND
            and not dedup_confirmed
        ):
            return {
                "agent_response": suggestion.message,
                "suggested_follow_ups": [_generate_runbook_anyway_suggestion()],
                "case_updated": case,
                "metadata": metadata,
            }

        # Step 3: Create the draft
        conversion_service = self.deps.conversion_service
        if not conversion_service:
            logger.warning(
                f"Runbook creation requested for case {case.case_id} but "
                f"conversion_service is not available"
            )
            return {
                "agent_response": (
                    "Runbook generation is not available at the moment. "
                    "You can create one from the Dashboard instead."
                ),
                "suggested_follow_ups": [],
                "case_updated": case,
                "metadata": metadata,
            }

        # Idempotence — mirror the authoritative guard in the service funnel
        # (_convert_from_case_impl) so the chat UX returns a clean "already
        # exists" message instead of firing a background task that then fails
        # with CASE_RUNBOOK_EXISTS. A case whose only prior drafts were discarded
        # is free to regenerate.
        try:
            existing = await conversion_service.get_conversion_by_case(
                case.case_id, case.user_id
            )
        except Exception as e:
            existing = None
            logger.warning(
                f"Existing-conversion check failed for case {case.case_id}: {e}. "
                "Proceeding to generate.",
                extra={"case_id": case.case_id},
            )
        if existing and existing.has_live_draft():
            return {
                "agent_response": (
                    "A runbook draft already exists for this case. You can view or "
                    "update it in the Dashboard under **Knowledge Base > Drafts**."
                ),
                "suggested_follow_ups": [],
                "case_updated": case,
                "metadata": metadata,
            }

        # Fire-and-forget: kick off conversion in background
        try:
            from faultmaven.modules.knowledge.domain.models.conversion import (
                CaseConversionRequest,
            )

            # Case-generated runbooks land in the case owner's personal KB.
            # Global is reserved for platform-curated content, and nothing
            # promotes a case-generated runbook there (#1897).
            request = CaseConversionRequest.from_case(case, scope="personal")
            # Don't await the full pipeline — fire and forget, behind the
            # turn's commit gate. A spawn failure stays here, before the
            # commit, and keeps its "Failed to start" reply below.
            task = asyncio.create_task(
                self._run_runbook_conversion(
                    conversion_service,
                    request,
                    case.user_id,
                    case.enterprise_id,
                    committed=plan.gate(),
                )
            )
            _CONVERSION_TASKS.add(task)
            task.add_done_callback(_CONVERSION_TASKS.discard)

            # Name only what the reader can act on while reading this turn.
            #
            # No in-chat notification is promised. The background task DOES
            # write a completion notification into the transcript, but it is a
            # `role: "system"` row and the copilot's conversation loader keeps
            # only user/assistant rows — and there is no push channel for case
            # messages, so that row is invisible on this turn and after a
            # reload alike. The FAILURE notifications ride the same row, so a
            # failed or empty conversion is silent there.
            #
            # No chat affordance is named either. "Generate runbook from this
            # case" is deliberately suppressed on THIS turn (see
            # `runbook_already_exists=True` below), and free-typed text never
            # reaches the creation path — `_RUNBOOK_CREATION_PATTERNS` matches
            # the DECIDE payload exactly, so the label is not typeable. Naming
            # it here would point at nothing.
            #
            # No failure is inferred from absence, either. `_persist_job` runs
            # only after the pipeline finishes, so nothing lands in Drafts
            # while the conversion is in flight: "not there yet" and "it
            # failed" look identical to the reader. Telling them to act on an
            # empty Drafts list would fire on the healthy path.
            #
            # What is left is the destination (true, and reachable now) and
            # the Dashboard's own create/edit path (`POST
            # /knowledge/runbooks/create` plus the Drafts editor), offered as
            # a standing capability rather than a failure diagnosis — the same
            # framing the SUGGEST message already uses ("You can also do this
            # later from the Dashboard"). That is the durable way out when the
            # silent failure above happens, and it costs the reader nothing
            # when it does not.
            agent_response = (
                "Creating your runbook draft from this case. "
                "It will appear in the Dashboard under "
                "**Knowledge Base > Drafts** once generation finishes. You can "
                "also create and edit runbooks there directly."
            )
            # Carry the dedup caveat onto the user-visible turn. Only NOT_READY
            # surfaces `suggestion.message` above, so a
            # SUGGEST_WITH_CAVEATS verdict would otherwise reach the user as
            # the unqualified line above — silently implying the KB was checked
            # when it was not (#944). Draft creation still proceeds: the case is
            # runbook-worthy, and what is uncertain is only whether a duplicate
            # already exists.
            if suggestion.verdict == RunbookSuggestion.SUGGEST_WITH_CAVEATS:
                agent_response = f"{suggestion.message}\n\n{agent_response}"
            logger.info(
                f"Runbook creation initiated for case {case.case_id}",
                extra={"case_id": case.case_id},
            )
            # Success path re-offers the standard terminal Q&A affordances so
            # the user can iterate on the summary while the background runbook
            # conversion runs. The runbook affordance is hidden on THIS turn —
            # we just kicked off a generation, so re-offering it would race the
            # background task and risk a duplicate draft. The suppression is
            # per-turn: it returns on subsequent terminal Q&A turns, where the
            # idempotence guard above answers a repeat click with a clean
            # "already exists" instead of a second draft.
            #
            # Because it is absent here, the text above must not name it — a
            # message that points at a chip this turn does not carry sends the
            # reader looking for something that is not on screen, and the label
            # is not typeable either (exact-match dispatch on the DECIDE
            # payload). The Dashboard create/edit path it names instead is
            # reachable independently of any turn's suggestion set.
            remaining = await _remaining_regens_for(
                self.deps.report_service,
                self.deps.repository,
                case,
                pending=plan.pending_reports(),
            )
            follow_ups = _resolved_suggestions(
                case, remaining, runbook_already_exists=True
            )
        except Exception as e:
            logger.warning(
                f"Failed to initiate runbook creation for case {case.case_id}: {e}",
                extra={"case_id": case.case_id},
            )
            agent_response = (
                "Failed to start runbook generation. "
                "You can try again or create one from the Dashboard."
            )
            # Failure path stays empty — the text already says "try again",
            # and the user will see the standard terminal Q&A suggestions
            # on the next turn anyway.
            follow_ups = []

        return {
            "agent_response": agent_response,
            "suggested_follow_ups": follow_ups,
            "case_updated": case,
            "metadata": metadata,
        }

    def _runbook_dedup_scope_resolver(self, case: "Case"):
        """Build the CASE OWNER's KB-scope resolver for runbook dedup.

        Dedup answers for the principal who will act on the answer — the case
        owner, whose Dashboard the suggestion points at (owner decision,
        fm#1030). Scope = global ∪ the owner's personal items ∪ items shared
        to the owner's teams, the same allowlist shape as the KB
        pre-fetch (``_prefetch_kb_context``).

        One deliberate divergence from that pre-fetch: NO try/except around
        the team arm. The pre-fetch swallows a team-arm failure and degrades
        to global ∪ personal — correct for seeding, wrong here, because a
        silently narrowed search would underpin a "checked, nothing similar"
        claim it did not establish. A failure raises out of the resolver, and
        ``evaluate_runbook_suggestion`` (which awaits it inside its dedup
        ``try``) takes the failure-caveat branch instead of answering.

        Standalone is not a failure: ``team_service`` is None there, so the
        team arm resolves empty by construction and the scope collapses to
        global ∪ owner-personal.
        """
        from faultmaven.modules.knowledge.domain.services.knowledge_service import (
            build_kb_scope_filter,
            resolve_shared_kb_ids,
        )

        async def _resolve() -> dict:
            owner_id = getattr(case, "user_id", None)
            shared_kb_ids: list[str] = []
            team_service = self.deps.team_service
            share_repository = self.deps.share_repository
            if owner_id and team_service and share_repository:
                owner_team_ids = await team_service.list_all_user_team_ids(owner_id)
                shared_kb_ids = await resolve_shared_kb_ids(
                    share_repository,
                    owner_team_ids,
                    getattr(case, "enterprise_id", None),
                )
            return build_kb_scope_filter(owner_id, shared_kb_ids)

        return _resolve

    async def _run_runbook_conversion(
        self,
        conversion_service,
        request,
        user_id: str,
        enterprise_id: str,
        *,
        committed: "asyncio.Future[Any]",
    ) -> None:
        """Background task for runbook conversion.

        ``committed`` is the spawning turn's commit gate (#1882). Nothing
        happens until it resolves: released, the turn committed and the
        conversion runs; cancelled, the turn did not commit and the task ends
        here, having written nothing. The knowledge writes behind it
        (``ConversionService.convert_from_case``: the conversion job, its
        draft, the synthetic source upload, and this method's completion
        notice) all sit behind the gate.

        ``enterprise_id`` is the SOURCE CASE's enterprise, and it is required, not
        optional. The conversion persists three RLS-tenanted rows (the synthetic
        ``uploaded_files`` conversion source, the ``conversion_jobs`` row, its
        ``conversion_drafts``); each is stamped with whatever this carries. It
        was a hardcoded single-tenant sentinel before #1143, which PostgreSQL
        RLS rejected for every tenant under ``TENANT_PROVIDER=multi``.

        Passing it explicitly is belt-and-braces, NOT a fix for a context that
        might not propagate — be clear about which. This whole task depends on
        inheriting the request's tenant contextvar and cannot work without it:
        the RLS binding itself is sampled from it per transaction (the ``begin``
        listener in ``infrastructure/persistence/database``), and so are the
        dedup read (``get_conversion_by_case``) and the completion-notification
        read/write below, none of which take an org argument. If that
        propagation ever broke, this parameter would not save the write — stamp
        and binding would simply disagree and RLS would refuse it, which is the
        fail-closed outcome we want rather than a silent cross-tenant write.

        What it buys instead is provenance: the stamp becomes a property of the
        resource being converted (the case's own org, hydrated from its row)
        rather than of the ambient context the task happened to be scheduled
        under, and the missing argument that caused #1143 becomes a TypeError
        instead of a sentinel.

        Logs success/failure and writes a completion notification to the case
        transcript. The notification is best-effort: if writing it fails, the
        background task swallows the secondary error rather than masking the
        primary outcome.

        Who reads these three strings decides what they may name. The copilot
        drops `role: "system"` rows, so its users never see them at all; the
        Dashboard renders the transcript, so it is the only reader to write
        for. That rules out naming a chat affordance — the Dashboard has no
        suggestion-chip UI whatsoever, so "click X" there points at a control
        that has never existed, and it also has no case-to-runbook trigger of
        its own to redirect to. What a Dashboard reader can reach is the
        Knowledge Base: the Drafts tab to view, and the "write a runbook from
        the template" form to author one by hand. The two unhappy notices
        therefore state plainly that nothing was saved and offer that manual
        path, which is a weaker remedy than the conversion they were promised
        but the only one on their screen.
        """
        try:
            # Shielded so that cancelling THIS task (shutdown) cannot cancel
            # the gate itself: a cancelled task cancels the future it awaits,
            # and the check below must be able to tell "the turn did not
            # commit" from "the task was cancelled".
            await asyncio.shield(committed)
        except asyncio.CancelledError:
            if not committed.cancelled():
                raise
            logger.info(
                "Runbook conversion for case %s not started: its turn did "
                "not commit",
                request.case_id,
                extra={"case_id": request.case_id},
            )
            return

        notification_content: str
        try:
            result = await conversion_service.convert_from_case(
                request=request,
                user_id=user_id,
                enterprise_id=enterprise_id,
            )
            if result.drafts:
                draft = result.drafts[0]
                logger.info(
                    f"Runbook draft created: {draft.runbook_id} "
                    f"(title='{draft.title}', quality={getattr(draft, 'quality_score', 'N/A')})",
                    extra={
                        "case_id": request.case_id,
                        "runbook_id": draft.runbook_id,
                    },
                )
                notification_content = (
                    f"Your runbook draft **{draft.title}** is ready. "
                    f"View it in the Dashboard under **Knowledge Base > Drafts**."
                )
            else:
                logger.warning(
                    f"Runbook conversion completed but no drafts produced "
                    f"for case {request.case_id}",
                    extra={"case_id": request.case_id},
                )
                notification_content = (
                    "Runbook generation finished without producing a draft, "
                    "so nothing was saved for this case. You can write one "
                    "yourself in the Dashboard under **Knowledge Base**."
                )
        except Exception as e:
            logger.error(
                f"Background runbook creation failed for case {request.case_id}: {e}",
                extra={"case_id": request.case_id},
                exc_info=True,
            )
            notification_content = (
                "Runbook generation failed, so no draft was created for this "
                "case. You can write one yourself in the Dashboard under "
                "**Knowledge Base**."
            )

        # Best-effort completion notification. The case is loaded fresh
        # because terminal cases can still receive Q&A turns that mutate
        # `messages`, and the per-case lock prevents this write from
        # interleaving with a concurrent Q&A turn.
        try:
            async with self._case_locks[request.case_id]:
                case = await self.deps.repository.get(request.case_id)
                if case is None:
                    logger.warning(
                        f"Case {request.case_id} not found when writing "
                        f"runbook completion notification — case may have "
                        f"been deleted while the background task was running.",
                        extra={"case_id": request.case_id},
                    )
                    return
                # No human wrote this, so it carries no author: the role says
                # "system", and a sentinel string would reach clients as a
                # non-resolvable principal id now that author_id persists
                # (ADR-013 D4: system turns have no author).
                append_message_row(
                    case,
                    MessageRowKind.SYSTEM_NOTICE,
                    notification_content,
                    turn_number=case.current_turn,
                    metadata={"source": "runbook_conversion_complete"},
                )
                case.message_count = len(case.messages)
                await self.deps.repository.save(case)
        except Exception as e:
            logger.warning(
                f"Failed to write runbook completion notification for case "
                f"{request.case_id}: {e}",
                extra={"case_id": request.case_id},
                exc_info=True,
            )
