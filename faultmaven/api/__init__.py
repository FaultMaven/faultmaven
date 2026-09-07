"""API layer package.

The FastAPI routers, dependencies, middleware and exception handlers.

This file is what makes the directory a REGULAR package rather than an
implicit namespace one. setuptools packaged it either way, but grimp walks
only regular packages — so while it was missing, `faultmaven.api` had zero
modules in the import graph and every import-linter contract naming it was
unfalsifiable. Contract 2 ("Services cannot import API layer") could not fail.
"""
