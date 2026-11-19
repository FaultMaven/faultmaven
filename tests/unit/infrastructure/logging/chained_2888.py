"""A chained exception whose every link carries a marker, for #2888's pins.

The marker is passed in at run time, never written here: a rendered frame
holds its source line, so a marker in this file would reach every output as
code, not as an exception's text. Run as a module, this is a worker
(``worker_process.serve``) that logs the exception and then raises it.
"""

import logging
import sys

from faultmaven.infrastructure import worker_process

#: Each link's class, as a frames render names it.
LINKS = ("RuntimeError:", "ExceptionGroup:", "OSError:", "TimeoutError:", "KeyError:")

# Its importable name, not ``__main__``: the parent re-emits the records under it.
logger = logging.getLogger(__spec__.name)


def _raised(error: BaseException) -> BaseException:
    try:
        raise error
    except BaseException as caught:
        return caught


def chained(marker: str) -> BaseException:
    """A ``RuntimeError`` carrying a note, raised from an ``ExceptionGroup``
    of two members, raised while handling a ``KeyError``: ``marker`` in each
    link's message and in the note."""
    try:
        try:
            raise KeyError(marker)
        except KeyError:
            members = [_raised(OSError(marker)), _raised(TimeoutError(marker))]
            raise ExceptionGroup(marker, members)
    except ExceptionGroup as group:
        error = RuntimeError(marker)
        error.add_note(marker)
        try:
            raise error from group
        except RuntimeError as caught:
            return caught


def _job(request, send):
    def work():
        error = chained(request["marker"])
        logger.error("the forwarded record", exc_info=error)
        raise error

    return work


if __name__ == "__main__":
    sys.exit(worker_process.serve(_job, "test job"))
