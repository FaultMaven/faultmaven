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
COPY requirements/enterprise.txt requirements.txt

# Install Python dependencies from lockfile
RUN pip install --no-cache-dir -r requirements.txt

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

# Create non-root user
RUN useradd --create-home --shell /bin/bash faultmaven \
    && chown -R faultmaven:faultmaven /app
USER faultmaven

# Expose port
EXPOSE 8090

# Health check
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8090/health || exit 1

# Run the application
CMD ["python", "-m", "faultmaven.main"]
