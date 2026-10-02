"""Tests for scripts/anthropic_thinking_controls.py (#1800).

The script spends money, so nothing here may reach the network: every test
runs with ``_send`` replaced by a function that fails if it is ever called.
"""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "anthropic_thinking_controls",
    REPO_ROOT / "scripts" / "anthropic_thinking_controls.py",
)
atc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(atc)

MODELS = ("claude-opus-5", "claude-opus-5-5", "claude-fable-5-1")


def _body(model="claude-opus-5", **extra):
    return {"model": model, "max_tokens": 8000, "messages": [], **extra}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("_send reached: a test would have made a live call")

    monkeypatch.setattr(atc, "_send", _boom)


@pytest.mark.unit
class TestBuildVariant:
    def test_omitted_strips_thinking_and_effort_keeps_other_output_config(self):
        body = _body(
            thinking={"type": "adaptive"},
            output_config={"effort": "high", "format": "x"},
        )
        out = atc.build_variant(body, "claude-opus-5", "omitted")
        assert "thinking" not in out
        assert out["output_config"] == {"format": "x"}

    def test_omitted_drops_empty_output_config(self):
        out = atc.build_variant(
            _body(output_config={"effort": "high"}), "claude-opus-5", "omitted"
        )
        assert "output_config" not in out

    def test_effort_low(self):
        out = atc.build_variant(_body(), "claude-opus-5", "effort_low")
        assert out["output_config"]["effort"] == "low"
        assert "thinking" not in out

    def test_disabled(self):
        out = atc.build_variant(_body(), "claude-opus-5", "disabled")
        assert out["thinking"] == {"type": "disabled"}
        assert "effort" not in out.get("output_config", {})

    @pytest.mark.parametrize("variant", atc.VARIANTS)
    def test_input_not_mutated(self, variant):
        body = _body(thinking={"type": "adaptive"}, output_config={"effort": "high"})
        before = copy.deepcopy(body)
        atc.build_variant(body, "claude-opus-5", variant)
        assert body == before

    def test_wrong_model_raises(self):
        with pytest.raises(ValueError):
            atc.build_variant(_body("claude-opus-5-5"), "claude-opus-5", "omitted")

    def test_unknown_variant_raises(self):
        with pytest.raises(ValueError):
            atc.build_variant(_body(), "claude-opus-5", "high")


@pytest.mark.unit
class TestSummarizeResponse:
    def test_200_with_thinking(self):
        payload = {
            "stop_reason": "tool_use",
            "content": [
                {"type": "thinking"},
                {"type": "text"},
                {"type": "tool_use", "name": "InvestigationResponse_Diagnosis"},
            ],
            "usage": {
                "input_tokens": 6100,
                "output_tokens": 5397,
                "output_tokens_details": {"thinking_tokens": 1206},
                "cache_creation_input_tokens": 32000,
                "cache_read_input_tokens": 0,
            },
        }
        row = atc.summarize_response(200, payload, 49.04)
        assert row["visible_tokens"] == 4191
        assert row["thinking_tokens"] == 1206
        assert row["cache_creation_input_tokens"] == 32000
        assert row["cache_read_input_tokens"] == 0
        assert row["blocks"] == [
            "thinking",
            "text",
            "tool_use:InvestigationResponse_Diagnosis",
        ]
        assert row["elapsed_s"] == 49.0

    def test_no_thinking_tokens_gives_none_visible(self):
        row = atc.summarize_response(200, {"usage": {"output_tokens": 10}}, 1.0)
        assert row["visible_tokens"] is None

    def test_400_gives_error_message(self):
        row = atc.summarize_response(400, {"error": {"message": "nope"}}, 0.5)
        assert row["error"] == "nope"
        assert "output_tokens" not in row


@pytest.mark.unit
class TestPlan:
    def test_approved_plan_is_the_2026_09_30_ruling(self):
        assert atc.APPROVED_PLAN == (
            ("claude-opus-5", "omitted"),
            ("claude-opus-5", "effort_low"),
            ("claude-opus-5", "disabled"),
            ("claude-opus-5-5", "omitted"),
            ("claude-opus-5-5", "effort_low"),
            ("claude-fable-5-1", "omitted"),
            ("claude-fable-5-1", "effort_low"),
        )

    def test_plan_requests_returns_the_seven_in_order(self):
        calls = atc.plan_requests({m: _body(m) for m in MODELS})
        assert [(m, v) for m, v, _ in calls] == list(atc.APPROVED_PLAN)
        assert all(body["model"] == m for m, _, body in calls)

    def test_missing_model_raises(self):
        with pytest.raises(ValueError):
            atc.plan_requests({m: _body(m) for m in MODELS[:2]})


@pytest.mark.unit
class TestMain:
    def _write(self, tmp_path):
        args = []
        for m in MODELS:
            p = tmp_path / f"{m}.json"
            p.write_text(json.dumps({"body": _body(m)}))
            args += ["--request", f"{m}={p}"]
        return args

    def test_dry_run_prints_seven_and_sends_nothing(
        self, tmp_path, capsys, monkeypatch
    ):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
        rc = atc.main(
            self._write(tmp_path) + ["--out", str(tmp_path / "o.jsonl"), "--dry-run"]
        )
        assert rc == 0
        assert len(capsys.readouterr().out.strip().splitlines()) == 7
        assert not (tmp_path / "o.jsonl").exists()

    def test_no_key_exits_2_and_sends_nothing(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        rc = atc.main(self._write(tmp_path) + ["--out", str(tmp_path / "o.jsonl")])
        assert rc == 2
