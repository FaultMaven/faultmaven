"""Persistence: the ORM models, the database engine and the stores built on them.

Import a name from the module that defines it: this ``__init__`` imports
nothing, so importing the models (as ``alembic/env.py`` does) does not load the
engine module (#2187).
"""
