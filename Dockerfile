# FaultMaven Backend Dockerfile
FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Set work directory
WORKDIR /app

# Copy lockfile first for better layer caching
COPY requirements/cloud.txt requirements.txt

# Install Python dependencies from the lockfile, with CPU-only torch.
# The lockfile pins the default (CUDA) torch wheel, which drags in ~4-5GB of
# nvidia-*/cuda-*/triton libraries. FaultMaven runs BGE-M3 on CPU — there is no
# GPU code path, and neither local nor cloud requests a GPU — so those are dead
# weight. Install the CPU torch wheel from the PyTorch CPU index, then the rest
# of the locked deps with the GPU-only lines stripped (all are "via torch").
RUN grep -vE '^(torch==|triton==|nvidia-|cuda-)' requirements.txt > /tmp/req-cpu.txt \
    && pip install --no-cache-dir torch==2.11.0 --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r /tmp/req-cpu.txt \
    && rm -f /tmp/req-cpu.txt

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
USER root

# Note: spaCy model no longer needed - PII protection uses K8s Presidio microservice

# Copy application code and project metadata
COPY pyproject.toml .
COPY faultmaven/ ./faultmaven/
COPY alembic/ ./alembic/
COPY alembic.ini .
# Knowledge resources, including the baseline KB pack at
# resources/knowledge/pack (runbooks + build-time vectors). The KB bootstrap
# ingests this pack at startup in seconds (no embedding model). Override at
# runtime with KB_PACK_DIR to ship an updated pack without rebuilding the image.
COPY resources/ ./resources/

# Install the package itself (no deps — already installed from lockfile)
RUN pip install --no-cache-dir --no-deps .

# Give the runtime user ownership of the application code (the user itself was
# created earlier, above, for the model cache).
RUN chown -R faultmaven:faultmaven /app
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

# Run the application
CMD ["python", "-m", "faultmaven.main"]
