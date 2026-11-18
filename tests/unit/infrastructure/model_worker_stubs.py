"""Model workers for #2851's tests, served in place of the real ones.

``python -m`` this module with ``FM_MODEL_STUB_ROLE`` ``encode`` stands in for
``encode_worker`` (``index_slot._ENCODE_WORKER``), with ``count`` for
``token_count_worker`` (``embedding_guard._TOKEN_COUNT_WORKER``). A worker
inherits the test's environment, so a test picks the stub with
``monkeypatch.setenv("FM_MODEL_STUB", ...)``. Every stub ends by its own state,
never by a clock: a loop runs until a bound kills it, a sleep outlasts any
bound a test sets. ``pins`` runs the REAL encode job over a stand-in model
class, with no CPU quota, and logs the thread counts it saw.
"""

import base64
import json
import logging
import os
import re
import signal
import sys
import time

from faultmaven.infrastructure import encode_worker, worker_process

# Its importable name, not ``__main__``: the parent re-emits the records
# under it, where the test listens.
logger = logging.getLogger(__spec__.name)

#: The record a stub logs once its work has begun, with its pid.
ENTERED = "the test worker has entered"

#: Cubic backtracking: one ``sub`` over 500 characters is one C call that never
#: drops the interpreter lock. Over a constant: no input reaches it.
_BACKTRACKING = re.compile(r"a*a*b")


class PanicException(BaseException):
    """Named as pyo3's: a Rust library's panic reaches Python as this."""


def vectors(count: int) -> str:
    """``count`` vectors of 1024 float32, the i-th all ``i + 0.25``, as the
    real worker answers them."""
    import numpy as np

    rows = np.repeat(np.arange(count, dtype="<f4")[:, None] + 0.25, 1024, axis=1)
    return base64.b64encode(rows.astype("<f4").tobytes()).decode("ascii")


def _tasks() -> int:
    return len(os.listdir("/proc/self/task"))


def _rayon_pool_size() -> int:
    """Threads a parallel ``tokenizers`` call adds to this process: its Rust
    pool, started on first use."""
    from tokenizers import Tokenizer, models, pre_tokenizers

    tokenizer = Tokenizer(models.WordLevel({"a": 0, "[UNK]": 1}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    before = _tasks()
    tokenizer.encode_batch(["a a a"] * 2000)
    return _tasks() - before


def _pins(request, send):
    """The real encode job over a stand-in model class, as on a node with no
    CPU quota: the load sets torch to every core, as ``configure_inference_
    threads`` does there."""
    import numpy as np

    from faultmaven.infrastructure import model_cache

    seen = {}

    class StandIn:
        def __init__(self, _name):
            import torch

            seen["torch_after_configure"] = torch.get_num_threads()

        def encode(self, texts, batch_size):
            import torch

            seen["torch_at_encode"] = torch.get_num_threads()
            seen["batch_size"] = batch_size
            seen["rayon_pool"] = _rayon_pool_size()
            logger.warning("pins %s", json.dumps(seen))
            return np.zeros((len(texts), 1024), dtype="<f4")

    model_cache.SENTENCE_TRANSFORMERS_AVAILABLE = True
    model_cache.SentenceTransformer = StandIn
    model_cache._cgroup_cpu_quota = lambda: None
    return encode_worker._encode(request, send)


def _encode(request, send):
    stub = os.environ["FM_MODEL_STUB"]
    if stub == "pins":
        return _pins(request, send)
    texts = request["parts"]

    def work() -> str:
        if stub == "vectors":
            return vectors(len(texts))
        if stub == "short":
            return vectors(len(texts) - 1)
        if stub == "garbled":
            return "not base64 !"
        if stub == "raises":
            raise RuntimeError("an encode that failed")
        if stub == "panics":
            raise PanicException("ThreadPoolBuildError")
        if stub == "allocates":
            return str(len(bytearray(1 << 40)))
        if stub == "is_killed":
            os.kill(os.getpid(), signal.SIGKILL)
        logger.warning("%s %d", ENTERED, os.getpid())
        if stub == "holds_the_lock_in_c":
            while True:
                _BACKTRACKING.sub("", "a" * 500)
        time.sleep(3600)  # never_returns
        return ""

    return work


def _count(request, send):
    stub = os.environ["FM_MODEL_STUB"]
    texts = request["parts"]
    if stub == "pins":
        os.environ["RAYON_NUM_THREADS"] = str(request["threads"])

        def pinned() -> str:
            logger.warning("pins %s", json.dumps({"rayon_pool": _rayon_pool_size()}))
            return "0"

        return pinned
    if stub == "words":
        return lambda: str(sum(len(text.split()) for text in texts))
    if stub == "not_a_count":
        return lambda: "many"

    def work() -> str:
        logger.warning("%s %d", ENTERED, os.getpid())
        if stub == "holds_the_lock_in_c":
            while True:
                _BACKTRACKING.sub("", "a" * 500)
        time.sleep(3600)  # never_returns
        return ""

    return work


if __name__ == "__main__":
    role = os.environ["FM_MODEL_STUB_ROLE"]
    job = _encode if role == "encode" else _count
    sys.exit(worker_process.serve(job, "test worker", result_parts=lambda r: [r]))
