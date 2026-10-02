import re
from datetime import UTC, datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

# ============================================================
# Evidence Models (Section 5)
# ============================================================


class EvidenceCategory(str, Enum):
    """
    Evidence classification by investigation purpose.

    Four claim-attached categories forming the presence/absence
    verification quartet: ``symptom_evidence`` (symptom present),
    ``causal_evidence`` (cause present), ``symptom_absence_evidence``
    (symptom gone after a fix), ``causal_absence_evidence`` (cause gone
    after a fix). Every row is the LLM's deliberate decision to record a
    specific extract as evidence for a specific claim, created only during
    INVESTIGATING. Contextual data lives on ``uploaded_files`` — no
    evidence row is needed until the agent extracts a claim-relevant
    slice. Rejection is expressed as the absence of an evidence row;
    hypothesis-level refutation lives on ``hypothesis_evidence.stance``.

    The verification gates (``mitigation_verified`` / ``solution_verified``)
    are NOT driven by an evidence category — they are set by the LLM via the
    User-Agent Handshake / compliance detection. The absence rows are the
    durable audit trail that the readiness checks consult
    (``assess_resolution_readiness`` via ``_has_causal_absence``) to decide
    RESOLVED vs CLOSED. (The pre-migration ``mitigation_evidence`` /
    ``solution_evidence`` stage-completion categories were removed in the
    GAP-5 legacy→absence migration.)
    """

    SYMPTOM_EVIDENCE = "symptom_evidence"
    """
    Shows problem manifestation.

    Purpose: Prove the problem exists and establish scope/timeline.

    Examples:
    - Error logs showing failures
    - Metrics showing degradation (high CPU, slow response times)
    - User impact reports
    - Deployment logs showing recent changes

    Advances Milestones: symptom_verified
    """

    CAUSAL_EVIDENCE = "causal_evidence"
    """
    Points to root cause.

    Purpose: Test hypothesis about what caused the problem.

    Examples:
    - Connection pool metrics (for "pool exhausted" hypothesis)
    - Memory dumps (for "memory leak" hypothesis)
    - Network traces (for "latency" hypothesis)
    - Config changes (for "misconfiguration" hypothesis)

    Advances Milestones: root_cause_identified
    """

    SYMPTOM_ABSENCE_EVIDENCE = "symptom_absence_evidence"
    """
    Shows that a previously-observed symptom is no longer present.

    Purpose: Verify a fix attempt worked — at the level of "is the
    user-visible problem gone?" The cause may or may not still be
    there; this category answers symptom presence, not causation.

    Collected during MITIGATION (sufficient for mitigation_verified)
    and during TREATMENT (defense-in-depth confirmation alongside
    causal-absence evidence).

    Examples:
    - ``kubectl get pods`` showing ``Running`` after CrashLoopBackOff
    - Latency back under SLO after a workaround
    - Error rate trending to zero in monitoring after rollback
    - User confirms "the dashboard loads now"

    Advances Milestones: mitigation_verified (primary), contributes to
    solution_verified.
    """

    CAUSAL_ABSENCE_EVIDENCE = "causal_absence_evidence"
    """
    Shows that a previously-identified cause is no longer present.

    Purpose: Verify a permanent fix at the level of "is the root
    cause eliminated?" This is distinct from symptom absence — a
    cause can be removed without an immediate symptom change (cached
    state, downstream debris), and a symptom can be masked without
    the cause being removed (mitigation).

    Collected during TREATMENT.

    Examples:
    - ``cat /etc/app/config.yaml`` showing the corrected setting
    - DB query plan showing the expected index is used after rebuild
    - Deployment manifest showing the bad image tag has been replaced
    - Config-management run output confirming the drift has been
      reconciled

    Advances Milestones: solution_verified.
    """


class EvidenceSourceType(str, Enum):
    """
    Fundamental type of data source.

    Aligned with the data classifier (Tier 0); each value names what kind
    of data the Evidence row's source is. ``USER_DESCRIPTION`` is the
    chat-quote case where the source is a verbatim system-output quote
    embedded in the user's message, not a file.
    """

    LOGS = "logs"
    """
    Time-ordered diagnostic output.

    Includes:
    - Application logs
    - System logs
    - Command output (kubectl, curl, docker logs, etc.)
    - Distributed trace data
    - API responses
    - Error messages

    Characteristics: Time-ordered textual records of system behavior
    """

    METRICS = "metrics"
    """
    Quantitative measurements.

    Includes:
    - Time-series metrics (CPU, memory, latency)
    - Dashboards and graphs
    - Performance data
    - Resource usage statistics
    - Monitoring alerts (triggered by metrics)

    Characteristics: Numerical data, often time-series
    """

    CONFIGURATION = "configuration"
    """
    Structured system/application configuration.

    Includes:
    - Config files (YAML, JSON, TOML, env vars)
    - Database schema
    - Infrastructure definitions (Kubernetes manifests, Terraform)
    - Dependency lists

    Characteristics: Defines how system should behave
    """

    CODE = "code"
    """
    Source code.

    Includes:
    - Application code snippets
    - Code reviews
    - Function definitions
    - Scripts
    - SQL queries

    Characteristics: Executable or interpretable program text
    """

    TEXT = "text"
    """
    Prose content from a file (uploaded documentation, README, runbook
    excerpt, ticket export). Post-010: this is for prose *files*, not
    for the user's own typed narrative or observations — those aren't
    evidence under the strict model. For verbatim system-output quotes
    the user typed inline in chat (e.g., "Got: HTTP/1.1 503 Service
    Unavailable"), use ``USER_DESCRIPTION`` instead.
    """

    IMAGE = "image"
    """
    Visual content.

    Includes:
    - Screenshots (errors, dashboards, terminals)
    - Architecture diagrams
    - Graphs and charts
    - Photos

    Characteristics: Requires visual interpretation
    """

    USER_DESCRIPTION = "user_description"
    """
    Verbatim system-output quote the user typed inline in a short chat
    message (below the 10K-char threshold that triggers paste-to-file
    intake). The extract is system output (an error message, a log line,
    a metric reading) that the user quoted in their own message rather
    than uploading as a file.

    This is the ONE case where ``evidence.source_file_id`` legitimately
    is NULL — the source is the user's chat message at the same turn
    (recoverable via ``collected_at_turn`` and the case_messages join).

    The user's own descriptions, opinions, or paraphrases are NOT
    evidence (per the strict "extract must be system output" rule); the
    agent should request actual output rather than promoting a
    paraphrase. This category is exclusively for cases where the user
    pasted/typed a verbatim system slice into chat.
    """


class EvidenceStance(str, Enum):
    """
    How evidence relates to a hypothesis.
    Evaluated by LLM after evidence submission against ALL active hypotheses.
    One evidence can have different stances for different hypotheses.
    """

    SUPPORTS = "supports"
    """Evidence supports hypothesis (increase confidence)"""

    NEUTRAL = "neutral"
    """Evidence neither supports nor contradicts"""

    REFUTES = "refutes"
    """Evidence contradicts hypothesis (decrease confidence)"""


# =============================================================================
# Uploaded File (Raw File Metadata)
# =============================================================================


# Pasted text and captured pages arrive with no filename, so the turns route
# mints one at ingestion (``resolve_paste_source_meta`` +
# ``f"{prefix}{ts}.txt"`` in modules/case/api/routes/dependencies.py). That name is a
# storage/transport artifact: the user never typed it and it says nothing
# about the content. It is still a real ``filename`` — dedup, the storage
# backend, extension sniffing and the classifier all consume it — so the fix
# for #666 is not to stop minting it, but to keep it out of anything a model
# or a user reads. ``display_name`` is that separation: the name to SHOW,
# distinct from the name it is STORED under.
_SYNTHETIC_FILENAME_RE = re.compile(
    r"^(pasted-content|page-capture)-\d{8}T\d{6}Z?\.txt$"
)

# Anchored on the minted shape, not just the prefix, so a file the user
# actually named "pasted-notes.txt" keeps its own name.
_MINTED_PREFIX_TO_KIND = {"pasted-content": "paste", "page-capture": "capture"}

# Detection is a three-way rule, because NEITHER signal is trustworthy alone.
#
# ``upload_source`` looks authoritative and is partly fabricated:
# ``investigation_service`` derives what it hands the engine as
# ``"paste" if att.filename.startswith("pasted-content-")``, and the upsert
# persists that over the real column (not COALESCE'd). So a user's own file
# named ``pasted-content-notes.txt`` comes back tagged ``paste``, and trusting
# the tag would erase their filename from every prompt, tool result, report
# citation and clarification card.
#
# The minted filename shape is not sufficient either: a paste can legitimately
# arrive named ``Untitled`` (the Copilot's older paste path, still present on
# staging cases), which matches no minted pattern. Trusting only the shape
# would call that a file the user chose and quote "Untitled" at them.
#
# What makes the two decidable together is that the fabrication has a
# signature: it fires only on the ``pasted-content-``/``page-capture-``
# PREFIX, which is strictly looser than the full minted SHAPE. So:
#
#   1. full minted shape        -> synthetic, conclusively (the route minted it)
#   2. prefix but not the shape -> a name the user chose that merely collides
#                                  with the prefix; the tag on such a row was
#                                  computed FROM that prefix, so it carries no
#                                  independent information and is ignored
#   3. neither                  -> the tag was not fabricated from the name, so
#                                  it is the genuine provenance ("Untitled")
#
# Rule 2 is also what kept #1201 from mattering here while it was live: a
# capture mis-tagged ``file_upload`` still matched rule 1 on its filename. That
# mis-tagging is fixed — the engine-dispatch metadata now carries
# ``input_origin`` — but the rule stays, both for rows written before the fix
# and because it is right on its own terms.
_MINTED_PREFIXES = ("pasted-content-", "page-capture-")


#: The server-generated placeholder title, ``Case-YYMMDD-N`` (older rows:
#: ``Case-YYYY-N``). Owned here so both the auto-titling route (which decides
#: whether a case still needs a name) and any reader that must not quote a
#: placeholder as if it were the case's subject (#1343 orientation) agree.
DEFAULT_CASE_TITLE_RE = re.compile(r"^Case-(?:\d{4}|\d{6})-\d+$")


def is_default_case_title(title: Optional[str]) -> bool:
    """True when ``title`` is still the auto-generated placeholder."""
    return bool(title and DEFAULT_CASE_TITLE_RE.match(title.strip()))


def is_minted_filename(filename: Optional[str]) -> bool:
    """True when this NAME was minted by the turns route, not typed by a user.

    Takes the name, not the row, because the two can disagree and the caller
    usually means the name. Content-hash dedup matches on bytes ALONE, so a
    paste can be handed back an ``UploadedFile`` for a real file the user
    uploaded earlier: the row is not synthetic, and the submitted name still
    is. Asking the row there answers the wrong question and re-emits the
    minted name (#1198 review) -- the exact defect #666 is about.

    Only rule 1 of the three-way rule applies to a bare name: there is no
    ``upload_source`` to weigh, and rule 2 (a user's own file whose name
    merely collides with the prefix) is the default anyway.
    """
    return bool(filename) and bool(_SYNTHETIC_FILENAME_RE.match(filename))


def minted_filename_phrase(filename: Optional[str]) -> Optional[str]:
    """Prose for a minted NAME, or None when the name is the user's own.

    The bare-name twin of ``UploadedFile.submission_phrase``, for the layers
    that hold a filename and no row -- preprocessing builds user-visible text
    before any ``UploadedFile`` exists. Same wording, one definition.
    """
    match = _SYNTHETIC_FILENAME_RE.match(filename or "")
    if not match:
        return None
    kind = _MINTED_PREFIX_TO_KIND[match.group(1)]
    return "the page you captured" if kind == "capture" else "the text you pasted"


# ``upload_source`` spellings that mean "the user pasted/captured this". Both
# paste spellings occur: the turns route writes ``text_paste``, the documented
# enum value is ``paste``.
_PASTE_UPLOAD_SOURCES = frozenset({"paste", "text_paste"})
_CAPTURE_UPLOAD_SOURCES = frozenset({"page_capture"})


class UploadedFile(BaseModel):
    """
    File the user submitted to a case (upload, paste, page capture).

    Post-010 strict evidence model — two-table separation:
    - **UploadedFile**: the file-of-record. Exists in any case state
      (INQUIRY, INVESTIGATING, terminal). Carries the raw bytes (via
      ``storage_ref``) and the preprocessing artifacts (``summary``,
      ``structural_index``, ``data_type``, coverage timestamps) that
      describe the file's content.
    - **Evidence**: the claim-anchored extract-of-record. Created only
      during INVESTIGATING when the LLM extracts a focused slice via
      ``evidence_to_add`` to support a specific claim (symptom, cause,
      mitigation, or solution).

    Files are not evidence. An evidence row references its source file
    via ``Evidence.source_file_id``; the file row holds the file-level
    metadata so the evidence row can stay focused on the claim it
    supports. Not all uploaded files produce evidence — the LLM
    decides which slices, if any, are claim-relevant.
    """

    file_id: str = Field(
        default_factory=lambda: f"file_{uuid4().hex[:12]}",
        description="Unique file identifier (same as data_id in data service)",
        pattern=r"^(file_|data_)[a-f0-9]{12,16}$",  # Accept both file_ and data_ prefixes
    )

    filename: str = Field(description="Original filename", min_length=1, max_length=255)

    @field_validator("filename", mode="after")
    @classmethod
    def _filename_not_empty(cls, v: str) -> str:
        """Mirror of the DB uploaded_files_filename_not_empty CHECK:
        filename must not be whitespace-only. Pydantic ``min_length=1``
        accepts a single space; the DB rejects it. Same rule, two layers
        — neither bypassable independently."""
        if not v.strip():
            raise ValueError("filename must not be whitespace-only")
        return v

    size_bytes: int = Field(ge=0, description="File size in bytes")

    content_type: Optional[str] = Field(
        default=None,
        description="MIME type as reported on upload (e.g., text/plain, application/pdf).",
        max_length=100,
    )

    content_hash: Optional[str] = Field(
        default=None,
        description=(
            "SHA-256 of the raw file content. Used for storage-backend dedup "
            "and integrity checks. NULL when the upload is still streaming "
            "or hashing was skipped."
        ),
        max_length=64,
    )

    uploaded_at_turn: int = Field(
        ge=0, description="Turn number when file was uploaded"
    )

    uploaded_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), description="Upload timestamp"
    )

    uploaded_by: Optional[str] = Field(
        default=None,
        description=(
            "User who uploaded the file. NULL for system-generated uploads "
            "or after the originating user is deleted (FK SET NULL)."
        ),
        max_length=36,
    )

    upload_source: str = Field(
        default="file_upload",
        description=(
            "Provenance of the upload: how the file got into the system. "
            "Values: file_upload, paste, screenshot, page_capture, "
            "agent_generated, conversion_source. Distinct from "
            "evidence.source_type, which classifies the data shape. "
            "page_capture is the marker the rerank-page-sections pass uses "
            "to detect Copilot extension page submissions; see "
            "context_builder/text_shaping.py."
        ),
        max_length=50,
    )

    storage_ref: Optional[str] = Field(
        default=None,
        description=(
            "A key for IFileStorageBackend.retrieve_file(), or None — never "
            "anything else. The backend interprets it (a key under the local "
            "backend's root, an S3 key, an Azure blob name). None while "
            "processing is pending, and always on a KB conversion-source "
            "row, whose file no backend holds."
        ),
        max_length=5000,
    )

    # ------------------------------------------------------------------
    # Preprocessing artifacts (migration 010)
    #
    # These describe the FILE, not any claim about it. Populated by the
    # Tier 0+1 preprocessor at upload time. Under the strict evidence
    # model, files are not evidence — evidence is a claim-anchored
    # extract from a file. The preprocessor's file-level outputs
    # (summary, structural index, data-type classification, coverage
    # timestamps) belong here rather than on a synthetic Evidence row.
    # ------------------------------------------------------------------
    summary: Optional[str] = Field(
        default=None,
        description=(
            "Preprocessing-generated short summary of the file. Used by "
            "the investigation agent to orient on file content without "
            "loading the whole file. May be None when preprocessing was "
            "skipped (e.g., KB-conversion source uploads)."
        ),
    )

    structural_index: Optional[str] = Field(
        default=None,
        description=(
            "Preprocessing-generated structural index of the file content "
            "(file_extract + search_map + file_meta). Read by the LLM "
            "in <evidence_collected> when the agent inspects the file. "
            "May be None when preprocessing was skipped."
        ),
    )

    data_type: Optional[str] = Field(
        default=None,
        description=(
            "Preprocessor's data-type classification of the file's "
            "content. Written as the fine-grained ``DataType`` value "
            "(e.g., 'logs_and_errors', 'metrics_and_performance', "
            "'structured_config'); rows written before #583 hold the "
            "6-valued ``EvidenceSourceType`` string ('logs', 'metrics', "
            "'configuration', ...) and were not migrated. A consumer that "
            "needs the 6-valued type reads it through "
            "``core.preprocessing.models.unified_data_type_of``, which "
            "accepts both; never parse the column as one vocabulary. "
            "Distinct from evidence.source_type, which stays 6-valued and "
            "classifies an individual extract's source type."
        ),
        max_length=50,
    )

    coverage_start_ts: Optional[datetime] = Field(
        default=None,
        description=(
            "Earliest timestamp parsed from the file's content. None "
            "when the file has no parseable timestamps."
        ),
    )

    coverage_end_ts: Optional[datetime] = Field(
        default=None,
        description=(
            "Latest timestamp parsed from the file's content. None when "
            "the file has no parseable timestamps."
        ),
    )

    coverage_source: Optional[str] = Field(
        default=None,
        description=(
            "Which timestamp-format pattern produced the coverage span, or "
            "``caller_declared`` when a forwarding client supplied it via "
            "``observed_at``. Promoted alongside the span it qualifies "
            "because it decides what may be ASSERTED from it: ``epoch_s`` / "
            "``epoch_ms`` are bare-integer regexes that match ordinary config "
            "values, and ``syslog_bsd_noyear`` carries a year the parser "
            "invented. NULL means the provenance was never recorded (rows "
            "written before this column existed) — unknown, not trusted."
        ),
        max_length=50,
    )

    # ------------------------------------------------------------------
    # Display identity (#666)
    #
    # ``filename`` is the name the file is STORED under. The properties
    # below are the name it is SHOWN under. Plain properties, not pydantic
    # fields: they are derived, never persisted, and never serialised into
    # an API response.
    #
    # Two registers, one rule. ``display_name`` is the IDENTIFIER — it goes
    # wherever a name is shown *as a name* (prompt attributes, tool results,
    # report citations) and the model is told to cite it, so it must pick
    # this item out of this case and keep doing so. ``submission_phrase``
    # is the PROSE form for sentences addressed to the user, where the
    # referent is already unambiguous.
    # ------------------------------------------------------------------

    @property
    def _minted_kind(self) -> Optional[str]:
        """``"paste"`` / ``"capture"`` / ``None`` — see the three-way rule above.

        Returns ``None`` both for an ordinary file and for one whose name
        merely collides with a minted prefix; the caller cannot tell those
        apart and does not need to, since both keep their filename.
        """
        match = _SYNTHETIC_FILENAME_RE.match(self.filename)
        if match:
            return _MINTED_PREFIX_TO_KIND[match.group(1)]  # rule 1
        if self.filename.startswith(_MINTED_PREFIXES):
            return None  # rule 2 — the tag was computed from this name
        source = self.upload_source or ""  # rule 3
        if source in _PASTE_UPLOAD_SOURCES:
            return "paste"
        if source in _CAPTURE_UPLOAD_SOURCES:
            return "capture"
        return None

    @property
    def is_pasted(self) -> bool:
        """True when the user pasted this content rather than choosing a file."""
        return self._minted_kind == "paste"

    @property
    def is_page_capture(self) -> bool:
        """True when the browser extension captured this from a web page."""
        return self._minted_kind == "capture"

    @property
    def input_origin(self) -> str:
        """How this content ARRIVED: ``text_paste`` | ``page_capture`` |
        ``file_upload``.

        The single reconciled answer, built on ``_minted_kind`` so it applies
        the same precedence every other consumer does — provenance tag first,
        minted-filename shape as the fallback for rows whose tag predates the
        current values, and rule 2's carve-out for a user's own
        prefix-colliding filename.

        Exists because the engine-dispatch path derived this fact a SECOND way
        (``att.filename.startswith("pasted-content-")``), which reported every
        page capture as ``file_upload`` — the primary Copilot channel, arriving
        indistinguishable from a file the user chose (#1201). One derivation,
        one answer.

        Normalises the two ``upload_source`` paste spellings: the turns route
        writes ``text_paste`` and older rows carry ``paste``, and no caller
        should have to know that. The value returned is always the canonical
        one, matching what the route sets today.
        """
        kind = self._minted_kind
        if kind == "paste":
            return "text_paste"
        if kind == "capture":
            return "page_capture"
        return "file_upload"

    @property
    def has_synthetic_filename(self) -> bool:
        """True when ``filename`` was minted by us, not supplied by the user.

        The predicate behind ``display_name``'s branch and behind
        ``submission_phrase``; callers wanting a name should ask for one of
        those rather than branch on this themselves.
        """
        return self._minted_kind is not None

    @property
    def display_name(self) -> str:
        """The citable name: what the model is told to reference this by.

        Real uploads keep their filename — the user chose it and recognises
        it. Pastes and page captures are named by HOW they arrived and WHEN,
        because the minted ``pasted-content-<ts>.txt`` is meaningless to the
        person who typed the text and reads as a file they never had (#666).

        Two properties of the minted name had to survive the substitution,
        and only one of them is obvious:

        **Unique.** Citation is the whole point — "In pasted logs, line 42"
        has to designate one item. A name derived from ``data_type`` does
        not: two pastes classified ``logs`` in one case share it, and every
        page capture would be "captured page". The turn number separates
        them, because the turns route mints at most one synthetic name per
        turn — ``pasted_content`` is a single form field, so a turn carries
        one paste or one capture, never two.

        **Stable.** ``data_type`` is rewritten by
        reclassification (see ``_handle_file_reclassification``), so a name
        built from it renames the item mid-case: cited as "pasted logs" on
        turn 3, gone by turn 4 when the user corrects it to command output —
        and the transcript's own back-reference then names nothing in context.
        ``summary`` is rewritten by the same flow and is not unique either,
        which is why neither is used here.

        ``uploaded_at_turn`` is written once at ingestion and is not revised.
        It was, until #1207: the engine appended a duplicate row carrying the
        CURRENT turn, and ``_upsert_uploaded_files`` assigns
        ``uploaded_at_turn = EXCLUDED.uploaded_at_turn`` with no COALESCE, so
        a deduped re-paste on turn 5 rewrote a turn-3 row to 5. The engine no
        longer appends that row, so this identifier is stable as designed.
        Pinned by ``test_uploaded_at_turn_is_immutable_across_a_deduped_reupload``
        in tests/unit/test_synthetic_filename_leak.py, which drives the engine
        and the upsert end to end.

        Note the repository upsert itself is unchanged: it still assigns those
        columns rather than COALESCE-ing them, so a FUTURE caller handing it a
        partial row would reintroduce the drift. #1207 removed the only known
        producer, not the possibility.

        The data type is not lost: it rides on the ``data_type`` attribute
        beside the name, and the summary on ``<summary>``. This slot's job
        is to designate, not to describe.
        """
        if not self.has_synthetic_filename:
            return self.filename
        kind = "captured page" if self.is_page_capture else "pasted text"
        return f"{kind} (turn {self.uploaded_at_turn})"

    @property
    def submission_phrase(self) -> Optional[str]:
        """How prose addressed to the user names this, or None for a real file.

        The sentence register: "I've recorded the text you pasted as command
        output". No turn number, because a sentence carries its own referent
        — this is only ever used about a file the sentence is already about.
        ``None`` means "this has a filename; quote that instead", which the
        caller decides because the quoting style differs per surface.
        """
        if self.is_page_capture:
            return "the page you captured"
        if self.is_pasted:
            return "the text you pasted"
        return None

    # NOTE: ``minted_filename_phrase`` (module level) is the same wording for
    # callers holding only a name. Keep the two in step.


# =============================================================================
# Evidence (Investigation Data Linked to Hypotheses)
# =============================================================================


class Evidence(BaseModel):
    """
    A claim-anchored extract recorded during INVESTIGATING.

    Post-010 single creation path: every Evidence row originates as an
    ``EvidenceToAdd`` entry the LLM declared on a specific turn. The LLM
    chooses the ``category`` from the four claim-anchored values and the
    ``source_type`` from ``EvidenceSourceType``. The system only infers
    ``advances_milestones`` (Tier 2 inference via ``CATEGORY_MILESTONE_MAP``),
    and only when the LLM has not already overridden it (Tier 3).

    LLM declares: summary, extract (optional), category, source_type,
        source_file_id (required unless source_type=USER_DESCRIPTION),
        likelihood, optionally advances_milestones
    LLM evaluates: stance per hypothesis (creates hypothesis_evidence links)
    System fills: evidence_id, collected_at_turn, collected_by,
        coverage timestamps (parsed from extract), advances_milestones (Tier 2)
    """

    evidence_id: str = Field(
        default_factory=lambda: f"ev_{uuid4().hex[:12]}",
        description="Unique evidence identifier",
        pattern=r"^ev_[a-f0-9]{12}$",
    )

    # ============================================================
    # Purpose Classification (LLM-declared via EvidenceToAdd)
    # ============================================================
    category: EvidenceCategory = Field(
        description=(
            "Claim-anchored category declared by the LLM (verification "
            "quartet): SYMPTOM_EVIDENCE | CAUSAL_EVIDENCE | "
            "SYMPTOM_ABSENCE_EVIDENCE | CAUSAL_ABSENCE_EVIDENCE"
        )
    )

    primary_purpose: str = Field(
        description="What this evidence validates (milestone name or hypothesis ID)",
        max_length=100,
    )

    # ============================================================
    # Content — two-field shape (see case-schema.md §4.3)
    # ============================================================
    summary: str = Field(
        description=(
            "Short label (≤500 chars) the LLM wrote when declaring this "
            "evidence via ``evidence_to_add``. ALWAYS present — use it for "
            "UI list views, headers, and quick scanning. The optional "
            "``extract`` field carries the verbatim slice that supports "
            "the summary."
        ),
        min_length=1,
        max_length=500,
    )

    @field_validator("summary", mode="after")
    @classmethod
    def _summary_not_empty(cls, v: str) -> str:
        """Mirror of the DB evidence_summary_not_empty CHECK: summary must
        not be whitespace-only. Pydantic ``min_length=1`` accepts a single
        space; the DB rejects it. Same rule, two layers — neither
        bypassable independently."""
        if not v.strip():
            raise ValueError("summary must not be whitespace-only")
        return v

    extract: Optional[str] = Field(
        default=None,
        description=(
            "Optional verbatim quote that supports the ``summary``. The "
            "LLM populates this when grounding the finding in a specific "
            "system-output slice (a log line, a metric reading, a config "
            "snippet). Distinct from ``summary`` (short label) and from "
            "``uploaded_files.storage_ref`` (file pointer). May be NULL "
            "when the summary is self-contained. File-level preprocessing "
            "artifacts (structural index, file summary) live on "
            "``uploaded_files``, never here — this field is for "
            "claim-relevant quotes only."
        ),
    )

    @field_validator("extract", mode="after")
    @classmethod
    def _extract_not_empty_when_set(cls, v: Optional[str]) -> Optional[str]:
        """Mirror of the DB evidence_extract_not_empty CHECK: if extract is
        set, it must not be whitespace-only. Cross-layer defense in depth —
        same rule, two layers, neither bypassable independently."""
        if v is not None and not v.strip():
            raise ValueError("extract must not be empty whitespace; pass None to omit")
        return v

    analysis: Optional[str] = Field(
        default=None,
        description="Agent analysis of this evidence and its significance to the investigation",
        max_length=2000,
    )

    # ============================================================
    # Processing Mode (Scenario-Driven Data Processing)
    # ============================================================
    processing_mode: Optional[str] = Field(
        default=None,
        description="Processing mode: triage | directed_analysis | semantic_search",
        max_length=50,
    )

    # ============================================================
    # Source Information
    # ============================================================
    source_type: EvidenceSourceType = Field(description="Type of evidence source")

    source_file_id: Optional[str] = Field(
        default=None,
        description=(
            "FK to the UploadedFile this extract came from. Required unless "
            "``source_type=USER_DESCRIPTION`` (the narrow case where the LLM "
            "extracted a verbatim system-output quote from the user's short "
            "chat message — no file involved). Enforced by the "
            "``evidence_source_invariant`` CHECK constraint at the DB level "
            "and by the ``_source_requires_file_unless_user_description`` "
            "validator at the Pydantic level."
        ),
    )

    is_primary: bool = Field(
        default=False,
        description=(
            "True for the principal evidence row in this case (the one "
            "that anchors the investigation summary). list_evidence_tool "
            "uses this for surface-level dashboards."
        ),
    )

    reliability_score: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "LLM-assessed reliability of this evidence (0.0-1.0). "
            "NULL when the agent has not scored the evidence."
        ),
    )

    tags: List[str] = Field(
        default_factory=list,
        description=(
            "Free-form tag list for evidence classification. Tag values must "
            "not contain commas (the SQLite serializer uses comma-separation; "
            "the comma-ban keeps round-trip lossless)."
        ),
    )

    @field_validator("tags", mode="after")
    @classmethod
    def _no_commas_in_tags(cls, v: List[str]) -> List[str]:
        for t in v:
            if "," in t:
                raise ValueError(
                    f"tag values must not contain commas (got {t!r}); commas "
                    "would break the SQLite serialization round-trip"
                )
        return v

    vectorized: bool = Field(
        default=False,
        description=(
            "Whether this evidence's structural index has been persisted into "
            "the case vector store. Set to True by the investigation engine "
            "after a vectorize_file run that actually wrote chunks — NOT after "
            "any successful run: a file with no chunkable content, or one seen "
            "while the embedder was unavailable, completes successfully and "
            "indexes nothing, and is not in the store (#941). Persisted across "
            "turns so proactive and reactive vectorization paths skip "
            "already-indexed evidence instead of re-embedding on every turn."
        ),
    )

    # ============================================================
    # Milestone Advancement
    # ============================================================
    advances_milestones: List[str] = Field(
        default_factory=list,
        description="Which milestones this evidence helped complete",
    )

    # ============================================================
    # Metadata
    # ============================================================
    collected_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When evidence was collected",
    )

    collected_by: str = Field(
        description="Who collected: user_id or 'system' for automated collection"
    )

    collected_at_turn: int = Field(
        ge=0, description="Turn number when evidence was collected"
    )

    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Structured diagnostic metadata from the preprocessing pipeline. "
            "Top-level keys are namespaced — see "
            "docs/architecture/data-and-storage/schemas/case-schema.md §4.3 "
            "'evidence.metadata JSON contract'. "
            "Canonical shape in "
            "faultmaven/core/preprocessing/evidence_metadata.py::EvidenceMetadata. "
            "Optional — chat-quoted Evidence rows have no preprocessing trace."
        ),
    )

    # Phase 3 — Case-level timeline. See case-schema.md §4.3 and
    # docs/working/WIP-data-processing-improvement-plan.md §Phase 3.
    # The time span the evidence's *content* covers, distinct from:
    #   collected_at (upload receipt time), collected_at_turn (agent turn).
    # Nullable: NULL for evidence without parseable timestamps (configs,
    # source code, screenshots, short pastes).
    coverage_start_ts: Optional[datetime] = Field(
        default=None,
        description=(
            "Earliest timestamp parsed from the evidence's content. "
            "None when the content has no parseable timestamps."
        ),
    )
    coverage_end_ts: Optional[datetime] = Field(
        default=None,
        description=(
            "Latest timestamp parsed from the evidence's content. "
            "None when the content has no parseable timestamps."
        ),
    )

    coverage_source: Optional[str] = Field(
        default=None,
        description=(
            "Which timestamp-format pattern produced the coverage span, or "
            "``caller_declared`` when a forwarding client supplied it via "
            "``observed_at``. Promoted alongside the span it qualifies "
            "because it decides what may be ASSERTED from it: ``epoch_s`` / "
            "``epoch_ms`` are bare-integer regexes that match ordinary config "
            "values, and ``syslog_bsd_noyear`` carries a year the parser "
            "invented. NULL means the provenance was never recorded (rows "
            "written before this column existed) — unknown, not trusted."
        ),
        max_length=50,
    )

    @model_validator(mode="after")
    def _source_requires_file_unless_user_description(self) -> "Evidence":
        """Mirror of the DB ``evidence_source_invariant`` CHECK (migration
        010): every Evidence row has a source.

        - source_file_id IS NOT NULL  → the extract came from a file
        - source_file_id IS NULL      → only legal when source_type is
          ``USER_DESCRIPTION`` (the LLM extracted a verbatim system-output
          quote from the user's short chat message; the source is the
          user message at ``collected_at_turn``)

        Cross-layer defense: same rule in Pydantic and the DB, neither
        bypassable independently."""
        if (
            self.source_file_id is None
            and self.source_type != EvidenceSourceType.USER_DESCRIPTION
        ):
            raise ValueError(
                "evidence.source_file_id is required unless "
                "source_type=USER_DESCRIPTION (the chat-quote case)"
            )
        return self


# ============================================================
# Case Entity Registry (Phase 4)
# ============================================================


class EntityType(str, Enum):
    """Controlled vocabulary for ``case_entities.entity_type``.

    Extending this vocabulary requires a design-doc edit (not just a
    code change) so the retrieval paths — agent tools, context-builder
    auto-injection — stay in sync with what producers emit. See
    ``docs/working/WIP-data-processing-improvement-plan.md`` §Phase 4.
    """

    IP = "ip"
    HOSTNAME = "hostname"
    USER = "user"
    PID = "pid"
    PORT = "port"
    SERVICE = "service"
    PATH = "path"
    DEVICE = "device"
    METRIC_NAME = "metric_name"


class CaseEntity(BaseModel):
    """One row in the case-level entity registry.

    Populated by the preprocessing pipeline post-extraction. The
    composite (case_id, entity_type, entity_value, evidence_id) is the
    primary key — re-extracting an evidence upserts by that tuple,
    preserving idempotency across re-runs.
    """

    case_id: str = Field(description="Case that owns this entity observation")
    entity_type: EntityType = Field(
        description=("Type of entity. Controlled vocabulary — see EntityType enum.")
    )
    entity_value: str = Field(
        max_length=255,
        description="The entity itself (IP address, hostname, PID, etc.)",
    )
    evidence_id: str = Field(
        description="Evidence row the entity was extracted from",
        pattern=r"^ev_[a-f0-9]{12}$",
    )
    mention_count: int = Field(
        default=1,
        ge=1,
        description="How many times the entity appeared in the evidence",
    )
    in_error_context: bool = Field(
        default=False,
        description=(
            "True when the entity appeared primarily in error / warning "
            "lines. Lets the agent distinguish 'IP X was involved in an "
            "error' from 'IP X showed up in ambient traffic'."
        ),
    )
    first_seen_ts: Optional[datetime] = Field(
        default=None,
        description=(
            "Earliest timestamp associated with this entity in this "
            "evidence — typically equals the evidence's coverage_start_ts "
            "(Phase 3a) when the evidence is time-bound, else None."
        ),
    )
