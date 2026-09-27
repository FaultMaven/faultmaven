"""Proactive and reactive evidence vectorization: starting embedding jobs off the turn's critical path and tracking which evidence has already been vectorized."""

import asyncio
import json
import logging
from typing import Any

from faultmaven.modules.agent.tools.vectorize_file_tool import (
    append_vectorization_advisory,
)

logger = logging.getLogger(__name__)


class EvidenceVectorizer:
    """Starts and tracks evidence vectorization jobs, owning the one in-flight set shared by every caller so a turn never double-starts a job already running."""

    def __init__(self, *, deps) -> None:
        self.deps = deps
        self._inflight_vectorize = {}

    async def start_proactive_vectorization(
        self,
        case: Any,
        tool_context: Any,
    ) -> dict[str, asyncio.Task]:
        """Start background vectorization for qualifying DA-mode evidence.

        Runs concurrently with the tool loop so case_evidence_search is
        available by the time the agent needs it. Only vectorizes files
        above the size threshold that haven't already been vectorized.

        Uses ``self._inflight_vectorize`` to dedup across turns: if a
        task is already running for a given evidence_id, the current
        turn reuses it instead of creating a second concurrent encode.
        The persistent ``Evidence.vectorized`` flag covers the already-
        completed state; the in-flight registry covers the running
        state. Together they prevent cross-turn task stacking.
        """
        from faultmaven.config.settings import get_settings
        from faultmaven.modules.agent.tools.vectorize_file_tool import (
            VECTORIZATION_MAX_SIZE_BYTES,
        )

        settings = get_settings()
        min_size = settings.agent.vectorization_min_size_bytes
        tasks: dict[str, asyncio.Task] = {}

        for ev in getattr(case, "evidence", []):
            # Vectorization size gate. Post-010: file-backed evidence has
            # its size on uploaded_files.size_bytes; chat-extracted evidence
            # (USER_DESCRIPTION, source_file_id IS NULL) has no backing file
            # and is never large enough to vectorize — treat size=0 so it
            # falls below the min-size threshold.
            file_meta = case.find_uploaded_file(getattr(ev, "source_file_id", None))
            size = (
                int(file_meta.size_bytes) if file_meta and file_meta.size_bytes else 0
            )
            if not (
                size >= min_size
                and size <= VECTORIZATION_MAX_SIZE_BYTES
                and not ev.vectorized
            ):
                continue

            existing = self._inflight_vectorize.get(ev.evidence_id)
            if existing is not None and not existing.done():
                # Another turn already started this; reuse the same task
                # so both turns observe the same completion.
                tasks[ev.evidence_id] = existing
                logger.debug(
                    "proactive_vectorization_reused_inflight",
                    extra={"evidence_id": ev.evidence_id},
                )
                continue

            task = asyncio.create_task(
                self._vectorize_evidence(ev.evidence_id, tool_context)
            )
            self._inflight_vectorize[ev.evidence_id] = task
            # Remove from registry once the task settles (success,
            # failure, or cancellation). If persistence succeeded the
            # flag is True and this evidence won't re-enter the loop;
            # if it failed the next turn can retry cleanly.
            task.add_done_callback(
                lambda t, eid=ev.evidence_id: self._inflight_vectorize.pop(eid, None)
            )
            tasks[ev.evidence_id] = task
            logger.info(
                "proactive_vectorization_started",
                extra={
                    "evidence_id": ev.evidence_id,
                    "content_size_bytes": size,
                },
            )
        return tasks

    async def _vectorize_evidence(
        self,
        evidence_id: str,
        tool_context: Any,
    ) -> bool:
        """Vectorize a single evidence file via the registered tool.

        On success, flips ``Evidence.vectorized`` to True via a scoped
        single-row repository UPDATE so proactive + reactive gates skip
        this evidence on subsequent turns. The flag is the single source
        of truth for "is this evidence already in the case vector store".

        No internal ``asyncio.wait_for``: time-bound policy belongs at the
        caller. Proactive callers run this unbounded as a background task
        — the in-flight registry prevents duplicates, and bounding a
        background task that the caller never synchronously awaits only
        guarantees wasted CPU when ``asyncio.wait_for`` cancels the
        asyncio Future while the thread-pool worker (which can't be
        safely killed) continues to completion. Reactive callers wrap
        this with ``asyncio.wait_for`` using
        ``AgentSettings.vectorization_reactive_timeout_seconds`` because
        they do block the agent.
        """
        try:
            result = await self.deps.investigation_tools.execute_tool(
                "vectorize_file",
                {"evidence_id": evidence_id},
                tool_context,
            )
        except Exception as e:
            logger.warning(
                "Vectorization failed for %s: %s",
                evidence_id,
                e,
                exc_info=True,
            )
            return False

        if not result.success:
            logger.warning(
                "vectorize_file returned failure for %s: %s",
                evidence_id,
                result.error,
            )
            return False

        # success is not "the file is in the index". `vectorize_file` reports a
        # file with no chunkable content as a success — the operation completed
        # and established a fact about the file — but nothing was written. This
        # boolean is the only thing the callers read: True flips the persistent
        # `vectorized` flag AND emits `_VECTORIZED_SYSTEM_MESSAGE`, telling the
        # model the file is searchable via case_evidence_search. The model then
        # searches, gets nothing, and reads it as "this file does not contain
        # that" — an index that was never written laundered into a finding about
        # the evidence (#941). The tool's own message says otherwise, but no
        # caller here renders it.
        #
        # `is not True`, deliberately: an unstated key and an unrecognisable
        # payload both mean "this caller did not tell us the file is indexed",
        # and the safe reading of that is that it isn't. Failing the other way
        # would make the guard depend on every future producer remembering to
        # set a key, with a false claim to the model as the penalty for
        # forgetting; failing this way costs a re-attempt.
        data = result.data if isinstance(result.data, dict) else {}
        if data.get("indexed") is not True:
            logger.info(
                "vectorize_file did not report an index for %s (%s) — not "
                "marking vectorized",
                evidence_id,
                data.get("message", ""),
            )
            return False

        logger.info("vectorize_file succeeded for %s", evidence_id)

        # Persist vectorized=True via a scoped single-row UPDATE. Must NOT
        # use repository.save(case) — this runs as a fire-and-forget task
        # that can complete after subsequent turns have written. An
        # aggregate save from a stale snapshot would silently wipe those
        # newer writes across every case-owned table.
        case_id = getattr(tool_context, "case_id", None)
        if case_id:
            try:
                await self.deps.repository.update_evidence_vectorized(
                    case_id, evidence_id, True
                )
            except Exception as e:
                logger.debug(
                    "Failed to persist vectorized flag for %s: %s",
                    evidence_id,
                    e,
                )

        # Flip the flag on the in-memory snapshot so the current turn's
        # gate sees it without another DB read.
        case = getattr(tool_context, "in_memory_case", None)
        if case is not None:
            for ev in getattr(case, "evidence", []) or []:
                if getattr(ev, "evidence_id", None) == evidence_id:
                    ev.vectorized = True
                    break

        return True

    @staticmethod
    def _evidence_is_vectorized(case: Any, evidence_id: str) -> bool:
        """Return True if the given evidence is marked vectorized on the
        in-memory case. Source of truth for dedup — the persistent
        Evidence.vectorized flag set by _vectorize_evidence on success.
        """
        if case is None:
            return False
        for ev in getattr(case, "evidence", []) or []:
            if getattr(ev, "evidence_id", None) == evidence_id:
                return bool(getattr(ev, "vectorized", False))
        return False

    async def track_da_result(
        self,
        func_name: str,
        evidence_id: str,
        tool_result: Any,
        result_text: str,
        case: Any | None,
        tool_context: Any,
        da_empty_search_counts: dict[str, int],
        proactive_tasks: dict[str, asyncio.Task],
    ) -> str:
        """Track DA failure signals and trigger vectorization when needed.

        Returns result_text, potentially with [SYSTEM] messages appended.
        Dedup of "already vectorized" is sourced from Evidence.vectorized
        (persistent) — within-turn and across-turn.
        """
        # If the proactive task for this evidence has just completed this
        # turn, emit the [SYSTEM] advisory once. _vectorize_evidence has
        # already flipped and persisted the flag by the time we see
        # task.result()==True, so subsequent reactive checks naturally
        # skip this evidence via _evidence_is_vectorized.
        if evidence_id in proactive_tasks:
            task = proactive_tasks[evidence_id]
            if task.done() and not task.cancelled():
                exc = task.exception()
                if exc:
                    logger.warning(
                        "Proactive vectorization task failed for %s: %s",
                        evidence_id,
                        exc,
                    )
                else:
                    # The advisory decision lives in the helper, not in this
                    # `if`: a file that indexed nothing gets `result_text` back
                    # unchanged however this site is reached (#941).
                    before = result_text
                    result_text = append_vectorization_advisory(
                        result_text, task.result()
                    )
                    if result_text != before:
                        logger.info(
                            "proactive_vectorization_completed",
                            extra={"evidence_id": evidence_id},
                        )

        # Track search_file empty results
        if func_name == "search_file" and tool_result.success:
            try:
                data = (
                    json.loads(tool_result.data)
                    if isinstance(tool_result.data, str)
                    else tool_result.data
                )
                if isinstance(data, dict) and data.get("results_count", 0) == 0:
                    da_empty_search_counts[evidence_id] = (
                        da_empty_search_counts.get(evidence_id, 0) + 1
                    )
                else:
                    da_empty_search_counts[evidence_id] = 0
            except (json.JSONDecodeError, TypeError):
                pass

            # Advisory after 3 consecutive empty searches
            count = da_empty_search_counts.get(evidence_id, 0)
            if count >= 3:
                result_text += (
                    f"\n\n[SYSTEM] Last {count} search_file calls on this "
                    "file returned zero results. Consider using "
                    "deep_analysis with a different query approach."
                )

        already_vectorized = self._evidence_is_vectorized(case, evidence_id)

        # Track deep_analysis confidence for the low-confidence trigger
        # below. In-turn only, like `da_empty_search_counts`: nothing carries
        # DA history across turns any more. The orchestration service that
        # reconstructed it is gone, and the `da_invocation_count` field it
        # read was never added to the Evidence model.
        if func_name == "deep_analysis" and tool_result.success and case:
            try:
                data = (
                    json.loads(tool_result.data)
                    if isinstance(tool_result.data, str)
                    else tool_result.data
                )
                if isinstance(data, dict):
                    confidence = float(data.get("confidence", 1.0))

                    # Low confidence trigger
                    if confidence < 0.2 and not already_vectorized:
                        result_text = await self._reactive_vectorize(
                            evidence_id,
                            tool_context,
                            result_text,
                            "low_confidence",
                        )
                        already_vectorized = self._evidence_is_vectorized(
                            case, evidence_id
                        )
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        # Track timeouts
        if (
            not tool_result.success
            and "timed out" in (getattr(tool_result, "error", "") or "").lower()
            and not already_vectorized
        ):
            result_text = await self._reactive_vectorize(
                evidence_id,
                tool_context,
                result_text,
                "tool_timeout",
            )
            already_vectorized = self._evidence_is_vectorized(case, evidence_id)

        # Reactive vectorization on repeated empty searches
        empty_count = da_empty_search_counts.get(evidence_id, 0)
        if empty_count >= 3 and not already_vectorized:
            result_text = await self._reactive_vectorize(
                evidence_id,
                tool_context,
                result_text,
                "repeated_empty_searches",
            )

        return result_text

    async def _reactive_vectorize(
        self,
        evidence_id: str,
        tool_context: Any,
        result_text: str,
        trigger: str,
    ) -> str:
        """Attempt reactive vectorization for a qualifying evidence file.

        On success, _vectorize_evidence flips + persists the Evidence
        vectorized flag, so subsequent reactive triggers in this turn
        will see it and skip via _evidence_is_vectorized.
        """
        from faultmaven.config.settings import get_settings
        from faultmaven.modules.agent.tools.vectorize_file_tool import (
            VECTORIZATION_MAX_SIZE_BYTES,
        )

        # Storage redesign 2026-04 phase 2: resolve size from case.evidence
        # (standalone evidence service deleted).
        ev_size = 0
        try:
            case = getattr(tool_context, "in_memory_case", None)
            if case is None and getattr(tool_context, "case_repository", None):
                case = await tool_context.case_repository.get(tool_context.case_id)
            if case is not None:
                for ev in getattr(case, "evidence", []) or []:
                    if getattr(ev, "evidence_id", None) == evidence_id:
                        # Post-010: size lives on uploaded_files via the
                        # source_file_id FK. Chat-extracted evidence has no
                        # backing file → size=0 (which falls below the
                        # vectorization min-size gate below).
                        file_meta = case.find_uploaded_file(
                            getattr(ev, "source_file_id", None)
                        )
                        ev_size = (
                            int(file_meta.size_bytes)
                            if file_meta and file_meta.size_bytes
                            else 0
                        )
                        break
        except Exception:
            return result_text

        settings = get_settings()
        if ev_size < settings.agent.vectorization_min_size_bytes:
            return result_text
        if ev_size > VECTORIZATION_MAX_SIZE_BYTES:
            return result_text

        # Reactive vectorization blocks the agent inside the tool loop;
        # bound it by the configurable reactive budget so a slow encode
        # can't eat the turn timeout. Proactive is unbounded elsewhere —
        # see _vectorize_evidence docstring for the split rationale.
        reactive_timeout = float(settings.agent.vectorization_reactive_timeout_seconds)
        try:
            success = await asyncio.wait_for(
                self._vectorize_evidence(evidence_id, tool_context),
                timeout=reactive_timeout,
            )
        except TimeoutError:
            logger.warning(
                "Reactive vectorization timed out for %s after %ss "
                "(trigger=%s). Agent proceeds without semantic search "
                "results for this turn; a proactive task for the same "
                "evidence may still be in flight.",
                evidence_id,
                reactive_timeout,
                trigger,
            )
            return result_text

        before = result_text
        result_text = append_vectorization_advisory(result_text, success)
        if result_text != before:
            logger.info(
                "reactive_vectorization_triggered",
                extra={
                    "evidence_id": evidence_id,
                    "trigger": trigger,
                    "content_size_bytes": ev_size,
                },
            )
        return result_text
