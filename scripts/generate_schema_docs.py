#!/usr/bin/env python3
"""Generate canonical schema documentation from SQLAlchemy models.

Emits a markdown block per table with a normalized DDL-style listing
(columns, types, nullability, defaults, primary key, foreign keys with
ON DELETE/UPDATE, unique constraints, check constraints, indexes), plus
a column-inventory summary. Output is dialect-agnostic — type strings
are SQLAlchemy compile output, not raw PG/SQLite DDL.

The intent is that case-schema.md / user-schema.md / knowledge-schema.md
embed the per-domain output verbatim, so the canonical column list
never drifts from the ORM.

Usage:
    python scripts/generate_schema_docs.py                       # All tables, stdout
    python scripts/generate_schema_docs.py --domain user         # User tables only
    python scripts/generate_schema_docs.py --domain case         # Case tables only
    python scripts/generate_schema_docs.py --domain knowledge    # Knowledge tables only
    python scripts/generate_schema_docs.py --domain config       # Config tables only
    python scripts/generate_schema_docs.py --table cases         # Single table
    python scripts/generate_schema_docs.py --output FILE         # Write to file
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


# Domain assignments. Edit this when tables are added or move.
TABLE_DOMAIN = {
    # User domain
    "enterprises": "user",
    "users": "user",
    "organizations": "user",
    "organization_members": "user",
    "teams": "user",
    "team_members": "user",
    "roles": "user",
    "permissions": "user",
    "role_permissions": "user",
    "user_audit_log": "user",
    "oauth_authorization_codes": "user",
    # Case domain
    "cases": "case",
    "case_messages": "case",
    "case_actions": "case",
    "case_tags": "case",
    "case_checkpoints": "case",
    "case_entities": "case",
    "evidence": "case",
    "hypotheses": "case",
    "hypothesis_evidence": "case",
    "solutions": "case",
    "uploaded_files": "case",
    "agent_executions": "case",
    "agent_tool_calls": "case",
    "investigation_sessions": "case",
    "reports": "case",
    # Knowledge domain
    "knowledge_items": "knowledge",
    "knowledge_item_tags": "knowledge",
    "knowledge_tags": "knowledge",
    "knowledge_suggestions": "knowledge",
    "conversion_jobs": "knowledge",
    "conversion_drafts": "knowledge",
    # Config domain
    "llm_config_overrides": "config",
}


def _format_default(col) -> str:
    """Render a column default for display."""
    if col.server_default is not None:
        try:
            return f" DEFAULT {col.server_default.arg}"
        except AttributeError:
            return f" DEFAULT {col.server_default}"
    if col.default is not None and not callable(col.default.arg):
        return f" DEFAULT {col.default.arg!r}"
    return ""


def _format_column(col) -> str:
    """One-line column definition: name TYPE [NOT NULL] [DEFAULT x]."""
    type_str = str(col.type)
    nullable = "" if col.nullable else " NOT NULL"
    default = _format_default(col)
    pk = (
        " PRIMARY KEY"
        if col.primary_key and len(col.table.primary_key.columns) == 1
        else ""
    )
    return f"  {col.name} {type_str}{nullable}{default}{pk}"


def _format_fk(fk) -> str:
    """Foreign key constraint with ON DELETE/UPDATE."""
    target = f"{fk.column.table.name}({fk.column.name})"
    on_delete = f" ON DELETE {fk.ondelete}" if fk.ondelete else ""
    on_update = f" ON UPDATE {fk.onupdate}" if fk.onupdate else ""
    return f"  FOREIGN KEY ({fk.parent.name}) REFERENCES {target}{on_delete}{on_update}"


def _format_table_ddl(table) -> str:
    """Render a CREATE-TABLE-like block for one table."""
    lines = [f"CREATE TABLE {table.name} ("]

    # Columns
    col_lines = [_format_column(c) for c in table.columns]
    lines.extend(c + "," for c in col_lines[:-1])
    lines.append(
        col_lines[-1] + ("," if (table.constraints or table.foreign_keys) else "")
    )

    # Composite primary key (if more than one PK column)
    pk_cols = [c.name for c in table.primary_key.columns]
    if len(pk_cols) > 1:
        lines.append(f"  PRIMARY KEY ({', '.join(pk_cols)}),")

    # Foreign keys
    fk_lines = [
        _format_fk(fk) for fk in sorted(table.foreign_keys, key=lambda f: f.parent.name)
    ]
    lines.extend(line + "," for line in fk_lines)

    # Unique constraints (excluding PK)
    from sqlalchemy import UniqueConstraint, CheckConstraint

    for constraint in table.constraints:
        if (
            isinstance(constraint, UniqueConstraint)
            and constraint.columns is not table.primary_key.columns
        ):
            cols = ", ".join(c.name for c in constraint.columns)
            name = f" CONSTRAINT {constraint.name}" if constraint.name else ""
            lines.append(f" {name} UNIQUE ({cols}),")
        elif isinstance(constraint, CheckConstraint):
            name = f" CONSTRAINT {constraint.name}" if constraint.name else ""
            lines.append(f" {name} CHECK ({constraint.sqltext}),")

    # Strip trailing comma on last line
    if lines[-1].endswith(","):
        lines[-1] = lines[-1][:-1]

    lines.append(");")

    # Indexes (rendered separately, post-CREATE)
    if table.indexes:
        lines.append("")
        for idx in sorted(table.indexes, key=lambda i: i.name or ""):
            cols = ", ".join(c.name for c in idx.columns)
            unique = "UNIQUE " if idx.unique else ""
            lines.append(f"CREATE {unique}INDEX {idx.name} ON {table.name} ({cols});")

    return "\n".join(lines)


def _format_column_inventory(table) -> str:
    """Render a one-row-per-column markdown table for quick column lookup."""
    lines = [
        "| Column | Type | Nullable | Default | PK | FK |",
        "|--------|------|----------|---------|----|----|",
    ]
    for col in table.columns:
        type_str = f"`{str(col.type)}`"
        nullable = "yes" if col.nullable else "no"
        default = ""
        if col.server_default is not None:
            try:
                default = f"`{col.server_default.arg}`"
            except AttributeError:
                default = f"`{col.server_default}`"
        pk = "✓" if col.primary_key else ""
        fks = sorted(
            {f"{fk.column.table.name}.{fk.column.name}" for fk in col.foreign_keys}
        )
        fk = ", ".join(f"`{t}`" for t in fks) if fks else ""
        lines.append(
            f"| `{col.name}` | {type_str} | {nullable} | {default} | {pk} | {fk} |"
        )
    return "\n".join(lines)


def _render_table(table) -> str:
    """Markdown block for one table: header, DDL, inventory."""
    domain = TABLE_DOMAIN.get(table.name, "?")
    return f"""### `{table.name}` ({domain} domain)

```sql
{_format_table_ddl(table)}
```

**Column inventory**

{_format_column_inventory(table)}
"""


def generate(domain: str | None = None, table_name: str | None = None) -> str:
    """Emit schema docs filtered by domain or single-table."""
    from faultmaven.infrastructure.persistence.models import Base

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if table_name:
        table = Base.metadata.tables.get(table_name)
        if table is None:
            available = ", ".join(sorted(Base.metadata.tables))
            raise SystemExit(f"Unknown table: {table_name}. Available: {available}")
        return f"# Schema for `{table_name}`\n\n*Auto-generated {timestamp}*\n\n{_render_table(table)}"

    tables_to_render = []
    for name in sorted(Base.metadata.tables):
        if domain is None or TABLE_DOMAIN.get(name) == domain:
            tables_to_render.append(Base.metadata.tables[name])

    if not tables_to_render:
        valid_domains = sorted(set(TABLE_DOMAIN.values()))
        raise SystemExit(f"No tables match. Valid domains: {valid_domains}")

    title = f"# Schema — {domain} domain" if domain else "# Schema — all domains"
    body = "\n\n---\n\n".join(_render_table(t) for t in tables_to_render)
    return (
        f"{title}\n\n"
        f"*Auto-generated by `scripts/generate_schema_docs.py` on {timestamp}.*\n"
        f"*Do not edit manually — regenerate after schema changes.*\n\n"
        f"**{len(tables_to_render)} tables.**\n\n"
        f"---\n\n{body}\n"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Generate canonical schema docs from SQLAlchemy models."
    )
    parser.add_argument(
        "--domain",
        choices=["user", "case", "knowledge", "config"],
        help="Filter to one domain.",
    )
    parser.add_argument(
        "--table",
        help="Render a single table.",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        help="Output file path (default: stdout).",
    )
    args = parser.parse_args()

    if args.domain and args.table:
        raise SystemExit("--domain and --table are mutually exclusive.")

    content = generate(domain=args.domain, table_name=args.table)

    if args.output:
        Path(args.output).write_text(content)
        print(f"Written to: {args.output}")
    else:
        print(content)


if __name__ == "__main__":
    main()
