#!/usr/bin/env python3
"""Generate an ER diagram from SQLAlchemy models.

Produces a Mermaid erDiagram that stays in sync with schema changes.
Run after migrations or model changes to update the diagram.

Usage:
    python scripts/generate_er_diagram.py                # Print to stdout
    python scripts/generate_er_diagram.py --output FILE  # Write to file
    python scripts/generate_er_diagram.py --update        # Update docs in-place
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


def generate_mermaid_er() -> str:
    """Generate Mermaid ER diagram from SQLAlchemy Base metadata."""
    from faultmaven.infrastructure.persistence.models import Base

    lines = ["```mermaid", "erDiagram"]

    # Collect FK relationships for drawing arrows
    relationships = []

    for table_name in sorted(Base.metadata.tables):
        table = Base.metadata.tables[table_name]

        lines.append(f"    {table_name} {{")
        for col in table.columns:
            col_type = str(col.type).split("(")[0]
            markers = ""
            if col.primary_key:
                markers = " PK"
            elif col.foreign_keys:
                markers = " FK"
            lines.append(f"        {col_type} {col.name}{markers}")
        lines.append("    }")

        # Collect relationships
        for fk in table.foreign_keys:
            target = fk.column.table.name
            if target != table_name:  # skip self-refs
                relationships.append((target, table_name))

    # Deduplicate and add relationships
    for parent, child in sorted(set(relationships)):
        lines.append(f'    {parent} ||--o{{ {child} : ""')

    lines.append("```")
    return "\n".join(lines)


def generate_table_summary() -> str:
    """Generate a markdown table summarizing all tables."""
    from faultmaven.infrastructure.persistence.models import Base

    lines = [
        "| Table | Columns | Primary Key | Foreign Keys |",
        "|-------|---------|-------------|--------------|",
    ]

    for table_name in sorted(Base.metadata.tables):
        table = Base.metadata.tables[table_name]
        col_count = len(table.columns)
        pk_cols = [c.name for c in table.columns if c.primary_key]
        fk_targets = sorted(
            {fk.column.table.name for fk in table.foreign_keys}
        )

        pk_str = ", ".join(pk_cols)
        fk_str = ", ".join(fk_targets) if fk_targets else "—"
        lines.append(f"| `{table_name}` | {col_count} | `{pk_str}` | {fk_str} |")

    return "\n".join(lines)


def generate_full_document() -> str:
    """Generate the complete ER diagram markdown document."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    from faultmaven.infrastructure.persistence.models import Base

    table_count = len(Base.metadata.tables)

    return f"""# FaultMaven Database ER Diagram

> **Auto-generated** from SQLAlchemy models on {timestamp}.
> Do not edit manually — run `python scripts/generate_er_diagram.py --update` to regenerate.
> Render with any Mermaid-compatible viewer (GitHub, VS Code, Mermaid Live Editor).

## Summary

**{table_count} tables** in the schema.

{generate_table_summary()}

## ER Diagram

{generate_mermaid_er()}
"""


def main():
    parser = argparse.ArgumentParser(
        description="Generate ER diagram from SQLAlchemy models."
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        help="Output file path (default: stdout)",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Update docs/architecture/data-and-storage/er-diagram.md in-place",
    )
    parser.add_argument(
        "--mermaid-only",
        action="store_true",
        help="Output only the Mermaid diagram (no markdown wrapper)",
    )
    args = parser.parse_args()

    if args.mermaid_only:
        content = generate_mermaid_er()
    else:
        content = generate_full_document()

    if args.update:
        output_path = (
            Path(__file__).parent.parent
            / "docs"
            / "architecture"
            / "data-and-storage"
            / "er-diagram.md"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content)
        print(f"Updated: {output_path}")
    elif args.output:
        Path(args.output).write_text(content)
        print(f"Written to: {args.output}")
    else:
        print(content)


if __name__ == "__main__":
    main()
