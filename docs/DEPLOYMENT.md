# FaultMaven Production Deployment Guide

This guide covers deploying FaultMaven in production environments, including cloud providers, security hardening, and scaling strategies.

## Prerequisites

### System Requirements

**Minimum (Self-Hosted, < 10 users):**
- 2 CPU cores
- 4GB RAM
- 20GB storage
- Ubuntu 20.04+ or similar Linux distribution

**Recommended (Production, < 100 users):**
- 4 CPU cores
- 8GB RAM
- 100GB storage (SSD recommended)
- Load balancer
- SSL/TLS certificate

**Enterprise (100+ users):**
- Kubernetes cluster
- PostgreSQL database cluster
- S3-compatible object storage
- Redis cluster
- Dedicated monitoring infrastructure

### Software Requirements

- Docker 24.0+
- Docker Compose 2.20+
- SSL/TLS certificate (Let's Encrypt recommended)
- Reverse proxy (nginx, Caddy, or Traefik)

---

## Quick Deployment (Self-Hosted)

### Step 1: Prepare Server

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Install Docker Compose
sudo apt install docker-compose-plugin -y

# Create deployment directory
mkdir -p /opt/faultmaven
cd /opt/faultmaven
```

### Step 2: Clone Deployment Configuration

```bash
git clone https://github.com/FaultMaven/faultmaven-deploy.git .
```

### Step 3: Configure Environment

```bash
# Copy environment template
cp .env.example .env

# Edit configuration
nano .env
```

**Required settings:**
```bash
# LLM Provider (choose ONE)
OPENAI_API_KEY=sk-your-key-here
# OR
ANTHROPIC_API_KEY=sk-ant-your-key-here

# Security
JWT_SECRET=$(openssl rand -hex 32)

# Domain (for production)
DOMAIN=faultmaven.yourdomain.com
```

### Step 4: Set Up SSL/TLS

**Option A: Let's Encrypt (Recommended)**

```bash
# Install Certbot
sudo apt install certbot python3-certbot-nginx -y

# Obtain certificate
sudo certbot certonly --nginx -d faultmaven.yourdomain.com

# Certificates will be at:
# /etc/letsencrypt/live/faultmaven.yourdomain.com/fullchain.pem
# /etc/letsencrypt/live/faultmaven.yourdomain.com/privkey.pem
```

**Option B: Self-Signed (Development Only)**

```bash
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout /etc/ssl/private/faultmaven.key \
  -out /etc/ssl/certs/faultmaven.crt
```

### Step 5: Configure Reverse Proxy

**nginx Configuration:**

```nginx
# /etc/nginx/sites-available/faultmaven

upstream faultmaven_api {
    server 127.0.0.1:8000;
}

upstream faultmaven_dashboard {
    server 127.0.0.1:3000;
}

server {
    listen 80;
    server_name faultmaven.yourdomain.com;
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name faultmaven.yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/faultmaven.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/faultmaven.yourdomain.com/privkey.pem;

    # Security headers
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-XSS-Protection "1; mode=block" always;

    # API Gateway
    location /v1/ {
        proxy_pass http://faultmaven_api;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket support
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        # Timeouts
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }

    # Health checks
    location /health {
        proxy_pass http://faultmaven_api;
        access_log off;
    }

    # Dashboard
    location / {
        proxy_pass http://faultmaven_dashboard;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # File upload size limit
    client_max_body_size 50M;
}
```

Enable the site:
```bash
sudo ln -s /etc/nginx/sites-available/faultmaven /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

### Step 6: Deploy Services

```bash
cd /opt/faultmaven
docker compose up -d
```

### Step 7: Verify Deployment

```bash
# Check service health
curl https://faultmaven.yourdomain.com/health

# Check capabilities endpoint
curl https://faultmaven.yourdomain.com/v1/meta/capabilities

# View logs
docker compose logs -f
```

---

## Cloud Provider Deployment

### AWS Deployment

**Architecture:**
- EC2 instance (t3.medium or larger)
- EBS volume for data persistence
- Application Load Balancer
- Route 53 for DNS
- Certificate Manager for SSL

**CloudFormation Template:**

```yaml
# See: https://github.com/FaultMaven/faultmaven-deploy/tree/main/cloud/aws
```

**Quick Deploy:**
```bash
# Using AWS CLI
aws cloudformation create-stack \
  --stack-name faultmaven \
  --template-body file://cloudformation.yaml \
  --parameters ParameterKey=DomainName,ParameterValue=faultmaven.example.com
```

### Google Cloud Platform

**Architecture:**
- Compute Engine VM (e2-standard-2)
- Persistent disk
- Cloud Load Balancer
- Cloud DNS
- Managed SSL certificates

**Deployment:**
```bash
gcloud compute instances create faultmaven \
  --machine-type=e2-standard-2 \
  --boot-disk-size=100GB \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud \
  --tags=http-server,https-server
```

### DigitalOcean

**One-Click Deployment:**
1. Use Docker Droplet (Ubuntu)
2. Add managed database (PostgreSQL) - optional
3. Add load balancer
4. Configure domain and SSL

**Manual:**
```bash
# Create droplet
doctl compute droplet create faultmaven \
  --size s-2vcpu-4gb \
  --image ubuntu-22-04-x64 \
  --region nyc1

# SSH and deploy
ssh root@your-droplet-ip
# Follow Quick Deployment steps above
```

---

## Security Hardening

### 1. Firewall Configuration

```bash
# UFW (Ubuntu)
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp    # SSH
sudo ufw allow 80/tcp    # HTTP
sudo ufw allow 443/tcp   # HTTPS
sudo ufw enable

# Block direct access to Docker ports
sudo ufw deny 8000/tcp
sudo ufw deny 3000/tcp
```

### 2. Strong JWT Secret

```bash
# Generate cryptographically secure secret
openssl rand -hex 32 > .jwt_secret
JWT_SECRET=$(cat .jwt_secret)

# Add to .env
echo "JWT_SECRET=$JWT_SECRET" >> .env

# Secure the file
chmod 600 .env .jwt_secret
```

### 3. Rate Limiting (nginx)

```nginx
# /etc/nginx/nginx.conf

http {
    # Rate limiting zones
    limit_req_zone $binary_remote_addr zone=api_limit:10m rate=10r/s;
    limit_req_zone $binary_remote_addr zone=login_limit:10m rate=5r/m;

    # In server block
    location /v1/auth/login {
        limit_req zone=login_limit burst=3 nodelay;
        proxy_pass http://faultmaven_api;
    }

    location /v1/ {
        limit_req zone=api_limit burst=20 nodelay;
        proxy_pass http://faultmaven_api;
    }
}
```

### 4. Fail2Ban Configuration

```bash
# Install Fail2Ban
sudo apt install fail2ban -y

# Create filter
sudo cat > /etc/fail2ban/filter.d/faultmaven.conf <<EOF
[Definition]
failregex = ^<HOST> .* "POST /v1/auth/login HTTP/.*" 401
            ^<HOST> .* "POST /v1/auth/login HTTP/.*" 403
ignoreregex =
EOF

# Create jail
sudo cat > /etc/fail2ban/jail.d/faultmaven.conf <<EOF
[faultmaven]
enabled = true
port = http,https
filter = faultmaven
logpath = /var/log/nginx/access.log
maxretry = 5
bantime = 3600
findtime = 600
EOF

# Restart Fail2Ban
sudo systemctl restart fail2ban
```

### 5. Database Encryption

```bash
# Enable encryption for SQLite databases
# In .env
DB_ENCRYPTION_KEY=$(openssl rand -hex 32)
```

---

## Backup Strategy

### Automated Backups

**Create backup script:**

```bash
#!/bin/bash
# /opt/faultmaven/backup.sh

BACKUP_DIR="/opt/faultmaven/backups"
DATE=$(date +%Y%m%d_%H%M%S)
RETENTION_DAYS=30

# Create backup directory
mkdir -p $BACKUP_DIR

# Backup Docker volumes
docker run --rm \
  -v faultmaven-deploy_case-data:/data \
  -v $BACKUP_DIR:/backup \
  alpine tar czf /backup/cases_$DATE.tar.gz -C /data .

docker run --rm \
  -v faultmaven-deploy_knowledge-data:/data \
  -v $BACKUP_DIR:/backup \
  alpine tar czf /backup/knowledge_$DATE.tar.gz -C /data .

docker run --rm \
  -v faultmaven-deploy_auth-data:/data \
  -v $BACKUP_DIR:/backup \
  alpine tar czf /backup/auth_$DATE.tar.gz -C /data .

# Backup Redis
docker exec fm-redis redis-cli SAVE
docker cp fm-redis:/data/dump.rdb $BACKUP_DIR/redis_$DATE.rdb

# Backup .env
cp .env $BACKUP_DIR/env_$DATE.backup

# Remove old backups
find $BACKUP_DIR -name "*.tar.gz" -mtime +$RETENTION_DAYS -delete
find $BACKUP_DIR -name "*.rdb" -mtime +$RETENTION_DAYS -delete

# Optional: Upload to S3
# aws s3 sync $BACKUP_DIR s3://your-backup-bucket/faultmaven/

echo "Backup completed: $DATE"
```

**Schedule with cron:**

```bash
# Make executable
chmod +x /opt/faultmaven/backup.sh

# Add to crontab (daily at 2 AM)
(crontab -l 2>/dev/null; echo "0 2 * * * /opt/faultmaven/backup.sh") | crontab -
```

### Restore from Backup

```bash
# Stop services
docker compose down

# Restore volume (example: cases)
docker run --rm \
  -v faultmaven-deploy_case-data:/data \
  -v /opt/faultmaven/backups:/backup \
  alpine tar xzf /backup/cases_20251120_020000.tar.gz -C /data

# Restore Redis
docker cp /opt/faultmaven/backups/redis_20251120_020000.rdb fm-redis:/data/dump.rdb

# Restart services
docker compose up -d
```

---

## Monitoring

### Health Check Monitoring

**Using cron + curl:**

```bash
# /opt/faultmaven/healthcheck.sh
#!/bin/bash

HEALTH_URL="https://faultmaven.yourdomain.com/health"
ALERT_EMAIL="admin@yourdomain.com"

if ! curl -sf $HEALTH_URL > /dev/null; then
    echo "FaultMaven health check failed at $(date)" | \
    mail -s "ALERT: FaultMaven Down" $ALERT_EMAIL
fi
```

### Prometheus + Grafana (Advanced)

```yaml
# docker-compose.monitoring.yml
version: '3.8'

services:
  prometheus:
    image: prom/prometheus:latest
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
      - prometheus-data:/prometheus
    ports:
      - "9090:9090"

  grafana:
    image: grafana/grafana:latest
    volumes:
      - grafana-data:/var/lib/grafana
    ports:
      - "3001:3000"
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=your_secure_password

volumes:
  prometheus-data:
  grafana-data:
```

---

## Scaling

### Horizontal Scaling

**Scale specific services:**

```bash
# Scale agent service to 3 instances
docker compose up -d --scale agent-service=3

# Behind a load balancer
# Update nginx upstream:
upstream faultmaven_api {
    least_conn;
    server 127.0.0.1:8000;
    server 127.0.0.1:8001;
    server 127.0.0.1:8002;
}
```

### Database Migration (PostgreSQL)

For > 100 users, migrate to PostgreSQL:

```bash
# Update .env
DB_TYPE=postgresql
DB_HOST=your-postgres-host
DB_PORT=5432
DB_NAME=faultmaven
DB_USER=faultmaven
DB_PASSWORD=secure_password

# Run migration
docker compose exec case-service python -m alembic upgrade head
```

---

## Maintenance

### Update to Latest Version

```bash
cd /opt/faultmaven

# Pull latest images
docker compose pull

# Restart with new images
docker compose up -d

# Check logs
docker compose logs -f
```

### Log Rotation

```bash
# /etc/logrotate.d/faultmaven
/var/lib/docker/containers/*/*.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    sharedscripts
    postrotate
        docker kill -s HUP $(docker ps -q) 2>/dev/null || true
    endscript
}
```

---

## Troubleshooting

### Services Won't Start

```bash
# Check logs
docker compose logs

# Check disk space
df -h

# Check memory
free -h

# Restart everything
docker compose down
docker compose up -d
```

### SSL Certificate Issues

```bash
# Test certificate
sudo certbot renew --dry-run

# Force renewal
sudo certbot renew --force-renewal

# Check nginx configuration
sudo nginx -t
```

### Performance Issues

```bash
# Check resource usage
docker stats

# Increase memory limits (docker-compose.yml)
services:
  agent-service:
    mem_limit: 2g
    cpus: 2
```

---

## Production Checklist

Before going live:

- [ ] SSL/TLS certificate configured
- [ ] Firewall rules applied
- [ ] Strong JWT secret generated
- [ ] Rate limiting enabled
- [ ] Fail2Ban configured
- [ ] Automated backups scheduled
- [ ] Health check monitoring enabled
- [ ] Log rotation configured
- [ ] DNS records configured
- [ ] Load testing performed
- [ ] Disaster recovery plan documented
- [ ] Security audit completed

---

**Last Updated:** 2025-11-20
**Version:** 2.0
**Status:** Current
