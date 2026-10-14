"""#1956: every LLM send site in the package is classified, by AST.

A send site is any ``<x>.route(...)`` / ``<x>.generate(...)`` call, and also
any way of reaching those methods without calling them at the site: an
uncalled reference (``send = router.route``, ``functools.partial(router.route,
...)``); a lookup by a constant name (``getattr(x, "route")``,
``x.__getattribute__("route")``, ``operator.methodcaller("route", ...)``,
``operator.attrgetter("route")``, ``type(x).__dict__["route"]``); and a call
through a lookup by a computed name (``getattr(x, name)(...)``, or a name bound
to ``getattr(x, name)`` and called with anything). Each one must be ONE of:

(R) redacted at the site. The guard calls nothing redacted it cannot model
    (#1971): every construct that touches a name the verdict rests on — the
    sent content name and every name that holds it or a part of it, the
    routing and splat locals, the context — must be one the classifier
    models, or the site is refused as ``unmodeled`` with the construct named.

    - Content goes in exactly one of ``messages=`` / ``prompt=``, as a NAME.
      Every binding of it that can reach the call is
      ``await case_redacted(..., ctx)``, ``await case_redacted_messages(...,
      ctx)`` or ``await <ctx>.asanitize_messages(...)``. "Can reach" is
      positional, not by line (a semicolon is two statements), and covers: both
      arms of a branch; a rebinding later in a loop around the call, after the
      closure that sends is defined, or in a nested function that declares it
      ``nonlocal``; an earlier binding a later one does not replace on every
      path — a ``try`` whose handler does not exit, any ``with`` (it may
      suppress), a ``break``/``continue`` in between; and a parameter a
      best-effort redaction may leave as the caller passed it. Every binding
      form counts (an import, ``except ... as``, a ``match`` capture, a
      ``def``/``class``, a comprehension variable). A binding inside the send's
      own statement (a walrus in a keyword, a splat or the receiver; an ``if``
      expression around the send) is refused, as is a send from inside a
      lambda or a comprehension.
    - The helper's context is provably not ``None``: a parameter with no
      default, or a name every binding of which is such. A literal, a call, an
      attribute or an unreadable binding (an unpacking, a ``with`` target) is
      refused.
    - What a messages redaction is handed is plain displays: a list display of
      message dict displays, passed positionally, whose every value but
      ``content`` is a constant. The redaction replaces each ``content`` and
      keeps every other value as given — unredacted, and shared with the
      input — so anything else is refused (``redaction-input``).
    - Nothing changes the sent value afterwards. Every name that holds it or a
      part of it is followed — an alias, an element, a loop or comprehension
      variable, a container, a shallow copy (whose own list is new, whose
      elements are not), a default argument — in the binding function and the
      functions nested in it. Refused: a mutating method (``_MUTATORS``)
      called on any of them or on anything reached from them; an item,
      attribute or augmented assignment through them; handing one to a call
      that is not a known reader, copy, redaction or send (a reader handed a
      ``key=`` or a lambda is not one); storing one on another object. A method
      outside ``_MUTATORS`` and ``_READ_METHODS``, a ``return``/``yield``, a
      ``match``, a ``del``, a ``global``/``nonlocal``, a ``with`` item, a lambda
      parameter of the same name: ``unmodeled``. So is, anywhere in the binding
      function, a frame read by the spellings the guard knows: a call whose
      last name is in ``_FRAME_ACCESS`` (``locals``, ``exec``, ``eval``,
      ``compile``, ``inspect.stack``, ...); an attribute in
      ``_REFLECTION_ATTRIBUTES`` (``__closure__``, ``cell_contents``,
      ``f_locals``, ``f_back``, ``tb_frame``, ``cr_frame``, ``gi_frame``,
      ``ag_frame``); any use of ``ctypes`` or ``gc``.
    - Every other keyword is a routing parameter (``model``, ``max_tokens``,
      ...) whose value is configuration: a constant; a module-level name the
      module binds only to configuration (an import is trusted; a ``def`` or
      ``class`` is not configuration), that no function rebinds and that
      nothing changes if mutable, or an attribute of one; the settings object
      or one of its methods called with configuration; a dict or list of those;
      a conditional between those (its test is not sent; a walrus in it is a
      binding like any other); or a parameter used for nothing but routing
      keywords.
    - A ``**`` splat provably carries routing parameters only: a dict literal or
      ``dict(...)`` keyed only by them, with configuration values; a
      conditional between such; a local bound only to such and changed only by
      assigning a routing key; or a call to an undecorated function defined
      once in the same module, handed configuration, every return of which is
      such. Its name proves nothing, and a parameter cannot be proven.
(A) allowlisted below, with the reason it needs no case pass of its own and the
    exact refusals of each of its sites (``Allowed.sites``: for each site, the
    multiset of ``(kind, detail)``, line numbers stripped, counted per site).
    Every check lists every reason, not the first, and details carry the
    source text, so a second defect at a site — of another kind or the same —
    changes its verdict and fails, and so does a second site.

Anything else fails, with its location and refusals. Trusted by name, outside
the invariant, by ruling: the settings object (``get_settings()``, a
``settings`` parameter, ``self._settings``) and what it returns; a parameter
used only as a routing value (its caller is not checked); imported names and
their attributes; the redaction helpers and ``asanitize_messages`` on any
receiver; the receiver expression of the send. Out of scope: reflection
through any spelling the guard does not know (an attribute named by a computed
string, ``inspect.getclosurevars``, a C extension). The #1952 guard
(``test_rebuilt_prompts_are_redacted_1949.py``) still pins the engine's own
prompt assembly; the two engine splat sites are allowlisted here on its
strength. The same idiom as ``test_openapi_documents_auth.py``: a classifier
that is told about every case, and refuses the ones it is not.
"""

from __future__ import annotations

import ast
import functools
import re
import textwrap
import warnings
from collections import Counter
from pathlib import Path
from typing import NamedTuple

import pytest

import faultmaven

pytestmark = pytest.mark.unit

_ROOT = Path(faultmaven.__file__).parent
_SEND_METHODS = {"route", "generate"}
_REDACTING_HELPERS = {"case_redacted", "case_redacted_messages"}
_REDACTING_METHODS = {"asanitize_messages"}

# The kinds of refusal.
UNCALLED = "uncalled-reference"
GETATTR = "lookup-by-constant-name"
GETATTR_COMPUTED = "lookup-by-computed-name"
POSITIONAL = "positional"
NO_CONTENT = "no-content-keyword"
TWO_CONTENTS = "two-content-keywords"
OTHER_CHANNEL = "unfollowed-keyword"
ROUTING_VALUE = "routing-value"
SPLAT = "splat"
EXPRESSION = "content-expression"
PARAMETER = "content-parameter"
UNREDACTED = "unredacted-binding"
NONE_CONTEXT = "none-context"
CHANGED = "changed-after-redaction"
REDACTION_INPUT = "redaction-input"
UNMODELED = "unmodeled"


def _unlined(detail: str) -> str:
    return re.sub(r"\bline \d+", "line _", detail)


def _verdict(problems) -> tuple:
    """A site's refusals as a multiset, line numbers stripped."""
    return tuple(sorted(Counter((k, _unlined(d)) for k, d in problems).elements()))


class Allowed(NamedTuple):
    sites: Counter  # each site's verdict, as many times as it has sites
    reason: str


def _allowed(reason: str, *sites) -> Allowed:
    return Allowed(Counter(_verdict(site) for site in sites), reason)


#: (path relative to the repo, innermost enclosing function) -> why its sites
#: need no case pass, and the exact refusals each of them is admitted with.
_STARLETTE = "a Starlette routing context's `route` attribute, not an LLM"
_LOGGER = "a logger's method chosen by level; the receiver is a logger, not an LLM"

ALLOWLIST: dict[tuple[str, str], Allowed] = {
    ("faultmaven/api/routes/admin_config.py", "check_llm_connection"): _allowed(
        "a constant connection-test prompt; no case content",
        [
            (EXPRESSION, "sends an expression ('Say hello'), not a redacted name"),
        ],
    ),
    ("faultmaven/infrastructure/llm/router.py", "generate"): _allowed(
        "router internals: ILLMProvider.generate delegating to route",
        [
            (
                ROUTING_VALUE,
                (
                    "max_tokens= is not configuration: 'max_tokens', bound at line _ "
                    "to computed (kwargs.get('max_tokens', 1000))"
                ),
            ),
            (
                ROUTING_VALUE,
                (
                    "min_output_tokens= is not configuration: 'min_output_tokens', "
                    "bound at line _ to computed (kwargs.get('min_output_tokens'))"
                ),
            ),
            (
                ROUTING_VALUE,
                (
                    "model= is not configuration: 'model', bound at line _ to "
                    "computed (kwargs.get('model'))"
                ),
            ),
            (
                ROUTING_VALUE,
                (
                    "provider_override= is not configuration: 'provider_override', "
                    "bound at line _ to computed (kwargs.get('provider_override'))"
                ),
            ),
            (
                ROUTING_VALUE,
                (
                    "reasoning_intent= is not configuration: 'reasoning_intent', "
                    "bound at line _ to computed (kwargs.get('reasoning_intent'))"
                ),
            ),
            (
                ROUTING_VALUE,
                (
                    "response_format= is not configuration: 'response_format', bound "
                    "at line _ to computed (kwargs.get('response_format'))"
                ),
            ),
            (
                ROUTING_VALUE,
                (
                    "temperature= is not configuration: 'temperature', bound at line "
                    "_ to computed (kwargs.get('temperature', 0.7))"
                ),
            ),
            (TWO_CONTENTS, "sends content in both messages= and prompt="),
            (
                OTHER_CHANNEL,
                "passes bypass_cache=, a channel the guard does not follow",
            ),
            (
                OTHER_CHANNEL,
                "passes cache_prompt=, a channel the guard does not follow",
            ),
            (OTHER_CHANNEL, "passes case_id=, a channel the guard does not follow"),
            (OTHER_CHANNEL, "passes data_type=, a channel the guard does not follow"),
            (OTHER_CHANNEL, "passes tool_choice=, a channel the guard does not follow"),
            (OTHER_CHANNEL, "passes tools=, a channel the guard does not follow"),
        ],
    ),
    ("faultmaven/infrastructure/llm/providers/registry.py", "route_request"): _allowed(
        "registry internals: the provider dispatch every routed call ends in",
        [
            (
                PARAMETER,
                "'prompt' is a parameter of route_request; redact it at the site",
            ),
            (
                ROUTING_VALUE,
                (
                    "model= is not configuration: 'model', which is handed to "
                    "record_provider_call(), which may change it at line _: "
                    "`record_provider_call(provider_name, model or getattr(response, "
                    "'model', None) or 'unknown', response, latency_ms, "
                    "outcome='kept' if _kept else 'low_confidence')`"
                ),
            ),
            (
                ROUTING_VALUE,
                (
                    "model= is not configuration: parameter 'model', also used as "
                    "something else"
                ),
            ),
            (
                SPLAT,
                (
                    "splats **kwargs, which can carry content: 'kwargs' is a "
                    "parameter: the guard cannot see what the caller puts in"
                ),
            ),
        ],
    ),
    (
        "faultmaven/modules/knowledge/domain/services/conversion_service/service.py",
        "_convert",
    ): _allowed(
        (
            "sends prepared.redacted_messages, redacted once in _prepare_conversion "
            "(pinned by test_conversion_model_boundary_redaction_1901.py); the model "
            "is prepared.knowledge_model (settings.llm.get_knowledge_model() there) "
            "and the splat pipeline's _knowledge_route_kwargs, which the guard does "
            "not follow across the dataclass and the import"
        ),
        [
            (
                ROUTING_VALUE,
                (
                    "model= is not configuration: 'knowledge_model', bound at line _ "
                    "to an attribute of a local or parameter "
                    "(prepared.knowledge_model)"
                ),
            ),
            (
                SPLAT,
                (
                    "splats **_knowledge_route_kwargs(self._settings), which can "
                    "carry content: a call to _knowledge_route_kwargs() the guard "
                    "cannot follow"
                ),
            ),
            (
                UNREDACTED,
                (
                    "'messages' is bound at line _ to prepared.redacted_messages, not"
                    " by a redaction"
                ),
            ),
        ],
    ),
    (
        "faultmaven/core/investigation/milestone_engine/generation.py",
        "_tool_loop_call",
    ): _allowed(
        (
            "the engine's tool-loop splat; every message it carries is built "
            "through case_redacted (pinned by test_rebuilt_prompts_are_redacted_1949.py)"
        ),
        [
            (NO_CONTENT, "no messages=/prompt= argument the guard can follow"),
            (
                SPLAT,
                (
                    "splats **call_kwargs, which can carry content: 'call_kwargs' is "
                    "bound at line _ to 'generate_kwargs' is bound at line _ to "
                    "'max_tokens' is 'max_tokens', which is handed to "
                    "generate_with_truncation_retry(), which may change it at line _:"
                    " `generate_with_truncation_retry(_tool_loop_call, "
                    "max_tokens=max_tokens, "
                    "ceiling=STRUCTURED_OUTPUT_MAX_TOKENS_CEILING, label=f'tool loop "
                    "iteration {iteration}')`"
                ),
            ),
            (
                SPLAT,
                (
                    "splats **call_kwargs, which can carry content: 'call_kwargs' is "
                    "bound at line _ to 'generate_kwargs' is bound at line _ to "
                    "'max_tokens' is 'max_tokens', which is handed to "
                    "record_provider_call(), which may change it at line _: "
                    "`record_provider_call(getattr(provider, 'provider_name', "
                    "'unknown'), call_kwargs.get('model') or getattr(result, 'model',"
                    " None) or 'unknown', result, getattr(result, 'response_time_ms',"
                    " 0))`"
                ),
            ),
            (
                SPLAT,
                (
                    "splats **call_kwargs, which can carry content: 'call_kwargs' is "
                    "bound at line _ to 'generate_kwargs' is bound at line _ to "
                    "'max_tokens' is 'max_tokens', which is handed to "
                    "self._fit_tool_loop_base(), which may change it at line _: "
                    "`self._fit_tool_loop_base([{'role': 'system', 'content': "
                    "da_system_instruction}, {'role': 'user', 'content': prompt}], "
                    "all_tools, tool_loop_budget, provider_name, max_tokens, "
                    "base_prompt_builder=base_prompt_builder, "
                    "redaction_ctx=redaction_ctx, token_cache=_msg_token_cache)`"
                ),
            ),
            (
                SPLAT,
                (
                    "splats **call_kwargs, which can carry content: 'call_kwargs' is "
                    "bound at line _ to 'generate_kwargs' is bound at line _ to "
                    "'max_tokens' is 'max_tokens', which is handed to "
                    "tool_loop_budget.hard(), which may change it at line _: "
                    "`tool_loop_budget.hard(max_tokens)`"
                ),
            ),
            (
                SPLAT,
                (
                    "splats **call_kwargs, which can carry content: 'call_kwargs' is "
                    "bound at line _ to 'generate_kwargs' is bound at line _ to "
                    "'max_tokens' is parameter 'max_tokens' built into a larger value"
                ),
            ),
            (
                SPLAT,
                (
                    "splats **call_kwargs, which can carry content: 'call_kwargs' is "
                    "bound at line _ to 'generate_kwargs' is bound at line _ to key "
                    "'cache_prompt' is not a routing parameter (`'cache_prompt': "
                    "True`)"
                ),
            ),
            (
                SPLAT,
                (
                    "splats **call_kwargs, which can carry content: 'call_kwargs' is "
                    "bound at line _ to 'generate_kwargs' is bound at line _ to key "
                    "'case_id' is not a routing parameter (`'case_id': case.case_id "
                    "if case is not None else None`)"
                ),
            ),
            (
                SPLAT,
                (
                    "splats **call_kwargs, which can carry content: 'call_kwargs' is "
                    "bound at line _ to 'generate_kwargs' is bound at line _ to key "
                    "'messages' is not a routing parameter (`'messages': "
                    "bounded_messages`)"
                ),
            ),
            (
                SPLAT,
                (
                    "splats **call_kwargs, which can carry content: 'call_kwargs' is "
                    "bound at line _ to 'generate_kwargs' is bound at line _ to key "
                    "'prompt' is not a routing parameter (`'prompt': ''`)"
                ),
            ),
            (
                SPLAT,
                (
                    "splats **call_kwargs, which can carry content: 'call_kwargs' is "
                    "bound at line _ to 'generate_kwargs' is bound at line _ to key "
                    "'tool_choice' is not a routing parameter (`'tool_choice': "
                    "choice`)"
                ),
            ),
            (
                SPLAT,
                (
                    "splats **call_kwargs, which can carry content: 'call_kwargs' is "
                    "bound at line _ to 'generate_kwargs' is bound at line _ to key "
                    "'tools' is not a routing parameter (`'tools': tools_for_call`)"
                ),
            ),
            (
                SPLAT,
                (
                    "splats **call_kwargs, which can carry content: 'call_kwargs' is "
                    "bound at line _ to 'generate_kwargs' is handed to "
                    "record_provider_call(), which may change it at line _: "
                    "`record_provider_call(getattr(provider, 'provider_name', "
                    "'unknown'), call_kwargs.get('model') or getattr(result, 'model',"
                    " None) or 'unknown', result, getattr(result, 'response_time_ms',"
                    " 0))`"
                ),
            ),
            (
                SPLAT,
                (
                    "splats **call_kwargs, which can carry content: 'call_kwargs' is "
                    "bound at line _ to 'generate_kwargs' is written through "
                    "generate_kwargs['model'] at line _: `generate_kwargs['model'] = "
                    "self.deps.da_model`"
                ),
            ),
            (
                SPLAT,
                (
                    "splats **call_kwargs, which can carry content: 'call_kwargs' is "
                    "bound at line _ to 'generate_kwargs' is written through "
                    "generate_kwargs['provider_override'] at line _: "
                    "`generate_kwargs['provider_override'] = "
                    "_override_provider.value`"
                ),
            ),
            (
                SPLAT,
                (
                    "splats **call_kwargs, which can carry content: 'call_kwargs' is "
                    "bound at line _ to 'max_tokens' is 'cap', which is handed to "
                    "record_provider_call(), which may change it at line _: "
                    "`record_provider_call(getattr(provider, 'provider_name', "
                    "'unknown'), call_kwargs.get('model') or getattr(result, 'model',"
                    " None) or 'unknown', result, getattr(result, 'response_time_ms',"
                    " 0))`"
                ),
            ),
            (
                SPLAT,
                (
                    "splats **call_kwargs, which can carry content: 'call_kwargs' is "
                    "bound at line _ to 'max_tokens' is 'cap', which is handed to "
                    "tool_loop_budget.hard(), which may change it at line _: "
                    "`tool_loop_budget.hard(cap)`"
                ),
            ),
            (
                SPLAT,
                (
                    "splats **call_kwargs, which can carry content: 'call_kwargs' is "
                    "bound at line _ to 'max_tokens' is parameter 'cap' built into a "
                    "larger value"
                ),
            ),
            (
                SPLAT,
                (
                    "splats **call_kwargs, which can carry content: 'call_kwargs' is "
                    "handed to record_provider_call(), which may change it at line _:"
                    " `record_provider_call(getattr(provider, 'provider_name', "
                    "'unknown'), call_kwargs.get('model') or getattr(result, 'model',"
                    " None) or 'unknown', result, getattr(result, 'response_time_ms',"
                    " 0))`"
                ),
            ),
            (
                SPLAT,
                (
                    "splats **call_kwargs, which can carry content: 'call_kwargs' is "
                    "written through call_kwargs['messages'] at line _: "
                    "`call_kwargs['messages'] = "
                    "self._bound_tool_loop_messages(messages, tool_loop_budget.soft, "
                    "provider_name, token_cache=_msg_token_cache, "
                    "tools=tools_for_call, window_tokens=tool_loop_budget.hard(cap))`"
                ),
            ),
        ],
    ),
    (
        "faultmaven/core/investigation/milestone_engine/generation.py",
        "llm_operation",
    ): _allowed(
        (
            "the engine's single-shot splat; its prompt is case_redacted at the top "
            "of _generate_structured_output_inner (pinned by #1952's guard)"
        ),
        [
            (NO_CONTENT, "no messages=/prompt= argument the guard can follow"),
            (
                SPLAT,
                (
                    "splats **generate_params, which can carry content: "
                    "'generate_params' is bound at line _ to 'max_tokens' is "
                    "'current_max_tokens', bound at line _ to computed "
                    "(max_tokens_state['value'])"
                ),
            ),
            (
                SPLAT,
                (
                    "splats **generate_params, which can carry content: "
                    "'generate_params' is bound at line _ to key 'bypass_cache' is "
                    "not a routing parameter (`'bypass_cache': "
                    "max_tokens_state['bumped']`)"
                ),
            ),
            (
                SPLAT,
                (
                    "splats **generate_params, which can carry content: "
                    "'generate_params' is bound at line _ to key 'case_id' is not a "
                    "routing parameter (`'case_id': case.case_id if case is not None "
                    "else None`)"
                ),
            ),
            (
                SPLAT,
                (
                    "splats **generate_params, which can carry content: "
                    "'generate_params' is bound at line _ to key 'prompt' is not a "
                    "routing parameter (`'prompt': final_prompt`)"
                ),
            ),
            (
                SPLAT,
                (
                    "splats **generate_params, which can carry content: "
                    "'generate_params' is written through "
                    "generate_params['min_output_tokens'] at line _: "
                    "`generate_params['min_output_tokens'] = min_output_tokens`"
                ),
            ),
            (
                SPLAT,
                (
                    "splats **generate_params, which can carry content: "
                    "'generate_params' is written through "
                    "generate_params['provider_override'] at line _: "
                    "`generate_params['provider_override'] = "
                    "_override_provider.value`"
                ),
            ),
            (
                SPLAT,
                (
                    "splats **generate_params, which can carry content: "
                    "'generate_params' is written through "
                    "generate_params['reasoning_intent'] at line _: "
                    "`generate_params['reasoning_intent'] = reasoning_intent`"
                ),
            ),
            (
                SPLAT,
                (
                    "splats **generate_params, which can carry content: "
                    "'generate_params' is written through "
                    "generate_params['response_format'] at line _: "
                    "`generate_params['response_format'] = strategy.response_format`"
                ),
            ),
            (
                SPLAT,
                (
                    "splats **generate_params, which can carry content: "
                    "'generate_params' is written through "
                    "generate_params['tool_choice'] at line _: "
                    "`generate_params['tool_choice'] = 'required'`"
                ),
            ),
            (
                SPLAT,
                (
                    "splats **generate_params, which can carry content: "
                    "'generate_params' is written through generate_params['tools'] at"
                    " line _: `generate_params['tools'] = "
                    "pydantic_to_openai_tools(schema_model)`"
                ),
            ),
        ],
    ),
    (
        "faultmaven/infrastructure/persistence/user_repository.py",
        "_account_filters",
    ): _allowed(
        "a SQLAlchemy `func` case-folding function chosen by dialect",
        [
            (
                GETATTR_COMPUTED,
                (
                    "calls fold = `getattr(func, SQLITE_CASEFOLD if self._on_sqlite()"
                    " else 'lower')`"
                ),
            ),
        ],
        [
            (
                GETATTR_COMPUTED,
                (
                    "calls fold = `getattr(func, SQLITE_CASEFOLD if self._on_sqlite()"
                    " else 'lower')`"
                ),
            ),
        ],
        [
            (
                GETATTR_COMPUTED,
                (
                    "calls fold = `getattr(func, SQLITE_CASEFOLD if self._on_sqlite()"
                    " else 'lower')`"
                ),
            ),
        ],
    ),
    ("faultmaven/api/route_enumeration.py", "iter_served_routes"): _allowed(
        _STARLETTE,
        [
            (GETATTR, "reaches .route through `getattr(context, 'route', None)`"),
        ],
    ),
    ("faultmaven/api/route_enumeration.py", "iter_documented_routes"): _allowed(
        _STARLETTE,
        [
            (GETATTR, "reaches .route through `getattr(context, 'route', None)`"),
        ],
    ),
    ("faultmaven/api/route_enumeration.py", "iter_served_endpoints"): _allowed(
        _STARLETTE,
        [
            (GETATTR, "reaches .route through `getattr(context, 'route', None)`"),
        ],
    ),
    ("faultmaven/api/route_enumeration.py", "iter_unresolved_routes"): _allowed(
        _STARLETTE,
        [
            (GETATTR, "reaches .route through `getattr(context, 'route', None)`"),
        ],
    ),
    ("faultmaven/api/middleware/route_policy.py", "_flattened_post_paths"): _allowed(
        _STARLETTE,
        [
            (GETATTR, "reaches .route through `getattr(context, 'route', None)`"),
        ],
    ),
    ("faultmaven/infrastructure/logging/coordinator.py", "log_once"): _allowed(
        _LOGGER,
        [
            (
                GETATTR_COMPUTED,
                "calls log_method = `getattr(logger, level.lower(), logger.info)`",
            ),
        ],
    ),
    ("faultmaven/infrastructure/logging/unified.py", "operation"): _allowed(
        _LOGGER,
        [
            (GETATTR_COMPUTED, "calls log_method = `getattr(self.logger, log_level)`"),
        ],
        [
            (GETATTR_COMPUTED, "calls log_method = `getattr(self.logger, log_level)`"),
        ],
    ),
    ("faultmaven/infrastructure/logging/unified.py", "operation_sync"): _allowed(
        _LOGGER,
        [
            (GETATTR_COMPUTED, "calls log_method = `getattr(self.logger, log_level)`"),
        ],
        [
            (GETATTR_COMPUTED, "calls log_method = `getattr(self.logger, log_level)`"),
        ],
    ),
    ("faultmaven/infrastructure/logging/unified.py", "log_event"): _allowed(
        _LOGGER,
        [
            (
                GETATTR_COMPUTED,
                (
                    "calls log_method = `getattr(self.logger, severity, "
                    "self.logger.info)`"
                ),
            ),
        ],
    ),
}

#: The sites that redact at the send, which the scan must find (positive
#: control: a classifier that recognised nothing would pass vacuously).
REDACTED_SITES = {
    ("faultmaven/core/investigation/intent_resolver.py", "_classify"),
    ("faultmaven/core/preprocessing/tier2/local_service.py", "_analyze"),
    ("faultmaven/modules/agent/domain/services/out_of_band.py", "_classify"),
    (
        "faultmaven/modules/agent/domain/services/out_of_band.py",
        "_one_answer_attempt",
    ),
    ("faultmaven/modules/agent/tools/document_qa_tool.py", "_synthesize"),
    ("faultmaven/modules/case/api/title_generation.py", "_generate_title_with_llm"),
    (
        "faultmaven/modules/knowledge/domain/services/conversion_service/pipeline.py",
        "_analyze",
    ),
    (
        "faultmaven/modules/knowledge/domain/services/document_preprocessor.py",
        "_run_content_triage",
    ),
}

_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
_NESTED_SCOPES = (*_FUNCS, ast.ClassDef, ast.Lambda)
_LAZY_SCOPES = (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_TRY = (ast.Try, ast.TryStar)
_WITH = (ast.With, ast.AsyncWith)

#: Keywords that carry routing parameters, never content.
_ROUTING_KWARGS = {
    "model",
    "max_tokens",
    "temperature",
    "top_p",
    "min_output_tokens",
    "reasoning_intent",
    "response_format",
    "provider_override",
}
_CONTENT_KWARGS = ("messages", "prompt")
#: Methods that write into a list, dict or object in place.
_MUTATORS = {
    "append",
    "extend",
    "insert",
    "update",
    "setdefault",
    "__setitem__",
    "__delitem__",
    "__setattr__",
    "__init__",
    "__iadd__",
    "__ior__",
}
#: Methods that only read, copy, or remove: none writes content in. Any method
#: in neither set is unmodeled.
_READ_METHODS = {
    "get",
    "keys",
    "values",
    "items",
    "copy",
    "count",
    "index",
    "startswith",
    "endswith",
    "strip",
    "lstrip",
    "rstrip",
    "split",
    "rsplit",
    "splitlines",
    "replace",
    "lower",
    "upper",
    "casefold",
    "encode",
    "find",
    "rfind",
    "partition",
    "rpartition",
    "pop",
    "popitem",
    "remove",
    "clear",
    "discard",
    "reverse",
    "__len__",
    "__contains__",
    "__getitem__",
}
#: Calls that only read what they are handed.
_READERS = {"len", "str", "repr", "bool", "isinstance", "id", "type", "hash"}
#: Calls that build a new container holding the same elements.
_SHALLOW_COPIES = {"list", "tuple", "dict", "sorted", "copy.copy"}
_DEEP_COPIES = {"copy.deepcopy"}
#: Calls a loop iterates through to reach the elements they are handed.
_ITERATORS = {"enumerate", "reversed", "iter", "zip"}
#: Calls that can read or rebind any name of the frame they run in, matched on
#: the last component of the callee (``builtins.exec``, ``inspect.stack``).
_FRAME_ACCESS = {
    "locals",
    "globals",
    "vars",
    "exec",
    "eval",
    "compile",
    "_getframe",
    "currentframe",
    "stack",
    "trace",
    "getouterframes",
    "getinnerframes",
}
#: Attributes that reach a frame's or a closure's names, and modules that
#: reach any object. Reflection through any other spelling is out of scope.
_REFLECTION_ATTRIBUTES = {
    "__closure__",
    "cell_contents",
    "f_locals",
    "f_back",
    "tb_frame",
    "cr_frame",
    "gi_frame",
    "ag_frame",
}
_REFLECTION_MODULES = {"ctypes", "gc"}


# --------------------------------------------------------------------------
# Positions, scopes and bindings
# --------------------------------------------------------------------------


def _parents(tree: ast.AST) -> dict:
    return {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}


def _pos(node) -> tuple:
    """Where ``node`` starts. Nodes with no position of their own (a with-item,
    a comprehension clause) take their first positioned descendant's."""
    anchor = next(n for n in ast.walk(node) if hasattr(n, "lineno"))
    return anchor.lineno, anchor.col_offset


def _line(node) -> int:
    return _pos(node)[0]


def _within(point: tuple, stmts) -> bool:
    """Whether ``point`` lies inside the statement list ``stmts``."""
    if not stmts:
        return False
    last = stmts[-1]
    return _pos(stmts[0]) <= point <= (last.end_lineno, last.end_col_offset)


def _text(node) -> str:
    """The first line of ``node``'s source."""
    return ast.unparse(node).strip().splitlines()[0]


def _exits(block) -> bool:
    return bool(block) and isinstance(
        block[-1], (ast.Raise, ast.Return, ast.Continue, ast.Break)
    )


def _scopes(node, parents) -> list:
    """Enclosing functions, innermost first."""
    chain = []
    while node in parents:
        node = parents[node]
        if isinstance(node, _FUNCS):
            chain.append(node)
    return chain


def _module(node, parents) -> ast.AST:
    while node in parents:
        node = parents[node]
    return node


def _own_nodes(func):
    """The function's (or module's) body, not descending into nested
    functions, lambdas or classes."""
    stack = list(func.body)
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _NESTED_SCOPES):
            stack.extend(ast.iter_child_nodes(node))


def _store_targets(target):
    if isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            yield from _store_targets(elt)
    elif isinstance(target, ast.Starred):
        yield from _store_targets(target.value)
    else:
        yield target


def _bound_names(node):
    """The names ``node`` binds in the scope it sits in, in every binding form."""
    targets: list = []
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        targets = [node.target]
    elif isinstance(node, (ast.AugAssign, ast.NamedExpr)):
        targets = [node.target]
    elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        targets = [node.target]
    elif isinstance(node, ast.withitem) and node.optional_vars is not None:
        targets = [node.optional_vars]
    for target in targets:
        for t in _store_targets(target):
            if isinstance(t, ast.Name):
                yield t.id
    if isinstance(node, ast.ExceptHandler) and node.name:
        yield node.name
    if isinstance(node, ast.alias):
        yield node.asname or node.name.split(".")[0]
    if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
        yield node.name
    if isinstance(node, ast.MatchMapping) and node.rest:
        yield node.rest
    if isinstance(node, (*_FUNCS, ast.ClassDef)):
        yield node.name


def _own_bindings(func, name: str) -> list:
    return [node for node in _own_nodes(func) if name in _bound_names(node)]


def _binding_nodes(func, name: str) -> list:
    """Where ``func`` binds ``name``: in its own body, and in a nested function
    that declares it ``nonlocal``."""
    out = _own_bindings(func, name)
    for inner in ast.walk(func):
        if (
            inner is not func
            and isinstance(inner, _FUNCS)
            and any(
                isinstance(n, ast.Nonlocal) and name in n.names
                for n in _own_nodes(inner)
            )
        ):
            out += _own_bindings(inner, name)
    return out


def _bound_value(node, name: str):
    """The value a plain binding gives ``name``; ``None`` for every other
    binding form (a loop target, an unpacking, an import, ...)."""
    if isinstance(node, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id == name for t in node.targets
    ):
        return node.value
    if isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and isinstance(
        node.target, ast.Name
    ):
        return node.value
    return None


def _binding_text(node, name: str) -> str:
    """How ``node`` binds ``name``, with its source."""
    value = _bound_value(node, name)
    if value is not None:
        return f"to {ast.unparse(value)}"
    return f"by `{_text(node)}`"


def _parameters(func) -> set[str]:
    a = func.args
    names = [x.arg for x in a.posonlyargs + a.args + a.kwonlyargs]
    names += [x.arg for x in (a.vararg, a.kwarg) if x is not None]
    return set(names)


def _defaults(func) -> dict:
    """Parameter name -> its default expression."""
    a = func.args
    positional = a.posonlyargs + a.args
    defaults = dict(
        zip(
            [p.arg for p in positional[len(positional) - len(a.defaults) :]], a.defaults
        )
    )
    defaults.update(
        {p.arg: d for p, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None}
    )
    return defaults


class _Binding(NamedTuple):
    """Where a name seen from a scope chain is bound: the innermost function
    that binds it or takes it as a parameter (``None`` for a module-level or
    builtin name), its bindings there, and the chain from there outwards."""

    func: ast.AST | None
    nodes: list
    is_param: bool
    chain: list


def _resolve(name: str, chain: list) -> _Binding:
    for i, func in enumerate(chain):
        nodes = _binding_nodes(func, name)
        is_param = name in _parameters(func)
        if nodes or is_param:
            return _Binding(func, nodes, is_param, chain[i:])
    return _Binding(None, [], False, [])


def _ancestors(node, parents):
    while node in parents:
        node = parents[node]
        yield node


def _stmt(node, parents):
    while not isinstance(node, ast.stmt):
        node = parents[node]
    return node


def _swallowed(node, point: tuple, parents) -> bool:
    """Whether an exception at ``node`` can be swallowed with execution going
    on to ``point``: ``node`` sits in a ``try`` body whose handlers do not all
    exit, or in any ``with`` body (a context manager may suppress), and
    ``point`` is not in that same body."""
    for anc in _ancestors(node, parents):
        if isinstance(anc, _FUNCS):
            return False
        if isinstance(anc, _TRY) and _within(_pos(node), anc.body):
            if not _within(point, anc.body) and not all(
                _exits(h.body) for h in anc.handlers
            ):
                return True
        if isinstance(anc, _WITH) and _within(_pos(node), anc.body):
            if not _within(point, anc.body):
                return True
    return False


def _kills(later, earlier, point: tuple, parents) -> bool:
    """Whether ``later`` always runs after ``earlier`` on the way to ``point``:
    it sits in a statement list that holds, at an earlier position, a statement
    containing ``earlier``, with no ``break``/``continue`` in between, and no
    exception at ``later`` can be swallowed on the way. Two arms of a branch
    never kill each other."""
    if _swallowed(later, point, parents):
        return False
    s_later, s_earlier = _stmt(later, parents), _stmt(earlier, parents)
    block_owner = parents[s_later]
    for field in ("body", "orelse", "finalbody", "handlers"):
        block = getattr(block_owner, field, None)
        if isinstance(block, list) and s_later in block:
            index = block.index(s_later)
            chain = {s_earlier, *_ancestors(s_earlier, parents)}
            starts = [i for i, stmt in enumerate(block[:index]) if stmt in chain]
            if not starts:
                return False
            between = block[starts[0] + 1 : index]
            return not any(
                isinstance(n, (ast.Break, ast.Continue))
                for stmt in between
                for n in ast.walk(stmt)
            )
    return False


def _definitely_bound(stmts, name: str, point: tuple) -> bool:
    """Whether running ``stmts`` (those before ``point``) binds ``name`` on
    every path: a plain binding; an ``if`` whose arms both do; a ``try`` body
    that does when ``point`` is in it or its ``else``, or when every handler
    exits or binds too; a ``finally`` that does; a ``with`` body that does when
    ``point`` is in it. A loop body may run zero times."""
    for stmt in stmts:
        if _pos(stmt) >= point:
            return False
        targets: list = []
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
        elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
            targets = [stmt.target]
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return True
        if isinstance(stmt, ast.If) and (
            _definitely_bound(stmt.body, name, point)
            and _definitely_bound(stmt.orelse, name, point)
        ):
            return True
        if isinstance(stmt, _TRY) and _try_binds(stmt, name, point):
            return True
        if isinstance(stmt, _WITH) and _within(point, stmt.body):
            if _definitely_bound(stmt.body, name, point):
                return True
    return False


def _try_binds(stmt, name: str, point: tuple) -> bool:
    if _definitely_bound(stmt.finalbody, name, point):
        return True
    if not _definitely_bound(stmt.body, name, point):
        return False
    if _within(point, stmt.body) or _within(point, stmt.orelse):
        return True
    in_handler = _within(point, stmt.finalbody) or any(
        _within(point, h.body) for h in stmt.handlers
    )
    return not in_handler and all(
        _exits(h.body) or _definitely_bound(h.body, name, point) for h in stmt.handlers
    )


def _loops_around(node, func, parents) -> list:
    out = []
    for anc in _ancestors(node, parents):
        if anc is func:
            break
        if isinstance(anc, (ast.For, ast.AsyncFor, ast.While)):
            out.append(anc)
    return out


# --------------------------------------------------------------------------
# What holds a value, and what can change it (#1971 rules 1 and 2)
# --------------------------------------------------------------------------


def _least(*depths):
    known = [d for d in depths if d is not None]
    return min(known) if known else None


def _reach(expr, depth: dict):
    """The depth from which a change through ``expr`` reaches the tracked
    value, or ``None`` when ``expr`` holds no part of it. Depth 0: the value
    itself, an alias, or a part of it — any change reaches. Depth 1: a new
    container whose elements are tracked (a shallow copy, ``[messages]``) —
    changing the container does not reach, changing an element does."""
    if isinstance(expr, ast.Name):
        return depth.get(expr.id)
    if isinstance(expr, (ast.NamedExpr, ast.Await, ast.Starred)):
        return _reach(expr.value, depth)
    if isinstance(expr, ast.IfExp):
        return _least(_reach(expr.body, depth), _reach(expr.orelse, depth))
    if isinstance(expr, ast.BoolOp):
        return _least(*(_reach(v, depth) for v in expr.values))
    if isinstance(expr, (ast.Subscript, ast.Attribute)):
        d = _reach(expr.value, depth)
        if d is None:
            return None
        if isinstance(expr, ast.Subscript) and isinstance(expr.slice, ast.Slice):
            return max(d, 1)
        return max(d - 1, 0)
    if isinstance(expr, ast.Call):
        return _call_reach(expr, depth)
    if isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
        d = _least(*(_reach(e, depth) for e in expr.elts))
        return None if d is None else d + 1
    if isinstance(expr, ast.Dict):
        d = _least(*(_reach(v, depth) for v in expr.values))
        return None if d is None else d + 1
    if isinstance(expr, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        names = {n.id for n in ast.walk(expr) if isinstance(n, ast.Name)}
        return 1 if names & set(depth) else None
    if isinstance(expr, ast.BinOp):
        d = _least(_reach(expr.left, depth), _reach(expr.right, depth))
        return None if d is None else max(d, 1)
    return None


def _call_reach(call, depth: dict):
    func = ast.unparse(call.func)
    args = [_reach(a, depth) for a in [*call.args, *(k.value for k in call.keywords)]]
    if func in _DEEP_COPIES:
        return None
    if func in _SHALLOW_COPIES:
        d = _least(*args)
        return None if d is None else max(d, 1)
    if func in _ITERATORS:
        return _least(*args)
    if isinstance(call.func, ast.Attribute):
        d = _reach(call.func.value, depth)
        if d is not None:
            # ``x.copy()`` is a shallow copy; any other method's result is
            # read as a part of ``x``.
            return max(d, 1) if call.func.attr == "copy" else max(d - 1, 0)
    return None


def _derivations(node, depth: dict):
    """(name, depth) for each name ``node`` binds to a part of the value."""
    if isinstance(node, (*_FUNCS, ast.Lambda)):
        # A default argument holds what it was given when the def ran.
        for param, default in _defaults(node).items():
            d = _reach(default, depth)
            if d is not None:
                yield param, d
        return
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
        d = None if node.value is None else _reach(node.value, depth)
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    elif isinstance(node, ast.AugAssign):
        d = _reach(node.value, depth)
        d = None if d is None else max(d, 1)
        targets = [node.target]
    elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        d = _reach(node.iter, depth)
        d = None if d is None else max(d - 1, 0)
        targets = [node.target]
    else:
        return
    if d is None:
        return
    for target in targets:
        unpacked = not isinstance(target, ast.Name)
        for t in _store_targets(target):
            if isinstance(t, ast.Name):
                yield t.id, max(d - 1, 0) if unpacked else d


def _holders(func, name: str) -> dict:
    """Every name in ``func`` and the functions nested in it that can hold the
    value ``name`` holds, or a part of it, with its depth (see ``_reach``)."""
    depth = {name: 0}
    grown = True
    while grown:
        grown = False
        for node in ast.walk(func):
            for held, d in _derivations(node, depth):
                if held not in depth or d < depth[held]:
                    depth[held] = d
                    grown = True
    return depth


def _root(expr):
    """The name an access chain starts from, and the hops between."""
    hops = 0
    while True:
        if isinstance(expr, (ast.Subscript, ast.Attribute)):
            expr, hops = expr.value, hops + 1
        elif isinstance(expr, ast.Call):
            expr = expr.func
        elif isinstance(expr, (ast.Starred, ast.Await, ast.NamedExpr)):
            expr = expr.value
        else:
            return (expr.id if isinstance(expr, ast.Name) else None), hops


def _takes_a_callable(call) -> bool:
    return any(k.arg == "key" for k in call.keywords) or any(
        isinstance(a, ast.Lambda)
        for a in [*call.args, *(k.value for k in call.keywords)]
    )


def _harmless(call) -> bool:
    """A call that reads, copies, redacts or sends what it is handed. A reader
    handed a callable (``sorted(..., key=...)``) or ``type()`` building a class
    is not one."""
    func = ast.unparse(call.func)
    if func in _READERS | _SHALLOW_COPIES | _DEEP_COPIES | _ITERATORS:
        return not _takes_a_callable(call) and not (
            func == "type" and len(call.args) > 1
        )
    if func in _REDACTING_HELPERS:
        return True
    return isinstance(call.func, ast.Attribute) and (
        call.func.attr in _SEND_METHODS | _REDACTING_METHODS
    )


def _call_changes(call, depth: dict):
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr in _MUTATORS:
        root, hops = _root(func.value)
        if root in depth and hops >= depth[root]:
            yield f"is changed by {ast.unparse(func)}()"
    if isinstance(func, ast.Name) and func.id in depth:
        yield f"is changed through {func.id}(), a method taken from it"
    if not _harmless(call) and any(
        _reach(arg, depth) is not None
        for arg in [*call.args, *(k.value for k in call.keywords)]
    ):
        yield f"is handed to {ast.unparse(func)}(), which may change it"


def _store_changes(node, depth: dict):
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    for t in (t for target in targets for t in _store_targets(target)):
        if isinstance(t, ast.Name):
            if isinstance(node, ast.AugAssign) and depth.get(t.id) == 0:
                yield f"is extended in place by {t.id} +="
            continue
        root, hops = _root(t)
        if root in depth and hops - 1 >= depth[root]:
            yield f"is written through {ast.unparse(t)}"
        elif node.value is not None and _reach(node.value, depth) is not None:
            yield f"is stored on {ast.unparse(t)}, where anything can change it"


def _changes(func, name: str) -> list:
    """Every place in ``func`` (and the functions nested in it) that can change
    the value ``name`` holds, through any name that holds it or a part of it:
    ``(node, how)``. Order-insensitive: a change before the binding counts."""
    depth = _holders(func, name)
    out = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            out += [(node, how) for how in _call_changes(node, depth)]
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            out += [(node, how) for how in _store_changes(node, depth)]
    return out


# --------------------------------------------------------------------------
# Fail-closed: every use of a tracked name is one the classifier models
# --------------------------------------------------------------------------

#: Expression nodes a use passes through on its way to the construct that
#: decides what happens to it.
_TRANSPARENT = (
    ast.Attribute,
    ast.Subscript,
    ast.Starred,
    ast.Await,
    ast.IfExp,
    ast.BoolOp,
    ast.BinOp,
    ast.UnaryOp,
    ast.Compare,
    ast.JoinedStr,
    ast.FormattedValue,
    ast.Tuple,
    ast.List,
    ast.Set,
    ast.Dict,
    ast.Slice,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.keyword,
)
#: Statements and clauses where a use is read, bound, stored or changed in a
#: way ``_derivations`` / ``_changes`` model.
_MODELED_OWNERS = (
    ast.Assign,
    ast.AugAssign,
    ast.Expr,
    ast.If,
    ast.While,
    ast.Assert,
    ast.For,
    ast.AsyncFor,
    ast.arguments,
)


_FLOWS_ON = object()


def _unmodeled_use(node, depth: dict, parents, returns: bool) -> str | None:
    """``None`` when the use of a tracked name at ``node`` is a construct the
    classifier models, else that construct. The use is followed up through the
    expressions it flows through to the construct that decides its fate."""
    child = node
    while True:
        parent = parents.get(child)
        if parent is None:
            return None
        if isinstance(parent, ast.Call):
            verdict = _unmodeled_call_use(parent, child)
            if verdict is not _FLOWS_ON:
                return verdict
            child = parent
            continue
        if isinstance(parent, ast.NamedExpr):
            child = parent  # the target is a binding; the value flows on
            continue
        if isinstance(parent, ast.comprehension):
            if child is parent.target and _reach(parent.iter, depth) is None:
                return f"a comprehension variable shadows it: `{_text(parent)}`"
            child = parent
            continue
        if isinstance(parent, _TRANSPARENT):
            child = parent
            continue
        if isinstance(parent, _MODELED_OWNERS):
            return None
        if isinstance(parent, ast.Return) and returns:
            return None  # a followed splat helper: its returns are checked
        if isinstance(parent, ast.AnnAssign) and child is not parent.annotation:
            return None
        if isinstance(parent, ast.withitem) and child is parent.optional_vars:
            return None  # a binding, refused by the binding check
        return f"`{_text(parent)}`"


def _unmodeled_call_use(call, child):
    """A use that reaches a call: as an argument, ``_call_changes`` decides
    (unless a reader is handed a callable); as the receiver, the method must
    be a known mutator (``_call_changes``) or reader (its result flows on)."""
    if child is not call.func:
        func = ast.unparse(call.func)
        readers = _READERS | _SHALLOW_COPIES | _DEEP_COPIES | _ITERATORS
        if func in readers and not _harmless(call):
            return f"`{_text(call)}`, a reader handed a callable or a class body"
        return None
    if isinstance(child, ast.Name):
        return None  # calling a holder: ``_call_changes``
    if not isinstance(child, ast.Attribute):
        return f"`{_text(call)}`, which calls it"
    if child.attr in _MUTATORS | _REDACTING_METHODS:
        return None
    if child.attr in _READ_METHODS | {"sort"} and not _takes_a_callable(call):
        return _FLOWS_ON
    return f"`{_text(call)}`, a method the guard does not model"


def _unmodeled(func, name: str, returns: bool = False) -> list:
    """Every construct in ``func`` (and the functions nested in it) touching
    the value ``name`` holds, through any name that holds it, that the
    classifier does not model: ``(node, construct)``. ``returns``: ``func`` is
    a splat helper whose returns are checked, so a ``return`` is modeled."""
    depth = _holders(func, name)
    parents = _parents(func)
    out = []
    for node in ast.walk(func):
        if isinstance(node, ast.Name) and node.id in depth:
            construct = _unmodeled_use(node, depth, parents, returns)
            if construct:
                out.append((node, f"{node.id!r} is used in {construct}"))
        elif isinstance(node, (ast.Global, ast.Nonlocal)) and set(node.names) & set(
            depth
        ):
            out.append((node, f"`{_text(node)}` binds it in another scope"))
        elif isinstance(node, ast.Lambda) and _parameters(node) & set(depth):
            out.append((node, f"a lambda parameter shadows it: `{_text(node)}`"))
        elif isinstance(node, ast.Call) and _reads_the_frame(node):
            out.append((node, f"`{_text(node)}` reads every name of the frame"))
        elif isinstance(node, ast.Attribute) and node.attr in _REFLECTION_ATTRIBUTES:
            out.append((node, f"`{_text(node)}` reaches a frame's or closure's names"))
        elif isinstance(node, ast.Name) and node.id in _REFLECTION_MODULES:
            out.append((node, f"`{node.id}` can reach any object"))
    return out


def _reads_the_frame(call) -> bool:
    func = call.func
    last = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if last == "vars":
        return not call.args
    return last in _FRAME_ACCESS


def _unmodeled_problems(func, name: str, what: str, returns=False) -> list:
    return list(
        dict.fromkeys(
            (UNMODELED, f"{what}: {construct} at line {_line(node)}")
            for node, construct in _unmodeled(func, name, returns)
        )
    )


# --------------------------------------------------------------------------
# Routing values and splats (#1971 rules 3 and 5)
# --------------------------------------------------------------------------


def _is_get_settings(expr) -> bool:
    return (
        isinstance(expr, ast.Call)
        and not expr.args
        and not expr.keywords
        and ast.unparse(expr.func).split(".")[-1] == "get_settings"
    )


def _settings_object(expr, chain: list) -> bool:
    """Whether ``expr`` is the settings object or an attribute of it:
    ``get_settings()``, a name bound only from it, a ``settings`` parameter,
    ``self._settings`` / ``self.settings``. Trusted by name (by ruling)."""
    attrs = []
    while isinstance(expr, ast.Attribute):
        attrs.append(expr.attr)
        expr = expr.value
    if _is_get_settings(expr):
        return True
    if not isinstance(expr, ast.Name):
        return False
    if expr.id == "self" and attrs and attrs[-1] in {"settings", "_settings"}:
        return True
    binding = _resolve(expr.id, chain)
    if binding.is_param:
        return expr.id == "settings" and not binding.nodes
    return bool(binding.nodes) and all(
        _is_get_settings(_bound_value(node, expr.id)) for node in binding.nodes
    )


def _used_only_for_routing(func, name: str) -> bool:
    """Whether every read of ``name`` in ``func`` is the whole value of a
    routing keyword of a send, or the test of a conditional splat."""
    parents = _parents(func)
    for node in ast.walk(func):
        if not (
            isinstance(node, ast.Name)
            and node.id == name
            and isinstance(node.ctx, ast.Load)
        ):
            continue
        keyword = parents.get(node)
        call = parents.get(keyword)
        if not (
            isinstance(keyword, ast.keyword)
            and keyword.arg in _ROUTING_KWARGS
            and isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr in _SEND_METHODS
        ):
            return False
    return True


def _routing_problems(
    value, chain: list, module, *, top=True, trusted=frozenset(), seen=frozenset()
) -> list:
    """Every reason ``value`` is not configuration; empty when it is. ``top``:
    the whole value of the keyword (where a routing-only parameter is
    accepted); ``trusted``: a helper's parameters, checked at its call site.
    Every reason is listed, not the first, so an allowlist pin names them
    all."""

    def nested(v):
        return _routing_problems(
            v, chain, module, top=False, trusted=trusted, seen=seen
        )

    if isinstance(value, ast.Constant) or _settings_object(value, chain):
        return []
    if isinstance(value, ast.UnaryOp):
        return nested(value.operand)
    if isinstance(value, ast.JoinedStr):
        if any(isinstance(v, ast.FormattedValue) for v in value.values):
            return [f"an f-string ({ast.unparse(value)})"]
        return []
    if isinstance(value, (ast.List, ast.Tuple)):
        return [why for elt in value.elts for why in nested(elt)]
    if isinstance(value, ast.Dict):
        out = [
            f"a dict with a computed key ({ast.unparse(value)})"
            for key in value.keys
            if not isinstance(key, ast.Constant)
        ]
        return out + [why for item in value.values for why in nested(item)]
    if isinstance(value, ast.IfExp):
        # The test selects and is not sent; a walrus in it is a binding, which
        # the binding checks (and the send's-own-statement rule) see.
        return [
            why
            for arm in (value.body, value.orelse)
            for why in _routing_problems(
                arm, chain, module, top=top, trusted=trusted, seen=seen
            )
        ]
    if isinstance(value, ast.Name):
        return _routing_name_problems(value.id, chain, module, top, trusted, seen)
    if isinstance(value, ast.Attribute):
        base = value
        while isinstance(base, ast.Attribute):
            base = base.value
        if (
            isinstance(base, ast.Name)
            and base.id not in trusted
            and _resolve(base.id, chain).func is None
        ):
            # An attribute of a module-level name: an enum member, a constant.
            return _module_name_problems(base.id, module, seen)
        return [f"an attribute of a local or parameter ({ast.unparse(value)})"]
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and _settings_object(value.func.value, chain)
    ):
        args = [*value.args, *(k.value for k in value.keywords)]
        return [why for arg in args for why in nested(arg)]
    return [f"computed ({ast.unparse(value)})"]


def _change_text(node, how: str) -> str:
    return f"{how} at line {_line(node)}: `{_text(node)}`"


def _routing_name_problems(name, chain, module, top, trusted, seen) -> list:
    if name in trusted:
        return []
    binding = _resolve(name, chain)
    if binding.func is None:
        return _module_name_problems(name, module, seen)
    key = (name, id(binding.func))
    if key in seen:
        return []
    seen = seen | {key}
    out = []
    if binding.is_param and not top:
        out.append(f"parameter {name!r} built into a larger value")
    elif binding.is_param and not _used_only_for_routing(binding.func, name):
        out.append(f"parameter {name!r}, also used as something else")
    for node in binding.nodes:
        value = _bound_value(node, name)
        where = f"{name!r}, bound at line {_line(node)}"
        if value is None:
            out.append(f"{where} {_binding_text(node, name)}")
            continue
        out += [
            f"{where} to {why}"
            for why in _routing_problems(
                value, binding.chain, module, top=top, trusted=trusted, seen=seen
            )
        ]
    # Storing it under a routing key of a dict is how a routing-only splat is
    # built; that dict's own changes are checked where it is splatted.
    out += [
        f"{name!r}, which {_change_text(node, how)}"
        for node, how in _changes(binding.func, name)
        if not _routing_key_assignment(node, binding.chain, module, trusted, seen)
    ]
    return out


def _module_name_problems(name, module, seen) -> list:
    """A module-level (or imported, or builtin) name is configuration when the
    module binds it only to configuration (an import is trusted; a ``def`` or
    ``class`` is not configuration), no function rebinds it (``global``), and
    — when it is a dict, list or set — nothing changes it."""
    out = [
        f"{name!r}, a module global a function rebinds: `{_text(node)}`"
        for node in ast.walk(module)
        if isinstance(node, ast.Global) and name in node.names
    ]
    key = (name, id(module))
    if key in seen:
        return out
    seen = seen | {key}
    nodes = [n for n in _binding_nodes(module, name) if not isinstance(n, ast.alias)]
    for node in nodes:
        value = _bound_value(node, name)
        where = f"{name!r}, bound at line {_line(node)}"
        if value is None:
            out.append(f"{where} {_binding_text(node, name)}")
            continue
        out += [
            f"{where} to {why}"
            for why in _routing_problems(value, [], module, top=False, seen=seen)
        ]
    mutable = (ast.Dict, ast.List, ast.Set)
    if any(isinstance(_bound_value(node, name), mutable) for node in nodes):
        out += [
            f"{name!r}, which {_change_text(node, how)}"
            for node, how in _changes(module, name)
        ]
    return out


def _module_function(module, name: str):
    """The function a module-level name refers to, when the guard can follow
    it: one undecorated ``def``, the name's only module-level binding, which
    no function rebinds."""
    defs = [n for n in module.body if isinstance(n, _FUNCS) and n.name == name]
    if len(defs) != 1 or not isinstance(defs[0], ast.FunctionDef):
        return None
    if defs[0].decorator_list or _binding_nodes(module, name) != defs:
        return None
    if any(isinstance(n, ast.Global) and name in n.names for n in ast.walk(module)):
        return None
    return defs[0]


def _splat_problems(
    value, chain: list, module, *, trusted=frozenset(), seen=frozenset()
) -> list:
    """Every reason a ``**`` splat may carry more than routing parameters with
    configuration values; empty when it cannot. Its name proves nothing."""

    def splat(v):
        return _splat_problems(v, chain, module, trusted=trusted, seen=seen)

    if isinstance(value, ast.Dict):
        return [
            why
            for key, item in zip(value.keys, value.values)
            for why in _routing_item_problems(key, item, chain, module, trusted, splat)
        ]
    if isinstance(value, ast.IfExp):
        return splat(value.body) + splat(value.orelse)
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
        if value.func.id == "dict" and _resolve("dict", chain).func is None:
            out = [why for arg in value.args for why in splat(arg)]
            for k in value.keywords:
                key = None if k.arg is None else ast.Constant(k.arg)
                out += _routing_item_problems(
                    key, k.value, chain, module, trusted, splat
                )
            return out
        helper = _module_function(module, value.func.id)
        if helper is not None and _resolve(value.func.id, chain).func is None:
            return _helper_problems(value, helper, chain, module, trusted, seen)
        return [f"a call to {value.func.id}() the guard cannot follow"]
    if isinstance(value, ast.Name):
        return _splat_name_problems(value.id, chain, module, trusted, seen)
    return [f"{ast.unparse(value)}"]


def _routing_item_problems(key, item, chain, module, trusted, splat) -> list:
    if key is None:
        return splat(item)
    if not (isinstance(key, ast.Constant) and key.value in _ROUTING_KWARGS):
        pair = f"{ast.unparse(key)}: {ast.unparse(item)}"
        return [f"key {ast.unparse(key)} is not a routing parameter (`{pair}`)"]
    return [
        f"{key.value!r} is {why}"
        for why in _routing_problems(item, chain, module, top=False, trusted=trusted)
    ]


def _helper_problems(call, helper, chain, module, trusted, seen) -> list:
    if helper in seen:
        return [f"{helper.name}() is recursive"]
    out = [
        f"{helper.name}() is handed {why}"
        for arg in [*call.args, *(k.value for k in call.keywords)]
        for why in _routing_problems(arg, chain, module, top=False, trusted=trusted)
    ]
    returns = [n for n in _own_nodes(helper) if isinstance(n, ast.Return)]
    if not returns or any(r.value is None for r in returns):
        out.append(f"{helper.name}() can return what the guard cannot follow")
    params = frozenset(_parameters(helper))
    out += [
        f"{helper.name}() returns {why}"
        for ret in returns
        if ret.value is not None
        for why in _splat_problems(
            ret.value, [helper], module, trusted=params, seen=seen | {helper}
        )
    ]
    return out


def _splat_name_problems(name, chain, module, trusted, seen) -> list:
    binding = _resolve(name, chain)
    if binding.func is None:
        return [f"{name!r} is not a local the guard can follow"]
    key = (name, id(binding.func))
    if key in seen:
        return []
    seen = seen | {key}
    out = []
    if name in trusted or binding.is_param:
        out.append(
            f"{name!r} is a parameter: the guard cannot see what the caller puts in"
        )
    for node in binding.nodes:
        value = _bound_value(node, name)
        where = f"{name!r} is bound at line {_line(node)}"
        if value is None:
            out.append(f"{where} {_binding_text(node, name)}")
            continue
        out += [
            f"{where} to {why}"
            for why in _splat_problems(
                value, binding.chain, module, trusted=trusted, seen=seen
            )
        ]
    out += [
        f"{name!r} {_change_text(node, how)}"
        for node, how in _changes(binding.func, name)
        if not _routing_key_assignment(node, binding.chain, module, trusted, seen)
    ]
    return out


def _routing_key_assignment(node, chain, module, trusted, seen) -> bool:
    """``kwargs["<routing key>"] = <configuration>``: the one change a
    routing-only dict may take."""
    if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
        return False
    target = node.targets[0]
    return (
        isinstance(target, ast.Subscript)
        and isinstance(target.value, ast.Name)
        and isinstance(target.slice, ast.Constant)
        and target.slice.value in _ROUTING_KWARGS
        and not _routing_problems(
            node.value, chain, module, top=False, trusted=trusted, seen=seen
        )
    )


def _value_names(expr, chain: list):
    """The names a routing or splat value is built from: not the settings
    object (trusted by name), not a called function's own name."""
    if _settings_object(expr, chain):
        return
    if isinstance(expr, ast.Name):
        yield expr.id
        return
    if isinstance(expr, ast.Call) and (
        isinstance(expr.func, ast.Name)
        or (
            isinstance(expr.func, ast.Attribute)
            and _settings_object(expr.func.value, chain)
        )
    ):
        for arg in [*expr.args, *(k.value for k in expr.keywords)]:
            yield from _value_names(arg, chain)
        return
    for child in ast.iter_child_nodes(expr):
        yield from _value_names(child, chain)


def _routing_locals(values, chain: list, module) -> tuple:
    """Every local (scope, name) the routing values and splats ``values`` are
    built from, followed through their bindings, the routing keys stored into
    them, and the same-module helpers they call: the names whose uses must
    all be modeled; and the helpers. The settings object is trusted by name,
    not followed."""
    stack = [(value, chain) for value in values]
    out: set = set()
    helpers: set = set()
    while stack:
        expr, here = stack.pop()
        for node in ast.walk(expr):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                helper = _module_function(module, node.func.id)
                if helper is not None and helper not in helpers:
                    helpers.add(helper)
                    out |= {
                        (helper, param)
                        for param in _parameters(helper)
                        if not _settings_object(ast.Name(param), [helper])
                    }
                    stack += [
                        (ret.value, [helper])
                        for ret in _own_nodes(helper)
                        if isinstance(ret, ast.Return) and ret.value is not None
                    ]
        for name in set(_value_names(expr, here)):
            binding = _resolve(name, here)
            if binding.func is None or (binding.func, name) in out:
                continue
            out.add((binding.func, name))
            for node in binding.nodes:
                value = _bound_value(node, name)
                if value is not None:
                    stack.append((value, binding.chain))
            for node in ast.walk(binding.func):
                if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Subscript) and _root(t)[0] == name
                    for t in node.targets
                ):
                    stack.append((node.value, binding.chain))
    return out, helpers


# --------------------------------------------------------------------------
# The redaction binding and its context (#1971 rule 4)
# --------------------------------------------------------------------------


def _context_problem(ctx, chain: list, seen=frozenset()) -> str | None:
    """``None`` when the context ``ctx`` is provably not ``None``: a parameter
    with no default, or a name every binding of which is one. Fail-closed:
    anything else (a literal, a call, an attribute, an unreadable binding) is
    refused."""
    if ctx is None:
        return "no context argument"
    if isinstance(ctx, ast.NamedExpr):
        return _context_problem(ctx.value, chain, seen)
    if isinstance(ctx, ast.IfExp):
        return _context_problem(ctx.body, chain, seen) or _context_problem(
            ctx.orelse, chain, seen
        )
    if isinstance(ctx, ast.BoolOp):
        return next(
            filter(None, (_context_problem(v, chain, seen) for v in ctx.values)), None
        )
    if not isinstance(ctx, ast.Name):
        return f"{ast.unparse(ctx)}, which may be None"
    binding = _resolve(ctx.id, chain)
    if binding.func is None:
        return f"{ctx.id!r}, a module-level name"
    default = _defaults(binding.func).get(ctx.id) if binding.is_param else None
    if default is not None:
        return f"{ctx.id!r}, a parameter defaulting to {ast.unparse(default)}"
    key = (ctx.id, id(binding.func))
    if key in seen:
        return None
    for node in binding.nodes:
        value = _bound_value(node, ctx.id)
        if value is None:
            return (
                f"{ctx.id!r}, bound at line {_line(node)} {_binding_text(node, ctx.id)}"
            )
        why = _context_problem(value, binding.chain, seen | {key})
        if why:
            return f"{ctx.id!r}, bound at line {_line(node)} to {why}"
    return None


def _redaction_problems(node, name: str, chain: list) -> list:
    """Why the binding is not an awaited redaction with a context that is
    provably not ``None``; and the context's own unmodeled uses."""
    value = _bound_value(node, name)
    call = value.value if isinstance(value, ast.Await) else None
    if not isinstance(call, ast.Call):
        call = None
    if (
        call is not None
        and isinstance(call.func, ast.Name)
        and (call.func.id in _REDACTING_HELPERS)
    ):
        ctx = call.args[1] if len(call.args) > 1 else None
        ctx = next((k.value for k in call.keywords if k.arg == "ctx"), ctx)
    elif (
        call is not None
        and isinstance(call.func, ast.Attribute)
        and (call.func.attr in _REDACTING_METHODS)
    ):
        # The receiver is the context: a None one raises and sends nothing.
        return _input_problems(call, name, node)
    else:
        detail = f"{name!r} is bound at line {_line(node)} {_binding_text(node, name)}"
        return [(UNREDACTED, f"{detail}, not by a redaction")]
    problems = []
    if call.func.id == "case_redacted_messages":
        problems += _input_problems(call, name, node)
    why = _context_problem(ctx, chain)
    if why:
        problems.append(
            (
                NONE_CONTEXT,
                f"{name!r} is redacted at line {_line(node)} with a context the "
                f"guard cannot prove is not None: {why}",
            )
        )
    ctx_names = (
        {n.id for n in ast.walk(ctx) if isinstance(n, ast.Name)} if ctx else set()
    )
    for ctx_name in sorted(ctx_names):
        binding = _resolve(ctx_name, chain)
        if binding.func is not None:
            problems += _unmodeled_problems(binding.func, ctx_name, "the context")
            problems += [
                (CHANGED, f"the context {ctx_name!r} {how} at line {_line(at)}")
                for at, how in _changes(binding.func, ctx_name)
            ]
    return problems


def _input_problems(call, name: str, node) -> list:
    """Every way the messages a redaction is handed are not plain displays.
    The redaction copies each message and replaces its ``content``; every
    other value goes to the provider as given, and stays shared with the
    input. So the input must be a list display of message dict displays,
    passed positionally, whose every value but ``content`` is a constant."""
    at = f"{name!r} is redacted at line {_line(node)} from"
    if not call.args:
        return [(REDACTION_INPUT, f"{at} an input passed by keyword: `{_text(call)}`")]
    messages = call.args[0]
    if not isinstance(messages, ast.List):
        return [(REDACTION_INPUT, f"{at} `{_text(messages)}`, not a list display")]
    out = []
    for message in messages.elts:
        if not isinstance(message, ast.Dict):
            out.append(f"`{_text(message)}`, not a dict display")
            continue
        for key, value in zip(message.keys, message.values):
            if key is None:
                out.append(f"`**{_text(value)}`, an unpacked message")
            elif not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                out.append(f"`{_text(key)}`, a computed message key")
            elif key.value != "content" and not isinstance(value, ast.Constant):
                pair = f"{_text(key)}: {_text(value)}"
                out.append(f"`{pair}`, neither content nor a constant")
    return [(REDACTION_INPUT, f"{at} {why}") for why in out]


# --------------------------------------------------------------------------
# The classifier
# --------------------------------------------------------------------------


def _content_problems(call, name: str, parents) -> list:
    """Why the content name ``name`` may reach ``call`` unredacted."""
    # The reference point: the call itself, or — when the name is bound in an
    # enclosing function — the nested def the call sits in (a closure can run
    # whenever, so a rebinding after its definition reaches it too).
    point, scopes = call, _scopes(call, parents)
    own = _stmt(call, parents)
    for i, func in enumerate(scopes):
        bindings = _binding_nodes(func, name)
        if not bindings:
            if name in _parameters(func):
                detail = (
                    f"{name!r} is a parameter of {func.name}; redact it at the site"
                )
                return [(PARAMETER, detail)]
            point = func
            continue
        at = _pos(point)
        problems = [
            (
                UNMODELED,
                f"{name!r} is bound inside the send's own statement: `{_text(b)}`",
            )
            for b in bindings
            if _stmt(b, parents) is own
        ]
        outside = [b for b in bindings if _stmt(b, parents) is not own]
        loops = _loops_around(point, func, parents)
        before = [b for b in outside if _pos(b) < at]
        reaching = [
            b
            for b in before
            if not any(_kills(b2, b, at, parents) for b2 in before if b2 is not b)
        ]
        after = [b for b in outside if _pos(b) > at]
        if point is not call:
            reaching += after  # rebinding after the closure is defined
        else:
            reaching += [
                b for b in after if set(_loops_around(b, func, parents)) & set(loops)
            ]
        if not reaching:
            problems.append((UNREDACTED, f"{name!r} is not bound before the send"))
        elif not _definitely_bound(func.body, name, at):
            how = (
                "as the caller passed it"
                if name in _parameters(func)
                else "unbound or unredacted"
            )
            problems.append(
                (UNREDACTED, f"{name!r} may reach the send {how} on some path")
            )
        for binding in reaching:
            problems += _redaction_problems(binding, name, scopes[i:])
        problems += [
            (CHANGED, f"{name!r} {how} at line {_line(node)}")
            for node, how in _changes(func, name)
        ]
        problems += _unmodeled_problems(func, name, "the sent value")
        return list(dict.fromkeys(problems))
    return [(UNREDACTED, f"{name!r} is not bound in an enclosing function")]


def _call_problems(call, parents) -> list:
    chain = _scopes(call, parents)
    module = _module(call, parents)
    problems = []
    lazy = next(
        (
            a
            for a in _ancestors(call, parents)
            if isinstance(a, (*_FUNCS, *_LAZY_SCOPES))
        ),
        None,
    )
    if isinstance(lazy, _LAZY_SCOPES):
        problems.append(
            (UNMODELED, f"sent from inside a {type(lazy).__name__}: `{_text(lazy)}`")
        )
    if call.args:
        problems.append((POSITIONAL, "passes a positional argument"))
    configuration = []  # the routing values and splats judged configuration
    for k in call.keywords:
        if k.arg is None:
            whys = _splat_problems(k.value, chain, module)
            splat = ast.unparse(k.value)
            problems += [
                (SPLAT, f"splats **{splat}, which can carry content: {why}")
                for why in whys
            ]
        elif k.arg in _ROUTING_KWARGS:
            whys = _routing_problems(k.value, chain, module)
            problems += [
                (ROUTING_VALUE, f"{k.arg}= is not configuration: {why}") for why in whys
            ]
        else:
            if k.arg not in _CONTENT_KWARGS:
                detail = f"passes {k.arg}=, a channel the guard does not follow"
                problems.append((OTHER_CHANNEL, detail))
            continue
        if not whys:
            configuration.append(k.value)
    tracked, helpers = _routing_locals(configuration, chain, module)
    for func, name in sorted(tracked, key=lambda fn: (_pos(fn[0]), fn[1])):
        what = f"the routing value {name!r}"
        problems += _unmodeled_problems(func, name, what, returns=func in helpers)
    sent = {k.arg: k.value for k in call.keywords if k.arg in _CONTENT_KWARGS}
    if not sent:
        problems.append(
            (NO_CONTENT, "no messages=/prompt= argument the guard can follow")
        )
    elif len(sent) > 1:
        problems.append((TWO_CONTENTS, "sends content in both messages= and prompt="))
    else:
        [value] = sent.values()
        if isinstance(value, ast.Name):
            problems += _content_problems(call, value.id, parents)
        else:
            detail = f"sends an expression ({ast.unparse(value)}), not a redacted name"
            problems.append((EXPRESSION, detail))
    return problems


#: Calls and attributes that look a method up by name: the argument that names it.
_LOOKUP_METHODS = {"__getattribute__", "__getattr__"}
_LOOKUP_FACTORIES = {"methodcaller", "attrgetter"}


def _is_namespace(expr) -> bool:
    """``x.__dict__`` or ``vars(x)``: a mapping from attribute names."""
    if isinstance(expr, ast.Attribute):
        return expr.attr == "__dict__"
    return (
        isinstance(expr, ast.Call)
        and ast.unparse(expr.func) == "vars"
        and bool(expr.args)
    )


def _lookup_name(node):
    """The expression naming the attribute ``node`` looks up, when ``node`` is
    a lookup by name: ``getattr(x, NAME)``, ``x.__getattribute__(NAME)``,
    ``methodcaller(NAME, ...)``, ``attrgetter(NAME)``, ``x.__dict__[NAME]``,
    ``vars(x)[NAME]``, ``x.__dict__.get(NAME)``. ``None`` otherwise."""
    if isinstance(node, ast.Subscript):
        return node.slice if _is_namespace(node.value) else None
    if not isinstance(node, ast.Call) or not node.args:
        return None
    func = node.func
    callee = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if callee == "getattr" and isinstance(func, ast.Name):
        return node.args[1] if len(node.args) > 1 else None
    if callee in _LOOKUP_METHODS:
        return node.args[-1]
    if callee in _LOOKUP_FACTORIES:
        return node.args[0]
    if (
        callee == "get"
        and isinstance(func, ast.Attribute)
        and _is_namespace(func.value)
    ):
        return node.args[0]
    return None


def _names_a_send(name) -> bool:
    return (
        isinstance(name, ast.Constant)
        and isinstance(name.value, str)
        and any(part in _SEND_METHODS for part in name.value.split("."))
    )


def _is_computed_getattr(node) -> bool:
    name = _lookup_name(node)
    return name is not None and not isinstance(name, ast.Constant)


def _bound_lookup(call, parents):
    """The computed lookup a called name was bound to, if any."""
    if not isinstance(call.func, ast.Name) or not (call.args or call.keywords):
        return None
    binding = _resolve(call.func.id, _scopes(call, parents))
    return next(
        (
            value
            for value in (_bound_value(n, call.func.id) for n in binding.nodes)
            if _is_computed_getattr(value)
        ),
        None,
    )


def classify(node, parents) -> tuple:
    """Every reason the site is not redacted at the send, as ``(kind,
    detail)``; empty when it is (R)."""
    if isinstance(node, ast.Attribute):
        site = _text(parents[node])
        return ((UNCALLED, f"an uncalled reference to .{node.attr} ({site})"),)
    name = _lookup_name(node)
    if _names_a_send(name):
        return ((GETATTR, f"reaches .{name.value} through `{_text(node)}`"),)
    if name is not None:
        return ((GETATTR_COMPUTED, f"calls what `{_text(node)}` finds"),)
    lookup = _bound_lookup(node, parents)
    if lookup is not None:
        detail = f"calls {node.func.id} = `{_text(lookup)}`"
        return ((GETATTR_COMPUTED, detail),)
    return tuple(_call_problems(node, parents))


def _send_sites(tree):
    parents = _parents(tree)
    for node in ast.walk(tree):
        name = _lookup_name(node)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _SEND_METHODS
        ):
            yield node
        elif (
            isinstance(node, ast.Attribute)
            and node.attr in _SEND_METHODS
            and isinstance(node.ctx, ast.Load)
            and not (
                isinstance(parents.get(node), ast.Call) and parents[node].func is node
            )
        ):
            yield node
        elif _names_a_send(name):
            yield node
        elif name is not None and _called(node, parents):
            yield node
        elif isinstance(node, ast.Call) and _bound_lookup(node, parents) is not None:
            yield node


def _called(node, parents) -> bool:
    """Whether a lookup's result is called: ``getattr(x, n)(...)``,
    ``methodcaller(n)(x)``, ``attrgetter(n)(x)``."""
    parent = parents.get(node)
    return isinstance(parent, ast.Call) and parent.func is node


@functools.lru_cache(maxsize=1)
def _scan() -> tuple:
    """Every send site: (path, innermost function, line, problems)."""
    return tuple(_walk_package())


def _walk_package():
    for path in sorted(_ROOT.rglob("*.py")):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", (DeprecationWarning, SyntaxWarning))
            tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = _parents(tree)
        rel = path.relative_to(_ROOT.parent).as_posix()
        for site in _send_sites(tree):
            scopes = _scopes(site, parents)
            where = scopes[0].name if scopes else "<module>"
            yield rel, where, _line(site), classify(site, parents)


def _kinds(problems) -> frozenset:
    return frozenset(kind for kind, _ in problems)


def _unadmitted(rows) -> list:
    """The refused sites no allowlist entry admits: in a function that is not
    listed, or beyond its entry's pins — each pinned verdict admits as many
    sites as it is pinned for, so a duplicated site fails too."""
    left = {key: Counter(entry.sites) for key, entry in ALLOWLIST.items()}
    out = []
    for rel, where, line, problems in rows:
        if not problems:
            continue
        verdict = _verdict(problems)
        pins = left.get((rel, where))
        if pins is not None and pins[verdict] > 0:
            pins[verdict] -= 1
            continue
        pinned = "beyond its entry's pins" if pins is not None else "not allowlisted"
        details = "\n    ".join(f"({k}, {d!r})" for k, d in verdict)
        out.append(f"{rel}:{line} in {where} [{pinned}]:\n    {details}")
    return out


def test_every_send_site_is_redacted_or_allowlisted():
    unknown = _unadmitted(_scan())
    assert unknown == [], (
        "an LLM send site must send what the case redaction returned "
        "(`messages = await case_redacted_messages(..., redaction_ctx)`, then "
        "`route(messages=messages)`), or be allowlisted here with its reason "
        "and its exact refusals:\n" + "\n".join(unknown)
    )


def test_the_redacted_sites_are_the_ones_expected():
    found = {(rel, where) for rel, where, _, problems in _scan() if not problems}
    assert found == REDACTED_SITES


def test_every_allowlist_entry_still_names_an_unredacted_site():
    """A stale entry, or a pin with no site left, would silently admit a new
    site that reuses its function name or its refusals."""
    observed: dict = {}
    for rel, where, _, problems in _scan():
        if problems:
            observed.setdefault((rel, where), Counter())[_verdict(problems)] += 1
    stale = {
        key: sorted(entry.sites - observed.get(key, Counter()))
        for key, entry in ALLOWLIST.items()
        if entry.sites - observed.get(key, Counter())
    }
    assert stale == {}


# --------------------------------------------------------------------------
# The classifier, on synthetic sources (positive and negative controls).
# --------------------------------------------------------------------------


def _verdicts(source: str) -> list:
    tree = ast.parse(textwrap.dedent(source))
    parents = _parents(tree)
    return [classify(call, parents) for call in _send_sites(tree)]


@pytest.mark.parametrize(
    "source",
    [
        # the direct form
        """
        async def f(router, prompt, ctx):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages)
        """,
        # bound in the enclosing function, sent from a retry closure
        """
        async def f(router, prompt, ctx):
            messages = await ctx.asanitize_messages([{"content": prompt}])

            async def _send(cap):
                return await router.route(messages=messages, max_tokens=cap)

            return await retry(_send)
        """,
        # a prompt rebound through the helper before the send
        """
        async def f(provider, ctx):
            prompt = f"Title: {raw}"
            prompt = await case_redacted(prompt, ctx)
            return await provider.generate(prompt=prompt)
        """,
        # both arms of a branch redact
        """
        async def f(router, prompt, ctx, cond):
            if cond:
                messages = await case_redacted_messages([{"content": prompt}], ctx)
            else:
                messages = await ctx.asanitize_messages([{"content": "x"}])
            return await router.route(messages=messages)
        """,
        # routing overrides splatted from a helper in the same module, every
        # return of which is a routing-only dict (its name proves nothing)
        """
        def _role_route_kwargs(settings, role):
            override = settings.llm.explicit_role_provider(role)
            return {"provider_override": override} if override else {}

        async def f(router, prompt, ctx, settings):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(
                messages=messages, model="m", **_role_route_kwargs(settings, "chat")
            )
        """,
        # #1971: the routing values the real sites pass — settings reads, a
        # routing-only parameter, module constants, an enum member, a constant
        # response_format, a routing dict built by item assignment
        """
        async def f(router, prompt, ctx, cap):
            from faultmaven.config.settings import get_settings

            settings = get_settings()
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            model = settings.llm.get_classifier_model()
            route_kwargs = {}
            override = settings.llm.explicit_role_provider("classifier")
            if override:
                route_kwargs["provider_override"] = override
            return await router.route(
                messages=messages,
                model=model,
                max_tokens=cap,
                min_output_tokens=MIN_OUTPUT_TOKENS,
                temperature=0.0,
                reasoning_intent=ReasoningIntent.EXTRACTION,
                response_format={"type": "json_object"},
                **route_kwargs,
            )
        """,
        """
        _CAP = 512

        async def f(self, prompt, ctx):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await self._router.route(
                messages=messages,
                model=self._settings.llm.get_synthesis_model(),
                max_tokens=_CAP,
                temperature=-0.0,
                **(
                    {"provider_override": override}
                    if (override := self._settings.llm.explicit_role_provider("x"))
                    else {}
                ),
            )
        """,
        # #1971: a shallow copy is a new list — appending to it, or replacing
        # one of its slots, does not reach the sent one
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            transcript = list(messages)
            transcript.append({"content": raw})
            return await router.route(messages=messages)
        """,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            snapshot = messages[:]
            snapshot.append({"content": raw})
            snapshot[0] = {"content": raw}
            return await router.route(messages=messages)
        """,
        # #1971: a deep copy is independent, whatever is done to it
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            shadow = copy.deepcopy(messages)
            shadow[0]["content"] = raw
            return await router.route(messages=messages)
        """,
        # #1971: a non-mutating method through an element, and a reader
        """
        async def f(router, prompt, ctx):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            if len(messages[-1]["content"].replace(" ", "")):
                return await router.route(messages=messages)
        """,
    ],
)
def test_the_classifier_accepts_a_redacted_send(source):
    assert _verdicts(source) == [()]


@pytest.mark.parametrize(
    "source",
    [
        # a literal list: the pre-#1956 shape of every side call
        """
        async def f(router, prompt):
            return await router.route(messages=[{"content": prompt}])
        """,
        # bound, but not by the redaction
        """
        async def f(router, prompt):
            messages = build(prompt)
            return await router.route(messages=messages)
        """,
        # redacted, then overwritten before the send
        """
        async def f(router, prompt, ctx):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            messages = [{"content": prompt}]
            return await router.route(messages=messages)
        """,
        # handed in by the caller: the guard cannot see where it came from
        """
        async def f(router, messages):
            return await router.route(messages=messages)
        """,
        # a splat
        """
        async def f(provider, kwargs):
            return await provider.generate(**kwargs)
        """,
        # positional
        """
        async def f(provider, prompt):
            return await provider.generate(prompt)
        """,
        # a redaction that is not awaited is a coroutine, not text
        """
        async def f(router, prompt, ctx):
            messages = case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages)
        """,
        # an alias: the send happens through a name the scan cannot follow
        """
        async def f(router, prompt):
            send = router.route
            return await send(messages=[{"content": prompt}])
        """,
        # functools.partial over the method
        """
        async def f(router, prompt):
            send = functools.partial(router.route, messages=[{"content": prompt}])
            return await send()
        """,
        # getattr
        """
        async def f(router, prompt):
            return await getattr(router, "route")(messages=[{"content": prompt}])
        """,
        # the helper, with a literal None context, redacts nothing
        """
        async def f(router, prompt):
            messages = await case_redacted_messages([{"content": prompt}], None)
            return await router.route(messages=messages)
        """,
        """
        async def f(provider, prompt):
            prompt = await case_redacted(prompt, None)
            return await provider.generate(prompt=prompt)
        """,
        # mutated after the redaction
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            messages.append({"role": "user", "content": raw})
            return await router.route(messages=messages)
        """,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            messages.extend([{"role": "user", "content": raw}])
            return await router.route(messages=messages)
        """,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            messages.insert(0, {"role": "system", "content": raw})
            return await router.route(messages=messages)
        """,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            messages += [{"role": "user", "content": raw}]
            return await router.route(messages=messages)
        """,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            messages[0]["content"] = raw
            return await router.route(messages=messages)
        """,
        # raw content through a second channel
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages, system=raw)
        """,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages, prompt=raw)
        """,
        # a splat that is not a routing override
        """
        async def f(router, prompt, ctx, extra):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages, **extra)
        """,
        # one arm of a branch redacts, the other does not
        """
        async def f(router, prompt, ctx, cond):
            if cond:
                messages = [{"content": prompt}]
            else:
                messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages)
        """,
        # redacted only on one arm, the caller's value on the other
        """
        async def f(router, messages, ctx, cond):
            if cond:
                messages = await case_redacted_messages(messages, ctx)
            return await router.route(messages=messages)
        """,
        # rebound later in the loop the send sits in
        """
        async def f(router, prompts, ctx):
            messages = await case_redacted_messages([{"content": "x"}], ctx)
            for p in prompts:
                await router.route(messages=messages)
                messages = [{"content": p}]
        """,
        # rebound after the closure that sends it is defined
        """
        async def f(router, prompt, ctx):
            messages = await case_redacted_messages([{"content": prompt}], ctx)

            async def _send():
                return await router.route(messages=messages)

            messages = [{"content": prompt}]
            return await _send()
        """,
        # rebound by a nested function that declares it nonlocal
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)

            def _reset():
                nonlocal messages
                messages = [{"content": raw}]

            _reset()
            return await router.route(messages=messages)
        """,
    ],
)
def test_the_classifier_refuses_an_unredacted_send(source):
    [problems] = _verdicts(source)
    assert problems != ()


#: #1971: shapes the name-based guard classified R. Each is refused for the
#: kind its rule names; the PR records the verdict the previous guard gave.
_UNPROVEN = {
    # rule 1: a name that holds the sent value, or a part of it, changes it
    "1-alias-appended": (
        CHANGED,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            alias = messages
            alias.append({"role": "user", "content": raw})
            return await router.route(messages=messages)
        """,
    ),
    "1-alias-of-an-alias": (
        CHANGED,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            alias = messages
            other = alias
            other[0]["content"] = raw
            return await router.route(messages=messages)
        """,
    ),
    "1-element-bound-to-a-name": (
        CHANGED,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            first = messages[0]
            first["content"] = raw
            return await router.route(messages=messages)
        """,
    ),
    "1-loop-over-the-elements": (
        CHANGED,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            for message in messages:
                message["content"] = raw
            return await router.route(messages=messages)
        """,
    ),
    "1-shallow-copy-shares-its-elements": (
        CHANGED,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            snapshot = messages[:]
            snapshot[0]["content"] = raw
            return await router.route(messages=messages)
        """,
    ),
    "1-held-in-a-container": (
        CHANGED,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            holder = [messages]
            holder[0].append({"content": raw})
            return await router.route(messages=messages)
        """,
    ),
    "1-handed-to-a-call": (
        CHANGED,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            _add_context(messages, raw)
            return await router.route(messages=messages)
        """,
    ),
    "1-stored-on-an-object": (
        CHANGED,
        """
        async def f(self, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            self.pending = messages
            self.pending.append({"content": raw})
            return await self.router.route(messages=messages)
        """,
    ),
    "1-changed-in-a-lambda": (
        CHANGED,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            add = lambda: messages.append({"content": raw})
            add()
            return await router.route(messages=messages)
        """,
    ),
    "1-changed-in-a-nested-function": (
        CHANGED,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            if raw:

                def _add():
                    messages.append({"content": raw})

                _add()
            return await router.route(messages=messages)
        """,
    ),
    # rule 2: a mutating method called through an element
    "2-element-update": (
        CHANGED,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            messages[0].update({"content": raw})
            return await router.route(messages=messages)
        """,
    ),
    "2-element-setdefault": (
        CHANGED,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            messages[-1].setdefault("name", raw)
            return await router.route(messages=messages)
        """,
    ),
    "2-content-blocks-appended": (
        CHANGED,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            messages[0]["content"].append({"type": "text", "text": raw})
            return await router.route(messages=messages)
        """,
    ),
    # rule 3: a splat is traced to what it holds, not trusted by its name
    "3-named-dict-with-content": (
        SPLAT,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            route_kwargs = {"system": raw}
            return await router.route(messages=messages, **route_kwargs)
        """,
    ),
    "3-named-dict-given-a-content-key": (
        SPLAT,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            route_kwargs = {}
            route_kwargs["system"] = raw
            return await router.route(messages=messages, **route_kwargs)
        """,
    ),
    "3-named-dict-updated": (
        SPLAT,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            route_kwargs = {"model": "m"}
            route_kwargs.update(system=raw)
            return await router.route(messages=messages, **route_kwargs)
        """,
    ),
    "3-named-parameter": (
        SPLAT,
        """
        async def f(router, prompt, ctx, route_kwargs):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages, **route_kwargs)
        """,
    ),
    "3-named-call-not-defined-here": (
        SPLAT,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages, **_route_kwargs_for(raw))
        """,
    ),
    "3-named-helper-returning-content": (
        SPLAT,
        """
        def _role_route_kwargs(raw):
            return {"system": raw}

        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages, **_role_route_kwargs(raw))
        """,
    ),
    # rule 4: a context bound to None through a variable
    "4-variable-bound-to-none": (
        NONE_CONTEXT,
        """
        async def f(router, prompt):
            ctx = None
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages)
        """,
    ),
    "4-none-on-one-arm": (
        NONE_CONTEXT,
        """
        async def f(router, prompt, turn_ctx, cond):
            ctx = turn_ctx if cond else None
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages)
        """,
    ),
    "4-none-through-another-name": (
        NONE_CONTEXT,
        """
        async def f(provider, prompt):
            nothing = None
            ctx = nothing
            prompt = await case_redacted(prompt, ctx=ctx)
            return await provider.generate(prompt=prompt)
        """,
    ),
    "4-parameter-defaulting-to-none": (
        NONE_CONTEXT,
        """
        async def f(router, prompt, ctx=None):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages)
        """,
    ),
    # rule 5: a routing keyword carries configuration, not content
    "5-model-from-an-f-string": (
        ROUTING_VALUE,
        """
        async def f(router, prompt, ctx):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages, model=f"m-{prompt}")
        """,
    ),
    "5-response-format-carrying-content": (
        ROUTING_VALUE,
        """
        async def f(router, prompt, ctx):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(
                messages=messages,
                response_format={"type": "json_schema", "schema": {"title": prompt}},
            )
        """,
    ),
    "5-routing-name-built-by-a-call": (
        ROUTING_VALUE,
        """
        async def f(router, prompt, ctx):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            fmt = build_format(prompt)
            return await router.route(messages=messages, response_format=fmt)
        """,
    ),
    "5-parameter-that-is-also-content": (
        ROUTING_VALUE,
        """
        async def f(router, prompt, ctx):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages, provider_override=prompt)
        """,
    ),
    "5-attribute-of-a-parameter": (
        ROUTING_VALUE,
        """
        async def f(router, case, ctx):
            messages = await case_redacted_messages([{"content": case.title}], ctx)
            return await router.route(messages=messages, model=case.description)
        """,
    ),
    "5-method-of-content": (
        ROUTING_VALUE,
        """
        async def f(router, prompt, ctx):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages, model=prompt.strip())
        """,
    ),
    "5-routing-dict-changed-with-content": (
        ROUTING_VALUE,
        """
        async def f(router, prompt, ctx):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            fmt = {"type": "json_schema"}
            fmt["schema"] = {"title": prompt}
            return await router.route(messages=messages, response_format=fmt)
        """,
    ),
    "5-module-dict-changed-with-content": (
        ROUTING_VALUE,
        """
        _FORMAT = {"type": "json_schema"}

        async def f(router, prompt, ctx):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            _FORMAT["schema"] = {"title": prompt}
            return await router.route(messages=messages, response_format=_FORMAT)
        """,
    ),
    "5-module-global-rebound": (
        ROUTING_VALUE,
        """
        MODEL = "m"

        def remember(raw):
            global MODEL
            MODEL = raw

        async def f(router, prompt, ctx):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages, model=MODEL)
        """,
    ),
    "5-routing-splat-carrying-content": (
        SPLAT,
        """
        async def f(router, prompt, ctx):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages, **{"model": prompt.strip()})
        """,
    ),
    # rule 6: a method getattr finds by a computed name
    "6-getattr-computed-called": (
        GETATTR_COMPUTED,
        """
        async def f(router, prompt, name):
            return await getattr(router, name)(messages=[{"content": prompt}])
        """,
    ),
    "6-getattr-computed-bound": (
        GETATTR_COMPUTED,
        """
        async def f(router, prompt, name):
            send = getattr(router, name)
            return await send(messages=[{"content": prompt}])
        """,
    ),
    "6-getattr-computed-positional": (
        GETATTR_COMPUTED,
        """
        async def f(router, prompt, name):
            send = getattr(router, name)
            return await send(prompt)
        """,
    ),
}


@pytest.mark.parametrize("kind, source", list(_UNPROVEN.values()), ids=list(_UNPROVEN))
def test_the_classifier_refuses_what_it_cannot_prove_1971(kind, source):
    [problems] = _verdicts(source)
    assert kind in _kinds(problems), problems


_REDACTED = """
async def f(router, prompt, ctx, raw):
    messages = await case_redacted_messages([{"content": prompt}], ctx)
"""


def _then(body: str) -> str:
    """``_REDACTED`` followed by ``body``, as the rest of ``f``."""
    lines = textwrap.dedent(body).strip("\n").splitlines()
    return _REDACTED + "\n".join("    " + line for line in lines) + "\n"


#: #1971 review round: shapes the previous head classified R that send raw
#: text (each executed against a fake router), or that crashed it. The guard
#: refuses anything it cannot model, so each is refused for the kind named.
_FAIL_CLOSED = {
    # a try/with body binding reaches past it only if every handler exits
    "try-redaction-that-may-raise": (
        UNREDACTED,
        """
        async def f(router, prompt, ctx, raw):
            try:
                messages = [{"content": raw}]
                messages = await case_redacted_messages(messages, ctx)
            except Exception:
                pass
            return await router.route(messages=messages)
        """,
    ),
    "try-best-effort-redaction-of-a-parameter": (
        UNREDACTED,
        """
        async def f(router, messages, ctx):
            try:
                messages = await case_redacted_messages(messages, ctx)
            except Exception:
                pass
            return await router.route(messages=messages)
        """,
    ),
    "suppress-best-effort-redaction-of-a-parameter": (
        UNREDACTED,
        """
        async def f(router, messages, ctx):
            with contextlib.suppress(Exception):
                messages = await case_redacted_messages(messages, ctx)
            return await router.route(messages=messages)
        """,
    ),
    # a binding inside or beside the send's own statement
    "semicolon-rebinding-on-the-send-line": (
        UNREDACTED,
        _then("""
            messages = [{"content": raw}]; return await router.route(messages=messages)
            """),
    ),
    "walrus-in-the-receiver": (
        UNMODELED,
        _then("""
            return await (
                router if (messages := [{"content": raw}]) else router
            ).route(messages=messages)
            """),
    ),
    "walrus-in-a-routing-test": (
        UNMODELED,
        _then("""
            return await router.route(
                model="m" if (messages := [{"content": raw}]) else "m",
                messages=messages,
            )
            """),
    ),
    "walrus-in-a-splat-test": (
        UNMODELED,
        _then("""
            return await router.route(
                **({} if (messages := [{"content": raw}]) else {}),
                messages=messages,
            )
            """),
    ),
    "send-inside-a-conditional-that-binds": (
        UNMODELED,
        _then("""
            return await router.route(messages=messages) if (
                messages := [{"content": raw}]
            ) else None
            """),
    ),
    "bind-then-send-in-one-tuple": (
        UNMODELED,
        _then("""
            return (
                messages := [{"content": raw}],
                await router.route(messages=messages),
            )[1]
            """),
    ),
    # lambdas and comprehensions are scopes
    "send-from-a-lambda-whose-parameter-shadows": (
        UNMODELED,
        _then("""
            send = lambda messages: router.route(messages=messages)
            return await send([{"content": raw}])
            """),
    ),
    "send-from-a-lambda-rebound-after": (
        UNMODELED,
        _then("""
            send = lambda: router.route(messages=messages)
            messages = [{"content": raw}]
            return await send()
            """),
    ),
    "send-from-a-comprehension-whose-variable-shadows": (
        UNMODELED,
        _then("""
            return [
                await router.route(messages=messages)
                for messages in [[{"content": raw}]]
            ][0]
            """),
    ),
    "send-from-a-generator-rebound-after": (
        UNMODELED,
        _then("""
            gen = (router.route(messages=messages) for _ in [0])
            messages = [{"content": raw}]
            for coro in gen:
                return await coro
            """),
    ),
    "lambda-default-argument-alias": (
        CHANGED,
        _then("""
            add = lambda m=messages: m.append({"content": raw})
            add()
            return await router.route(messages=messages)
            """),
    ),
    "def-default-argument-alias": (
        CHANGED,
        _then("""
            def add(m=messages):
                m.append({"content": raw})

            add()
            return await router.route(messages=messages)
            """),
    ),
    # mutators and constructs that write or read the value unmodeled
    "list-init-in-place": (
        CHANGED,
        _then("""
            messages.__init__([{"content": raw}])
            return await router.route(messages=messages)
            """),
    ),
    "dict-init-on-an-element": (
        CHANGED,
        _then("""
            messages[0].__init__(content=raw)
            return await router.route(messages=messages)
            """),
    ),
    "setattr-dunder-on-an-element": (
        CHANGED,
        _then("""
            messages[0].__setattr__("content", raw)
            return await router.route(messages=messages)
            """),
    ),
    "dict-keyword-copy-holds-it": (
        CHANGED,
        _then("""
            holder = dict(m=messages)
            holder["m"].append({"content": raw})
            return await router.route(messages=messages)
            """),
    ),
    "enumerate-keyword-argument": (
        CHANGED,
        _then("""
            for i, m in enumerate(iterable=messages):
                m["content"] = raw
            return await router.route(messages=messages)
            """),
    ),
    "comprehension-element-init": (
        CHANGED,
        _then("""
            [m.__init__(content=raw) for m in messages]
            return await router.route(messages=messages)
            """),
    ),
    "redaction-input-shares-a-non-content-key": (
        REDACTION_INPUT,
        """
        async def f(router, prompt, ctx, raw):
            src = [{"content": prompt, "name": []}]
            messages = await case_redacted_messages(src, ctx)
            src[0]["name"].append(raw)
            return await router.route(messages=messages)
        """,
    ),
    "match-capture-aliases-an-element": (
        UNMODELED,
        _then("""
            match messages:
                case [first, *_]:
                    first["content"] = raw
            return await router.route(messages=messages)
            """),
    ),
    "yield-hands-it-to-the-consumer": (
        UNMODELED,
        _then("""
            yield messages
            yield await router.route(messages=messages)
            """),
    ),
    "global-rebound-by-another-function": (
        UNMODELED,
        """
        def _poison(raw):
            global messages
            messages = [{"content": raw}]

        async def f(router, prompt, ctx, raw):
            global messages
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            _poison(raw)
            return await router.route(messages=messages)
        """,
    ),
    "frame-read-through-locals": (
        UNMODELED,
        _then("""
            locals()["messages"].append({"content": raw})
            return await router.route(messages=messages)
            """),
    ),
    "sorted-with-a-side-effecting-key": (
        UNMODELED,
        _then("""
            sorted(messages, key=lambda m: m.update(content=raw) or 0)
            return await router.route(messages=messages)
            """),
    ),
    "type-builds-a-class-holding-it": (
        UNMODELED,
        _then("""
            X = type("X", (), {"m": messages})
            X.m.append({"content": raw})
            return await router.route(messages=messages)
            """),
    ),
    "a-method-the-guard-does-not-model": (
        UNMODELED,
        _then("""
            messages.sort(key=lambda m: m.update(content=raw) or 0)
            return await router.route(messages=messages)
            """),
    ),
    # every binding form counts
    "import-as-the-sent-name": (
        UNREDACTED,
        _then("""
            from evil import payload as messages
            return await router.route(messages=messages)
            """),
    ),
    "except-as-the-sent-name": (
        UNREDACTED,
        _then("""
            try:
                raise ValueError(raw)
            except ValueError as messages:
                return await router.route(messages=messages)
            """),
    ),
    "match-capture-rebinds-the-sent-name": (
        UNREDACTED,
        _then("""
            match [{"content": raw}]:
                case messages:
                    pass
            return await router.route(messages=messages)
            """),
    ),
    "def-rebinds-the-sent-name": (
        UNREDACTED,
        _then("""
            def messages():
                pass

            return await router.route(messages=messages)
            """),
    ),
    "with-as-the-sent-name": (
        UNREDACTED,
        _then("""
            with open(raw) as messages:
                return await router.route(messages=messages)
            """),
    ),
    "with-as-a-routing-name": (
        ROUTING_VALUE,
        _then("""
            with open("m") as model:
                return await router.route(messages=messages, model=model)
            """),
    ),
    "with-as-a-splat-name": (
        SPLAT,
        _then("""
            with open("m") as kw:
                return await router.route(messages=messages, **kw)
            """),
    ),
    "class-attribute-as-a-routing-value": (
        ROUTING_VALUE,
        """
        class Holder:
            content = RAW_FROM_CASE

        async def f(router, prompt, ctx):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages, model=Holder.content)
        """,
    ),
    "routing-dict-reinitialised": (
        SPLAT,
        _then("""
            kw = {"model": "m"}
            kw.__init__(system=raw)
            return await router.route(messages=messages, **kw)
            """),
    ),
    "splat-helper-decorated": (
        SPLAT,
        """
        def _inject(fn):
            return lambda *a: {"system": RAW}

        @_inject
        def _route_kwargs(settings):
            return {}

        async def f(router, prompt, ctx, settings):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages, **_route_kwargs(settings))
        """,
    ),
    "splat-helper-rebound-after-its-def": (
        SPLAT,
        """
        def _route_kwargs(settings):
            return {}

        _route_kwargs = lambda settings: {"system": RAW}

        async def f(router, prompt, ctx, settings):
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages, **_route_kwargs(settings))
        """,
    ),
    # the context fails closed
    "context-from-a-tuple-unpacking": (
        NONE_CONTEXT,
        """
        async def f(router, prompt):
            ctx, = (None,)
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages)
        """,
    ),
    "context-from-a-with-target": (
        NONE_CONTEXT,
        """
        async def f(router, prompt):
            with contextlib.nullcontext() as ctx:
                messages = await case_redacted_messages([{"content": prompt}], ctx)
                return await router.route(messages=messages)
        """,
    ),
    "context-from-a-call": (
        NONE_CONTEXT,
        """
        async def f(router, prompt, ctx):
            ctx = (lambda c=None: c)()
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(messages=messages)
        """,
    ),
    "context-from-an-attribute": (
        NONE_CONTEXT,
        """
        async def f(self, prompt):
            messages = await case_redacted_messages([{"content": prompt}], self._ctx)
            return await self.router.route(messages=messages)
        """,
    ),
    # a lookup by a constant method name is a send site
    "methodcaller-route": (
        GETATTR,
        _then("""
            return await operator.methodcaller("route", messages=[{"content": raw}])(
                router
            )
            """),
    ),
    "getattribute-route": (
        GETATTR,
        _then("""
            return await router.__getattribute__("route")(
                messages=[{"content": raw}]
            )
            """),
    ),
    "class-dict-route": (
        GETATTR,
        _then("""
            return await type(router).__dict__["route"](
                router, messages=[{"content": raw}]
            )
            """),
    ),
    "attrgetter-route": (
        GETATTR,
        _then("""
            return await operator.attrgetter("route")(router)(
                messages=[{"content": raw}]
            )
            """),
    ),
    # second review round: a redaction's input must be plain displays
    "W1-input-built-from-an-earlier-name": (
        REDACTION_INPUT,
        """
        async def f(router, prompt, ctx, raw):
            meta = {"tags": []}
            src = [{"role": "user", "content": prompt, "meta": meta}]
            messages = await case_redacted_messages(src, ctx)
            meta["tags"].append(raw)
            return await router.route(messages=messages)
        """,
    ),
    "W2-message-dict-built-earlier": (
        REDACTION_INPUT,
        """
        async def f(router, prompt, ctx, raw):
            calls = []
            m = {"role": "user", "content": prompt, "tool_calls": calls}
            messages = await case_redacted_messages([m], ctx)
            calls.append(raw)
            return await router.route(messages=messages)
        """,
    ),
    "W3-input-passed-by-keyword": (
        REDACTION_INPUT,
        """
        async def f(router, prompt, ctx, raw):
            meta = []
            messages = await case_redacted_messages(
                messages=[{"role": "user", "content": prompt, "meta": meta}], ctx=ctx
            )
            meta.append(raw)
            return await router.route(messages=messages)
        """,
    ),
    "W4-raw-text-in-a-non-content-key": (
        REDACTION_INPUT,
        """
        async def f(router, prompt, ctx, raw):
            messages = await case_redacted_messages(
                [{"role": "user", "content": prompt, "name": raw}], ctx
            )
            return await router.route(messages=messages)
        """,
    ),
    "W5-module-template-changed-by-a-helper": (
        REDACTION_INPUT,
        """
        _TEMPLATE = [{"role": "system", "content": "x", "meta": {}}]

        def _poison(raw):
            _TEMPLATE[0]["meta"]["note"] = raw

        async def f(router, ctx, raw):
            messages = await case_redacted_messages(_TEMPLATE, ctx)
            _poison(raw)
            return await router.route(messages=messages)
        """,
    ),
    # second review round: frame helpers by any spelling, and reflection
    "W6-closure-cell-of-a-lambda": (
        UNMODELED,
        _then("""
            probe = lambda: len(messages)
            probe.__closure__[0].cell_contents.append({"content": raw})
            return await router.route(messages=messages)
            """),
    ),
    "W7-closure-cell-of-a-def-made-before": (
        UNMODELED,
        """
        async def f(router, prompt, ctx, raw):
            def probe():
                return len(messages)

            messages = await case_redacted_messages([{"content": prompt}], ctx)
            probe.__closure__[0].cell_contents.append({"content": raw})
            return await router.route(messages=messages)
        """,
    ),
    "W8-inspect-stack-frame-locals": (
        UNMODELED,
        _then("""
            inspect.stack()[0].frame.f_locals["messages"].append({"content": raw})
            return await router.route(messages=messages)
            """),
    ),
    "W9-traceback-frame-locals": (
        UNMODELED,
        _then("""
            try:
                raise RuntimeError
            except RuntimeError:
                frame = sys.exc_info()[2].tb_frame
                frame.f_locals["messages"].append({"content": raw})
            return await router.route(messages=messages)
            """),
    ),
    "W10-builtins-exec": (
        UNMODELED,
        _then("""
            builtins.exec("messages.append({'content': raw})")
            return await router.route(messages=messages)
            """),
    ),
    "W11-ctypes-through-id": (
        UNMODELED,
        _then("""
            ctypes.cast(id(messages), ctypes.py_object).value.append({"content": raw})
            return await router.route(messages=messages)
            """),
    ),
    "W12-gc-objects": (
        UNMODELED,
        _then("""
            [o for o in gc.get_objects() if o is messages][0].append({"content": raw})
            return await router.route(messages=messages)
            """),
    ),
    "W13-coroutine-frame": (
        UNMODELED,
        _then("""
            coro = asyncio.current_task().get_coro()
            coro.cr_frame.f_locals["messages"].append({"content": raw})
            return await router.route(messages=messages)
            """),
    ),
    # a walrus in a conditional's test is a binding like any other
    "Q5-test-rebinds-a-routing-name": (
        ROUTING_VALUE,
        _then("""
            model = "m"
            return await router.route(
                max_tokens=5 if (model := raw) else 5,
                model=model,
                messages=messages,
            )
            """),
    ),
    "Q5-test-rebinds-a-splat-name": (
        SPLAT,
        _then("""
            kw = {}
            return await router.route(
                max_tokens=5 if (kw := {"system": raw}) else 5,
                messages=messages,
                **kw,
            )
            """),
    ),
    "Q5-test-makes-an-alias": (
        CHANGED,
        _then("""
            cap = 5 if (alias := messages) else 5
            alias.append({"content": raw})
            return await router.route(messages=messages, max_tokens=cap)
            """),
    ),
    "Q5-test-rebinds-the-context": (
        NONE_CONTEXT,
        """
        async def f(router, prompt, ctx):
            messages = await case_redacted_messages(
                [{"content": prompt}], ctx if (ctx := None) is None else ctx
            )
            return await router.route(messages=messages)
        """,
    ),
}


@pytest.mark.parametrize(
    "kind, source", list(_FAIL_CLOSED.values()), ids=list(_FAIL_CLOSED)
)
def test_the_classifier_fails_closed_1971(kind, source):
    verdicts = _verdicts(source)
    assert verdicts
    assert all(problems for problems in verdicts), verdicts
    assert kind in set().union(*map(_kinds, verdicts)), verdicts


@pytest.mark.parametrize(
    "source",
    [
        # a handler that re-raises: the body's binding reaches what follows
        """
        async def f(router, prompt, ctx):
            try:
                messages = await case_redacted_messages([{"content": prompt}], ctx)
            except Exception:
                raise
            return await router.route(messages=messages)
        """,
        # the send inside the try body, after the redaction (the real sites)
        """
        async def f(router, prompt, ctx):
            try:
                messages = await case_redacted_messages([{"content": prompt}], ctx)
                return await router.route(messages=messages)
            except Exception:
                return None
        """,
        # a conditional splat whose test is a routing-only flag
        """
        def _role_route_kwargs(settings, role):
            override = settings.llm.explicit_role_provider(role)
            return {"provider_override": override} if override else {}

        async def f(router, prompt, model, pinned, *, ctx):
            from faultmaven.config.settings import get_settings

            settings = get_settings()
            messages = await case_redacted_messages([{"content": prompt}], ctx)
            return await router.route(
                messages=messages,
                model=model,
                **(_role_route_kwargs(settings, "synthesis") if pinned else {}),
            )
        """,
    ],
)
def test_the_classifier_accepts_what_it_models_1971(source):
    assert _verdicts(source) == [()]


def test_a_route_key_read_is_not_a_lookup_1971():
    """``scope.get("route")`` reads a dict, not an attribute by name."""
    assert _verdicts("""
        def f(request):
            return request.scope.get("route")
        """) == []


_CHECK = ("faultmaven/api/routes/admin_config.py", "check_llm_connection")
_LLMOP = (
    "faultmaven/core/investigation/milestone_engine/generation.py",
    "llm_operation",
)
_GENERATE = ("faultmaven/infrastructure/llm/router.py", "generate")
_CONVERT = (
    "faultmaven/modules/knowledge/domain/services/conversion_service/service.py",
    "_convert",
)
_LOG = ("faultmaven/infrastructure/logging/coordinator.py", "log_once")
_ROUTES = ("faultmaven/api/route_enumeration.py", "iter_served_routes")

#: #1971 rule 7: an entry admits the exact refusals it pins, per site — so a
#: second defect fails, of another kind or the same.
_ADMITTED_FOR_ITS_KIND = {
    "own-shape": (
        _CHECK,
        """
        async def check_llm_connection(provider):
            return await provider.generate(prompt="Say hello", max_tokens=50)
        """,
        True,
    ),
    "other-kind": (
        _CHECK,
        """
        async def check_llm_connection(provider, raw):
            return await provider.generate(prompt="Say hello", system=raw)
        """,
        False,
    ),
    "same-kind-other-expression": (
        _CHECK,
        """
        async def check_llm_connection(provider, case):
            return await provider.generate(
                prompt=f"Say hello {case.description}", max_tokens=50
            )
        """,
        False,
    ),
    "same-kind-second-splat": (
        _LLMOP,
        """
        async def llm_operation(provider, prompt, raw):
            generate_params = {"prompt": prompt, "max_tokens": 10}
            return await provider.generate(**generate_params, **{"messages": raw})
        """,
        False,
    ),
    "same-kind-another-channel": (
        _GENERATE,
        """
        async def generate(self, prompt, raw, **kwargs):
            return await self.route(prompt=prompt, messages=None, system=raw)
        """,
        False,
    ),
    "same-kind-other-binding": (
        _CONVERT,
        """
        async def _convert(self, prepared, failure_mode):
            knowledge_model = prepared.knowledge_model
            messages = [{"role": "user", "content": failure_mode.description}]
            return await self._llm_router.route(
                messages=messages,
                model=knowledge_model,
                **_knowledge_route_kwargs(self._settings),
            )
        """,
        False,
    ),
    "same-kind-llm-send-in-a-logger": (
        _LOG,
        """
        def log_once(router, method, raw):
            return getattr(router, method)(messages=[{"content": raw}])
        """,
        False,
    ),
    "same-kind-llm-send-in-starlette": (
        _ROUTES,
        """
        async def iter_served_routes(router, raw):
            return await getattr(router, "route")(messages=[{"content": raw}])
        """,
        False,
    ),
}


_TOOL = (
    "faultmaven/core/investigation/milestone_engine/generation.py",
    "_tool_loop_call",
)
_REGISTRY = ("faultmaven/infrastructure/llm/providers/registry.py", "route_request")


def _real_rows_with(rel: str, where: str, line: str) -> list:
    """The sites of the real function ``where`` in ``rel``, classified after
    ``line`` is inserted just before the statement of its send."""
    path = _ROOT.parent / rel
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    parents = _parents(tree)
    site = next(
        s
        for s in _send_sites(tree)
        if [f.name for f in _scopes(s, parents)][:1] == [where]
    )
    stmt = _stmt(site, parents)
    lines = source.splitlines(keepends=True)
    lines.insert(stmt.lineno - 1, " " * stmt.col_offset + line + "\n")
    tree = ast.parse("".join(lines))
    parents = _parents(tree)
    return [
        (rel, where, _line(s), classify(s, parents))
        for s in _send_sites(tree)
        if [f.name for f in _scopes(s, parents)][:1] == [where]
    ]


#: #1971 second review round: one line added to a real allowlisted function.
#: Its pins name every defect at the site, and count its sites, so each fails.
_REAL_ADDITIONS = {
    "llm_operation-unchanged": (_LLMOP, "pass", True),
    "llm_operation-prompt-rebound": (
        _LLMOP,
        'generate_params["prompt"] = raw_prompt',
        False,
    ),
    "llm_operation-system-key": (
        _LLMOP,
        'generate_params["system"] = raw_prompt',
        False,
    ),
    "llm_operation-update-messages": (
        _LLMOP,
        'generate_params.update(messages=[{"content": raw_prompt}])',
        False,
    ),
    "tool_loop_call-unchanged": (_TOOL, "pass", True),
    "tool_loop_call-messages-replaced": (
        _TOOL,
        'call_kwargs["messages"] = [{"content": raw}]',
        False,
    ),
    "tool_loop_call-system-key": (_TOOL, 'call_kwargs["system"] = raw', False),
    "route_request-unchanged": (_REGISTRY, "pass", True),
    "route_request-system-key": (_REGISTRY, 'kwargs["system"] = raw', False),
    "check_llm_connection-unchanged": (_CHECK, "pass", True),
    "check_llm_connection-a-second-site": (
        _CHECK,
        'await provider.generate(prompt="Say hello", max_tokens=50)',
        False,
    ),
}


@pytest.mark.parametrize(
    "entry, line, admitted", list(_REAL_ADDITIONS.values()), ids=list(_REAL_ADDITIONS)
)
def test_a_defect_added_to_an_allowlisted_function_fails_1971(entry, line, admitted):
    rows = _real_rows_with(*entry, line)
    assert rows
    assert (_unadmitted(rows) == []) is admitted


@pytest.mark.parametrize(
    "entry, source, admitted",
    list(_ADMITTED_FOR_ITS_KIND.values()),
    ids=list(_ADMITTED_FOR_ITS_KIND),
)
def test_an_allowlist_entry_admits_only_its_pinned_refusals_1971(
    entry, source, admitted
):
    rows = [(*entry, 1, problems) for problems in _verdicts(source)]
    assert rows
    assert (_unadmitted(rows) == []) is admitted
