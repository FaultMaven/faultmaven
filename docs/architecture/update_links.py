#!/usr/bin/env python3
"""
Update markdown links after architecture docs reorganization.

Handles:
- [text](./path/to/doc.md) - Inline links
- [text](path/to/doc.md) - Inline links without ./
- [text]: path/to/doc.md - Reference links
- Relative path calculation based on new locations
- Broken link detection and reporting
"""

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Tuple

def load_path_mapping(mapping_file: str) -> Dict[str, str]:
    """Load the old path → new path mapping."""
    with open(mapping_file, 'r') as f:
        return json.load(f)

def calculate_relative_path(from_file: str, to_file: str) -> str:
    """
    Calculate relative path from one file to another.

    Args:
        from_file: Source file path (e.g., "investigation-engine/milestone.md")
        to_file: Target file path (e.g., "data-and-storage/case-storage.md")

    Returns:
        Relative path (e.g., "../data-and-storage/case-storage.md")
    """
    from_parts = Path(from_file).parent.parts
    to_parts = Path(to_file).parts

    # Calculate how many levels up we need to go
    common_prefix_len = 0
    for i, (a, b) in enumerate(zip(from_parts, to_parts[:-1])):
        if a == b:
            common_prefix_len = i + 1
        else:
            break

    # Build relative path
    levels_up = len(from_parts) - common_prefix_len
    up_path = '../' * levels_up if levels_up > 0 else ''
    down_path = '/'.join(to_parts[common_prefix_len:])

    return up_path + down_path if up_path or down_path else Path(to_file).name

def find_markdown_files(base_dir: str) -> List[str]:
    """Find all markdown files in the directory tree."""
    md_files = []
    for root, dirs, files in os.walk(base_dir):
        for file in files:
            if file.endswith('.md'):
                rel_path = os.path.relpath(os.path.join(root, file), base_dir)
                md_files.append(rel_path)
    return md_files

def update_links_in_content(content: str, current_file: str, path_mapping: Dict[str, str]) -> Tuple[str, List[str]]:
    """
    Update all markdown links in content.

    Returns:
        Tuple of (updated_content, list_of_warnings)
    """
    warnings = []

    # Pattern for inline links: [text](path)
    inline_pattern = r'\[([^\]]+)\]\((\./)?([^)#]+?\.md)(#[^)]+)?\)'

    # Pattern for reference links: [text]: path
    reference_pattern = r'^(\[[^\]]+\]):\s+(\./)?([^\s#]+?\.md)(#\S+)?$'

    def replace_inline_link(match):
        text = match.group(1)
        dot_slash = match.group(2) or ''
        old_path = match.group(3)
        anchor = match.group(4) or ''

        # Extract just the filename
        filename = Path(old_path).name

        if filename in path_mapping:
            new_path = path_mapping[filename]
            # Calculate relative path from current file to new location
            relative_path = calculate_relative_path(current_file, new_path)
            return f'[{text}]({relative_path}{anchor})'
        elif old_path.startswith('../') or old_path.startswith('./'):
            # Already a relative path, might not need updating
            return match.group(0)
        else:
            warnings.append(f"Unknown file reference: {old_path}")
            return match.group(0)

    def replace_reference_link(match):
        label = match.group(1)
        dot_slash = match.group(2) or ''
        old_path = match.group(3)
        anchor = match.group(4) or ''

        filename = Path(old_path).name

        if filename in path_mapping:
            new_path = path_mapping[filename]
            relative_path = calculate_relative_path(current_file, new_path)
            return f'{label}: {relative_path}{anchor}'
        else:
            warnings.append(f"Unknown file reference: {old_path}")
            return match.group(0)

    # Update inline links
    updated_content = re.sub(inline_pattern, replace_inline_link, content)

    # Update reference links
    updated_content = re.sub(reference_pattern, replace_reference_link, updated_content, flags=re.MULTILINE)

    return updated_content, warnings

def main():
    base_dir = '/home/swhouse/product/faultmaven/docs/architecture'
    mapping_file = os.path.join(base_dir, 'path-mapping.json')

    # Load path mapping
    path_mapping = load_path_mapping(mapping_file)

    # Find all markdown files
    md_files = find_markdown_files(base_dir)

    print(f"Found {len(md_files)} markdown files to process")
    print()

    total_updates = 0
    all_warnings = []

    for md_file in md_files:
        if md_file == 'path-mapping.json':
            continue

        full_path = os.path.join(base_dir, md_file)

        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                content = f.read()

            updated_content, warnings = update_links_in_content(content, md_file, path_mapping)

            if updated_content != content:
                with open(full_path, 'w', encoding='utf-8') as f:
                    f.write(updated_content)
                total_updates += 1
                print(f"✓ Updated: {md_file}")

            if warnings:
                all_warnings.extend([(md_file, w) for w in warnings])

        except Exception as e:
            print(f"✗ Error processing {md_file}: {e}")

    print()
    print(f"Summary: Updated {total_updates} files")

    if all_warnings:
        print()
        print("Warnings:")
        for file, warning in all_warnings:
            print(f"  {file}: {warning}")
    else:
        print("No warnings!")

if __name__ == '__main__':
    main()
