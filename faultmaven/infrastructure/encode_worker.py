"""A KB indexing encode, a phased job of ``worker_process`` run as ``python -m``
by the KB indexing slot (``index_slot``, Principle 14, #2851).

BGE-M3 loads inside the work's bound, through ``ModelCache``, and encodes the
texts at ``ENCODE_BATCH_SIZE``; the answer is the vectors as little-endian
float32, base64, one raw part. The parent's thread count ``threads`` is pinned
three times: the BLAS/OpenMP knobs and the Rust tokenizer's pool before the
imports read them, and torch's own count again after the load, which
``configure_inference_threads`` sets to the node's cores when the container
has no CPU quota.
"""

import base64
import os
import sys
from typing import Any, Callable, Dict, List

from faultmaven.infrastructure import worker_process

#: The knobs native pools read once, at their start.
THREAD_KNOBS = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "RAYON_NUM_THREADS",
)


def _encode(request: Dict[str, Any], send) -> Callable[[], str]:
    threads = int(request["threads"])
    for knob in THREAD_KNOBS:
        os.environ[knob] = str(threads)
    texts: List[str] = request["parts"]

    def work() -> str:
        import numpy as np
        import torch

        from faultmaven.infrastructure.model_cache import ENCODE_BATCH_SIZE, ModelCache

        model = ModelCache().get_bge_m3_model("index-worker")
        if model is None:
            raise RuntimeError("BGE-M3 would not load")
        torch.set_num_threads(threads)
        vectors = model.encode(texts, batch_size=ENCODE_BATCH_SIZE)
        raw = np.ascontiguousarray(vectors, dtype="<f4").tobytes()
        return base64.b64encode(raw).decode("ascii")

    return work


if __name__ == "__main__":
    sys.exit(worker_process.serve(_encode, "KB encode", result_parts=lambda v: [v]))
