"""6-stage document preprocessing pipeline for conversion.

Stages:
1. Format extraction (via DocumentParser)
1b. Already-a-runbook gate: refuse a document that is already a FaultMaven
    runbook (ALREADY_A_RUNBOOK). A document with runbook frontmatter whose body
    is not the canonical skeleton is WARNED about instead, and only when that
    frontmatter carries `symptom_class` — see ``is_near_runbook`` for why the
    frontmatter alone is too generic to warn on
2. Content cleanup (strip boilerplate)
3. Sensitive content scan (PII redaction)
4. Size check (hard limit at 30K tokens)
5. Source metadata extraction
6. Content triage (LLM classifier)

Stages 0-5 are ``preparation.prepare``, run in a worker process this module
starts and kills (Principle 14, #2622): during them no byte of the document is
parsed, cleaned, redacted or counted on the API process. Stage 6 runs here, on
the loop, over the triage sample the worker cut.
"""

import asyncio
import dataclasses
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from pypdf import get_configuration

import faultmaven
from faultmaven.core.investigation.confidence_repair import (
    ConfidenceAction,
    ConfidenceRepair,
    classify,
    short_repr,
)
from faultmaven.core.investigation.confidence_repair import (
    count as count_confidence_repair,
)
from faultmaven.exceptions import RawModelContentRefused
from faultmaven.infrastructure.llm.dispatch import (
    route_model_call,
)
from faultmaven.infrastructure.llm.json_response import loads_llm_json
from faultmaven.infrastructure.security.model_content import model_boundary_redacted
from faultmaven.modules.knowledge.domain.models.conversion import (
    ConversionErrorCode,
    PreprocessingResult,
    RedactionReport,
    TriageResult,
)
from faultmaven.modules.knowledge.domain.services import preparation_worker
from faultmaven.modules.knowledge.domain.services.preparation_worker import (
    OUT_OF_MEMORY_EXIT,
)

logger = logging.getLogger(__name__)

#: How long a request waits for a preparation slot, and then for its worker,
#: each from its own start (#2153). The route has no other limit until the
#: ingress's 660 s, so both waits and the model calls after them fit, and a
#: legitimate dense PDF (10.7 s measured for one page) has room. The worker is
#: killed at its bound.
EXTRACTION_TIMEOUT_SECONDS = 120

#: ``Retry-After`` on ``CONVERSION_BUSY``: a slot's worker is killed at its
#: deadline, so every slot held when a request gives up is free within one.
CONVERSION_BUSY_RETRY_AFTER_SECONDS = EXTRACTION_TIMEOUT_SECONDS

#: Preparations at once. A slot frees only once its worker is dead.
PREPARATION_SLOTS = 2

#: The worker's address-space limit (``RLIMIT_AS``), so that a plain-text file
#: at the default 10 MB upload limit always gets through Stages 0-5 and a
#: memory failure can come only from parsing a PDF, DOCX or HTML file (a larger
#: ``MAX_UPLOAD_SIZE_MB`` needs a larger limit). Measured on
#: seven 10 MB text shapes: 238 MiB peak for log text, 299 MiB for CJK, 532 MiB
#: for random bytes 0x80-0xFF read as Latin-1 (14.5 million tokens, the
#: costliest), against 64 MiB once imported; this is 1.44 times the costliest.
#: Two slots of it are 1.5 GiB of address space, at most 2 x 489 MiB resident
#: as measured. Beside a 1.5 GiB API process that passes a 2 GiB pod limit, so
#: the worker volunteers for the OOM killer (``preparation_worker``): it is
#: killed before the API is (#2622).
PREPARATION_MEMORY_LIMIT_BYTES = 768 * 1024 * 1024

#: The worker's CPU-time limit: the deadline, plus 10 s. The parent's kill at
#: the deadline always comes first; this bounds a worker whose parent died.
PREPARATION_CPU_SECONDS = EXTRACTION_TIMEOUT_SECONDS + 10

#: What the parent reads from one worker. An accepted document is at most
#: 30,000 tokens, and a cl100k token at most 128 bytes in JSON (a run of
#: spaces): 3.84 MB of text, 0.26 MB of triage sample, and 50 log records of
#: at most 2,000 characters (``preparation_worker``). A worker past it is a
#: fault, not the document's: a server error.
PREPARATION_OUTPUT_LIMIT_BYTES = 5 * 1024 * 1024

#: Where the worker runs: the directory holding this process's ``faultmaven``,
#: so ``-m``, which searches the working directory first, imports the same code.
_PACKAGE_ROOT = Path(faultmaven.__file__).resolve().parents[1]

#: The stage the worker runs, by reference. Always this value: tests point it at
#: a stage of their own, and no input of a request reaches it.
_PREPARATION_STAGE = "faultmaven.modules.knowledge.domain.services.preparation:prepare"

_TOO_SLOW = (
    "Reading this document took longer than the {limit}-second limit, so it was "
    "not converted. Export it to text (TXT or Markdown), or split it into smaller "
    "documents, and convert that instead."
)
_OUT_OF_MEMORY = (
    "Reading this document needed more memory than the conversion allows, so it "
    "was not converted. Export its text to a TXT or Markdown file and convert that "
    "instead."
)
_UNREADABLE = (
    "This document could not be read, so it was not converted. Export its text to "
    "a TXT or Markdown file and convert that instead."
)

_slots: Optional[tuple[asyncio.AbstractEventLoop, asyncio.Semaphore]] = None


class PreparationFailed(RuntimeError):
    """The preparation worker failed for a cause not known to be the document's:
    it failed to start, died before reading the file or after parsing it
    without the memory code, or broke the frame protocol. A server error, with
    no advice to the user (#2622)."""


def _refusal(reason: str, error_code: ConversionErrorCode) -> PreprocessingResult:
    return PreprocessingResult(
        extracted_text="",
        source_metadata={},
        is_rejected=True,
        rejection_reason=reason,
        error_code=error_code,
    )


def _preparation_slots() -> asyncio.Semaphore:
    """The slots, made on the running loop: a ``Semaphore`` binds to one."""
    global _slots
    loop = asyncio.get_running_loop()
    if _slots is None or _slots[0] is not loop:
        _slots = (loop, asyncio.Semaphore(PREPARATION_SLOTS))
    return _slots[1]


async def _frames(proc: asyncio.subprocess.Process, seen: Dict[str, Any]):
    """The worker's result, or None at its end of output. Stage frames set
    ``seen["stage"]``; log records are emitted here, under this request."""
    received = 0
    while True:
        try:
            line = await proc.stdout.readline()
        except ValueError as e:  # one line past the stream's limit
            raise PreparationFailed("the worker's output passed its limit") from e
        if not line:
            return None
        received += len(line)
        if received > PREPARATION_OUTPUT_LIMIT_BYTES:
            raise PreparationFailed("the worker's output passed its limit")
        try:
            frame = json.loads(line)
            if "result" in frame:
                return frame["result"]
            if "stage" in frame:
                seen["stage"] = int(frame["stage"])
            else:
                name, level, message = frame["log"]
                logging.getLogger(name).log(level, message)
        except (ValueError, TypeError, KeyError) as e:
            raise PreparationFailed("the worker wrote a malformed frame") from e


async def _run_worker(file_path: Path, content_type: str) -> Any:
    """One worker over the file: its result, or the refusal its end means.
    The worker is dead and reaped before this returns or raises."""
    request = {
        "stage": _PREPARATION_STAGE,
        "file_path": os.path.abspath(file_path),
        "content_type": content_type,
        "pdf_limits": dataclasses.asdict(get_configuration()),
        "parent_pid": os.getpid(),
        "memory_bytes": PREPARATION_MEMORY_LIMIT_BYTES,
        "cpu_seconds": PREPARATION_CPU_SECONDS,
        "log_level": logging.getLogger().getEffectiveLevel(),
    }
    seen: Dict[str, Any] = {"stage": None}
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        preparation_worker.__name__,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        cwd=_PACKAGE_ROOT,
        limit=PREPARATION_OUTPUT_LIMIT_BYTES,
    )
    try:
        async with asyncio.timeout(EXTRACTION_TIMEOUT_SECONDS) as deadline:
            try:
                proc.stdin.write(json.dumps(request).encode() + b"\n")
                await proc.stdin.drain()
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass  # it died before reading; its exit answers below
            result = await _frames(proc, seen)
            if result is not None:
                return result
            code = await proc.wait()
    except TimeoutError:
        if not deadline.expired():
            raise
        logger.warning(
            "conversion of %s: extraction ran past %s s in stage %s; killed",
            file_path.name,
            EXTRACTION_TIMEOUT_SECONDS,
            seen["stage"],
        )
        return _refusal(
            _TOO_SLOW.format(limit=EXTRACTION_TIMEOUT_SECONDS),
            ConversionErrorCode.DOCUMENT_TOO_COMPLEX,
        )
    finally:
        if proc.returncode is None:
            proc.kill()
        # Reads its output to the end, unread: a wait alone never returns while
        # a full pipe holds the worker's stdout open.
        await proc.communicate()

    ended = (
        f"the preparation worker of {file_path.name} exited {code} "
        f"in stage {seen['stage']}"
    )
    if code == OUT_OF_MEMORY_EXIT:
        logger.warning("conversion refused, out of memory: %s", ended)
        return _refusal(_OUT_OF_MEMORY, ConversionErrorCode.DOCUMENT_TOO_COMPLEX)
    if seen["stage"] == 1:
        logger.warning("conversion refused, dead while parsing: %s", ended)
        return _refusal(_UNREADABLE, ConversionErrorCode.DOCUMENT_TOO_COMPLEX)
    logger.error("conversion failed: %s", ended)
    raise PreparationFailed(ended)


async def _prepare(file_path: Path, content_type: str) -> Any:
    """Stages 0-5 in a worker, under a slot: the worker's result, or a
    refusal. No slot within the bound is ``CONVERSION_BUSY``."""
    slots = _preparation_slots()
    try:
        async with asyncio.timeout(EXTRACTION_TIMEOUT_SECONDS):
            await slots.acquire()
    except TimeoutError:
        logger.warning(
            "conversion of %s: no extraction worker free within %s s",
            file_path.name,
            EXTRACTION_TIMEOUT_SECONDS,
        )
        return _refusal(
            "The server is busy converting other documents, so this one "
            "could not start. Try again in "
            f"{CONVERSION_BUSY_RETRY_AFTER_SECONDS} seconds.",
            ConversionErrorCode.CONVERSION_BUSY,
        )
    try:
        return await _run_worker(file_path, content_type)
    finally:
        slots.release()


# =============================================================================
# Content Triage Prompt
# =============================================================================

TRIAGE_SYSTEM_PROMPT = """You are a document classifier. Determine if the provided text excerpt contains troubleshooting content.

Troubleshooting content includes: diagnostic procedures, error resolution steps, incident response procedures, runbooks, postmortems, or vendor troubleshooting guides.

Non-troubleshooting content includes: pure architecture docs, marketing material, API reference docs without error handling, meeting notes, project plans.

Respond with JSON:
{"is_actionable": true/false, "confidence": 0.0-1.0, "reason": "brief explanation"}"""

#: A "not actionable" verdict above this is a hard reject (``NOT_ACTIONABLE``);
#: at or below it the document converts with a warning.
TRIAGE_HARD_REJECT_CONFIDENCE = 0.8

#: The confidence of a verdict that states none usable. It must stay at or below
#: the threshold above, so a verdict without a usable confidence can only warn.
TRIAGE_DEFAULT_CONFIDENCE = 0.5


def _triage_confidence(raw: Any) -> float:
    """Read the classifier's ``confidence`` with #1502's rule (fm#1672).

    Nothing holds the classifier to ``0.0-1.0``, and a model that answers ``50``
    meaning 50% used to clear the hard-reject threshold. ``classify`` is the one
    implementation of the rule: a value in ``(1, 100]`` is a percentage and is
    rescaled, a ``bool`` is coerced to ``1.0``/``0.0``, and anything else
    (negative, above 100, NaN, infinity, not a number) is treated as absent and
    takes :data:`TRIAGE_DEFAULT_CONFIDENCE` — the advisory path, never the hard
    reject. A missing or ``null`` confidence is absence the model chose, not a
    repair, and is not counted. Every repair is counted on
    ``faultmaven_schema_field_repairs_total`` and logged with the raw value.
    """
    if raw is None:
        return TRIAGE_DEFAULT_CONFIDENCE
    kind, value = classify(raw)
    if kind == "conforming":
        return value
    if kind in ("rescaled", "coerced"):
        action = ConfidenceAction(kind)
    else:
        action, value = ConfidenceAction.DEFAULTED, TRIAGE_DEFAULT_CONFIDENCE
    repair = ConfidenceRepair(
        schema="TriageResult",
        field="confidence",
        action=action,
        raw=raw,
        value=value,
    )
    count_confidence_repair(repair)
    logger.warning(
        "Content triage confidence repaired: %s",
        repair.note(),
        extra={"action": action.value, "raw": short_repr(raw), "value": value},
    )
    return value


class DocumentPreprocessor:
    """Orchestrates the preprocessing pipeline."""

    def __init__(self, llm_router=None, settings=None, sanitizer=None):
        self._llm_router = llm_router
        self._settings = settings
        # The DI sanitizer the conversion service is handed: what the triage
        # call sends is redacted with it at this layer (#1901), not left to
        # whichever router is configured.
        self._sanitizer = sanitizer

    async def preprocess(
        self, file_path: Path, content_type: str, *, conversion_id: str
    ) -> PreprocessingResult:
        """Run the full preprocessing pipeline: Stages 0-5 in a worker process,
        then Stage 6 on the loop. ``conversion_id`` tags the triage call's
        trace (#2575).

        Two waits, each up to ``EXTRACTION_TIMEOUT_SECONDS``: for a slot, then
        for the worker, timed from its start. A request that gets no slot is
        ``CONVERSION_BUSY``; a worker past its bound is killed and the
        document refused. A worker that dies is refused by its memory code or
        its stage (``_run_worker``), else raises ``PreparationFailed``.
        """
        prepared = await _prepare(file_path, content_type)
        if isinstance(prepared, PreprocessingResult):
            return prepared
        if "refused" in prepared:
            return _refusal(
                prepared["reason"], ConversionErrorCode(prepared["refused"])
            )
        extracted_text = prepared["extracted_text"]
        source_metadata = prepared["source_metadata"]
        redaction_report = RedactionReport(**prepared["redaction_report"])
        token_count = prepared["token_count"]
        warnings = prepared["warnings"]

        # Stage 6: Content triage (classifier LLM)
        triage_result = await self._run_content_triage(
            prepared["triage_sample"], conversion_id=conversion_id
        )
        if triage_result:
            if (
                not triage_result.is_actionable
                and triage_result.confidence > TRIAGE_HARD_REJECT_CONFIDENCE
            ):
                return PreprocessingResult(
                    extracted_text=extracted_text,
                    source_metadata=source_metadata,
                    redaction_report=redaction_report,
                    triage_result=triage_result,
                    token_count=token_count,
                    is_rejected=True,
                    rejection_reason=(
                        "This document does not appear to contain troubleshooting content. "
                        "The conversion pipeline produces runbooks from diagnostic procedures, "
                        "incident reports, vendor troubleshooting guides, or postmortems."
                    ),
                    error_code=ConversionErrorCode.NOT_ACTIONABLE,
                )
            elif (
                not triage_result.is_actionable
                and triage_result.confidence <= TRIAGE_HARD_REJECT_CONFIDENCE
            ):
                warnings.append(
                    "This document may not contain sufficient troubleshooting content. "
                    "Generated runbooks may have incomplete sections."
                )

        return PreprocessingResult(
            extracted_text=extracted_text,
            source_metadata=source_metadata,
            redaction_report=redaction_report,
            triage_result=triage_result,
            warnings=warnings,
            token_count=token_count,
        )

    async def _run_content_triage(
        self, sample_text: str, *, conversion_id: str
    ) -> Optional[TriageResult]:
        """Stage 6: Send the first 2K tokens, the sample the worker cut, to the
        classifier to determine if actionable.

        Triage is advisory and fails OPEN — a document that cannot be
        classified is still converted — with one exception: a redaction that
        is required and cannot run. That is not a triage failure but a refusal
        to send, so it is raised between the two fail-open guards below, and a
        ``RedactionUnavailableError`` stops the conversion with nothing sent
        (#1901), as it stops an investigation turn.
        """
        if not self._llm_router or not self._settings:
            return None

        try:
            classifier_model = self._settings.llm.get_classifier_model()
        except RawModelContentRefused:
            # A refusal is a programming error, never a fallback (#1998).
            raise
        except Exception as e:
            logger.warning(f"Content triage failed, proceeding anyway: {e}")
            return None

        messages = await model_boundary_redacted(
            [
                {"role": "system", "content": TRIAGE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Classify this document excerpt:\n\n{sample_text}",
                },
            ],
            self._sanitizer,
        )

        try:
            # A knowledge-base document: its trace carries the conversion and
            # the enterprise, by which deleting the conversion purges it (#2575).
            response = await route_model_call(
                self._llm_router,
                messages=messages,
                model=classifier_model,
                conversion_id=conversion_id,
                max_tokens=256,
                temperature=0.1,
                response_format={"type": "json_object"},
                # Land on CLASSIFIER_PROVIDER when one is set — the model
                # alone doesn't route; see intent_resolver for the same
                # pattern. The classifier ships PINNED, so this kwarg is
                # normally present and a router used here must accept it.
                **(
                    {"provider_override": override}
                    if (
                        override := self._settings.llm.explicit_role_provider(
                            "classifier"
                        )
                    )
                    else {}
                ),
            )

            if response.is_truncated:
                # Fail-open is right here — triage is advisory and a document
                # that cannot be classified should still be converted — but the
                # reason must not be silent. A 256-token budget for a three-field
                # JSON verdict should never run out, so a cut here means the
                # model is writing prose instead of the verdict, and that is
                # worth seeing rather than swallowing as a parse error (#1094).
                logger.warning(
                    "Content triage response truncated at the 256-token cap; "
                    "proceeding without a triage verdict"
                )
                return None

            # Fence-tolerant, for the reason in `loads_llm_json`. This site
            # fails QUIETLY — the exception handler below returns None and the
            # document proceeds with no triage verdict — so a provider without
            # a JSON mode silently disabled triage rather than reporting it.
            result = loads_llm_json(response.content, strict=True)
            return TriageResult(
                is_actionable=result.get("is_actionable", True),
                confidence=_triage_confidence(result.get("confidence")),
                reason=result.get("reason", ""),
            )
        except RawModelContentRefused:
            # A refusal is a programming error, never a fallback (#1998).
            raise
        except Exception as e:
            logger.warning(f"Content triage failed, proceeding anyway: {e}")
            return None
