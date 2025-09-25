#!/bin/bash

# Local LLM Service Management Script
# This script manages the local LLM Docker service for FaultMaven
# Usage: ./local_llm_service.sh <command> [local_llm_model]
# Commands: start, stop, status, restart

set -e

# Default configuration
CONTAINER_NAME="local-llm-service"
HOST_ENDPOINT="8080"
CPU_CORES="8"
MEMORY_LIMIT="16g"
LLM_MODELS_DIR="/var/lib/llm"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Print colored output
print_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
print_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
print_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Check if Docker is available
check_docker() {
    if ! command -v docker &> /dev/null; then
        print_error "Docker is not installed or not in PATH"
        exit 1
    fi

    if ! docker info &> /dev/null; then
        print_error "Docker daemon is not running or not accessible"
        exit 1
    fi
}

# Check if the LLM models directory exists
check_models_directory() {
    if [ ! -d "$LLM_MODELS_DIR" ]; then
        print_error "LLM models directory does not exist: $LLM_MODELS_DIR"
        print_info "Please create the directory and place your GGUF model files there"
        exit 1
    fi
}

# Get the GGUF filename for a model
get_model_filename() {
    local model_name="$1"
    local model_dir="$LLM_MODELS_DIR/$model_name"

    if [ ! -d "$model_dir" ]; then
        print_error "Model directory does not exist: $model_dir"
        return 1
    fi

    # Look for GGUF files in the model directory
    local gguf_file=$(find "$model_dir" -name "*.gguf" -type f | head -n 1)

    if [ -z "$gguf_file" ]; then
        print_error "No GGUF file found in $model_dir"
        return 1
    fi

    # Return relative path from /models/ mount point
    echo "$model_name/$(basename "$gguf_file")"
}

# Load configuration from .env file
load_env_config() {
    local env_file="${ENV_FILE:-$(pwd)/.env}"

    if [ ! -f "$env_file" ]; then
        print_warning ".env file not found at: $env_file"
        return 1
    fi

    # Read LOCAL_LLM_MODEL from .env file
    local configured_model=$(grep -E "^LOCAL_LLM_MODEL=" "$env_file" | cut -d'=' -f2 | tr -d '"' | tr -d ' ')
    echo "$configured_model"
}

# Get the currently running model name
get_running_model() {
    if ! is_container_running; then
        return 1
    fi

    # Extract model path from container arguments
    local model_path=$(docker inspect --format='{{range .Args}}{{.}} {{end}}' "$CONTAINER_NAME" 2>/dev/null | grep -o -- '-m [^ ]*' | cut -d' ' -f2)

    if [ -n "$model_path" ]; then
        # Extract model name from path (e.g., /models/Deeps03-qwen2-1.5b-log-classifier/file.gguf -> Deeps03-qwen2-1.5b-log-classifier)
        echo "$model_path" | sed 's|/models/||' | cut -d'/' -f1
    else
        return 1
    fi
}

# Check if running model matches configured model
check_model_consistency() {
    local configured_model=$(load_env_config)
    local running_model=$(get_running_model)

    if [ -z "$configured_model" ]; then
        print_warning "No LOCAL_LLM_MODEL configured in .env file"
        return 1
    fi

    if [ -z "$running_model" ]; then
        print_info "No model currently running"
        return 1
    fi

    if [ "$configured_model" = "$running_model" ]; then
        print_success "Running model matches configured model: $running_model"
        return 0
    else
        print_warning "Model mismatch detected:"
        print_warning "  Configured: $configured_model"
        print_warning "  Running: $running_model"
        return 1
    fi
}

# Check if container is running
is_container_running() {
    docker ps --filter "name=$CONTAINER_NAME" --filter "status=running" --format "{{.Names}}" | grep -q "^${CONTAINER_NAME}$"
}

# Check if container exists (running or stopped)
container_exists() {
    docker ps -a --filter "name=$CONTAINER_NAME" --format "{{.Names}}" | grep -q "^${CONTAINER_NAME}$"
}

# Get container status
get_container_status() {
    if container_exists; then
        docker inspect --format='{{.State.Status}}' "$CONTAINER_NAME"
    else
        echo "not_found"
    fi
}

# Check if local LLM service is responding
check_service_health() {
    local max_attempts=30
    local attempt=1

    print_info "Checking service health on port $HOST_ENDPOINT..."

    while [ $attempt -le $max_attempts ]; do
        if curl -s -f "http://localhost:$HOST_ENDPOINT/health" > /dev/null 2>&1; then
            return 0
        fi

        if [ $attempt -eq 1 ]; then
            print_info "Waiting for service to become healthy..."
        fi

        sleep 2
        ((attempt++))
    done

    return 1
}

# Start the local LLM service
start_service() {
    local model_name="$1"

    # If no model specified, try to use configured model from .env
    if [ -z "$model_name" ]; then
        model_name=$(load_env_config)
        if [ -n "$model_name" ]; then
            print_info "Using configured model from .env: $model_name"
        else
            print_error "Model name is required for start command and no LOCAL_LLM_MODEL found in .env"
            print_info "Usage: $0 start <model_name>"
            print_info "Available models in $LLM_MODELS_DIR:"
            ls -la "$LLM_MODELS_DIR" 2>/dev/null || print_warning "Models directory is empty or not accessible"
            exit 1
        fi
    fi

    print_info "Starting local LLM service with model: $model_name"

    # Check prerequisites
    check_docker
    check_models_directory

    # Get the model filename
    MODEL_FILENAME=$(get_model_filename "$model_name")
    if [ $? -ne 0 ]; then
        exit 1
    fi

    print_info "Using model file: $MODEL_FILENAME"

    # Stop existing container if running
    if is_container_running; then
        print_warning "Container is already running. Stopping it first..."
        stop_service
    elif container_exists; then
        print_info "Removing existing stopped container..."
        docker rm "$CONTAINER_NAME" > /dev/null
    fi

    # Start the container
    print_info "Starting Docker container..."
    docker run -d --name "$CONTAINER_NAME" \
        -p "${HOST_ENDPOINT}:8080" \
        -v "$LLM_MODELS_DIR:/models" \
        --cpus="$CPU_CORES" \
        --memory="$MEMORY_LIMIT" \
        ghcr.io/ggml-org/llama.cpp:server \
        -m "/models/$MODEL_FILENAME" \
        --host 0.0.0.0 \
        -c 4096 \
        -t "$CPU_CORES" \
        -ngl 0 \
        --parallel 2

    if [ $? -eq 0 ]; then
        print_success "Container started successfully"

        # Check if service becomes healthy
        if check_service_health; then
            print_success "Local LLM service is running and healthy on port $HOST_ENDPOINT"
            print_info "Model: $model_name"
            print_info "Endpoint: http://localhost:$HOST_ENDPOINT"
            print_info "Health check: http://localhost:$HOST_ENDPOINT/health"
        else
            print_error "Service started but health check failed"
            print_info "Check container logs with: docker logs $CONTAINER_NAME"
            exit 1
        fi
    else
        print_error "Failed to start container"
        exit 1
    fi
}

# Stop the local LLM service
stop_service() {
    print_info "Stopping local LLM service..."

    if is_container_running; then
        docker stop "$CONTAINER_NAME" > /dev/null
        print_success "Service stopped"
    elif container_exists; then
        print_warning "Container exists but is not running"
    else
        print_warning "Container does not exist"
        return 0
    fi

    # Clean up the container
    if container_exists; then
        docker rm "$CONTAINER_NAME" > /dev/null
        print_info "Container removed"
    fi
}

# Show service status
show_status() {
    local status=$(get_container_status)

    print_info "Local LLM Service Status:"
    echo "  Container: $CONTAINER_NAME"
    echo "  Status: $status"

    if [ "$status" = "running" ]; then
        echo "  Port: $HOST_ENDPOINT"
        echo "  Endpoint: http://localhost:$HOST_ENDPOINT"

        # Show container resource usage
        local stats=$(docker stats --no-stream --format "table {{.CPUPerc}}\t{{.MemUsage}}" "$CONTAINER_NAME" 2>/dev/null | tail -n 1)
        if [ -n "$stats" ]; then
            echo "  Resource usage: $stats"
        fi

        # Check health
        if check_service_health; then
            print_success "Service is healthy and responding"
        else
            print_warning "Service is running but not responding to health checks"
        fi

        # Show model information if available
        local model_info=$(docker inspect --format='{{range .Args}}{{.}} {{end}}' "$CONTAINER_NAME" 2>/dev/null | grep -o -- '-m [^ ]*' | cut -d' ' -f2)
        if [ -n "$model_info" ]; then
            echo "  Model: $model_info"
        fi

        echo
        # Check model consistency with .env configuration
        if check_model_consistency; then
            : # Success message already printed
        else
            local configured_model=$(load_env_config)
            if [ -n "$configured_model" ]; then
                print_warning "Consider running: $0 restart $configured_model"
            fi
        fi

    elif [ "$status" = "exited" ]; then
        print_warning "Container has exited"
        print_info "Check logs with: docker logs $CONTAINER_NAME"
    elif [ "$status" = "not_found" ]; then
        print_info "Container does not exist"
    fi

    # Show available models
    if [ -d "$LLM_MODELS_DIR" ]; then
        echo
        print_info "Available models in $LLM_MODELS_DIR:"
        find "$LLM_MODELS_DIR" -name "*.gguf" -type f 2>/dev/null | while read -r file; do
            local rel_path=${file#$LLM_MODELS_DIR/}
            local dir_name=$(dirname "$rel_path")
            local file_size=$(du -h "$file" 2>/dev/null | cut -f1)
            echo "  - $dir_name ($file_size)"
        done
    fi
}

# Restart the service
restart_service() {
    local model_name="$1"

    if [ -z "$model_name" ]; then
        # First try to use configured model from .env
        model_name=$(load_env_config)
        if [ -n "$model_name" ]; then
            print_info "Using configured model from .env: $model_name"
        else
            # Fall back to getting model from running container
            if is_container_running; then
                local current_model=$(docker inspect --format='{{range .Args}}{{.}} {{end}}' "$CONTAINER_NAME" 2>/dev/null | grep -o -- '-m [^ ]*' | cut -d' ' -f2)
                if [ -n "$current_model" ]; then
                    # Extract model name from path
                    model_name=$(echo "$current_model" | sed 's|/models/||' | cut -d'/' -f1)
                    print_info "Using current model: $model_name"
                fi
            fi
        fi

        if [ -z "$model_name" ]; then
            print_error "Model name is required for restart command and no LOCAL_LLM_MODEL found in .env"
            print_info "Usage: $0 restart <model_name>"
            exit 1
        fi
    fi

    stop_service
    sleep 2
    start_service "$model_name"
}

# Check and fix model consistency
check_and_fix_service() {
    print_info "Checking model consistency..."

    if ! is_container_running; then
        print_info "Service is not running, starting with configured model..."
        start_service
        return $?
    fi

    if check_model_consistency; then
        print_success "Service is running with correct model"
        return 0
    else
        local configured_model=$(load_env_config)
        if [ -n "$configured_model" ]; then
            print_info "Restarting service with correct model: $configured_model"
            restart_service "$configured_model"
            return $?
        else
            print_error "No LOCAL_LLM_MODEL configured in .env file"
            return 1
        fi
    fi
}

# Show usage information
show_usage() {
    echo "Local LLM Service Management Script"
    echo
    echo "Usage: $0 <command> [model_name]"
    echo
    echo "Commands:"
    echo "  start [model_name]   Start the local LLM service with specified or configured model"
    echo "  stop                 Stop the local LLM service"
    echo "  restart [model_name] Restart the service with specified or configured model"
    echo "  status               Show current service status and model consistency"
    echo "  check                Check model consistency and restart if needed"
    echo "  help                 Show this help message"
    echo
    echo "Note: If no model_name is provided, the script will use LOCAL_LLM_MODEL from .env file"
    echo
    echo "Configuration:"
    echo "  Container name: $CONTAINER_NAME"
    echo "  Host port: $HOST_ENDPOINT"
    echo "  CPU cores: $CPU_CORES"
    echo "  Memory limit: $MEMORY_LIMIT"
    echo "  Models directory: $LLM_MODELS_DIR"
    echo
    echo "Examples:"
    echo "  $0 start llama2-7b"
    echo "  $0 status"
    echo "  $0 stop"
    echo "  $0 restart llama2-7b"
}

# Main script logic
main() {
    local command="$1"
    local model_name="$2"

    case "$command" in
        start)
            start_service "$model_name"
            ;;
        stop)
            stop_service
            ;;
        restart)
            restart_service "$model_name"
            ;;
        status)
            show_status
            ;;
        check)
            check_and_fix_service
            ;;
        help|--help|-h)
            show_usage
            ;;
        *)
            print_error "Unknown command: $command"
            echo
            show_usage
            exit 1
            ;;
    esac
}

# Run main function with all arguments
main "$@"