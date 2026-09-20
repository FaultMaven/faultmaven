"""Which provider and model each capability role actually runs on.

The role fields (``CLASSIFIER_PROVIDER``, ``SYNTHESIS_PROVIDER``,
``MULTIMODAL_PROVIDER``, ``DA_PROVIDER``, ``KNOWLEDGE_PROVIDER``,
``STRUCTURED_OUTPUT_PROVIDER``, ``CODE_PROVIDER``) are resolved in two
different ways and neither is visible from the anchor alone:

* three of them ship **pinned** to gemini, so they stay put when
  ``CHAT_PROVIDER`` moves — that is what makes an A/B comparison of the anchor
  a controlled one; and
* the rest ship **unset**, so they follow ``CHAT_PROVIDER`` and move with it.

An admin surface that lists only the anchor therefore reports a configuration
that omits load-bearing routing, and one that listed only the explicitly-set
roles would mislead differently — the inherited ones would simply be absent
(#1206). This module answers the whole question for every role: the provider,
the model, the environment key each came from, whether the value was set for
that role or inherited from the anchor, and whether the provider it names is
actually initialized.

That last one is not decoration. ``ProviderRegistry.route_request`` honours a
``provider_override`` only when that provider is in ``self._providers``;
otherwise it logs a warning and falls back to the normal routing chain. So a
pin whose credential is missing is reported as configured everywhere while
something else answers the calls — reporting the pin without saying it is
inert would be a new version of the same lie.

Pure and read-only: nothing here writes settings, and ``_ALLOWED_OVERRIDES``
(what an admin may change from the dashboard) is untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Collection, List, Mapping, Optional

from faultmaven.config.settings import LLM_MODEL_TASKS, LLMProvider

#: The value came from the environment (``.env``, or the shipped default for
#: that key). Same spelling as ``get_config_source_map`` uses, so one response
#: carries one provenance vocabulary.
SOURCE_ENV = "env-default"

#: The value came from a dashboard-written override in ``config_overrides``.
SOURCE_OVERRIDE = "admin-override"

#: No key was set for this role, so it follows ``CHAT_PROVIDER``.
SOURCE_INHERITED = "inherited"

#: Nothing configures this at all — the resolved model is the empty string.
SOURCE_UNSET = "unset"

#: The role whose provider IS the anchor. Its settings field is ``provider``
#: (env ``CHAT_PROVIDER``), not ``chat_provider``, and its override key is
#: ``primary_provider`` — three spellings for one thing, which is why this is
#: named once here rather than special-cased at each use.
_ANCHOR_ROLE = "chat"
_ANCHOR_ENV_KEY = "CHAT_PROVIDER"
_ANCHOR_OVERRIDE_KEY = "primary_provider"


@dataclass(frozen=True)
class ResolvedRole:
    """What one capability role resolves to, and where each half came from."""

    role: str
    provider: str
    model: str
    provider_source: str
    model_source: str
    provider_key: str
    model_key: str
    provider_initialized: bool

    @property
    def inherited(self) -> bool:
        """The role follows ``CHAT_PROVIDER`` rather than naming a provider."""
        return self.provider_source == SOURCE_INHERITED


def _provider_name(value) -> str:
    """The wire name of a provider, whether it arrives as enum or string."""
    return value.value if hasattr(value, "value") else str(value)


def _resolve_model(
    llm, role: str, provider: str, sources: Mapping[str, str]
) -> tuple[str, str, str]:
    """``(model, model_key, model_source)`` for *role* on *provider*.

    Mirrors the resolution order of
    ``LLMSettings._get_model_for_provider_and_task`` — per-task key, then the
    provider's base model — but reports WHICH of the two answered. Knowing
    that matters on this surface: the dashboard can write ``{provider}_model``
    and cannot write ``{provider}_{task}_model``, so a role holding a per-task
    value does not move when an admin changes that provider's model.
    """
    if provider == LLMProvider.LOCAL.value:
        # Local ignores per-task keys entirely (see _get_model_for_provider_and_task).
        model = getattr(llm, "local_model", None) or ""
        if not model:
            return ("", "", SOURCE_UNSET)
        return (model, "LOCAL_LLM_MODEL", sources.get("local_model", SOURCE_ENV))

    per_task = getattr(llm, f"{provider}_{role}_model", None)
    if per_task:
        # Per-task keys are not in _ALLOWED_OVERRIDES, so environment only.
        return (per_task, f"{provider}_{role}_model".upper(), SOURCE_ENV)

    base = getattr(llm, f"{provider}_model", None)
    if base:
        base_key = f"{provider}_model"
        return (base, base_key.upper(), sources.get(base_key, SOURCE_ENV))

    return ("", "", SOURCE_UNSET)


def resolve_role_routing(
    llm,
    config_sources: Optional[Mapping[str, str]] = None,
    initialized_providers: Optional[Collection[str]] = None,
) -> List[ResolvedRole]:
    """Resolve every capability role, in :data:`LLM_MODEL_TASKS` order.

    Args:
        llm: the ``LLMSettings`` instance to read, with any admin overrides
            already applied to it (``apply_overrides_to_settings`` mutates it
            in place, so this sees effective values, not ``.env`` values).
        config_sources: provenance per overridable key, as
            ``get_config_source_map`` returns it. A key absent from the map is
            reported as :data:`SOURCE_ENV`. Passing the map rather than
            hardcoding "role keys are never overridable" is deliberate: if the
            write half of #1206 ever adds a role key to ``_ALLOWED_OVERRIDES``,
            this reports it as an override without being edited.
        initialized_providers: the providers the registry actually built
            (``get_provider_status()`` keys). A role whose provider is not in
            it has its ``provider_initialized`` reported False — the pin is
            inert and calls fall back to the chain. ``None`` means the caller
            could not ask, and every row reports False rather than claiming a
            reachability nobody measured.
    """
    sources = config_sources or {}
    live = set(initialized_providers or ())

    anchor = _provider_name(getattr(llm, "provider", ""))
    rows: List[ResolvedRole] = []

    for role in LLM_MODEL_TASKS:
        explicit = llm.explicit_role_provider(role)

        if role == _ANCHOR_ROLE:
            provider = explicit or anchor
            provider_key = _ANCHOR_ENV_KEY
            provider_source = sources.get(_ANCHOR_OVERRIDE_KEY, SOURCE_ENV)
        elif explicit is not None:
            provider = explicit
            provider_key = f"{role}_provider".upper()
            provider_source = sources.get(f"{role}_provider", SOURCE_ENV)
        else:
            provider = anchor
            provider_key = _ANCHOR_ENV_KEY
            provider_source = SOURCE_INHERITED

        model, model_key, model_source = _resolve_model(llm, role, provider, sources)

        rows.append(
            ResolvedRole(
                role=role,
                provider=provider,
                model=model,
                provider_source=provider_source,
                model_source=model_source,
                provider_key=provider_key,
                model_key=model_key,
                provider_initialized=provider in live,
            )
        )

    return rows
