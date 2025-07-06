# Use official Python image
FROM python:3.10-slim

# Set working directory
WORKDIR /fmv/app

# Copy dependencies first
COPY requirements.txt /fmv/

# Install dependencies
RUN pip install --no-cache-dir -r /fmv/requirements.txt

# Copy application files
COPY app/ /fmv/app/
COPY config/ /fmv/config/

# Ensure Python recognizes the app module
ENV PYTHONPATH="/fmv"

# Expose FastAPI port
EXPOSE 8000

# Run FastAPI app in production mode
CMD ["uvicorn", "app.query_processing:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
