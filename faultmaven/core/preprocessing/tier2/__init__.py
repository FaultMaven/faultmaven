"""
Interpreted search ("deep analysis").

On-demand interpretation of raw data, invoked by the investigation
agent's ``deep_analysis`` tool — not during upload preprocessing. The
package name ``tier2`` refers to the agent-tools tier system: Tier 2 is
keyword/regex search (``search_file``), Tier 3 is interpreted analysis.

One implementation, ``local_service.LocalTier2Service``, which reaches the
model through the LLM router. ``DEEP_ANALYSIS_BACKEND`` is ``local`` (the
default) or ``disabled``, in which case the agent has no ``deep_analysis``
tool and works from the Tier 1 structural index and ``search_file``.

Design Reference:
    docs/architecture/data-processing/data-preprocessing-design-specification.md §4
"""
