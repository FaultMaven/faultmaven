"""Only ``utils/token_estimation.py`` names tiktoken (#2948).

Its loader hands out an encoder that encodes every text as plain text; a module
importing tiktoken itself would decide the special-token policy again, as five
of six encode calls once did, wrongly. The scan reads code, not comments: an
import, a name, or the module's name as a string (``import_module``).
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SOURCE = Path(__file__).resolve().parents[3] / "faultmaven"
LOADER = SOURCE / "utils" / "token_estimation.py"


def _is_tiktoken(name) -> bool:
    return isinstance(name, str) and name.split(".")[0] == "tiktoken"


def _naming(path: Path) -> list[str]:
    return [
        f"{path.relative_to(SOURCE.parent)}:{node.lineno}"
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if (
            isinstance(node, ast.Import)
            and any(_is_tiktoken(a.name) for a in node.names)
        )
        or (isinstance(node, ast.ImportFrom) and _is_tiktoken(node.module))
        or (isinstance(node, ast.Name) and node.id == "tiktoken")
        or (
            isinstance(node, ast.Constant)
            and _is_tiktoken(node.value)
            and " " not in node.value
        )
    ]


def test_tiktoken_is_named_only_by_the_shared_loader():
    elsewhere = [
        site
        for path in sorted(SOURCE.rglob("*.py"))
        if path != LOADER and "tiktoken" in path.read_text(encoding="utf-8")
        for site in _naming(path)
    ]

    assert _naming(LOADER), "the scan has to see the loader's own import"
    assert elsewhere == [], "load encodings through _get_tiktoken_encoder"
