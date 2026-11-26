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

from faultmaven.infrastructure import encode_worker, token_count_worker, worker_process

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


#: The cores this stub's node shows, with no CPU quota: more than any count a
#: test pins, whatever the runner has.
NODE_CORES = 32


def _pins(request, send):
    """The real encode job over a stand-in model class, on a node of
    ``NODE_CORES`` cores with no CPU quota. Its load runs a parallel torch op,
    as a real load does, which builds the intra-op pool at the count torch was
    pinned to. Nothing native is imported before the job pins its knobs."""
    from faultmaven.infrastructure import model_cache

    seen = {}

    class StandIn:
        def __init__(self, _name):
            import torch

            seen["torch_at_load"] = torch.get_num_threads()
            torch.ones(512, 512) @ torch.ones(512, 512)
            seen["threads_after_load"] = _tasks()

        def tokenizer(self, texts, **_kwargs):
            return {"input_ids": [text.split() for text in texts]}

        def encode(self, texts, batch_size):
            import numpy as np
            import torch

            seen["torch_at_encode"] = torch.get_num_threads()
            seen["batch_size"] = batch_size
            seen["rayon_pool"] = _rayon_pool_size()
            logger.warning("pins %s", json.dumps(seen))
            return np.zeros((len(texts), 1024), dtype="<f4")

    model_cache.SENTENCE_TRANSFORMERS_AVAILABLE = True
    model_cache.SentenceTransformer = StandIn
    model_cache._cgroup_cpu_quota = lambda: None
    os.cpu_count = lambda: NODE_CORES
    return encode_worker._encode(request, send)


def _count_pins(request, send):
    """The real count job (``token_count_worker._count``, its pin included)
    over a stand-in tokenizer file: it logs the threads its Rust pool added."""
    import tempfile

    import huggingface_hub
    from tokenizers import Tokenizer, models, pre_tokenizers

    tokenizer = Tokenizer(models.WordLevel({"a": 0, "[UNK]": 1}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    path = os.path.join(tempfile.mkdtemp(), "tokenizer.json")
    tokenizer.save(path)
    huggingface_hub.hf_hub_download = lambda *_a, **_k: path
    work = token_count_worker._count(request, send)

    def counted() -> str:
        before = _tasks()
        answer = work()
        logger.warning("pins %s", json.dumps({"rayon_pool": _tasks() - before}))
        return answer

    return counted


def _no_model(request, send, panics=False):
    """The real encode job over a model cache that cannot load the model: it
    answers None, or its load panics."""
    from faultmaven.infrastructure import model_cache

    def load(*_args):
        raise PanicException("a load that panicked")

    model_cache.SENTENCE_TRANSFORMERS_AVAILABLE = False
    if panics:
        model_cache.ModelCache.get_bge_m3_model = load
    return encode_worker._encode(request, send)


def _encode(request, send):
    stub = os.environ["FM_MODEL_STUB"]
    if stub == "pins":
        return _pins(request, send)
    if stub in ("no_model", "model_panics"):
        return _no_model(request, send, panics=stub == "model_panics")
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
        return _count_pins(request, send)
    if stub == "words":  # the plain count and a priced one, 10 more a text
        words = sum(len(text.split()) for text in texts)
        return lambda: f"{words} {words + 10 * len(texts)}"
    answers = {"not_a_count": "many", "one_part": "3", "three_parts": "3 3 3"}
    answers.update({"not_decimal": "3 -3", "two_spaces": "3  3"})
    if stub in answers:
        return lambda: answers[stub]

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
