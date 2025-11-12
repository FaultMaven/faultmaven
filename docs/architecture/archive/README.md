# Archived Design Documents

This directory contains deprecated design documents that have been replaced by more recent, consolidated versions.

## Archived Documents

### case-data-model-design.md
**Archived**: 2025-01-09
**Replaced by**: [case-storage-design.md](../case-storage-design.md)
**Reason**: Overly complex fully-normalized design (32 tables). Replaced with pragmatic hybrid approach (10 tables with JSONB for low-cardinality data).

### db-design-specifications.md
**Archived**: 2025-01-09
**Replaced by**: [case-storage-design.md](../case-storage-design.md)
**Reason**: Inconsistent with actual implementation. Consolidated into single authoritative design document with hybrid normalization strategy.

## Current Design Standards

For current database design, refer to:
- **[case-storage-design.md](../case-storage-design.md)** - Authoritative PostgreSQL schema (10-table hybrid design)
- **[db-abstraction-layer-specification.md](../db-abstraction-layer-specification.md)** - Storage adapter architecture
