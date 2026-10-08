"""Knowledge Module - Vertical Slice.

This module owns all knowledge-related functionality for the RAG system:
- Knowledge base search and retrieval (semantic + full-text)
- Document embedding and vector search
- Knowledge ingestion and processing
- Knowledge item management

Other modules import its public surface from ``contracts`` (and the runbook
taxonomy from ``taxonomy``), never from this package. It holds no imports on
purpose: ``taxonomy`` is read by the ORM, and a package ``__init__`` that
imported the routes would turn every such read into an import of the whole
module — and a cycle through ``persistence.models``.
"""
