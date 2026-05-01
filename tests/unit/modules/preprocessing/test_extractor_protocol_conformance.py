"""Runtime Protocol-conformance tests for all Tier 1 extractors.

The :class:`Extractor` protocol (`extractors/protocol.py`) is declared
``@runtime_checkable``, so ``isinstance(x, Extractor)`` returns True only when
``x`` exposes ``strategy_name``, ``llm_calls_used``, and ``extract(content)``.

Without this test, an extractor can drift out of conformance silently:
- annotating ``extract`` as ``-> str`` while returning ``ExtractResult``
- adding a second positional parameter that the dispatch never passes
- removing a Protocol-required attribute

A code review on 2026-05-01 surfaced 6 of 11 extractors with the wrong
return-type annotation and one (``VisualEvidenceExtractor``) with an
unreachable second parameter. The fix is in
``fix/data-pipeline-review-mediums-lows``; this test pins the contract so
the 7th regression doesn't slip in.
"""

from __future__ import annotations

import inspect

import pytest

from faultmaven.modules.preprocessing.extractors.command_output_extractor import (
    CommandOutputExtractor,
)
from faultmaven.modules.preprocessing.extractors.config_extractor import (
    StructuredConfigExtractor,
)
from faultmaven.modules.preprocessing.extractors.documentation_extractor import (
    DocumentationExtractor,
)
from faultmaven.modules.preprocessing.extractors.error_report_extractor import (
    ErrorReportExtractor,
)
from faultmaven.modules.preprocessing.extractors.logs_extractor import (
    LogsAndErrorsExtractor,
)
from faultmaven.modules.preprocessing.extractors.metrics_extractor import (
    MetricsAndPerformanceExtractor,
)
from faultmaven.modules.preprocessing.extractors.profiling_extractor import (
    ProfilingDataExtractor,
)
from faultmaven.modules.preprocessing.extractors.protocol import (
    ExtractResult,
    Extractor,
)
from faultmaven.modules.preprocessing.extractors.source_code_extractor import (
    SourceCodeExtractor,
)
from faultmaven.modules.preprocessing.extractors.text_extractor import (
    UnstructuredTextExtractor,
)
from faultmaven.modules.preprocessing.extractors.trace_extractor import (
    TraceDataExtractor,
)
from faultmaven.modules.preprocessing.extractors.visual_extractor import (
    VisualEvidenceExtractor,
)

# Tuple of (label, extractor instance) for every Tier 1 extractor in the
# system. Add new extractors here when they land — the conformance check
# will then enforce the contract on them too.
_ALL_EXTRACTORS = [
    ("command_output", CommandOutputExtractor()),
    ("config", StructuredConfigExtractor()),
    ("documentation", DocumentationExtractor()),
    ("error_report", ErrorReportExtractor()),
    ("logs", LogsAndErrorsExtractor()),
    ("metrics", MetricsAndPerformanceExtractor()),
    ("profiling", ProfilingDataExtractor()),
    ("source_code", SourceCodeExtractor()),
    ("text", UnstructuredTextExtractor()),
    ("trace", TraceDataExtractor()),
    ("visual", VisualEvidenceExtractor()),
]


@pytest.mark.unit
@pytest.mark.parametrize(
    "label, extractor",
    _ALL_EXTRACTORS,
    ids=[label for label, _ in _ALL_EXTRACTORS],
)
class TestExtractorProtocolConformance:
    """Pin the Extractor Protocol contract on every registered extractor."""

    def test_isinstance_extractor(self, label: str, extractor) -> None:
        """Each extractor must satisfy the runtime-checkable Protocol."""
        assert isinstance(
            extractor, Extractor
        ), f"{label} extractor does not satisfy Extractor protocol"

    def test_strategy_name_is_str(self, label: str, extractor) -> None:
        """strategy_name returns a non-empty string."""
        name = extractor.strategy_name
        assert isinstance(name, str), f"{label}.strategy_name is not str"
        assert name, f"{label}.strategy_name is empty"

    def test_llm_calls_used_is_int(self, label: str, extractor) -> None:
        """llm_calls_used returns a non-negative int."""
        n = extractor.llm_calls_used
        assert isinstance(n, int), f"{label}.llm_calls_used is not int"
        assert n >= 0, f"{label}.llm_calls_used is negative: {n}"

    def test_extract_signature_matches_protocol(self, label: str, extractor) -> None:
        """extract takes exactly one positional content argument.

        The dispatch in preprocessing_service._run_single_dispatch only
        passes content; any additional required-or-default parameter is
        unreachable at runtime and indicates the extractor drifted.
        """
        sig = inspect.signature(extractor.extract)
        # Exclude `self` (it's bound on the instance, so won't be in params)
        params = list(sig.parameters.values())
        assert len(params) == 1, (
            f"{label}.extract has {len(params)} params, expected 1 (content); "
            f"got: {[p.name for p in params]}"
        )
        assert (
            params[0].name == "content"
        ), f"{label}.extract first param is '{params[0].name}', expected 'content'"

    def test_extract_return_annotation(self, label: str, extractor) -> None:
        """extract is annotated as returning ExtractResult.

        Catches the drift that motivated this test in the first place
        (6 of 11 extractors had `-> str` while returning ExtractResult).
        """
        sig = inspect.signature(extractor.extract)
        ann = sig.return_annotation
        assert (
            ann is ExtractResult
        ), f"{label}.extract return annotation is {ann!r}, expected ExtractResult"

    def test_extract_returns_extract_result_on_empty(
        self, label: str, extractor
    ) -> None:
        """Round-trip: extract on empty/whitespace input returns ExtractResult.

        Empty input is the cheapest universal probe — any extractor that
        secretly returns a string instead of ExtractResult fails here.
        """
        result = extractor.extract("")
        assert isinstance(
            result, ExtractResult
        ), f"{label}.extract returned {type(result).__name__}, expected ExtractResult"
