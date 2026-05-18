"""Real-LLM integration tests.

These tests hit a live LLM provider API and are opt-in via the `real_llm`
pytest marker plus the appropriate API key env var. See
``docs/development/testing/standards.md`` for the discipline and
``conftest.py`` for the provider fixture.

Cost: each test makes 1-3 LLM API calls. Default model is Haiku
(cheapest current Anthropic model); override via ``REAL_LLM_TEST_MODEL``.
Expected per-test cost: <$0.01.

Run:
    pytest -m real_llm  # all real-LLM tests
    pytest tests/integration/real_llm/test_inv01_handshake_recovery.py -m real_llm
"""
