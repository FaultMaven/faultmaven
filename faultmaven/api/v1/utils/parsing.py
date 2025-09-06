"""API utility functions."""

from typing import List, Optional, Union


def parse_comma_separated_tags(tags_input: Optional[Union[str, List[str]]] = None) -> List[str]:
    """Parse comma-separated tags into a list of strings."""
    if not tags_input:
        return []
    
    if isinstance(tags_input, str):
        return [tag.strip() for tag in tags_input.split(",") if tag.strip()]
    
    if isinstance(tags_input, list):
        return [str(tag).strip() for tag in tags_input if str(tag).strip()]
    
    return []


def parse_comma_separated_strings(input_value: Optional[Union[str, List[str]]] = None) -> List[str]:
    """Generic function to parse comma-separated strings into arrays."""
    return parse_comma_separated_tags(input_value)


def ensure_list_field(field_value: Optional[Union[str, List]] = None) -> List:
    """Ensure a field is always returned as a list."""
    if not field_value:
        return []
    
    if isinstance(field_value, list):
        return field_value
    
    if isinstance(field_value, str):
        return [field_value.strip()] if not ',' in field_value else parse_comma_separated_tags(field_value)
    
    return [str(field_value)]


def normalize_tags_field(tags_value: Optional[Union[str, List[str]]] = None) -> List[str]:
    """Normalize tags field to ensure API contract compliance (List[str]).
    
    Handles legacy data where tags might be stored as strings instead of arrays.
    Used to prevent frontend errors when API contract is violated.
    
    Args:
        tags_value: Tags value that could be string, list, or None
        
    Returns:
        List[str]: Always returns a list of strings
    """
    if not tags_value:
        return []
    
    if isinstance(tags_value, str):
        # Parse string tags (comma-separated) into array
        if tags_value.strip():
            return [tag.strip() for tag in tags_value.split(',') if tag.strip()]
        else:
            return []
    
    if isinstance(tags_value, list):
        # Ensure all items are strings and filter out empty ones
        return [str(tag).strip() for tag in tags_value if str(tag).strip()]
    
    # Handle any other type by converting to string and treating as single tag
    tag_str = str(tags_value).strip()
    return [tag_str] if tag_str else []