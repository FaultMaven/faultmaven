# Built-in Runbooks — source of truth

These markdown files are the **authoritative source** for FaultMaven's built-in
troubleshooting runbooks. They live here, in the public repository, on purpose:

- **Transparency** — anyone can read exactly what the platform's built-in
  knowledge contains, and its full edit history.
- **Community contribution** — improvements and new runbooks are welcome as pull
  requests against this directory.

## Layout

```
resources/knowledge/runbooks/<domain>/<runbook>.md
```

`<domain>` is one of: `application`, `compute`, `database`, `messaging`,
`networking`, `security`, `storage`. Each file needs YAML frontmatter — `id` and
`title` are mandatory (the Knowledge Base item id is derived from `id`):

```markdown
---
id: my-runbook
title: "My Runbook"
domain: database
service: postgresql
scope: global
tags: [postgres, latency]
---

## Problem Definition
...
```

See [runbook-content-architecture.md](../../../docs/architecture/knowledge-and-ai/runbook-content-architecture.md)
for the full template, taxonomy, and quality bar.

## How a runbook reaches the running KB

These `.md` files are **not** ingested directly. They are compiled into a **KB
pack** (runbooks + pre-computed embeddings) that the app ingests at startup. So
contributing is two steps:

1. **You:** open a PR adding/editing a `.md` here.
2. **A maintainer:** rebuilds the pack with the [KB Toolkit](../../../docs/architecture/knowledge-and-ai/kb-pack-architecture.md)
   (`kb-build-pack`) and commits the refreshed `resources/knowledge/pack/`. CI
   (`scripts/check_kb_pack.py`) guards that the shipped pack matches these sources.

> Do **not** hand-edit `resources/knowledge/pack/` — it is generated from the
> files here and will be overwritten by the next build. Edit runbooks **here**.

For the pack format, build, and delivery, see
[kb-pack-architecture.md](../../../docs/architecture/knowledge-and-ai/kb-pack-architecture.md).
