"""Dispatch table from data type to ``EntityExtractor``.

The preprocessor calls ``extract_entities_for_data_type`` after each
successful extraction. Data types without a registered extractor get
an empty list — both METRICS (all-numeric, no meaningful entity
vocabulary) and UNSTRUCTURED_TEXT (unbounded false positives) belong
to this bucket for now.

Extending coverage later — adding ``SOURCE_CODE`` say — is a matter
of building the extractor and wiring it here.
"""

from __future__ import annotations

from faultmaven.models.api import DataType
from faultmaven.modules.preprocessing.entities.command_output import (
    CommandOutputEntityExtractor,
)
from faultmaven.modules.preprocessing.entities.config import ConfigEntityExtractor
from faultmaven.modules.preprocessing.entities.logs import LogsEntityExtractor
from faultmaven.modules.preprocessing.entities.protocol import (
    EntityExtractor,
    EntityObservation,
)
from faultmaven.modules.preprocessing.entities.trace import TraceEntityExtractor

# Lazy-instantiated — extractors are stateless regex holders, but we
# don't need to pay for them at import time.
_EXTRACTORS: dict[DataType, EntityExtractor] = {
    DataType.LOGS_AND_ERRORS: LogsEntityExtractor(),
    DataType.COMMAND_OUTPUT: CommandOutputEntityExtractor(),
    DataType.STRUCTURED_CONFIG: ConfigEntityExtractor(),
    DataType.TRACE_DATA: TraceEntityExtractor(),
}


def extract_entities_for_data_type(
    data_type: DataType,
    content: str,
    error_line_indices: set[int] | None = None,
) -> list[EntityObservation]:
    """Return entity observations for ``content`` parsed as ``data_type``.

    Data types without a registered extractor return ``[]`` — empty is
    a valid, carries-no-harm state for the Phase 4 registry.
    """
    extractor = _EXTRACTORS.get(data_type)
    if extractor is None:
        return []
    return extractor.extract(content, error_line_indices=error_line_indices)
