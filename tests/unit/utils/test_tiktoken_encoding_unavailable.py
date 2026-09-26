"""Counting keeps working when tiktoken's encoding cannot be loaded.

tiktoken downloads ``cl100k_base`` on first use. The Docker image bakes it in,
but a process run outside the image with no network gets no encoder, and
``utils.token_estimation``'s loader, which every count shares, returns None.
Every count then falls back to four characters a token. The knowledge base's
document preprocessor used to call tiktoken directly with no fallback, so there
it raised instead, and every document upload failed.
"""

from __future__ import annotations

import pytest

from faultmaven.core.preprocessing import vector_storage
from faultmaven.modules.knowledge.domain.services import document_preprocessor

pytestmark = pytest.mark.unit


@pytest.fixture
def no_encoder(monkeypatch):
    for module in (vector_storage, document_preprocessor):
        monkeypatch.setattr(module, "_get_tiktoken_encoder", lambda model="gpt-4": None)


def test_the_knowledge_base_counts_instead_of_failing_the_upload(no_encoder):
    assert document_preprocessor.count_tokens("x" * 400) == 100


def test_vector_storage_counts_and_slices_by_characters(no_encoder):
    assert vector_storage._estimate_tokens("x" * 400) == 100
    assert vector_storage._get_last_n_tokens("a" * 10 + "b" * 40, 10) == "b" * 40


def test_with_the_encoder_both_count_real_tokens():
    """Control: the fallback above is the degraded path, not the only one."""
    text = "支付服务在部署后开始返回错误" * 10
    assert document_preprocessor.count_tokens(text) != len(text) // 4
    assert vector_storage._estimate_tokens(text) == document_preprocessor.count_tokens(
        text
    )
