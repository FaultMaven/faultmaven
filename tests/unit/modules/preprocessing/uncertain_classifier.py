"""A real ``DataClassifier`` forced to an uncertain verdict (#2355).

Forced rather than coaxed, so a test's outcome does not hang on which content
the rules happen to be unsure about.
"""

from __future__ import annotations

from faultmaven.models.api import DataType
from faultmaven.modules.preprocessing.classifier import DataClassifier


def uncertain_classifier(guess: DataType | None = None) -> DataClassifier:
    """Every verdict below the threshold, optionally with a given guess."""
    real = DataClassifier()

    class _Uncertain(DataClassifier):
        def classify(self, *args, **kwargs):
            result = real.classify(*args, **kwargs)
            result.classification_failed = True
            result.confidence = 0.40
            result.suggested_types = [
                DataType.LOGS_AND_ERRORS,
                DataType.UNSTRUCTURED_TEXT,
            ]
            if guess is not None:
                result.data_type = guess
            return result

    return _Uncertain()
