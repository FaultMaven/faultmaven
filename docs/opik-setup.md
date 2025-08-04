# Opik Observability Setup

## Overview

FaultMaven uses [Comet Opik](https://www.comet.com/site/products/opik/) for observability, tracing LLM calls, agent workflows, and performance metrics.

## Hostname Resolution Setup

The team Opik server runs at `opik.faultmaven.local:30080`. You need to ensure this hostname resolves correctly on your system.

### Linux/macOS Setup

Add the hostname to your `/etc/hosts` file:

```bash
# Add this line to /etc/hosts (requires sudo)
sudo echo "192.168.0.111 opik.faultmaven.local" >> /etc/hosts
```

Or edit manually:
```bash
sudo nano /etc/hosts
```

Add this line:
```
192.168.0.111 opik.faultmaven.local
```

### Windows Setup

Edit `C:\Windows\System32\drivers\etc\hosts` as Administrator and add:
```
192.168.0.111 opik.faultmaven.local
```

### Verify Resolution

Test that the hostname resolves:
```bash
# Should return 192.168.0.111
nslookup opik.faultmaven.local

# Should show Opik UI or connection
curl -I http://opik.faultmaven.local:30080
```

## Configuration

### Default Setup (Team Server)

FaultMaven is pre-configured to use the team Opik server. Just ensure hostname resolution works and run:

```bash
cp .env.example .env
# Add your API keys to .env
./run_faultmaven.sh
```

### Custom Opik Server

If you need to connect to a different Opik instance:

```bash
# Create custom config
cp scripts/config/opik_remote.sh.example scripts/config/opik_custom.sh
# Edit with your server details
nano scripts/config/opik_custom.sh

# Use custom server
source scripts/config/opik_custom.sh
./run_faultmaven.sh
```

## What You'll See in Opik

Once connected, Opik will capture:

- **🔍 LLM Router Activity**: Provider selection, fallbacks, response times
- **🤖 Agent Workflows**: Complete troubleshooting flows from triage to solution
- **📊 Performance Metrics**: Token usage, latency, success/failure rates
- **🛡️ Security Operations**: Data sanitization and redaction traces
- **📈 Caching Efficiency**: Cache hit/miss ratios and performance gains

## Accessing the Dashboard

- **URL**: http://opik.faultmaven.local:30080
- **Project**: "FaultMaven Development"
- **Workspace**: "faultmaven-local"

## Troubleshooting

### Connection Issues

If FaultMaven can't connect to Opik:

1. **Check hostname resolution**:
   ```bash
   ping opik.faultmaven.local
   ```

2. **Check port accessibility**:
   ```bash
   telnet opik.faultmaven.local 30080
   ```

3. **Check FaultMaven logs** for Opik-related errors:
   ```bash
   ./run_faultmaven.sh | grep -i opik
   ```

### Opik Service Status

Check if the Opik service is running:
```bash
curl -f http://opik.faultmaven.local:30080/health
```

If you get connection refused or timeout, the Opik service may be down.

## Network Requirements

- **Port**: 30080 must be accessible from your development machine
- **Protocol**: HTTP (not HTTPS)
- **Firewall**: Ensure no blocking of outbound connections to 192.168.0.111:30080