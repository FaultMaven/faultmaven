# FaultMaven Troubleshooting Guide

This guide helps you diagnose and resolve common issues with FaultMaven deployment and operation.

## Table of Contents

- [Installation Issues](#installation-issues)
- [Service Startup Issues](#service-startup-issues)
- [API Connection Issues](#api-connection-issues)
- [Authentication Issues](#authentication-issues)
- [Performance Issues](#performance-issues)
- [Database Issues](#database-issues)
- [Knowledge Base Issues](#knowledge-base-issues)
- [Browser Extension Issues](#browser-extension-issues)
- [Docker Issues](#docker-issues)
- [Network Issues](#network-issues)

---

## Installation Issues

### Docker Compose Not Found

**Symptom:**
```bash
docker-compose: command not found
```

**Solution:**
```bash
# Modern Docker includes compose as a plugin
docker compose version

# If that doesn't work, install Docker Compose plugin
sudo apt install docker-compose-plugin

# Or install standalone (older method)
sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose
```

### Permission Denied

**Symptom:**
```bash
Got permission denied while trying to connect to the Docker daemon socket
```

**Solution:**
```bash
# Add user to docker group
sudo usermod -aG docker $USER

# Log out and back in, or:
newgrp docker

# Verify
docker ps
```

### Port Already in Use

**Symptom:**
```
Error starting userland proxy: listen tcp4 0.0.0.0:8000: bind: address already in use
```

**Solution:**

**Option 1: Find and stop conflicting process**
```bash
# Find what's using port 8000
sudo lsof -i :8000
# or
sudo netstat -tulpn | grep :8000

# Kill the process
sudo kill -9 <PID>
```

**Option 2: Change FaultMaven port**
```bash
# Edit .env
API_PORT=8080

# Restart
docker compose down
docker compose up -d
```

---

## Service Startup Issues

### Services Not Starting

**Symptom:**
Services exit immediately after starting

**Diagnosis:**
```bash
# Check service logs
docker compose logs gateway
docker compose logs case-service

# Check service status
docker compose ps

# Check resource usage
docker stats
```

**Common Causes:**

**1. Missing Environment Variables**
```bash
# Check .env file exists
ls -la .env

# Verify required variables
grep -E "OPENAI_API_KEY|ANTHROPIC_API_KEY|FIREWORKS_API_KEY" .env
grep JWT_SECRET .env

# If missing, add them
cp .env.example .env
nano .env
```

**2. Invalid Configuration**
```bash
# Validate docker-compose.yml
docker compose config

# If errors, fix syntax issues
```

**3. Out of Memory**
```bash
# Check available memory
free -h

# If low, increase swap or add more RAM
# Temporary: restart Docker
sudo systemctl restart docker
```

### Gateway Returns 502 Bad Gateway

**Symptom:**
Gateway is running but returns 502 for all requests

**Solution:**
```bash
# Check if backend services are running
docker compose ps

# Check backend service health
curl http://localhost:8001/health  # Auth
curl http://localhost:8003/health  # Case
curl http://localhost:8004/health  # Knowledge

# Check gateway logs
docker compose logs -f gateway

# Restart services
docker compose restart
```

---

## API Connection Issues

### Cannot Connect to API

**Symptom:**
Extension or dashboard shows "Cannot connect to API"

**Diagnosis:**
```bash
# Test API health
curl http://localhost:8000/health

# Test from external network
curl http://YOUR_SERVER_IP:8000/health

# Check firewall
sudo ufw status

# Check nginx (if using)
sudo nginx -t
sudo systemctl status nginx
```

**Solutions:**

**1. API Not Responding**
```bash
# Check if gateway is running
docker compose ps gateway

# Check gateway logs
docker compose logs gateway

# Restart gateway
docker compose restart gateway
```

**2. Firewall Blocking**
```bash
# Allow API port
sudo ufw allow 8000/tcp

# If using nginx, allow HTTP/HTTPS
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
```

**3. CORS Issues**
```bash
# Check browser console for CORS errors
# If present, verify gateway CORS configuration

# In gateway service config, ensure CORS is enabled for your domain
```

### SSL Certificate Errors

**Symptom:**
Browser shows "Your connection is not private"

**Solution:**

**For Self-Signed Certificates (Dev Only):**
```bash
# Browser: Click "Advanced" → "Proceed to site"
```

**For Let's Encrypt:**
```bash
# Verify certificate
sudo certbot certificates

# Renew if expired
sudo certbot renew

# Restart nginx
sudo systemctl reload nginx
```

---

## Authentication Issues

### Login Fails with 401 Unauthorized

**Symptom:**
Correct credentials return 401

**Diagnosis:**
```bash
# Check auth service
docker compose logs auth-service

# Test auth endpoint directly
curl -X POST http://localhost:8000/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","password":"test123"}'
```

**Solutions:**

**1. User Doesn't Exist**
```bash
# Register user first
curl -X POST http://localhost:8000/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "email":"test@example.com",
    "password":"test123",
    "full_name":"Test User"
  }'
```

**2. Database Issues**
```bash
# Check if auth database exists
docker compose exec auth-service ls -la /app/data/

# Reset database (WARNING: deletes all users)
docker compose down
docker volume rm faultmaven-deploy_auth-data
docker compose up -d
```

### JWT Token Expired

**Symptom:**
"Token expired" or 401 after successful login

**Solution:**
```bash
# Check JWT_EXPIRE_MINUTES in .env
grep JWT_EXPIRE_MINUTES .env

# Increase if too short (default: 60 minutes)
JWT_EXPIRE_MINUTES=1440  # 24 hours

# Restart services
docker compose restart
```

### Invalid JWT Secret

**Symptom:**
"Invalid signature" errors

**Solution:**
```bash
# Ensure JWT_SECRET is set and consistent
grep JWT_SECRET .env

# If missing or changed, set it
JWT_SECRET=$(openssl rand -hex 32)
echo "JWT_SECRET=$JWT_SECRET" >> .env

# Restart all services
docker compose down
docker compose up -d

# NOTE: This invalidates all existing tokens
# Users will need to log in again
```

---

## Performance Issues

### Slow API Responses

**Diagnosis:**
```bash
# Check resource usage
docker stats

# Check disk I/O
iostat -x 1

# Check logs for errors
docker compose logs -f | grep -i error
```

**Solutions:**

**1. High CPU Usage**
```bash
# Identify resource-hungry service
docker stats

# Scale horizontally (if applicable)
docker compose up -d --scale agent-service=3

# Increase CPU limits
# Edit docker-compose.yml
services:
  agent-service:
    cpus: 2.0
```

**2. Memory Issues**
```bash
# Check memory usage
free -h

# Increase container memory limits
services:
  knowledge-service:
    mem_limit: 4g
```

**3. Slow LLM Responses**
```bash
# Check LLM API status
# OpenAI: https://status.openai.com
# Anthropic: https://status.anthropic.com

# Try switching providers in .env
OPENAI_API_KEY=  # Comment out
ANTHROPIC_API_KEY=sk-ant-your-key  # Use instead

# Restart
docker compose restart agent-service
```

### Dashboard Slow to Load

**Solutions:**

**1. Clear Browser Cache**
```bash
# In browser: Ctrl+Shift+Delete → Clear cache
```

**2. Optimize nginx**
```nginx
# /etc/nginx/sites-available/faultmaven

# Add caching
location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|woff|woff2|ttf)$ {
    expires 1y;
    add_header Cache-Control "public, immutable";
}

# Enable gzip
gzip on;
gzip_types text/plain text/css application/json application/javascript;
```

**3. Check Dashboard Service**
```bash
# Verify dashboard is running
docker compose ps dashboard

# Check logs
docker compose logs dashboard

# Restart
docker compose restart dashboard
```

---

## Database Issues

### Database Locked

**Symptom:**
```
database is locked
```

**Solution:**
```bash
# SQLite can lock under high concurrency
# Restart the affected service
docker compose restart case-service

# For production, consider migrating to PostgreSQL
```

### Database Corruption

**Symptom:**
```
database disk image is malformed
```

**Solution:**

**If you have backups:**
```bash
# Stop services
docker compose down

# Restore from backup (see DEPLOYMENT.md)
docker run --rm \
  -v faultmaven-deploy_case-data:/data \
  -v /opt/faultmaven/backups:/backup \
  alpine tar xzf /backup/cases_LATEST.tar.gz -C /data

# Restart
docker compose up -d
```

**If no backups:**
```bash
# Try SQLite recovery
docker compose exec case-service sqlite3 /app/data/cases.db ".recover" > recovered.sql

# Create new database and import
docker compose exec case-service sqlite3 /app/data/cases_new.db < recovered.sql

# Replace old database
docker compose exec case-service mv /app/data/cases.db /app/data/cases.db.bak
docker compose exec case-service mv /app/data/cases_new.db /app/data/cases.db

# Restart
docker compose restart case-service
```

---

## Knowledge Base Issues

### Documents Not Searchable

**Symptom:**
Uploaded documents don't appear in search results

**Diagnosis:**
```bash
# Check knowledge service logs
docker compose logs knowledge-service

# Check job worker (processes uploads)
docker compose logs job-worker

# Check ChromaDB
curl http://localhost:8000/v1/knowledge/collections
```

**Solutions:**

**1. Job Worker Not Running**
```bash
# Check if job worker is running
docker compose ps job-worker

# Start if stopped
docker compose up -d job-worker

# Check logs
docker compose logs -f job-worker
```

**2. ChromaDB Connection Issues**
```bash
# Check if ChromaDB is running
docker compose ps chromadb

# Test connection
curl http://localhost:8000/health
# Should show ChromaDB status

# Restart ChromaDB
docker compose restart chromadb knowledge-service
```

**3. Document Processing Failed**
```bash
# Check job worker logs for errors
docker compose logs job-worker | grep -i error

# Re-upload document
# Or manually trigger re-processing (if endpoint exists)
```

### Search Returns No Results

**Solutions:**

**1. No Documents Uploaded**
```bash
# Verify documents exist
curl -H "Authorization: Bearer YOUR_TOKEN" \
  http://localhost:8000/v1/knowledge/documents
```

**2. Search Query Too Specific**
```bash
# Try broader query
# Instead of: "how to fix error XYZ123"
# Try: "error handling"
```

**3. Embedding Model Issues**
```bash
# Check knowledge service can load embedding model
docker compose logs knowledge-service | grep -i embedding

# May need to pull model on first run
# Check disk space
df -h
```

---

## Browser Extension Issues

### Extension Won't Connect

**Symptom:**
Extension shows "Disconnected" or "Cannot connect"

**Solutions:**

**1. Check API URL**
```
# In extension settings:
# Self-hosted: http://localhost:8000
# Production: https://faultmaven.yourdomain.com
```

**2. CORS Issues**
```bash
# Check browser console (F12) for CORS errors
# If present, verify gateway CORS config allows extension origin
```

**3. Certificate Issues (HTTPS)**
```bash
# If using self-signed cert, browser may block
# Solution: Visit API URL directly and accept certificate
# Then reload extension
```

### Extension Not Appearing

**Solutions:**

**1. Chrome**
```
1. Go to chrome://extensions
2. Enable "Developer mode"
3. Check if FaultMaven extension is enabled
4. If not listed, click "Load unpacked" and select extension folder
```

**2. Firefox**
```
1. Go to about:debugging#/runtime/this-firefox
2. Check if FaultMaven extension is loaded
3. If not, click "Load Temporary Add-on"
```

---

## Docker Issues

### Disk Space Full

**Symptom:**
```
no space left on device
```

**Solution:**
```bash
# Check disk usage
df -h

# Clean Docker system
docker system prune -a -f

# Remove old images
docker image prune -a -f

# Remove unused volumes (CAREFUL!)
docker volume prune -f

# Check volume sizes
docker system df -v
```

### Images Won't Pull

**Symptom:**
```
error pulling image configuration: download failed
```

**Solutions:**

**1. Network Issues**
```bash
# Test connectivity
ping docker.io

# Try again
docker compose pull
```

**2. Docker Hub Rate Limits**
```bash
# Login to Docker Hub for higher limits
docker login

# Then retry
docker compose pull
```

**3. Use Mirror/Cache**
```bash
# Configure Docker registry mirror
# Edit /etc/docker/daemon.json
{
  "registry-mirrors": ["https://your-mirror.com"]
}

# Restart Docker
sudo systemctl restart docker
```

---

## Network Issues

### Services Can't Communicate

**Symptom:**
Gateway can't reach backend services

**Diagnosis:**
```bash
# Check Docker network
docker network ls

# Inspect network
docker network inspect faultmaven-deploy_default

# Check if services are on same network
docker compose ps
```

**Solution:**
```bash
# Recreate network
docker compose down
docker network rm faultmaven-deploy_default
docker compose up -d
```

### DNS Resolution Issues

**Symptom:**
```
could not resolve host
```

**Solution:**
```bash
# Check Docker DNS
docker compose exec gateway cat /etc/resolv.conf

# Restart Docker daemon
sudo systemctl restart docker

# Or specify DNS in docker-compose.yml
services:
  gateway:
    dns:
      - 8.8.8.8
      - 8.8.4.4
```

---

## Getting More Help

If you can't resolve your issue:

### 1. Collect Diagnostic Information

```bash
# System info
uname -a
docker version
docker compose version

# Service status
docker compose ps

# Recent logs
docker compose logs --tail=100 > faultmaven-logs.txt

# Resource usage
docker stats --no-stream > docker-stats.txt

# Configuration
docker compose config > docker-compose-config.txt
```

### 2. Check Documentation

- [Architecture](ARCHITECTURE.md) - System design
- [API Documentation](API.md) - API reference
- [Development Guide](DEVELOPMENT.md) - Development setup
- [Deployment Guide](DEPLOYMENT.md) - Production deployment

### 3. Search GitHub Issues

https://github.com/FaultMaven/FaultMaven/issues

### 4. Create New Issue

Include:
- FaultMaven version
- Operating system and version
- Docker version
- Exact error messages
- Steps to reproduce
- Diagnostic information (from step 1)

### 5. Community Support

- [GitHub Discussions](https://github.com/FaultMaven/FaultMaven/discussions)
- Stack Overflow (tag: `faultmaven`)

---

**Last Updated:** 2025-11-20
**Version:** 2.0
**Status:** Current
