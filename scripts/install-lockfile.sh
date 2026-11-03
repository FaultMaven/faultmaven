#!/bin/sh
# Install a requirements/*.txt lockfile with the CPU build of torch.
#
#     sh scripts/install-lockfile.sh requirements/test.txt
#
# The one way CI and the image install a lockfile: the Dockerfile, ci-cd.yml
# and benchmarks.yml all call this. The lockfiles pin PyPI's torch, whose Linux
# wheel is the CUDA build, and it brings nvidia-*, cuda-* and triton with it:
# 3.1 GB downloaded per CI job (measured 2026-11-03). FaultMaven runs BGE-M3 on
# CPU and has no GPU code path, so the image has always installed the CPU build
# instead. CI installed the lockfile as written, so it tested a different torch
# from the one that ships, and paid ~1.5 min per job to download and unpack it.
#
# torch comes first, from the PyTorch CPU index, at the version the lockfile
# pins (read from it, so no second copy of the pin can drift), and with
# --no-deps so nothing else comes from that index. Then the rest of the
# lockfile, from PyPI, without the GPU-only lines. The order matters:
# sentence-transformers requires torch, so with torch absent pip would resolve
# PyPI's CUDA build for it. Every removed line is required only by torch's
# CUDA build ("via torch", "via cuda-toolkit", ...), and torch's own
# requirements are in the lockfile; `pip check` below fails the install if
# either stops being true.

set -eu

lockfile="${1:?usage: install-lockfile.sh <requirements/LOCKFILE.txt>}"
cpu_index="https://download.pytorch.org/whl/cpu"
gpu_only='^(torch==|triton==|nvidia-|cuda-)'

# A lockfile that stopped pinning torch would otherwise install whatever
# version sentence-transformers' range resolves to: refuse instead.
torch_pin="$(grep -E '^torch==' "$lockfile")" || {
    echo "install-lockfile: $lockfile pins no torch (no 'torch==' line)" >&2
    exit 1
}

rest="$(mktemp)"
trap 'rm -f "$rest"' EXIT
grep -vE "$gpu_only" "$lockfile" > "$rest"

python -m pip install --no-deps --index-url "$cpu_index" "$torch_pin"
python -m pip install -r "$rest"

python -m pip check
# The property this script exists for, checked on what was installed rather
# than on what was asked for.
python - <<'EOF'
import importlib.metadata
import re
import sys

gpu = sorted(
    d.metadata["Name"]
    for d in importlib.metadata.distributions()
    if re.match(r"(nvidia-|cuda-|triton$)", (d.metadata["Name"] or "").lower())
)
if gpu:
    sys.exit(f"install-lockfile: GPU packages were installed: {', '.join(gpu)}")
print(f"install-lockfile: torch {importlib.metadata.version('torch')}, no GPU packages")
EOF
