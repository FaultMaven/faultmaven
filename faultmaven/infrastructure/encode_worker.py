"""A KB indexing encode, a phased job of ``worker_process`` run as ``python -m``
by the KB indexing slot (``index_slot``, Principle 14, #2851).

BGE-M3 loads inside the work's bound, through ``ModelCache``, and encodes the
texts at ``ENCODE_BATCH_SIZE``; the answer is the vectors as little-endian
float32, base64, one raw part. The parent's thread count ``threads`` is pinned
before the load: the BLAS/OpenMP knobs and the Rust tokenizer's pool before
the imports read them, and ``model_cache.THREAD_COUNT``, which the load's
``configure_inference_threads`` pins torch to (with no CPU quota it would
otherwise build a pool of every core, #2851).
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

#: The failure text of a worker whose model would not load, which the KB slot
#: reads as ``index_slot.ModelNotLoaded`` (#2461).
MODEL_NOT_LOADED = "BGE-M3 would not load"


def _encode(request: Dict[str, Any], send) -> Callable[[], str]:
    threads = int(request["threads"])
    for knob in THREAD_KNOBS:
        os.environ[knob] = str(threads)
    texts: List[str] = request["parts"]

    def work() -> str:
        import numpy as np

        from faultmaven.infrastructure import model_cache

        model_cache.THREAD_COUNT = threads
        model = model_cache.ModelCache().get_bge_m3_model("index-worker")
        if model is None:
            raise RuntimeError(MODEL_NOT_LOADED)
        vectors = model.encode(texts, batch_size=model_cache.ENCODE_BATCH_SIZE)
        raw = np.ascontiguousarray(vectors, dtype="<f4").tobytes()
        return base64.b64encode(raw).decode("ascii")

    return work


if __name__ == "__main__":
    sys.exit(worker_process.serve(_encode, "KB encode", result_parts=lambda v: [v]))
