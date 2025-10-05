---
id: network-dns-resolution-failure
title: "Networking - DNS Resolution Failure"
technology: networking
severity: high
tags:
  - networking
  - dns
  - resolution
  - connectivity
difficulty: beginner
version: "1.0.0"
last_updated: "2025-01-15"
verified_by: "FaultMaven Team"
status: verified
---

# Networking - DNS Resolution Failure

> **Purpose**: Resolve domain name resolution failures

## Quick Reference Card

**🔍 Symptoms:**
- Cannot resolve domain names
- `nslookup` or `dig` fails
- Applications show "unknown host" errors
- Web browsers cannot load sites

**⚡ Common Causes:**
1. **DNS server unreachable** (40%) - DNS server down or unreachable
2. **Wrong DNS configuration** (30%) - Incorrect /etc/resolv.conf
3. **Network connectivity** (20%) - No route to DNS server
4. **Firewall blocking** (10%) - Port 53 blocked

**🚀 Quick Fix:**
```bash
# Test DNS resolution
nslookup google.com

# Check DNS servers
cat /etc/resolv.conf
```

**⏱️ Estimated Resolution Time:** 5-15 minutes

---

## Diagnostic Steps

### Step 1: Test DNS Resolution

```bash
# Test with nslookup
nslookup google.com

# Test with dig
dig google.com

# Test with host
host google.com

# Test specific DNS server
nslookup google.com 8.8.8.8
dig @8.8.8.8 google.com
```

### Step 2: Check DNS Configuration

```bash
# View current DNS servers
cat /etc/resolv.conf

# Check systemd-resolved (Ubuntu/Debian)
resolvectl status

# Check DNS search domains
cat /etc/resolv.conf | grep search
```

### Step 3: Test Network Connectivity

```bash
# Test connectivity to DNS server
ping 8.8.8.8
ping <your-dns-server>

# Test DNS port (53)
nc -zvu 8.8.8.8 53  # UDP
nc -zv 8.8.8.8 53   # TCP
```

---

## Solutions

### Solution 1: Fix DNS Server Configuration

**When to use:** Wrong or missing DNS servers

```bash
# Temporary fix - edit resolv.conf
sudo nano /etc/resolv.conf

# Add public DNS servers
nameserver 8.8.8.8
nameserver 8.8.4.4
nameserver 1.1.1.1

# For permanent fix on systemd-resolved systems
sudo nano /etc/systemd/resolved.conf
# Add:
[Resolve]
DNS=8.8.8.8 8.8.4.4
FallbackDNS=1.1.1.1

# Restart resolver
sudo systemctl restart systemd-resolved

# Test resolution
nslookup google.com
```

**Time to resolution:** ~5 minutes

### Solution 2: Restart DNS Resolver

**When to use:** DNS service stuck or not responding

```bash
# Restart systemd-resolved
sudo systemctl restart systemd-resolved

# Clear DNS cache
sudo systemd-resolve --flush-caches
# Or:
sudo resolvectl flush-caches

# Restart networking (if needed)
sudo systemctl restart NetworkManager
```

**Time to resolution:** ~2 minutes

### Solution 3: Fix Network Connectivity

**When to use:** Cannot reach DNS servers

```bash
# Check routes
ip route show

# Add route if missing
sudo ip route add default via <gateway-ip>

# Check firewall
sudo iptables -L | grep 53
sudo ufw status

# Allow DNS traffic
sudo ufw allow out 53
```

**Time to resolution:** ~10 minutes

---

## Prevention

### Best Practices

- ✅ Use multiple DNS servers
- ✅ Monitor DNS resolution
- ✅ Use local DNS cache
- ✅ Document DNS configuration
- ❌ Don't rely on single DNS server
- ❌ Don't ignore DNS failures

---

## Related Issues

**Related runbooks:**
- [Networking - Connection Timeout](network-connection-timeout.md)

**External resources:**
- [DNS Best Practices](https://www.ietf.org/rfc/rfc1912.txt)

---

## Version History

| Version | Date       | Author           | Changes                  |
|---------|------------|------------------|--------------------------|
| 1.0.0   | 2025-01-15 | FaultMaven Team  | Initial verified version |

---

## License & Attribution

Licensed under Apache-2.0 License.
