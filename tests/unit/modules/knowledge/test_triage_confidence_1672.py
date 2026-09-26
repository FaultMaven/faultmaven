"""fm#1672: document triage reads its confidence with #1502's rule.

The triage prompt asks the classifier for ``"confidence": 0.0-1.0``, and a
"not actionable" verdict above 0.8 is a hard reject (``NOT_ACTIONABLE``). On
``main`` the parse was ``float(result.get("confidence", 0.5))`` with no bound,
so a model answering ``50`` meaning 50% hard-rejected the document, as did
``150`` and ``Infinity``; ``NaN`` took neither the reject nor the warning, and a
non-number lost the verdict entirely.

The parse now goes through ``confidence_repair.classify``, the one
implementation of the rule: ``(1, 100]`` is rescaled, a ``bool`` coerced, and
anything else treated as absent, which takes the default and so the advisory
path. Every test here drives the real ``preprocess`` with a stubbed model
answer, except the two that pin the rule's single implementation and the
model's bound.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from faultmaven.core.investigation import reliability_metrics
from faultmaven.core.investigation.confidence_repair import classify, short_repr
from faultmaven.infrastructure.llm.providers import LLMResponse
from faultmaven.modules.knowledge.domain.models.conversion import (
    ConversionErrorCode,
    TriageResult,
)
from faultmaven.modules.knowledge.domain.services import document_preprocessor
from faultmaven.modules.knowledge.domain.services.document_preprocessor import (
    TRIAGE_DEFAULT_CONFIDENCE,
    DocumentPreprocessor,
)

pytestmark = pytest.mark.unit

# Technical enough to pass the heuristic gate, long enough for the length gate,
# and not a runbook — so the pipeline reaches Stage 6.
_DOCUMENT = (
    "# Payments service notes\n\n"
    + (
        "The payments API returns an error when the upstream times out. "
        "Operators run kubectl rollout restart deployment/payments to recover. "
    )
    * 4
)

_SOFT_WARNING = "may not contain sufficient troubleshooting content"

# What each action's log line says, so a line that reports the wrong action in
# its text — or falls through to another action's wording — is caught.
_NOTE = {
    "rescaled": "read as a percentage",
    "coerced": "coerced to",
    "defaulted": f"treated as absent, default {TRIAGE_DEFAULT_CONFIDENCE:.4g} applies",
}


class _ClassifierStub:
    """Answers every triage call with one fixed body, and counts the calls."""

    def __init__(self, body: str) -> None:
        self.body = body
        self.calls = 0

    async def route(self, **kwargs) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            content=self.body,
            confidence=1.0,
            provider="stub",
            model=kwargs["model"],
            tokens_used=1,
            response_time_ms=1,
        )


_SETTINGS = SimpleNamespace(
    llm=SimpleNamespace(
        get_classifier_model=lambda: "classifier",
        explicit_role_provider=lambda role: None,
    )
)


async def _triage(tmp_path: Path, confidence_json: str | None):
    """Run ``preprocess`` with the classifier saying "not actionable".

    ``confidence_json`` is spliced into the body verbatim, so ``NaN`` and
    ``Infinity`` arrive as the JSON tokens a model would write; ``None`` omits
    the key.
    """
    fields = ['"is_actionable": false', '"reason": "stub"']
    if confidence_json is not None:
        fields.insert(1, f'"confidence": {confidence_json}')
    stub = _ClassifierStub("{" + ", ".join(fields) + "}")
    path = tmp_path / "doc.md"
    path.write_text(_DOCUMENT)
    with patch.object(reliability_metrics, "schema_field_repairs_total") as counter:
        result = await DocumentPreprocessor(stub, _SETTINGS).preprocess(
            path, "text/markdown"
        )
    assert stub.calls == 1, "the triage stage never ran"
    return result, counter


HARD, SOFT = "hard_reject", "soft_warning"


def _path_taken(result) -> str:
    if result.is_rejected:
        assert result.error_code == ConversionErrorCode.NOT_ACTIONABLE
        return HARD
    assert any(_SOFT_WARNING in w for w in result.warnings), result.warnings
    return SOFT


@pytest.mark.parametrize(
    "answer, path, value, action",
    [
        # The issue's four, plus a non-number.
        pytest.param("50", SOFT, 0.5, "rescaled", id="50-percent"),
        # #1502's ruling reads ``true`` as full confidence ("at least as much
        # information as an out-of-range number"), so "not actionable" with
        # ``true`` still rejects, as on main. What changed is how: a counted
        # coercion rather than Python's bool-is-int. A ruling that a bool must
        # never reject would treat the coerced arm as absent at this one site.
        pytest.param("true", HARD, 1.0, "coerced", id="true"),
        pytest.param("-1", SOFT, TRIAGE_DEFAULT_CONFIDENCE, "defaulted", id="-1"),
        pytest.param("NaN", SOFT, TRIAGE_DEFAULT_CONFIDENCE, "defaulted", id="NaN"),
        pytest.param('"high"', SOFT, TRIAGE_DEFAULT_CONFIDENCE, "defaulted", id="high"),
        # Positive control: a genuine hard reject stays one, and is not counted.
        pytest.param("0.95", HARD, 0.95, None, id="0.95-control"),
        # Where a wrong arm would change the path, not only the value. A
        # percentage that means "confident" still rejects — defaulting it would
        # warn; a bool false coerces to 0.0 rather than the default; above 100 and
        # infinity default rather than clamp, since a clamp to 1.0 would reject.
        pytest.param("90", HARD, 0.9, "rescaled", id="90-percent"),
        pytest.param('"50"', SOFT, 0.5, "rescaled", id="numeric-string"),
        pytest.param("false", SOFT, 0.0, "coerced", id="false"),
        pytest.param("150", SOFT, TRIAGE_DEFAULT_CONFIDENCE, "defaulted", id="150"),
        pytest.param(
            "Infinity", SOFT, TRIAGE_DEFAULT_CONFIDENCE, "defaulted", id="Infinity"
        ),
        # Absence the model chose is the default, and not a repair.
        pytest.param("null", SOFT, TRIAGE_DEFAULT_CONFIDENCE, None, id="null"),
        pytest.param(None, SOFT, TRIAGE_DEFAULT_CONFIDENCE, None, id="missing"),
        pytest.param("0.8", SOFT, 0.8, None, id="threshold-is-soft"),
    ],
)
async def test_triage_takes_the_path_the_repaired_confidence_decides(
    tmp_path, caplog, answer, path, value, action
):
    caplog.set_level(logging.WARNING, logger=document_preprocessor.__name__)

    result, counter = await _triage(tmp_path, answer)

    assert _path_taken(result) == path
    assert result.triage_result is not None
    assert result.triage_result.confidence == pytest.approx(value)

    repaired = [r for r in caplog.records if getattr(r, "action", None) is not None]
    if action is None:
        counter.labels.assert_not_called()
        assert repaired == []
    else:
        counter.labels.assert_called_once_with(
            schema="TriageResult", field="confidence", action=action
        )
        counter.labels.return_value.inc.assert_called_once_with()
        assert [(r.action, r.value) for r in repaired] == [
            (action, pytest.approx(value))
        ]
        # The raw value is quoted, not only the outcome.
        assert repaired[0].raw == short_repr(json.loads(answer))
        assert _NOTE[action] in repaired[0].getMessage()


@pytest.mark.parametrize(
    "raw",
    [
        0,
        1,
        0.42,
        " 0.5 ",
        1.5,
        90,
        "95",
        100,
        True,
        False,
        100.5,
        -0.1,
        float("nan"),
        float("inf"),
        float("-inf"),
        "high",
        "nan",
        [0.9],
        {"v": 1},
        10**400,
        "1e400",
    ],
)
def test_the_triage_follows_the_one_implementation_of_the_rule(raw):
    """The triage must not grow its own copy of the rule: whatever ``classify``
    says, the triage reads — a recoverable value as recovered, anything else as
    the default. If ``classify`` is amended, this follows it; a local copy would
    not."""
    kind, value = classify(raw)
    expected = TRIAGE_DEFAULT_CONFIDENCE if kind == "unrepairable" else value
    with patch.object(reliability_metrics, "schema_field_repairs_total"):
        assert document_preprocessor._triage_confidence(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", [50, -1, float("nan"), float("inf")])
def test_the_triage_result_itself_is_bounded(raw):
    """A construction site that skips the repair cannot carry an out-of-range
    value to the threshold: it fails validation, and the triage stage's handler
    turns that into no verdict — never a hard reject."""
    with pytest.raises(ValidationError):
        TriageResult(is_actionable=False, confidence=raw, reason="")
