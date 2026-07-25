# Operations

Production operations documentation.

## Sections

| Section | Description |
|---------|-------------|
| [Data & Storage Management](./data-storage-management.md) | Data directory layout, KB runbook management, backup/restore |
| [Monitoring](./monitoring/) | Logging, metrics, and observability |
| [Security](./security/) | Security implementation and policies |
| [Runbooks](./runbooks/) | Troubleshooting guides |

## Monitoring

Logging and observability documentation.

| Document | Description |
|----------|-------------|
| [Architecture](./monitoring/architecture.md) | Logging system design |
| [Configuration](./monitoring/configuration.md) | Logging configuration |
| [Logging Policy](./monitoring/logging-policy.md) | Logging standards |
| [Implementation Guide](./monitoring/implementation-guide.md) | Adding logging |
| [Operations Runbook](./monitoring/operations-runbook.md) | Log management |
| [Developer Guide](./monitoring/developer-guide.md) | Logging for developers |
| [Testing Guide](./monitoring/testing-guide.md) | Testing logging |

## Security

Security implementation guides.

| Document | Description |
|----------|-------------|
| [Role-Based Access Control](./security/role-based-access-control.md) | RBAC implementation |
| [Service Account Credentials](./security/service-account-credentials.md) | Slack-agent auth under `AUTH_MODE=oauth`; minting, rotation, lockout recovery |
| [PII Sanitization](./security/pii-sanitization-configuration.md) | Privacy protection |
| [Client Protection](./security/client-protection.md) | Client-side security |
| [Implementation Guide](./security/implementation-guide.md) | Security implementation |
| [Comprehensive Protection](./security/comprehensive-protection-implementation-guide.md) | Full protection guide |

## Runbooks

Troubleshooting guides for common issues.

| Runbook | Description |
|---------|-------------|
| [PostgreSQL](./runbooks/postgresql/) | Database issues |
| [Redis](./runbooks/redis/) | Cache issues |
| [Networking](./runbooks/networking/) | Connection issues |
