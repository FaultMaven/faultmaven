# Local LLM Setup Guide for FaultMaven

## Table of Contents
1. [Introduction](#introduction)
2. [Background](#background)
3. [Prerequisites](#prerequisites)
4. [Complete Setup Process](#complete-setup-process)
5. [Testing and Validation](#testing-and-validation)
6. [Troubleshooting](#troubleshooting)
7. [Performance Optimization](#performance-optimization)
8. [Advanced Configuration](#advanced-configuration)

## Introduction

This guide provides step-by-step instructions for setting up a local Large Language Model (LLM) using Docker Model CLI and Docker Model Runner to power FaultMaven's local provider.

## Background

### Understanding LLM Infrastructure

Running LLMs locally involves two main software components:

#### 1. Inference Engines (The Math Layer)
Handle core computational work - efficient tensor operations, quantization, and batching:
- **llama.cpp** – C++ CPU/GPU backend for LLaMA-family models (basis for GGUF ecosystem)
- **vLLM** – High-throughput inference engine with PagedAttention and batching
- **TensorRT-LLM** – NVIDIA's GPU-optimized runtime
- **ONNX Runtime** – General inference engine for ONNX models
- **DeepSpeed-Inference** – Microsoft's optimized inference library

#### 2. Model Hosting/Serving Frameworks (The Service Layer)
Provide service layers: HTTP/gRPC APIs, batching, scaling, monitoring:
- **Docker Model Runner (DMR)** – Docker's containerized service with OpenAI-compatible API
- **Ollama** – Local model hosting with simple API and GGUF management
- **Hugging Face TGI** – Production-grade serving stack
- **Triton Inference Server** – NVIDIA's multi-framework serving system
- **Ray Serve/RayLLM** – Distributed serving framework

**Key Distinction**: Inference engines are like database storage engines (InnoDB), while hosting frameworks are like database servers (MySQL) that expose APIs and manage connections.

### Why Use Local LLMs?

**Privacy & Security**: Complete data control, no external API calls, compliance with data governance
**Cost Control**: No per-token API costs, predictable infrastructure expenses
**Customization**: Fine-tune models, control versions, customize inference parameters
**Reliability**: No external dependencies, consistent performance, offline operation

### Docker Model Ecosystem

This guide focuses on Docker's LLM ecosystem:
- **Docker Model CLI** ([docker/model-cli](https://github.com/docker/model-cli)) – Command-line interface for managing AI/ML models
- **Docker Model Runner** ([docker/model-runner](https://github.com/docker/model-runner)) – Containerized service with OpenAI-compatible API

**Benefits**:
- Docker-native integration with existing infrastructure
- OpenAI-compatible API for seamless FaultMaven integration
- GGUF model support via llama.cpp backend for efficient CPU inference
- Easy model management with Docker OCI artifacts

## Prerequisites

### System Requirements

**Minimum Requirements:**
- **CPU**: 8+ cores (Intel/AMD x64)
- **RAM**: 16GB+ (32GB recommended)
- **Storage**: 50GB+ free space (SSD recommended)
- **OS**: Linux (Ubuntu 20.04+), macOS, or Windows with WSL2

**Recommended for Production:**
- **CPU**: 16+ cores with AVX2 support
- **RAM**: 64GB+
- **Storage**: NVMe SSD with 100GB+ free space
- **GPU**: Optional but significantly improves performance

### Software Dependencies

**Required:**
- Docker 20.10+ (for containerized deployment)
- Docker Compose 2.0+ (for multi-service setup)
- Python 3.11+ (for FaultMaven backend)
- Git (for cloning repositories)

**Optional but Recommended:**
- NVIDIA Docker Runtime (for GPU acceleration)
- Ollama (for easy model management)
- vLLM (for high-performance inference)

### Network Requirements

- **Port 8000**: FaultMaven API server
- **Port 8080**: Local LLM server (configurable)
- **Port 6379**: Redis (if using external Redis)
- **Port 8001**: ChromaDB (if using external ChromaDB)

## Complete Setup Process

This section provides a complete end-to-end process from installing Docker Model CLI to running a local LLM with Docker Model Runner.

### Step 1: Install Docker Model CLI

```bash
# Clone and build Docker Model CLI
git clone https://github.com/docker/model-cli.git
cd model-cli
make build

# Install as Docker CLI plugin (newer versions)
make link
# otherwise run the following statements
mkdir -p ~/.docker/cli-plugins
mv docker-model ~/.docker/cli-plugins/
chmod +x ~/.docker/cli-plugins/docker-model

# Verify installation
docker model --help
```

### Step 2: Install Docker Model Runner

```bash
# Install Docker Model Runner service
docker model install-runner

# For GPU support (optional)
# docker model install-runner --gpu cuda
# docker model install-runner --gpu auto
```

### Step 3: Pull an Open LLM Model

**Option A: Pull from Docker Hub (Recommended)**
```bash
# List available models
docker model list

# Pull a recommended model for troubleshooting
docker model pull ai/llama-3-8b-instruct-q4

# Alternative: Pull a code-focused model
docker model pull ai/codellama-7b-instruct
```

**Option B: Package Custom GGUF Model**
```bash
# Download a GGUF model from Hugging Face
mkdir -p /opt/models
wget https://huggingface.co/microsoft/Phi-3-mini-4k-instruct-gguf/resolve/main/Phi-3-mini-4k-instruct-q4.gguf -O /opt/models/phi-3-mini-q4.gguf

# Package into Docker artifact
docker model package --gguf /opt/models/phi-3-mini-q4.gguf --push ai/phi-3-mini-q4
```

### Step 4: Start Docker Model Runner

**Method 1: Using Docker Model CLI (Recommended)**
```bash
# Start Model Runner with the pulled model
docker model run ai/llama-3-8b-instruct-q4

# This will start the service and expose it on port 8080
```

**Method 2: Manual Docker Run**
```bash
# Run Model Runner manually
docker run -d \
  --name model-runner \
  -p 8080:8080 \
  docker/model-runner:latest \
  --model ai/llama-3-8b-instruct-q4

# Check status
docker logs model-runner
```

### Step 5: Verify Setup

```bash
# Check Model Runner status
docker model status

# List available models
curl http://localhost:8080/models

# Test the API
curl http://localhost:8080/engines/llama.cpp/v1/chat/completions \
  -X POST \
  -H "Content-Type: application/json" \
  -d '{
    "model": "ai/llama-3-8b-instruct-q4",
    "messages": [{"role": "user", "content": "Hello, how are you?"}]
  }'
```

### Step 6: Configure FaultMaven

Create or update your `.env` file in the FaultMaven root directory:

```bash
# Set local as primary provider
CHAT_PROVIDER=local

# Docker Model Runner configuration
LOCAL_LLM_URL=http://localhost:8080
LOCAL_LLM_MODEL=ai/llama-3-8b-instruct-q4

# Optional: Configure fallback providers
FIREWORKS_API_KEY=your_fireworks_key_here
OPENAI_API_KEY=your_openai_key_here
```

### Alternative: Ollama Setup

For a simpler setup without Docker Model CLI:

```bash
# Install Ollama
curl -fsSL https://ollama.ai/install.sh | sh
ollama serve

# Pull models
ollama pull llama3:8b-instruct

# Configure FaultMaven for Ollama
LOCAL_LLM_URL=http://localhost:11434
LOCAL_LLM_MODEL=llama3:8b-instruct
```

## Testing and Validation

### Verify Docker Model Runner

```bash
# Check Model Runner status
docker model status

# List available models
curl http://localhost:8080/models

# Test chat completion API
curl http://localhost:8080/engines/llama.cpp/v1/chat/completions \
  -X POST \
  -H "Content-Type: application/json" \
  -d '{
    "model": "ai/llama-3-8b-instruct-q4",
    "messages": [{"role": "user", "content": "What is FaultMaven?"}]
  }'
```

### Test FaultMaven Integration

```bash
# Start FaultMaven
cd /path/to/FaultMaven
python -m faultmaven.main

# Test provider registration
curl http://localhost:8000/api/v1/health/providers

# Test agent endpoint
curl -X POST http://localhost:8000/api/v1/agent/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "I have a web server returning 500 errors. Help me troubleshoot this.",
    "session_id": "test-session-001"
  }'
```

### Performance Validation

```bash
# Monitor resource usage
htop  # CPU and memory
nvidia-smi  # GPU usage (if applicable)

# Test response times
time curl -X POST http://localhost:8000/api/v1/agent/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Test message", "session_id": "perf-test"}'
```

## Troubleshooting

### Common Issues

#### 1. Docker Model Runner Not Starting

**Symptoms:**
- Connection refused errors on port 8080
- Timeout errors from FaultMaven
- "Model Runner not found" errors

**Solutions:**
```bash
# Check if port 8080 is in use
sudo netstat -tulpn | grep :8080

# Check Docker Model Runner status
docker model status

# Check container logs
docker logs model-runner

# Restart Model Runner
docker model install-runner --force
```

#### 2. Model Not Found or Not Loading

**Symptoms:**
- "Model not found" errors
- Empty model lists
- Model fails to load

**Solutions:**
```bash
# List available models
docker model list

# Pull the model explicitly
docker model pull ai/llama-3-8b-instruct

# Check if model is properly downloaded
docker images | grep ai/

# Create model from Docker Hub
curl http://localhost:8080/models/create -X POST -d '{"from": "ai/llama-3-8b-instruct"}'
```

#### 3. Docker Model CLI Not Working

**Symptoms:**
- "docker model: command not found"
- Permission denied errors
- CLI plugin not recognized

**Solutions:**
```bash
# Verify CLI plugin installation
ls -la ~/.docker/cli-plugins/docker-model

# Reinstall CLI plugin (newer versions)
cd model-cli
make build
make link

# Test CLI
docker model --help
```

#### 4. FaultMaven Provider Registration Fails

**Symptoms:**
- Local provider not in health check
- Fallback to cloud providers
- Connection timeout errors

**Solutions:**
```bash
# Verify environment variables
echo $LOCAL_LLM_URL
echo $LOCAL_LLM_MODEL

# Test direct API call
curl http://localhost:8080/models

# Check FaultMaven logs
tail -f faultmaven.log

# Verify Model Runner is accessible
curl -v http://localhost:8080/engines/llama.cpp/v1/chat/completions
```

#### 5. Poor Performance

**Symptoms:**
- Slow response times (>10 seconds)
- High CPU/memory usage
- Model loading failures

**Solutions:**
```bash
# Use smaller, quantized models
docker model pull ai/llama-3-8b-instruct-q4
docker model pull ai/phi-3-mini-4k-instruct

# Limit Docker resources
docker run --cpus=4 -m 8g docker/model-runner:latest

# Check system resources
htop
free -h
```

### Debug Commands

```bash
# Check system resources
free -h
df -h
lscpu

# Monitor network connections
ss -tulpn | grep :11434

# Check Docker containers
docker ps -a
docker logs container_name

# Test API endpoints
curl -v http://localhost:11434/api/tags
```

## Performance Optimization

### Hardware Optimization

**CPU Optimization:**
- Use CPUs with AVX2/AVX512 support
- Enable CPU frequency scaling
- Set CPU affinity for LLM processes

**Memory Optimization:**
- Use high-speed RAM (DDR4-3200+)
- Enable huge pages: `echo 1024 > /proc/sys/vm/nr_hugepages`
- Monitor memory usage and adjust model sizes

**Storage Optimization:**
- Use NVMe SSDs for model storage
- Enable filesystem caching
- Use tmpfs for temporary model files

### Software Optimization

**Model Selection:**
```bash
# Use quantized models for better performance
docker model pull ai/llama-3-8b-instruct-q4    # 4-bit quantization
docker model pull ai/llama-3-8b-instruct-q5    # 5-bit quantization
docker model pull ai/phi-3-mini-4k-instruct    # Smaller, efficient model

# Code-focused models for troubleshooting
docker model pull ai/codellama-7b-instruct-q4
docker model pull ai/deepseek-coder-6.7b-instruct
```

**Docker Model Runner Optimization:**
```bash
# Run Model Runner with resource limits
docker run -d \
  --name model-runner \
  -p 8080:8080 \
  --cpus=4 \
  --memory=8g \
  docker/model-runner:latest

# Configure model with specific parameters
docker model configure ai/llama-3-8b-instruct \
  --context-size 4096 \
  --batch-size 512
```

**Docker Configuration:**
```bash
# Optimize Docker daemon for model workloads
sudo tee /etc/docker/daemon.json << EOF
{
  "default-runtime": "runc",
  "storage-driver": "overlay2",
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "100m",
    "max-file": "3"
  }
}
EOF

# Restart Docker daemon
sudo systemctl restart docker
```

### FaultMaven Configuration Optimization

```bash
# Optimize FaultMaven for local LLM
LOCAL_LLM_TIMEOUT=60
LOCAL_LLM_MAX_RETRIES=1
LOCAL_LLM_CONFIDENCE_SCORE=0.6

# Enable caching for better performance
ENABLE_RESPONSE_CACHING=true
CACHE_TTL_SECONDS=300

# Optimize memory usage
MEMORY_MAX_WORKING_SIZE_MB=256
MEMORY_MAX_SESSION_SIZE_MB=128
```

## Advanced Configuration

### Multi-Model Setup

**Running Multiple Models:**
```bash
# Start multiple Ollama instances on different ports
OLLAMA_HOST=0.0.0.0:11434 ollama serve &
OLLAMA_HOST=0.0.0.0:11435 ollama serve &
OLLAMA_HOST=0.0.0.0:11436 ollama serve &

# Pull different models
ollama pull llama3:8b-instruct
ollama pull codellama:7b-instruct
ollama pull mistral:7b-instruct
```

**Load Balancing Configuration:**
```bash
# Use nginx for load balancing
upstream llm_backend {
    server localhost:11434;
    server localhost:11435;
    server localhost:11436;
}

server {
    listen 8080;
    location / {
        proxy_pass http://llm_backend;
    }
}
```

### Custom Model Integration

**Fine-tuned Models:**
```bash
# Create custom model with Modelfile
cat > Modelfile << EOF
FROM llama3:8b-instruct
SYSTEM "You are a specialized troubleshooting assistant for web applications."
EOF

# Build custom model
ollama create faultmaven-troubleshooter -f Modelfile
```

**Hugging Face Integration:**
```bash
# Pull models directly from Hugging Face
ollama pull huggingface/microsoft/DialoGPT-medium
ollama pull huggingface/microsoft/DialoGPT-large
```

### Monitoring and Observability

**Health Monitoring:**
```bash
# Create health check script
#!/bin/bash
curl -f http://localhost:11434/api/tags > /dev/null 2>&1
if [ $? -eq 0 ]; then
    echo "Local LLM: Healthy"
else
    echo "Local LLM: Unhealthy"
    exit 1
fi
```

**Performance Monitoring:**
```bash
# Monitor response times
curl -w "@curl-format.txt" -o /dev/null -s http://localhost:8000/api/v1/agent/chat

# Create curl format file
cat > curl-format.txt << EOF
     time_namelookup:  %{time_namelookup}\n
        time_connect:  %{time_connect}\n
     time_appconnect:  %{time_appconnect}\n
    time_pretransfer:  %{time_pretransfer}\n
       time_redirect:  %{time_redirect}\n
  time_starttransfer:  %{time_starttransfer}\n
                     ----------\n
          time_total:  %{time_total}\n
EOF
```

### Security Considerations

**Network Security:**
```bash
# Restrict access to local LLM
iptables -A INPUT -p tcp --dport 11434 -s 127.0.0.1 -j ACCEPT
iptables -A INPUT -p tcp --dport 11434 -j DROP
```

**Authentication (Optional):**
```bash
# Use nginx for basic authentication
location / {
    auth_basic "LLM Access";
    auth_basic_user_file /etc/nginx/.htpasswd;
    proxy_pass http://localhost:11434;
}
```

This comprehensive guide provides everything needed to set up and configure local LLMs for FaultMaven, from basic setup to advanced optimization and monitoring.