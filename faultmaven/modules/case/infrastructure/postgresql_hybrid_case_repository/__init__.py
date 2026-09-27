"""PostgreSQL implementation of ``ICaseRepository`` for the case module.

Split across ``repository.py`` (the class and its public surface),
``rows.py`` (row-to-domain-model mapping), ``loading.py`` (SELECT-side
helpers) and ``saving.py`` (INSERT/UPSERT-side helpers).
"""
