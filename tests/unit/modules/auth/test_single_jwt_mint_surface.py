"""One module in the process signs JWTs, and this is the guard that says so.

**The set this ranges over:** every ``.py`` file shipped in the ``faultmaven``
package — all of them, not a list someone maintains. **The direction:** it finds
every call to PyJWT's ``encode`` and asserts the set of files making one is
exactly ``{modules/auth/domain/services/jwt_token_generator.py}``. A new module
that starts signing therefore fails without anyone remembering this file exists.

Why a repo-wide walk rather than another tombstone: #853 removed
``AuthService``'s parallel mint and pinned it with a test that names
``AuthService``, so it ranges over one class. It could not see
``UserService._encode_reset_token``, which signed password-reset JWTs with
``auth_service._private_key`` — another service's private attribute — under
``security.jwt_algorithm``. That pairing is not merely untidy: an RSA PEM handed
to an HMAC signer is rejected by PyJWT, so the flow was broken in HS256 mode and
no guard was positioned to notice (#959). A guard that ranges over the callers
that exist today cannot catch the caller added tomorrow.

Detection is AST-based and import-aware, so it survives renaming: ``import jwt as
j; j.encode(...)``, ``from jwt import encode; encode(...)`` and any reference to
PyJWT's ``PyJWT`` class (``jwt.PyJWT().encode(...)``) all count. What it does not
chase — a module rebound at runtime, ``getattr(jwt, "enc" + "ode")``, a signer
imported from a third-party wrapper — are deliberate-evasion shapes, not
plausible accidents; this guard is aimed at the colleague who adds a mint in good
faith, and a reviewer is the control for the rest.

**The boundary is the shipped package, deliberately.**
``scripts/generate_oauth_keys.py:95`` signs a throwaway token to self-test a
freshly generated key pair. It is out of the set because ``scripts/`` is
excluded from the wheel (``pyproject.toml`` ``[tool.setuptools.packages.find]``)
and never copied into the image, so it is not a mint surface any deployment can
reach — it was seen, not missed.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pytest

import faultmaven

#: The one file allowed to sign. Both generator implementations live here, each
#: holding its own key, which is what makes key and algorithm impossible to
#: mismatch.
MINT_CHOKEPOINT = "modules/auth/domain/services/jwt_token_generator.py"

#: Fail-closed floor. The package has ~440 modules; a walk that finds fewer than
#: this has stopped walking, and an empty enumeration would otherwise report
#: "no module signs" while checking nothing.
MINIMUM_FILES_SCANNED = 300

PACKAGE_ROOT = Path(faultmaven.__file__).resolve().parent


class _JWTEncodeFinder(ast.NodeVisitor):
    """Collect ``jwt.encode`` calls, following the names PyJWT is bound to."""

    def __init__(self) -> None:
        # Module aliases: ``import jwt`` / ``import jwt as j``
        self.module_aliases: Set[str] = set()
        # Direct bindings: ``from jwt import encode`` / ``... as sign``
        self.encode_aliases: Set[str] = set()
        # Names PyJWT's own class is bound to. Any mention counts: the class
        # exists to encode and decode, the allowlist is a single file, and a
        # false positive here costs one conversation while a false negative
        # costs the property.
        self.pyjwt_class_aliases: Set[str] = set()
        self.hits: List[int] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "jwt" or alias.name.startswith("jwt."):
                self.module_aliases.add(alias.asname or alias.name.split(".")[0])
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module and (node.module == "jwt" or node.module.startswith("jwt.")):
            for alias in node.names:
                if alias.name == "encode":
                    self.encode_aliases.add(alias.asname or alias.name)
                elif alias.name == "PyJWT":
                    self.pyjwt_class_aliases.add(alias.asname or alias.name)
                elif alias.name in ("api_jws", "api_jwt"):
                    # ``from jwt import api_jwt; api_jwt.encode(...)``
                    self.module_aliases.add(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # ``jwt.PyJWT`` — the encoder class reached through the module.
        if node.attr == "PyJWT" and isinstance(node.value, ast.Name):
            if node.value.id in self.module_aliases:
                self.hits.append(node.lineno)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        # ``PyJWT`` imported directly and used anywhere.
        if node.id in self.pyjwt_class_aliases:
            self.hits.append(node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "encode":
            value = func.value
            if isinstance(value, ast.Name) and value.id in self.module_aliases:
                self.hits.append(node.lineno)
        elif isinstance(func, ast.Name) and func.id in self.encode_aliases:
            self.hits.append(node.lineno)
        self.generic_visit(node)


def find_jwt_encode_calls(root: Path) -> Tuple[Dict[str, List[int]], int]:
    """Return ``({relative path: [line numbers]}, files_scanned)`` under *root*.

    A file that cannot be parsed is a failure of the walk, not something to skip
    quietly: skipping is how a guard ends up reporting a clean sweep of files it
    never read.
    """
    offenders: Dict[str, List[int]] = {}
    scanned = 0

    for path in sorted(root.rglob("*.py")):
        scanned += 1
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        finder = _JWTEncodeFinder()
        finder.visit(tree)
        if finder.hits:
            offenders[path.relative_to(root).as_posix()] = finder.hits

    return offenders, scanned


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.architecture
def test_only_the_token_generator_signs_jwts():
    """Exactly one module in the shipped package calls ``jwt.encode``."""
    offenders, scanned = find_jwt_encode_calls(PACKAGE_ROOT)

    # The floor is asserted HERE, in the guard itself: a walk that found
    # nothing must fail this test rather than pass it vacuously.
    assert scanned >= MINIMUM_FILES_SCANNED, (
        f"Only {scanned} files were scanned under {PACKAGE_ROOT}; expected at "
        f"least {MINIMUM_FILES_SCANNED}. The walk is not reaching the package, "
        "so a clean result here would mean nothing."
    )

    assert set(offenders) == {MINT_CHOKEPOINT}, (
        "JWT signing must happen in exactly one place — "
        f"{MINT_CHOKEPOINT} — where the key and the algorithm belong to the "
        "same object and every mint passes the deactivated-account check. "
        f"Files signing outside it: {sorted(set(offenders) - {MINT_CHOKEPOINT})}. "
        "Mint through IJWTTokenGenerator instead (#959; #853 for the "
        "AuthService mint this replaced)."
    )


@pytest.mark.unit
def test_the_finder_sees_every_spelling_of_the_call(tmp_path):
    """Mutation check on the detector: a guard must fail in its own failure state.

    A detector that only matches the literal text ``jwt.encode`` would pass this
    file and miss three of the four modules below — and a pin that cannot see a
    violation is indistinguishable from one with nothing to find.
    """
    modules = {
        "plain.py": "import jwt\nT = jwt.encode(C, K, algorithm='HS256')\n",
        "aliased_module.py": "import jwt as j\nT = j.encode(C, K, algorithm='HS256')\n",
        "direct_name.py": "from jwt import encode\nT = encode(C, K)\n",
        "aliased_name.py": "from jwt import encode as sign_it\nT = sign_it(C, K)\n",
        "pyjwt_class.py": "import jwt\nT = jwt.PyJWT().encode(C, K)\n",
        "pyjwt_imported.py": "from jwt import PyJWT\nT = PyJWT().encode(C, K)\n",
        # Not a JWT mint: a same-named method on something that is not PyJWT.
        "innocent.py": "def to_bytes(text):\n    return text.encode('utf-8')\n",
    }
    for name, source in modules.items():
        (tmp_path / name).write_text(source)

    offenders, scanned = find_jwt_encode_calls(tmp_path)

    assert scanned == 7
    assert set(offenders) == {
        "plain.py",
        "aliased_module.py",
        "direct_name.py",
        "aliased_name.py",
        "pyjwt_class.py",
        "pyjwt_imported.py",
    }
