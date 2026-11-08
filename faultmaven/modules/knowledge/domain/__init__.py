"""Knowledge Domain Layer.

This package contains domain logic for knowledge management:
- services: Domain services for knowledge operations
- models: Domain models and entities

Import a name from the module that defines it: this ``__init__`` imports
nothing, so the document preparation worker can import its parsers without
loading the knowledge service and its stores (#2622).
"""
