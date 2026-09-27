"""SQLite implementation of the case repository, split by concern.

``repository.py`` holds ``SQLiteCaseRepository`` (the public
``ICaseRepository`` implementation) and ``RepositoryException``. ``rows.py``,
``loading.py`` and ``saving.py`` hold the pure helpers it delegates to, so
this package mirrors ``postgresql_hybrid_case_repository/``'s layout for a
file-to-file diff between the two backends.
"""
