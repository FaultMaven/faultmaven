"""How a runbook is authored from a case: the policy both case writers share (#1880).

A case is an incident record and a runbook is reusable knowledge. Two paths turn
one into the other — ``ConversionService.convert_from_case`` (chat-triggered,
into the owner's personal or team drafts) and
``SuggestionService.extract_knowledge_from_case`` (``POST
/cases/{id}/extract-knowledge``, into the global review inbox) — and both hand
the model material that names the incident: the case title, the statement, the
evidence. This module is the one statement of what the model is told to do with
it, and of how the runbook id is minted from what the model wrote. Both paths
render these constants and call these functions, so the rules cannot drift
apart the way they did when the extraction path alone carried them.

Three rules, rendered verbatim into each path's prompt:

* :data:`CASE_ID_RULE` — the id comes from the title the model writes, never
  from the case title;
* :data:`TECHNOLOGY_RULE` — ``service`` is the technology, not the user's own
  service ("`service` is the technology, not the team",
  ``runbook-content-architecture.md``). A case supplies no technology, so the
  model infers it, exactly as it already infers ``symptom_class``;
* :data:`DE_IDENTIFICATION_RULES` — what a runbook may not carry.

And the mint, applied after the write: :func:`mint_case_runbook_id` reads the
draft's own frontmatter ``service`` and ``title``.
"""

from typing import Any, Dict, Optional

from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    RunbookValidator,
)
from faultmaven.utils.runbook_id import is_hash_only_runbook_id, runbook_id_from_parts

#: The id rule. The id is minted from the title the model writes, so the
#: model's care goes on the title.
CASE_ID_RULE = """The frontmatter `id` field is kebab-case derived from the DE-IDENTIFIED title
you write below — NEVER from the case title, which names an incident. It is
normalised after you write it, so spend your care on the title."""

#: Where the technology goes. A case names the user's own services ("checkout",
#: "payment-api"), which match no pack value and are incident identifiers
#: besides; the technology is what a runbook is filed and retrieved under.
TECHNOLOGY_RULE = "Put the technology in `service` and `tags`, where it is free text."

#: What a runbook built from a case may not carry, and what it must keep.
DE_IDENTIFICATION_RULES = """DE-IDENTIFICATION — mandatory, and applied to every section including code
blocks. A runbook is reusable knowledge, not an incident record. Remove:
- absolute timestamps and dates (write relative time: "after ~2 hours")
- user names, email addresses and account identifiers
- hostnames, IP addresses, internal URLs, cluster and namespace names
- customer and enterprise names
- ticket, incident and case identifiers
Replace each with a generic placeholder (`<hostname>`, `<namespace>`) or with a
description of the role it played. KEEP product names, versions, error strings
and command shapes — those are what make the runbook usable."""


def case_stem_runbook_id(case_id: str) -> str:
    """The last-resort runbook id: the case's own identifier, slugged.

    Opaque, but an internal case identifier and therefore safe to publish —
    unlike the case TITLE, which names an incident (see
    :func:`mint_case_runbook_id`).

    No local kebab repair and no literal fallback: since #1230/#1243
    ``runbook_id_from_parts`` guarantees a non-empty kebab id itself, and a
    shared literal would give every degenerate case the same id — the
    collision that pair of issues removed.
    """
    return runbook_id_from_parts("case", case_id)


def _draft_frontmatter(content: str) -> Dict[str, Any]:
    """The draft's frontmatter as the validator parses it, or ``{}``.

    The validator's own parse, so "what the id and title are read from" is the
    same text the gate will read them as.
    """
    try:
        metadata = RunbookValidator()._extract_metadata(content)
    except Exception:
        return {}
    return metadata if isinstance(metadata, dict) else {}


def draft_title(content: str) -> Optional[str]:
    """The draft's own frontmatter ``title``, when it has a usable one.

    ``None`` for a draft with no frontmatter, no title, or the rule-8
    ``[INSUFFICIENT SOURCE DATA]`` placeholder the skeleton carries — that last
    one is a form, not a name.
    """
    title = _draft_frontmatter(content).get("title")
    if not isinstance(title, str):
        return None
    title = title.strip()
    if not title or "INSUFFICIENT SOURCE DATA" in title:
        return None
    return title


def mint_case_runbook_id(content: str, case_id: str) -> str:
    """The kebab-case ``id`` to force onto a case-built draft's frontmatter.

    Minted from the draft's OWN ``service`` + ``title`` — the same
    ``(service, title)`` mint the document path uses — and deliberately NOT from
    the case title.

    That distinction was measured, not reasoned about. The extraction path's
    first cut minted from the case title, and the eval's deliberately-noisy
    fixture ("INC-48213: prod-web-07 returning 502 for customer Contoso from
    2026-03-14 02:11 UTC") produced a body the model had de-identified perfectly
    and a frontmatter line reading
    ``id: case-inc-48213-prod-web-07-returning-502-for-customer-c-fd3a``. The id
    is inside the content, so it is chunked, embedded and retrieved: a ticket
    number, a hostname and a customer name would have entered the corpus through
    the one field the service writes itself.

    The emitted title is de-identified because the prompt says so
    (:data:`DE_IDENTIFICATION_RULES`); the mint is normalisation only, so a
    title that slipped is a prompt failure, not one this can catch. Falls back
    to the case stem when the draft carries no usable title.
    """
    metadata = _draft_frontmatter(content)
    title = metadata.get("title")
    service = metadata.get("service")
    if isinstance(title, str) and title.strip():
        minted = runbook_id_from_parts(
            service if isinstance(service, str) else "", title
        )
        # The mint no longer returns ``""`` for a title that filters to nothing
        # — it returns ``runbook-<hash>`` (#1230). That is a valid id but a
        # nameless one, and the case stem is strictly more traceable, so the
        # "no usable title" fallback below is preserved by asking the mint which
        # branch it took.
        if not is_hash_only_runbook_id(minted):
            return minted
    return case_stem_runbook_id(case_id)
