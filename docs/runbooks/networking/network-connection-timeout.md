---
id: network-connection-timeout
title: "Networking - Connection Timeout"
technology: networking
severity: medium
tags:
  - networking
  - timeout
  - connectivity
  - firewall
difficulty: beginner
version: "1.0.0"
last_updated: "2025-01-15"
verified_by: "FaultMaven Team"
status: verified
---

# Networking - Connection Timeout

> **Purpose**: Diagnose and resolve network connection timeout issues

## Quick Reference Card

**🔍 Symptoms:**
- Connection timeout errors
- Services unreachable
- Intermittent connectivity
- Slow response times

**⚡ Common Causes:**
1. **Firewall blocking** (40%) - Port blocked by firewall
2. **Service not listening** (30%) - Service down or not bound to port
3. **Network routing** (20%) - No route to destination
4. **MTU issues** (10%) - Path MTU discovery problems

**🚀 Quick Fix:**
```bash
# Test connectivity
telnet <host> <port>
nc -zv <host> <port>
```

**⏱️ Estimated Resolution Time:** 10-20 minutes

---

## Diagnostic Steps

### Step 1: Test Basic Connectivity

```bash
# Ping test
ping <host>

# Test specific port
telnet <host> <port>
nc -zv <host> <port>

# Traceroute to see path
traceroute <host>
mtr <host>  # Better than traceroute
```

### Step 2: Check Service Status

```bash
# Check if service is listening
sudo netstat -tulpn | grep <port>
sudo ss -tulpn | grep <port>

# Check listening addresses
sudo lsof -i :<port>
```

### Step 3: Check Firewall

```bash
# Linux firewall
sudo iptables -L -n
sudo ufw status

# Check specific port
sudo iptables -L | grep <port>
```

---

## Solutions

### Solution 1: Allow Port in Firewall

```bash
# UFW
sudo ufw allow <port>/tcp

# iptables
sudo iptables -A INPUT -p tcp --dport <port> -j ACCEPT
sudo iptables-save

# firewalld
sudo firewall-cmd --permanent --add-port=<port>/tcp
sudo firewall-cmd --reload
```

### Solution 2: Restart Service

```bash
# Restart service
sudo systemctl restart <service>

# Check it's listening
sudo netstat -tulpn | grep <port>
```

### Solution 3: Fix Routing

```bash
# Check routes
ip route show

# Add route
sudo ip route add <network> via <gateway>

# Test connectivity
ping <host>
```

---

## Prevention

### Best Practices

- ✅ Monitor service availability
- ✅ Document firewall rules
- ✅ Use connection monitoring
- ✅ Set appropriate timeouts
- ❌ Don't block without documenting
- ❌ Don't ignore timeout alerts

---

## Related Issues

**Related runbooks:**
- [Networking - DNS Resolution Failure](network-dns-resolution-failure.md)

---

## Version History

| Version | Date       | Author           | Changes                  |
|---------|------------|------------------|--------------------------|
| 1.0.0   | 2025-01-15 | FaultMaven Team  | Initial verified version |

---

## License & Attribution

Licensed under Apache-2.0 License.
