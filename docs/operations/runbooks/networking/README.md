# Networking Runbooks

Troubleshooting guides for common networking issues.

## Available Runbooks

- [DNS Resolution Failure](network-dns-resolution-failure.md) - Cannot resolve domain names
- [Connection Timeout](network-connection-timeout.md) - Connection timeouts

## Common Diagnostic Commands

```bash
# DNS resolution testing
nslookup <domain>
dig <domain>
host <domain>

# Connectivity testing
ping <host>
telnet <host> <port>
nc -zv <host> <port>

# Route tracing
traceroute <host>
mtr <host>

# Network interface status
ip addr show
ifconfig

# DNS configuration
cat /etc/resolv.conf

# Firewall rules (Linux)
sudo iptables -L -n -v

# Active connections
netstat -tuln
ss -tuln
```

## Prerequisites

Most networking runbooks assume you have:
- Basic networking tools installed (`ping`, `telnet`, `dig`, etc.)
- Root/sudo access for some diagnostics
- Understanding of network fundamentals

## Related Resources

- [Linux Network Administration](https://linux.die.net/man/)
- [TCP/IP Guide](https://www.ietf.org/rfc/)
- [DNS Best Practices](https://www.ietf.org/rfc/rfc1912.txt)
