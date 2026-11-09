"""Evidence preprocessing: Tier 0 classification and Tier 1 extraction.

Import a name from the module that defines it: this ``__init__`` imports
nothing, so the Tier 1 worker (``extraction_worker``) loads the extractors and
not the service, its settings or its models (#2637).
"""
