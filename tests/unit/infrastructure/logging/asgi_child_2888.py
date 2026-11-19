"""A uvicorn worker child's load of the app (the factory), with ``faultmaven.main`` failing
as it is imported: the target of a ``multiprocessing`` spawn, as uvicorn's
children are. The marker is passed in at run time (``chained_2888`` says why)."""

import sys
import types


def import_the_app(marker: str) -> None:
    def refuse(_name):
        raise RuntimeError(marker)

    broken = types.ModuleType("faultmaven.main")
    broken.__getattr__ = refuse
    sys.modules["faultmaven.main"] = broken
    from faultmaven.asgi import create_app

    create_app()
