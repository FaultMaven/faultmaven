"""Installs the suite's network guard in a child process a test starts (#2975).

``tests/child_process.child_env`` puts this directory on the child's
``PYTHONPATH`` while the test process runs guarded, and Python imports
``sitecustomize`` at start-up, before the child's own code.
"""

from tests import network_guard

network_guard.install()
