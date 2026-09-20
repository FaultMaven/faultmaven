"""Every capability role reports the provider and model it actually runs on.

`GET /admin/llm/config` published `primary_provider` and nothing about the
other seven roles, so the page it feeds reported a configuration omitting
load-bearing routing: `CLASSIFIER_PROVIDER`, `SYNTHESIS_PROVIDER` and
`MULTIMODAL_PROVIDER` ship pinned to gemini and stay put when the anchor is
flipped, while `da`/`knowledge`/`structured_output` ship unset and move with
it (#1206). An admin who switched the anchor to openai was shown "openai" by
a page on which three roles still called Gemini.

Pins:
  1. every role is reported, in `LLM_MODEL_TASKS` order;
  2. the reported (provider, model) EQUALS what the real getters resolve —
     this module reports resolution, it does not re-derive it;
  3. inheritance is distinguishable from a pin: an unset role reports
     `inherited` and the anchor's key, a set one reports its own key;
  4. provenance follows the override map rather than a hardcoded "role keys
     are never overridable", so the deferred write half of #1206 cannot make
     this lie by being added to `_ALLOWED_OVERRIDES` alone;
  5. a pin whose provider was never initialized is reported inert — that is
     the case where routing silently falls back to the chain.
"""

import pytest

from faultmaven.config.role_routing import (
    SOURCE_ENV,
    SOURCE_INHERITED,
    SOURCE_OVERRIDE,
    SOURCE_UNSET,
    resolve_role_routing,
)
from faultmaven.config.settings import LLM_MODEL_TASKS, LLMProvider, LLMSettings

# Every env var these tests touch, cleared up front. `delenv` clears
# os.environ only — the `.env` file is a source pydantic-settings reads
# separately — so each construction also passes `_env_file=None`, the idiom
# `settings.py` uses. (`tests/conftest.py` neuters the dotenv sources
# suite-wide as well; doing both means these exact-value assertions do not
# depend on a patch living in another file.)
_ENV_VARS = (
    "CHAT_PROVIDER",
    "CLASSIFIER_PROVIDER",
    "SYNTHESIS_PROVIDER",
    "MULTIMODAL_PROVIDER",
    "CODE_PROVIDER",
    "DA_PROVIDER",
    "KNOWLEDGE_PROVIDER",
    "STRUCTURED_OUTPUT_PROVIDER",
    "GEMINI_MODEL",
    "GEMINI_CLASSIFIER_MODEL",
    "GEMINI_SYNTHESIS_MODEL",
    "OPENAI_MODEL",
    "OPENAI_DA_MODEL",
    "GROQ_MODEL",
    "ANTHROPIC_MODEL",
    "LOCAL_LLM_MODEL",
)

_ALL_PROVIDERS = tuple(p.value for p in LLMProvider)


@pytest.fixture
def clean_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def _rows(llm, **kwargs):
    return {row.role: row for row in resolve_role_routing(llm, **kwargs)}


@pytest.mark.unit
@pytest.mark.llm
class TestEveryRoleIsReported:
    def test_reports_every_role_in_task_order(self, clean_env):
        rows = resolve_role_routing(LLMSettings(_env_file=None))
        assert [row.role for row in rows] == list(LLM_MODEL_TASKS)

    def test_shipped_defaults_split_pinned_from_inherited(self, clean_env):
        """The shipped shape, which is the one the page was misreporting."""
        rows = _rows(LLMSettings(_env_file=None), initialized_providers=("gemini",))

        pinned = {r.role for r in rows.values() if r.provider_source == SOURCE_ENV}
        inherited = {r.role for r in rows.values() if r.inherited}
        assert pinned == {"chat", "multimodal", "synthesis", "classifier"}
        assert inherited == {"code", "da", "knowledge", "structured_output"}

        # Every role on gemini, but for two different reasons — which is
        # exactly what one anchor field could not express.
        assert {r.provider for r in rows.values()} == {"gemini"}
        assert rows["classifier"].model == "gemini-3.5-flash-lite"
        assert rows["synthesis"].model == "gemini-3.5-flash-lite"
        assert rows["chat"].model == "gemini-3.7-flash"

    def test_flipping_the_anchor_leaves_the_pins_put(self, clean_env):
        """The bug as reported: primary_provider says openai, three roles do not."""
        clean_env.setenv("CHAT_PROVIDER", "openai")
        rows = _rows(
            LLMSettings(_env_file=None), initialized_providers=("gemini", "openai")
        )

        assert [
            (r.role, r.provider, r.provider_source)
            for r in rows.values()
            if r.provider != "openai"
        ] == [
            ("multimodal", "gemini", SOURCE_ENV),
            ("synthesis", "gemini", SOURCE_ENV),
            ("classifier", "gemini", SOURCE_ENV),
        ]
        assert all(
            rows[role].inherited
            for role in ("code", "da", "knowledge", "structured_output")
        )


@pytest.mark.unit
@pytest.mark.llm
class TestReportedValuesMatchTheRealGetters:
    """This module REPORTS resolution; it must never become a second copy of it.

    A divergence here is the failure mode the issue is about — a field that
    states the configured value where the effective one differs — so the
    assertion is against `LLMSettings`'s own getters rather than against
    literals, across shapes where the two could plausibly disagree.
    """

    SHAPES = [
        {},
        {"CHAT_PROVIDER": "openai"},
        {"CHAT_PROVIDER": "local", "LOCAL_LLM_MODEL": "llama3.2"},
        {
            "CHAT_PROVIDER": "openai",
            "CLASSIFIER_PROVIDER": "groq",
            "DA_PROVIDER": "anthropic",
        },
        # A per-task model on a pinned role: the one place where the base
        # model is NOT the answer.
        {
            "CHAT_PROVIDER": "anthropic",
            "DA_PROVIDER": "openai",
            "OPENAI_DA_MODEL": "gpt-5.4-mini",
        },
        # A role pinned to local while the anchor is not: local resolves its
        # model from LOCAL_LLM_MODEL and ignores per-task keys entirely.
        {
            "CHAT_PROVIDER": "cohere",
            "MULTIMODAL_PROVIDER": "local",
            "LOCAL_LLM_MODEL": "llama3.2",
        },
    ]

    @pytest.mark.parametrize(
        "shape", SHAPES, ids=lambda s: ",".join(s) or "shipped-defaults"
    )
    def test_provider_and_model_equal_the_getters(self, clean_env, shape):
        for key, value in shape.items():
            clean_env.setenv(key, value)
        llm = LLMSettings(_env_file=None)
        rows = _rows(llm)

        for role in LLM_MODEL_TASKS:
            if role == "chat":
                expected_provider = llm.provider
                expected_model = llm.get_model("chat")
            else:
                expected_provider = getattr(llm, f"get_{role}_provider")()
                expected_model = getattr(llm, f"get_{role}_model")()
            assert rows[role].provider == expected_provider.value, role
            assert rows[role].model == expected_model, role

    def test_per_task_model_names_its_own_key(self, clean_env):
        """`GEMINI_CLASSIFIER_MODEL` answered, so changing `GEMINI_MODEL`
        from the dashboard would not move this role — which is why the key
        that actually answered is published."""
        rows = _rows(LLMSettings(_env_file=None))
        assert rows["classifier"].model_key == "GEMINI_CLASSIFIER_MODEL"
        assert rows["chat"].model_key == "GEMINI_MODEL"

    def test_local_reports_its_own_model_key(self, clean_env):
        clean_env.setenv("CHAT_PROVIDER", "local")
        clean_env.setenv("LOCAL_LLM_MODEL", "llama3.2")
        rows = _rows(LLMSettings(_env_file=None))
        assert (rows["chat"].model, rows["chat"].model_key) == (
            "llama3.2",
            "LOCAL_LLM_MODEL",
        )

    def test_no_model_configured_is_reported_unset_not_guessed(self, clean_env):
        """A provider with no model reports '' and `unset`, rather than
        inheriting some other provider's model."""
        clean_env.setenv("CHAT_PROVIDER", "local")  # LOCAL_LLM_MODEL not set
        rows = _rows(LLMSettings(_env_file=None))
        assert rows["chat"].model == ""
        assert rows["chat"].model_key == ""
        assert rows["chat"].model_source == SOURCE_UNSET


@pytest.mark.unit
@pytest.mark.llm
class TestProvenance:
    def test_each_role_names_the_key_that_decided_it(self, clean_env):
        clean_env.setenv("CHAT_PROVIDER", "openai")
        rows = _rows(LLMSettings(_env_file=None))
        assert rows["classifier"].provider_key == "CLASSIFIER_PROVIDER"
        assert rows["structured_output"].provider_key == "CHAT_PROVIDER"
        assert rows["chat"].provider_key == "CHAT_PROVIDER"

    def test_anchor_override_is_reported_on_the_chat_row(self, clean_env):
        """`apply_overrides_to_settings` writes the dashboard's
        `primary_provider` onto `settings.llm.provider`, so the effective
        anchor is what resolves — and its provenance is published."""
        llm = LLMSettings(_env_file=None)
        object.__setattr__(llm, "provider", LLMProvider.OPENAI)
        rows = _rows(llm, config_sources={"primary_provider": SOURCE_OVERRIDE})

        assert rows["chat"].provider == "openai"
        assert rows["chat"].provider_source == SOURCE_OVERRIDE
        # An inherited role is reported as inherited, not as the anchor's
        # provenance: what it followed is the question, and the chat row
        # already answers where that came from.
        assert rows["da"].provider == "openai"
        assert rows["da"].provider_source == SOURCE_INHERITED

    def test_overridden_base_model_is_reported_on_every_role_using_it(self, clean_env):
        clean_env.setenv("CHAT_PROVIDER", "openai")
        llm = LLMSettings(_env_file=None)
        object.__setattr__(llm, "openai_model", "gpt-5.4-mini")
        rows = _rows(llm, config_sources={"openai_model": SOURCE_OVERRIDE})

        assert rows["chat"].model_source == SOURCE_OVERRIDE
        assert rows["da"].model_source == SOURCE_OVERRIDE
        # The gemini pins are untouched by an openai override.
        assert rows["classifier"].model_source == SOURCE_ENV

    def test_role_provenance_follows_the_allowlist_not_a_hardcoded_answer(
        self, clean_env
    ):
        """No role key is in `_ALLOWED_OVERRIDES` today — the write half of
        #1206 is deliberately deferred. When it lands, adding the key must be
        enough: this reports whatever the provenance map says rather than
        asserting role keys are environment-only."""
        from faultmaven.config.llm_config_overrides import _ALLOWED_OVERRIDES

        assert "classifier_provider" not in _ALLOWED_OVERRIDES

        rows = _rows(
            LLMSettings(_env_file=None),
            config_sources={"classifier_provider": SOURCE_OVERRIDE},
        )
        assert rows["classifier"].provider_source == SOURCE_OVERRIDE

    def test_absent_from_the_map_is_env_not_a_crash(self, clean_env):
        rows = _rows(LLMSettings(_env_file=None), config_sources={})
        assert {r.provider_source for r in rows.values()} == {
            SOURCE_ENV,
            SOURCE_INHERITED,
        }


@pytest.mark.unit
@pytest.mark.llm
class TestInertPinsAreReportedInert:
    """`route_request` honours a `provider_override` only when that provider
    is in `self._providers`; otherwise it warns and falls back to the normal
    chain. Publishing a pin without saying it is unreachable would restate
    the issue's own failure — a configured value where the effective one
    differs."""

    def test_pin_without_a_credential_is_not_initialized(self, clean_env):
        clean_env.setenv("CHAT_PROVIDER", "openai")
        rows = _rows(LLMSettings(_env_file=None), initialized_providers=("openai",))

        assert rows["chat"].provider_initialized is True
        for role in ("multimodal", "synthesis", "classifier"):
            assert rows[role].provider == "gemini"
            assert rows[role].provider_initialized is False, role

    def test_pin_off_the_fallback_chain_is_still_initialized(self, clean_env):
        """Reachability is registry membership, NOT chain membership: a
        pinned role is legitimately off-chain (that is what #1193 fixed), so
        keying this on the chain would report every shipped pin as broken in
        strict mode."""
        clean_env.setenv("CHAT_PROVIDER", "openai")
        rows = _rows(
            LLMSettings(_env_file=None), initialized_providers=("openai", "gemini")
        )
        assert rows["classifier"].provider_initialized is True

    def test_unmeasured_reachability_is_not_claimed(self, clean_env):
        """`None` means the caller could not ask. Reporting True there would
        assert something nobody measured."""
        rows = _rows(LLMSettings(_env_file=None), initialized_providers=None)
        assert not any(r.provider_initialized for r in rows.values())


@pytest.mark.unit
@pytest.mark.llm
class TestNoRoleIsSilentlyMissed:
    def test_every_provider_and_role_pair_resolves_without_raising(self, clean_env):
        """The resolver reads attributes by name (`{provider}_{role}_model`),
        so a provider whose per-task fields do not exist must still resolve
        rather than raise on a surface an admin loads."""
        for provider in _ALL_PROVIDERS:
            clean_env.setenv("CHAT_PROVIDER", provider)
            rows = resolve_role_routing(LLMSettings(_env_file=None))
            assert len(rows) == len(LLM_MODEL_TASKS), provider
            assert all(isinstance(r.model, str) for r in rows), provider
