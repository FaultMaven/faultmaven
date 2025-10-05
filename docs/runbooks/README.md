# FaultMaven Knowledge Base

**📚 [Complete System Documentation](../KNOWLEDGE_BASE_SYSTEM.md)** - Master guide covering structure, sources, contribution workflow, review process, ingestion, maintenance, and tools.

## Overview

This is the official runbook library for FaultMaven, containing verified troubleshooting guides for common infrastructure and application issues.

All runbooks follow a standardized format and have been tested in real environments to ensure accuracy and effectiveness.

**System-Wide Knowledge Base:** These runbooks enhance AI responses for ALL users through automatic RAG retrieval. This is distinct from user-managed personal knowledge bases.

## Core Library (Team-Verified ✅)

These runbooks have been created and verified by the FaultMaven team:

### Kubernetes (4 runbooks)
- [Pod CrashLoopBackOff](kubernetes/k8s-pod-crashloopbackoff.md) - Continuous restart failures
- [Pod OOMKilled](kubernetes/k8s-pod-oomkilled.md) - Memory exhaustion issues
- [Pod ImagePullBackOff](kubernetes/k8s-pod-imagepullbackoff.md) - Container registry problems
- [Node Not Ready](kubernetes/k8s-node-not-ready.md) - Infrastructure node issues

### Redis (2 runbooks)
- [Connection Refused](redis/redis-connection-refused.md) - Redis connectivity issues
- [Out of Memory](redis/redis-out-of-memory.md) - Memory exhaustion and eviction

### PostgreSQL (2 runbooks)
- [Connection Pool Exhausted](postgresql/postgres-connection-pool-exhausted.md) - Connection leak issues
- [Slow Queries](postgresql/postgres-slow-queries.md) - Performance degradation

### Networking (2 runbooks)
- [DNS Resolution Failure](networking/network-dns-resolution-failure.md) - Domain resolution issues
- [Connection Timeout](networking/network-connection-timeout.md) - Generic connectivity problems

## Browse by Category

- [View all Kubernetes runbooks](kubernetes/)
- [View all Redis runbooks](redis/)
- [View all PostgreSQL runbooks](postgresql/)
- [View all Networking runbooks](networking/)

## Contributing

We welcome community contributions! Here's how to contribute:

1. **Read the Guidelines**: Review [CONTRIBUTING.md](CONTRIBUTING.md) for detailed instructions
2. **Use the Template**: Copy [TEMPLATE.md](TEMPLATE.md) as your starting point
3. **Test Your Content**: Verify all commands work in a real environment
4. **Submit a Pull Request**: Follow the PR template checklist

## Quality Standards

All runbooks must:
- Follow the [TEMPLATE.md](TEMPLATE.md) structure exactly
- Include complete YAML frontmatter metadata
- Contain tested and verified commands
- Pass automated validation checks
- Be reviewed and approved by Knowledge Curators

## For Knowledge Curators

If you're reviewing runbook submissions, see [REVIEW_GUIDELINES.md](REVIEW_GUIDELINES.md) for the complete review process.

## Statistics

- **Total Runbooks**: 10
- **Technologies Covered**: 4 (Kubernetes, Redis, PostgreSQL, Networking)
- **Status**: Core library established
- **Last Updated**: 2025-01-15

## License

All runbooks are licensed under the Apache-2.0 License. See the main repository LICENSE file for details.
