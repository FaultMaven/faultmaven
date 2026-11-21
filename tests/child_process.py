"""The environment of a child process a test starts to import ``faultmaven`` (#2975).

"No test reaches a network or a model server" (CLAUDE.md), but its guard
(``tests/network_guard.py``) patches the pytest process only, and each
child-boot test built its child's environment by hand. One lacked
``HF_HUB_OFFLINE``, so its lifespan downloaded BGE-M3 from huggingface.co on
every run: ~256 s of a 316 s boot, under a 600 s timeout.

``child_env`` is the one base: offline, as the image runs, no ``.env``, this
tree's ``faultmaven``; and, while the test process runs guarded, the
``sitecustomize`` in ``tests/child_site/`` installs the same guard in the child,
with the allowances its environment names. The ``-m real_llm`` lane runs both
unguarded.
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
