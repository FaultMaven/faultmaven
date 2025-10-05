"""Doctor/Patient prompt system with configurable versions.

Provides three prompt versions optimized for different use cases:
- MINIMAL (~800 tokens): Fast, cost-efficient, simple queries
- STANDARD (~1,300 tokens): Balanced, recommended default
- DETAILED (~1,800 tokens): Maximum guidance, complex cases
"""

from enum import Enum
from typing import Dict

from .minimal import MINIMAL_SYSTEM_PROMPT
from .standard import STANDARD_SYSTEM_PROMPT
from .detailed import DETAILED_SYSTEM_PROMPT


class PromptVersion(str, Enum):
    """Available doctor/patient prompt versions"""
    MINIMAL = "minimal"      # ~800 tokens - fast and cheap
    STANDARD = "standard"    # ~1,300 tokens - balanced (default)
    DETAILED = "detailed"    # ~1,800 tokens - comprehensive


# Prompt version registry
PROMPT_VERSIONS: Dict[PromptVersion, str] = {
    PromptVersion.MINIMAL: MINIMAL_SYSTEM_PROMPT,
    PromptVersion.STANDARD: STANDARD_SYSTEM_PROMPT,
    PromptVersion.DETAILED: DETAILED_SYSTEM_PROMPT,
}


def get_doctor_patient_prompt(version: PromptVersion = PromptVersion.STANDARD) -> str:
    """Get doctor/patient system prompt by version.
    
    Args:
        version: Prompt version to use (minimal/standard/detailed)
        
    Returns:
        System prompt template string with placeholders for:
        - {diagnostic_state_context}
        - {conversation_history}
        - {user_query}
        
    Examples:
        >>> prompt = get_doctor_patient_prompt(PromptVersion.STANDARD)
        >>> formatted = prompt.format(
        ...     diagnostic_state_context="Phase 1, problem: API slow",
        ...     conversation_history="User: API is slow\nFaultMaven: ...",
        ...     user_query="Show me the logs"
        ... )
    """
    if version not in PROMPT_VERSIONS:
        raise ValueError(
            f"Unknown prompt version: {version}. "
            f"Must be one of: {', '.join(v.value for v in PromptVersion)}"
        )
    
    return PROMPT_VERSIONS[version]


def get_prompt_token_estimate(version: PromptVersion) -> int:
    """Get estimated token count for prompt version (base prompt only).
    
    Does not include dynamic context (diagnostic state, history, query).
    Add ~750 tokens for typical context overhead.
    
    Args:
        version: Prompt version
        
    Returns:
        Estimated base token count
    """
    estimates = {
        PromptVersion.MINIMAL: 800,
        PromptVersion.STANDARD: 1300,
        PromptVersion.DETAILED: 1800,
    }
    return estimates.get(version, 1300)


__all__ = [
    "PromptVersion",
    "get_doctor_patient_prompt",
    "get_prompt_token_estimate",
    "PROMPT_VERSIONS",
]
