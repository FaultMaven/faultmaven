"""The runbook taxonomy: every closed frontmatter vocabulary, defined once.

Source: ``docs/architecture/knowledge-and-ai/runbook-content-architecture.md``
§Taxonomy Schema. That table is canonical; these enums are its one copy in
code, in its order, and every reader in this repository derives from them —
the runbook validator, the conversion and extraction prompts, the request
models (and through them the published ``openapi.json``), the ORM ``CHECK``
constraints and the retrieval reranker. A value is added or removed by editing
the spec and the enum together; ``test_runbook_taxonomy_one_owner.py`` parses
the spec table and fails when the two differ (#1886).

Before this module each vocabulary was hand-copied into five places, and the
copies drifted: ``conversion_drafts_severity_check`` lost ``info``, so a
runbook the validator passed could never be verified.

Why a leaf module beside ``contracts.py`` rather than inside it: ``contracts``
re-exports domain models, so importing it gives the importer a path into
``domain``. The ORM and the knowledge module's own infrastructure read these
vocabularies, and import-linter's layers contract would report that path (the
reason ``exceptions.py`` is a leaf too). This module imports nothing from the
application, so every layer can read it.

‼ Each member is a plain ``NAME = "value"`` assignment, and nothing else goes in
an enum body. kb-toolkit's cross-repo parity gate (``check_vocab_cross_repo.py``)
reads vocabularies out of this repository's source with ``ast`` and never
imports it; computed members would be invisible to it.
"""

from enum import Enum
from typing import Optional, Tuple, Type


class RunbookDomain(str, Enum):
    """``domain``: the engineering vertical a runbook belongs to."""

    DATABASE = "database"
    NETWORKING = "networking"
    COMPUTE = "compute"
    APPLICATION = "application"
    SECURITY = "security"
    STORAGE = "storage"
    MESSAGING = "messaging"


class SymptomClass(str, Enum):
    """``symptom_class``: the controlled failure-mode vocabulary.

    A list field in frontmatter; every item must be one of these. Long-tail
    symptoms go in the free-text ``tags`` instead (spec §Taxonomy Design Rules).
    """

    AUTH_FAILURE = "auth_failure"
    CONNECTION_REFUSED = "connection_refused"
    CPU_SATURATION = "cpu_saturation"
    CRASH_LOOP = "crash_loop"
    DATA_LOSS = "data_loss"
    DEPLOYMENT_FAILURE = "deployment_failure"
    DISK_FULL = "disk_full"
    IMAGE_PULL_FAILURE = "image_pull_failure"
    LATENCY = "latency"
    NODE_FAILURE = "node_failure"
    OOM = "oom"
    REPLICATION_LAG = "replication_lag"
    SCHEDULING_FAILURE = "scheduling_failure"
    SERVICE_UNAVAILABLE = "service_unavailable"
    THROUGHPUT_DEGRADATION = "throughput_degradation"
    TIMEOUT = "timeout"


class RunbookSeverity(str, Enum):
    """``severity``: the impact level a runbook addresses.

    Stored in ``conversion_drafts.severity``, whose CHECK is built from this.
    """

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class KnowledgeScope(str, Enum):
    """``scope``: the KB tier, which is also a knowledge item's visibility.

    One vocabulary, not two: a runbook's frontmatter ``scope`` is the tier its
    knowledge item is published at. Stored in ``knowledge_items.scope`` and
    ``conversion_jobs.scope``, whose CHECKs are built from this. Compare
    against members (``KnowledgeScope.PERSONAL``), not string literals.

    Values:
        GLOBAL: Platform-wide built-in runbooks (FaultMaven-shipped only).
        TEAM: Shared to one or more teams via the share table (``resource_shares``
            rows; the scope enum is the derived convenience — ``team`` ⟺ at least
            one share row, maintained by the KB write path). ADR-013 §D4.
        PERSONAL: Visible only to one user (requires owner_id).
    """

    GLOBAL = "global"
    TEAM = "team"
    PERSONAL = "personal"


class RunbookDifficulty(str, Enum):
    """``difficulty`` (optional): the expertise a runbook assumes."""

    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"
    EXPERT = "expert"


class RunbookStatus(str, Enum):
    """``status``: the runbook's lifecycle state in its frontmatter.

    Not ``DraftStatus``: that is the conversion draft row's own lifecycle
    (``draft`` / ``verified`` / ``discarded``), a different vocabulary.
    """

    DRAFT = "draft"
    IN_REVIEW = "in-review"
    VERIFIED = "verified"
    STALE = "stale"
    DEPRECATED = "deprecated"


#: Every closed vocabulary, keyed by the frontmatter field it governs.
TAXONOMY_FIELDS: Tuple[Tuple[str, Type[Enum]], ...] = (
    ("domain", RunbookDomain),
    ("symptom_class", SymptomClass),
    ("severity", RunbookSeverity),
    ("scope", KnowledgeScope),
    ("difficulty", RunbookDifficulty),
    ("status", RunbookStatus),
)


def vocabulary(enum_cls: Type[Enum]) -> Tuple[str, ...]:
    """The allowed values of one vocabulary, in the spec's order."""
    return tuple(member.value for member in enum_cls)


def render_vocabulary(enum_cls: Type[Enum], separator: str = ", ") -> str:
    """The allowed values as one string, for prompts and error messages."""
    return separator.join(vocabulary(enum_cls))


def member_value(enum_cls: Type[Enum], raw: object) -> Optional[str]:
    """``raw`` when it is exactly one of the vocabulary's values, else ``None``.

    For writers that copy an UNVALIDATED frontmatter value into a column whose
    CHECK is built from the vocabulary: the column records the value only when
    the constraint admits it, so one off-vocabulary file cannot abort the write.
    """
    return raw if isinstance(raw, str) and raw in vocabulary(enum_cls) else None
