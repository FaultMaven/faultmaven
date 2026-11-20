"""Only ``utils/token_estimation.py`` reaches tiktoken (#2948).

Its loader hands out an encoder that encodes every text as plain text; a module
reaching a raw encoding would decide the special-token policy again, as five
of six encode calls once did, wrongly. The scan reads code, not comments: an
import of tiktoken or of a name ``tiktoken``, the name or an attribute
``tiktoken``, a read of another object's ``_encoding`` (the wrapper's raw
encoding), and the module's name where a module is looked up by string.
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SOURCE = Path(__file__).resolve().parents[3] / "faultmaven"
LOADER = SOURCE / "utils" / "token_estimation.py"
#: Calls that take a module's name as their first argument.
_LOOKUPS = {"import_module", "__import__", "find_spec"}


def _is_tiktoken(node) -> bool:
    name = node.value if isinstance(node, ast.Constant) else node
    return isinstance(name, str) and name.split(".")[0] == "tiktoken"


def _called(node: ast.Call) -> str | None:
    return getattr(node.func, "attr", None) or getattr(node.func, "id", None)


def _reaches(node: ast.AST) -> bool:
    if isinstance(node, ast.Import):
        return any(_is_tiktoken(alias.name) for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        return _is_tiktoken(node.module) or any(
            alias.name == "tiktoken" for alias in node.names
        )
    if isinstance(node, ast.Name):
        return node.id == "tiktoken"
    if isinstance(node, ast.Attribute):
        own = isinstance(node.value, ast.Name) and node.value.id == "self"
        return node.attr == "tiktoken" or (node.attr == "_encoding" and not own)
    if isinstance(node, ast.Call):
        return (
            _called(node) in _LOOKUPS and bool(node.args) and _is_tiktoken(node.args[0])
        )
    if isinstance(node, ast.Subscript):
        return ast.unparse(node.value) == "sys.modules" and _is_tiktoken(node.slice)
    return False


def _sites(source: str) -> list[int]:
    return [node.lineno for node in ast.walk(ast.parse(source)) if _reaches(node)]


@pytest.mark.parametrize(
    "source",
    [
        "import tiktoken.load",
        "from tiktoken import get_encoding",
        "from faultmaven.utils.token_estimation import tiktoken as tk",
        "token_estimation.tiktoken.get_encoding('cl100k_base')",
        "_get_tiktoken_encoder('gpt-4')._encoding.encode(text)",
        "importlib.import_module('tiktoken')",
        "__import__('tiktoken.core')",
        "importlib.util.find_spec('tiktoken')",
        "sys.modules['tiktoken']",
    ],
)
def test_the_scan_sees_each_way_to_a_raw_encoding(source):
    assert _sites(source) == [1]


@pytest.mark.parametrize(
    "source",
    [
        "config = {'tokenizer': 'tiktoken'}",
        "logging.getLogger('tiktoken')",
        "self._encoding = False",
        '"""tiktoken counts tokens."""  # import tiktoken',
    ],
)
def test_the_scan_passes_text_that_only_mentions_tiktoken(source):
    assert _sites(source) == []


def test_tiktoken_is_reached_only_by_the_shared_loader():
    elsewhere = [
        f"{path.relative_to(SOURCE.parent)}:{line}"
        for path in sorted(SOURCE.rglob("*.py"))
        if path != LOADER
        for text in [path.read_text(encoding="utf-8")]
        if "tiktoken" in text or "_encoding" in text
        for line in _sites(text)
    ]

    assert _sites(LOADER.read_text(encoding="utf-8")), "the scan sees the loader"
    assert elsewhere == [], "load encodings through _get_tiktoken_encoder"
