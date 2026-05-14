"""Services package.

Submodules (``faultmaven.services.base``, ``faultmaven.services.analytics``,
``faultmaven.services.service_factory``, etc.) are imported directly by
consumers; this package is kept import-light to avoid circular imports
during Python's package-then-submodule resolution.
"""
