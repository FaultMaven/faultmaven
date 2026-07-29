"""Operator console entrypoints.

The modules in this package back the ``fm-*`` commands declared under
``[project.scripts]`` in ``pyproject.toml``. They live inside the package —
rather than in ``scripts/`` — because ``scripts/`` is excluded from the wheel
and never copied into the container image, so a path-based invocation
(``python scripts/auth/...``) cannot work in a pod (#887). Installing the
package puts these commands on ``PATH`` instead::

    kubectl exec deploy/faultmaven-api -- fm-provision-sso-org --name ...

Each module exposes a synchronous ``main()`` that argparse-parses ``sys.argv``
and exits with a meaningful status code; ``asyncio.run`` is wrapped inside.

Dev-only conveniences (``create_user.py``, ``list_users.py``,
``list_users_fast.py``) deliberately stay in ``scripts/auth/`` — they are run
from a checkout, not from a pod.
"""
