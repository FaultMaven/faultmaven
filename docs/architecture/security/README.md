# Security

Documentation for FaultMaven's authentication, authorization, and security architecture.

## Documents

- **[IAM Design](./iam-design.md)** - Identity and Access Management: authentication, authorization, RBAC, and token handling
- **[RBAC Design](./rbac.md)** - Role definitions, permission model, protected endpoints, and admin access control
- **[Case-Scoped PII Redaction](./case-scoped-pii-redaction.md)** - Case-scoped bidirectional PII redaction at the LLM boundary
- **[Break-Glass Content Access](./break-glass-content-access.md)** - How a platform operator reaches a Cloud tenant's case content: the grant model, the audit trail, and why the multi-tenant read rebinds RLS rather than bypassing it (ADR-012 D8/D9)

## Purpose

This section covers FaultMaven's security architecture, including user authentication, session security, authorization patterns, and data protection strategies.

## Related Documentation

- **Operations Security**: See [operations/security/](../../operations/security/) for API key security and operational security procedures
- **Data Protection**: See data-and-storage/ for encryption and secure storage patterns
