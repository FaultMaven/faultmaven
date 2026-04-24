"""Utilities for the Case module.

Contains helpers that compose the module's primary repository and
service interfaces — concurrency retry, optimistic-locking wrappers,
etc. Keep each utility narrow: no business logic, no orchestration.
"""

from faultmaven.modules.case.utils.retry import update_case_with_retry

__all__ = ["update_case_with_retry"]
