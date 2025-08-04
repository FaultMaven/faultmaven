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

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Note: spaCy model no longer needed - PII protection uses K8s Presidio microservice

# Copy application code
COPY faultmaven/ ./faultmaven/
COPY config/ ./config/

# Create non-root user
RUN useradd --create-home --shell /bin/bash faultmaven \
    && chown -R faultmaven:faultmaven /app
USER faultmaven

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Run the application
CMD ["python", "-m", "faultmaven.main"] 