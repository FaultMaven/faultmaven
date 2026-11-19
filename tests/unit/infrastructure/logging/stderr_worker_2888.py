"""A worker (``worker_process.serve``) that writes the request's marker and
more than 1 MiB to its stderr, through ``sys.stderr``, ``print`` (its fd 1 is
the stderr) and the fd itself, then returns, exits 7, or keeps writing. The
marker is passed in at run time (``chained_2888`` says why)."""

import os
import sys

from faultmaven.infrastructure import worker_process

PADDING = 1 << 20


def _job(request, send):
    def work():
        marker = request["marker"]
        sys.stderr.write(f"{marker}\n")
        sys.stderr.flush()
        print(marker, flush=True)
        os.write(2, marker.encode())
        sys.stderr.buffer.write(b"x" * PADDING)
        sys.stderr.flush()
        while request["end"] == "hold":
            sys.stderr.buffer.write(b"x" * 4096)
            sys.stderr.flush()
        if request["end"] == "crash":
            os._exit(7)
        return {"done": True}

    return work


if __name__ == "__main__":
    sys.exit(worker_process.serve(_job, "stderr job"))
