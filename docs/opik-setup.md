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

### Targeted Tracing Configuration

FaultMaven supports **targeted tracing** to reduce noise and focus on specific issues. You can enable tracing for specific users, sessions, or operations without restarting the server.

#### Environment Variables

Add these to your `.env` file for targeted tracing:

```bash
# Global tracing control
OPIK_TRACK_DISABLE=false          # Set to 'true' to disable all tracing

# Target specific users (comma-separated)
OPIK_TRACK_USERS=debug_user,qa_user

# Target specific sessions (comma-separated)  
OPIK_TRACK_SESSIONS=session-abc-123,session-def-456

# Target specific operations (comma-separated)
OPIK_TRACK_OPERATIONS=llm_query,agent_run,knowledge_search
```

#### Common Targeting Scenarios

**1. Debug Specific User Issues:**
```bash
OPIK_TRACK_USERS=problematic_user_123
```

**2. Monitor Only Expensive Operations:**
```bash
OPIK_TRACK_OPERATIONS=llm_query,vector_search,agent_reasoning
```

**3. Investigate Session-Specific Problems:**
```bash
OPIK_TRACK_SESSIONS=session_with_errors_xyz
```

**4. Combined Targeting (user AND operation must match):**
```bash
OPIK_TRACK_USERS=debug_user,test_user
OPIK_TRACK_OPERATIONS=troubleshoot,generate_solution
```

**5. Temporarily Disable All Tracing:**
```bash
OPIK_TRACK_DISABLE=true
```

#### Runtime Changes

All targeting can be changed at runtime by updating environment variables:

```bash
# Update targeting without restart
export OPIK_TRACK_USERS=new_target_user
# Changes take effect immediately for new operations
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

### Targeted Tracing Benefits

With targeted tracing configured, you'll see:

- **🎯 Reduced Noise**: Only traces from targeted users/sessions/operations
- **🔍 Focused Debugging**: Clear view of specific user problems
- **⚡ Better Performance**: Less overhead when tracing is limited
- **🚨 Incident Investigation**: Isolate problematic sessions quickly
- **📈 Selective Monitoring**: Track only expensive or critical operations

**Example**: If you set `OPIK_TRACK_USERS=problematic_user_123`, Opik will only show traces for that user, making it easy to debug their specific issues without noise from other users.

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

### Targeted Tracing Issues

If you're not seeing expected traces:

1. **Check targeting configuration**:
   ```bash
   # Verify environment variables are set
   echo $OPIK_TRACK_USERS
   echo $OPIK_TRACK_SESSIONS
   echo $OPIK_TRACK_OPERATIONS
   ```

2. **Verify user/session context**:
   - Ensure the user ID or session ID matches exactly (case-sensitive)
   - Check that the operation names match the traced function names

3. **Test with global disable**:
   ```bash
   # Temporarily disable targeting to see all traces
   export OPIK_TRACK_DISABLE=false
   export OPIK_TRACK_USERS=
   export OPIK_TRACK_SESSIONS=
   export OPIK_TRACK_OPERATIONS=
   ```

4. **Check targeting logic**:
   - If multiple targeting variables are set, ALL must match for tracing to occur
   - Empty targeting variables mean "trace everything"
   - Whitespace in lists is automatically trimmed

5. **Debug targeting decisions**:
   ```bash
   # Enable debug logging to see targeting decisions
   export LOG_LEVEL=DEBUG
   ./run_faultmaven.sh | grep -i "tracing disabled\|tracing enabled"
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