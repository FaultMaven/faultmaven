---
id: redis-connection-refused
title: "Redis - Connection Refused"
technology: redis
severity: high
tags:
  - redis
  - connection
  - networking
  - firewall
difficulty: beginner
version: "1.0.0"
last_updated: "2025-01-15"
verified_by: "FaultMaven Team"
status: verified
---

# Redis - Connection Refused

> **Purpose**: Resolve connection refused errors when connecting to Redis

## Quick Reference Card

**🔍 Symptoms:**
- Error: `Connection refused` or `ECONNREFUSED`
- Applications cannot connect to Redis
- `redis-cli ping` fails
- Timeout errors when connecting

**⚡ Common Causes:**
1. **Redis not running** (50%) - Redis service stopped or crashed
2. **Firewall blocking** (25%) - Port 6379 blocked by firewall
3. **Wrong host/port** (15%) - Connecting to incorrect address
4. **Bind address configuration** (10%) - Redis only listening on localhost

**🚀 Quick Fix:**
```bash
# Check if Redis is running
redis-cli ping
# Or:
telnet localhost 6379
```

**⏱️ Estimated Resolution Time:** 5-15 minutes

---

## Diagnostic Steps

### Step 1: Verify Redis Service Status

```bash
# Check if Redis is running
sudo systemctl status redis
# Or:
sudo systemctl status redis-server

# Check Redis process
ps aux | grep redis-server

# Try connecting locally
redis-cli ping
```

**Expected output if running:**
```
PONG
```

### Step 2: Check Network Connectivity

```bash
# Test connectivity to Redis port
telnet <redis-host> 6379
nc -zv <redis-host> 6379

# From application server
curl telnet://<redis-host>:6379

# Check listening ports
sudo netstat -tulpn | grep 6379
# Or:
sudo ss -tulpn | grep 6379
```

### Step 3: Check Redis Configuration

```bash
# View bind address
redis-cli CONFIG GET bind

# Check Redis config file
cat /etc/redis/redis.conf | grep -E '^bind|^port'

# Check protected mode
redis-cli CONFIG GET protected-mode
```

### Step 4: Check Firewall Rules

```bash
# Linux firewall (iptables)
sudo iptables -L -n | grep 6379

# UFW
sudo ufw status

# Check cloud provider security groups (AWS/GCP/Azure)
```

---

## Solutions

### Solution 1: Start Redis Service

**When to use:** Redis service is stopped

```bash
# Start Redis
sudo systemctl start redis
# Or:
sudo systemctl start redis-server

# Enable on boot
sudo systemctl enable redis

# Verify status
sudo systemctl status redis

# Test connection
redis-cli ping
```

**Time to resolution:** ~2 minutes

### Solution 2: Configure Bind Address

**When to use:** Redis only listening on 127.0.0.1, need external access

```bash
# Edit Redis config
sudo nano /etc/redis/redis.conf

# Find and modify bind directive:
# bind 127.0.0.1  # Only localhost (default)
bind 0.0.0.0      # All interfaces (use with caution!)
# Or specific IP:
bind 0.0.0.0 ::1 192.168.1.10

# Disable protected mode if binding to 0.0.0.0
protected-mode no

# Restart Redis
sudo systemctl restart redis

# Verify new bind address
redis-cli CONFIG GET bind
```

**⚠️ Security Warning:** Binding to 0.0.0.0 exposes Redis publicly. Always:
- Use firewall rules to restrict access
- Enable authentication (requirepass)
- Use TLS/SSL if possible
- Consider using bind to specific internal IP only

**Time to resolution:** ~5 minutes

### Solution 3: Configure Firewall

**When to use:** Firewall blocking Redis port

```bash
# Allow Redis port in firewall

# UFW (Ubuntu)
sudo ufw allow 6379/tcp
sudo ufw reload

# firewalld (RHEL/CentOS)
sudo firewall-cmd --permanent --add-port=6379/tcp
sudo firewall-cmd --reload

# iptables
sudo iptables -A INPUT -p tcp --dport 6379 -j ACCEPT
sudo iptables-save

# AWS Security Group (CLI)
aws ec2 authorize-security-group-ingress \
  --group-id sg-xxxxx \
  --protocol tcp \
  --port 6379 \
  --cidr 10.0.0.0/16  # Restrict to VPC

# Verify
sudo iptables -L -n | grep 6379
```

**Best practice:** Only allow from specific IPs/networks

**Time to resolution:** ~10 minutes

### Solution 4: Fix Application Configuration

**When to use:** Application using wrong host/port

```bash
# Verify correct Redis connection details
# Check application config
cat /path/to/app/config | grep redis

# Common environment variables
echo $REDIS_HOST
echo $REDIS_PORT

# Update application config
# Example for Node.js:
REDIS_HOST=redis-server.example.com
REDIS_PORT=6379

# Example connection strings:
redis://localhost:6379
redis://redis-server:6379/0
redis://:password@redis-server:6379

# Restart application
sudo systemctl restart myapp
```

**Time to resolution:** ~5 minutes

---

## Root Cause Analysis

**Why connection refused happens:**

1. **Service Not Running:** Redis server not started or crashed
2. **Network Configuration:** Redis bound only to localhost (127.0.0.1)
3. **Firewall Rules:** Port 6379 blocked at network/host level
4. **Wrong Credentials:** Incorrect host/port in application config

**TCP connection process:**
```
Application → DNS resolution → TCP SYN to port 6379
                                        ↓
                              Port open & listening?
                                        ↓
                              Yes → Connection accepted
                              No → Connection refused
```

---

## Prevention

### Immediate Prevention

1. **Enable Redis service on boot**
   ```bash
   sudo systemctl enable redis
   ```

2. **Monitor Redis availability**
   ```bash
   # Simple health check script
   #!/bin/bash
   if ! redis-cli ping > /dev/null 2>&1; then
     echo "Redis down!" | mail -s "Alert" admin@example.com
     sudo systemctl restart redis
   fi
   ```

3. **Use Redis Sentinel for high availability**
   ```bash
   # Setup Sentinel for automatic failover
   ```

### Long-Term Prevention

1. **Implement monitoring**
   ```yaml
   # Prometheus redis_exporter
   # Alert on Redis down
   - alert: RedisDown
     expr: redis_up == 0
     for: 1m
   ```

2. **Use connection pooling in applications**
   ```python
   # Python example
   import redis
   pool = redis.ConnectionPool(
       host='redis-server',
       port=6379,
       max_connections=10,
       socket_keepalive=True
   )
   r = redis.Redis(connection_pool=pool)
   ```

3. **Configure Redis authentication**
   ```bash
   # In redis.conf
   requirepass your-strong-password-here

   # Connect with auth
   redis-cli -a your-strong-password-here ping
   ```

### Best Practices

- ✅ Enable authentication (requirepass)
- ✅ Bind to specific IPs, not 0.0.0.0
- ✅ Use firewall to restrict access
- ✅ Monitor Redis health continuously
- ✅ Use connection pooling in apps
- ✅ Enable Redis on system boot
- ✅ Document connection details
- ❌ Don't expose Redis publicly without auth
- ❌ Don't bind to 0.0.0.0 without firewall
- ❌ Don't skip monitoring

---

## Related Issues

**Related runbooks:**
- [Redis - Out of Memory](redis-out-of-memory.md)

**External resources:**
- [Redis Security](https://redis.io/docs/management/security/)
- [Redis Configuration](https://redis.io/docs/management/config/)

---

## Version History

| Version | Date       | Author           | Changes                  |
|---------|------------|------------------|--------------------------|
| 1.0.0   | 2025-01-15 | FaultMaven Team  | Initial verified version |

---

## License & Attribution

Licensed under Apache-2.0 License. Contributions welcome via Pull Request.
