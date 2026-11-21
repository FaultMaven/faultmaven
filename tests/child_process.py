"""The environment of a child process a test starts to import ``faultmaven`` (#2975).

"No test reaches a network or a model server" (CLAUDE.md), but the guard that
enforces it (``tests/network_guard.py``) patches the pytest process only: a
child has its own interpreter. And each test that booted the app in a child
built that child's environment by hand, so they disagreed. The default-database
boot probe had no ``HF_HUB_OFFLINE``, ran with an empty ``HOME``, and so the
lifespan's BGE-M3 preload downloaded the model from huggingface.co on every run:
~256 s of a 316 s boot, inside a 600 s timeout a CI runner overran.

``child_env`` is the one base. The child runs offline, as the image does (its
Dockerfile's runtime ``ENV``), reads no ``.env``, and imports ``faultmaven`` from
this tree. While the test process runs guarded, the child does too: the
``sitecustomize`` in ``tests/child_site/``, on its ``PYTHONPATH``, installs the
same guard before the child's own code runs, with the allowances the
environment the caller passes names (``DATABASE_URL``, ``REDIS_HOST``). The
opt-in ``-m real_llm`` lane runs the parent unguarded, and its children with it.
"""

from __future__ import annotations

import os
from pathlib import Path

import faultmaven
from tests import network_guard

#: The tree the test process imports ``faultmaven`` (and ``tests``) from.
REPO_ROOT = Path(faultmaven.__file__).resolve().parent.parent

#: Holds the ``sitecustomize`` that installs the network guard in the child.
GUARDED_SITE = Path(__file__).resolve().parent / "child_site"


def child_env(home: Path | str, **overrides: str) -> dict[str, str]:
    """A child's whole environment: the base, then the caller's ``overrides``.

    Nothing is inherited from ``os.environ``; a caller that wants the parent's
    variables passes them.
    """
    path = [str(REPO_ROOT)]
    if network_guard.GUARD is not None:
        path.append(str(GUARDED_SITE))
    return {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": os.pathsep.join(path),
        "FAULTMAVEN_SKIP_DOTENV": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        **overrides,
    }
