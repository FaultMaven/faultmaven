# FaultMaven Backend Dockerfile
FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    ca-certificates \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Every pip in this build verifies against that OS store, not its vendored bundle.
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt

# Neither OpenSSL's default store nor the pip building this image may trust
# "emSign ECC Root CA - G3" (GHSA-6q48-6c7w-9gw2: compromised intermediate keys)
# or "AAA Certificate Services". Listing ca-certificates above (unpinned: a pin
# breaks on its next fix) takes the package that blacklists both. aiohttp's
# sessions verify against OpenSSL's default paths, and pip 24.0 here vendors
# certifi 2023.07.22, which holds both. certifi==2026.10.19 stays the store for
# httpx/requests (huggingface_hub, tiktoken, web search) and lacks both too:
# #2110's handshake test of ~50 endpoints passed under it alone.
# The root count and ISRG Root X1 are positive controls: an empty store fails.
RUN python - <<'EOF'
import hashlib, os, ssl
def check_roots(ctx, label):
    certs = ctx.get_ca_certs()
    names = {v for cert in certs for rdn in cert["subject"] for k, v in rdn if k == "commonName"}
    digests = {hashlib.sha256(der).hexdigest() for der in ctx.get_ca_certs(binary_form=True)}
    trusted = sorted(names & {"emSign ECC Root CA - G3", "AAA Certificate Services"})
    assert len(certs) >= 100 and "ISRG Root X1" in names, f"{label}: {len(certs)} roots, or no ISRG Root X1"
    assert not trusted, f"{label}: still trusts {trusted}"
    assert "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b" not in digests, f"{label}: G3 by SHA-256"
    return len(certs)
count = check_roots(ssl.create_default_context(), "default context (aiohttp)")
assert os.path.realpath(ssl.get_default_verify_paths().cafile) == os.path.realpath(os.environ["PIP_CERT"]), "OpenSSL's cafile is not PIP_CERT"
fresh = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
fresh.load_verify_locations(cafile=os.environ["PIP_CERT"])
check_roots(fresh, "PIP_CERT (pip)")
print(f"trust store OK: {count} roots, neither removed root trusted")
EOF

# Set work directory
WORKDIR /app

# Copy lockfile first for better layer caching
COPY requirements/cloud.txt requirements.txt

# Install Python dependencies from the lockfile, with CPU-only torch.
# The lockfile pins the default (CUDA) torch wheel, which drags in ~4-5GB of
# nvidia-*/cuda-*/triton libraries. FaultMaven runs BGE-M3 on CPU — there is no
# GPU code path, and neither local nor cloud requests a GPU — so those are dead
# weight. scripts/install-lockfile.sh installs the CPU torch wheel at the
# lockfile's pin, then the rest of the lockfile without the GPU-only lines,
# and refuses a lockfile that pins no torch. CI installs every lockfile through
# the same script, so the tests run against the torch this image ships.
# Bind-mounted for this step only: scripts/ is not part of the image.
RUN --mount=type=bind,source=scripts/install-lockfile.sh,target=/tmp/install-lockfile.sh \
    PIP_NO_CACHE_DIR=1 sh /tmp/install-lockfile.sh requirements.txt

# Create the non-root user early so the HuggingFace cache it owns is written
# once and never re-copied by a later `chown -R /app` (copy-up would otherwise
# double the ~2GB model layer in the image).
RUN useradd --create-home --shell /bin/bash faultmaven

# Pre-download the BGE-M3 embedding model into the user's HuggingFace cache so
# the image is self-contained: no ~2GB download at startup and no network
# dependency (works air-gapped). Placed before the source COPY so this ~2GB
# layer stays cached across code changes. HF_HOME persists to runtime, so the
# app resolves the model from this cache. Downloaded AS the runtime user, so
# the files are already correctly owned (no chown needed).
ENV HF_HOME=/home/faultmaven/.cache/huggingface
USER faultmaven
# Download BGE-M3, then drop the redundant *.safetensors weights: the runtime
# resolves the model to its pytorch_model.bin revision, so the separately
# cached safetensors copy (~2.2GB) is dead weight. After trimming, re-load the
# model in offline mode so a broken cache fails the build rather than the pod.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-m3')" \
    && for f in "$HF_HOME"/hub/models--BAAI--bge-m3/snapshots/*/*.safetensors; do \
         [ -e "$f" ] && { readlink -f "$f" | xargs -r rm -f; rm -f "$f"; }; \
       done \
    && HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-m3'); print('offline model load OK')"
# tiktoken's cl100k_base encoding, baked in for the same reason. Token counts
# for OpenAI, OpenRouter, Anthropic and Fireworks go through it, as do chunk
# sizing in vector_storage and the knowledge base's document preprocessor.
# tiktoken downloads it on first use, with no request timeout. Without it, an
# image with no network counts those tokens at four characters a token, which
# undercounts CJK and log text several times over.
# TIKTOKEN_CACHE_DIR is set before the prefetch and persists to runtime. The
# second load goes through an unreachable proxy, so a cache tiktoken does not
# read fails the build rather than the pod.
ENV TIKTOKEN_CACHE_DIR=/home/faultmaven/.cache/tiktoken
RUN python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" \
    && HTTPS_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 \
       https_proxy=http://127.0.0.1:9 http_proxy=http://127.0.0.1:9 \
       python -c "import tiktoken; tiktoken.get_encoding('cl100k_base'); print('offline tiktoken load OK')"
USER root

# Note: spaCy model no longer needed - PII protection uses K8s Presidio microservice

# Copy application code and project metadata, owned by the runtime user at copy
# time. Using COPY --chown (instead of a later `chown -R /app`) avoids an
# overlay copy-up that would duplicate everything under /app into an extra layer.
COPY --chown=faultmaven:faultmaven pyproject.toml .
COPY --chown=faultmaven:faultmaven faultmaven/ ./faultmaven/
COPY --chown=faultmaven:faultmaven alembic/ ./alembic/
COPY --chown=faultmaven:faultmaven alembic.ini .
# Knowledge resources, including the baseline KB pack at
# resources/knowledge/pack (runbooks + build-time vectors). The KB bootstrap
# ingests this pack at startup in seconds (no embedding model). Override at
# runtime with KB_PACK_DIR to ship an updated pack without rebuilding the image.
COPY --chown=faultmaven:faultmaven resources/ ./resources/

# Install the package itself (no deps — already installed from lockfile)
RUN pip install --no-cache-dir --no-deps .

# The runtime user must own the /app directory itself so it can create ./data
# (SQLite DB, ChromaDB, evidence) at startup. Copied files are already owned via
# COPY --chown above, so this sets only the directory node — not a recursive
# chown — and creates no copy-up layer.
RUN chown faultmaven:faultmaven /app
USER faultmaven

# Container runtime defaults. With no explicit config the app applies the
# zero-config "local" preset, which is tuned for a laptop (127.0.0.1:8000,
# reload on). Setting these here makes the image serve on the exposed port
# (0.0.0.0:8090) without a file-watching reloader. Preset application respects
# already-set env vars, so these win; cloud/compose can still override them.
ENV HOST=0.0.0.0 \
    PORT=8090 \
    RELOAD=false

# Load the embedding model from the baked cache only — never reach out to
# HuggingFace at runtime. The model was pre-downloaded above, so startup is
# fast and works with no network/HF access.
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

# Expose port
EXPOSE 8090

# Health check
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8090/health || exit 1

# Run the application through the launcher, not `-m faultmaven.main`: that would
# execute main.py twice (as __main__, then when uvicorn imports the app string).
CMD ["python", "-m", "faultmaven.server"]
