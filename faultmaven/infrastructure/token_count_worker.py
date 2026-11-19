"""BGE-M3's token count, a phased job of ``worker_process`` run as ``python -m``
by ``embedding_guard.count_index_tokens`` (Principle 14, #2851).

The Rust ``tokenizers`` library over the snapshot's ``tokenizer.json``, never
torch: special tokens on, each text truncated at the model's sequence limit,
summed. Its counts equal ``AutoTokenizer``'s on stripped text and exceed them
by at most 2 a text on edge whitespace (a BOM, a zero-width character), so the
sum is an upper bound. The snapshot is read with ``local_files_only``: never
the network. The thread pool is pinned before ``tokenizers`` is imported: it
reads ``RAYON_NUM_THREADS`` once, and unpinned it starts a thread per visible
core, which the address-space cap cannot hold on a large node.
"""

import os
import sys
from typing import Any, Callable, Dict, List

from faultmaven.infrastructure import worker_process

#: Texts per ``encode_batch``.
COUNT_BATCH = 64


def _count(request: Dict[str, Any], send) -> Callable[[], str]:
    os.environ["RAYON_NUM_THREADS"] = str(request["threads"])
    texts: List[str] = request["parts"]

    def work() -> str:
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer

        from faultmaven.infrastructure.model_cache import (
            BGE_M3_MAX_SEQ_LENGTH,
            BGE_M3_MODEL_ID,
        )

        tokenizer = Tokenizer.from_file(
            hf_hub_download(BGE_M3_MODEL_ID, "tokenizer.json", local_files_only=True)
        )
        tokenizer.enable_truncation(max_length=BGE_M3_MAX_SEQ_LENGTH)
        total = 0
        for at in range(0, len(texts), COUNT_BATCH):
            batch = tokenizer.encode_batch(
                texts[at : at + COUNT_BATCH], add_special_tokens=True
            )
            total += sum(len(encoded.ids) for encoded in batch)
        return str(total)

    return work


if __name__ == "__main__":
    sys.exit(worker_process.serve(_count, "token count", result_parts=lambda n: [n]))
