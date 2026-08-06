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

Detection is AST-based and import-aware, so it survives renaming. What it
catches, as shipped and as self-checked below:

* ``jwt.encode(...)`` under any module alias (``import jwt as j``), including a
  dotted reach through the submodules — ``jwt.api_jwt.encode(...)``,
  ``jwt.api_jws.encode(...)``, ``import jwt.api_jwt`` then dotted access.
* ``from jwt import encode`` / ``from jwt import api_jwt``, under any alias.
* Any mention of the encoder classes ``PyJWT`` and ``PyJWS``, attribute or
  from-import. Both, because either one encodes and singling out one of a pair
  is how a blind spot is written down as a policy.

What it does NOT catch, stated so the next reader knows the shape of the hole:
a module rebound at runtime (``signer = jwt; signer.encode(...)``), attribute
access assembled dynamically (``getattr(jwt, "enc" + "ode")``), a mint reached
through a third-party wrapper that imports PyJWT itself, and a call through a
local alias of a submodule object (``m = jwt.api_jwt`` then ``m.encode(...)``).
Those are deliberate-evasion shapes; this guard is aimed at the colleague who
adds a mint in good faith, and review is the control for the rest.

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

    #: Encoder classes in PyJWT. Any mention counts: they exist to encode and
    #: decode, the allowlist is a single file, and a false positive costs one
    #: conversation while a false negative costs the property.
    ENCODER_CLASSES = ("PyJWT", "PyJWS")

    def __init__(self) -> None:
        # Module aliases: ``import jwt`` / ``import jwt as j``, and submodules
        # bound directly (``from jwt import api_jwt``).
        self.module_aliases: Set[str] = set()
        # Direct bindings: ``from jwt import encode`` / ``... as sign``
        self.encode_aliases: Set[str] = set()
        # Names an encoder class is bound to.
        self.encoder_class_aliases: Set[str] = set()
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
                elif alias.name in self.ENCODER_CLASSES:
                    self.encoder_class_aliases.add(alias.asname or alias.name)
                else:
                    # ``from jwt import api_jwt; api_jwt.encode(...)`` — and any
                    # other submodule, since every one of them is reached the
                    # same way.
                    self.module_aliases.add(alias.asname or alias.name)
        self.generic_visit(node)

    def _rooted_in_jwt(self, node: ast.expr) -> bool:
        """Is this expression a dotted chain rooted at a jwt alias?

        ``jwt.api_jwt.encode(...)`` parses as Attribute(Attribute(Name)), so
        matching only ``Attribute(value=Name)`` walks straight past it — the
        blind spot that let ``jwt.api_jwt.encode`` through the first version of
        this finder.
        """
        while isinstance(node, ast.Attribute):
            node = node.value
        return isinstance(node, ast.Name) and node.id in self.module_aliases

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # ``jwt.PyJWT`` / ``jwt.api_jwt.PyJWS`` — an encoder class reached
        # through the module, at any depth.
        if node.attr in self.ENCODER_CLASSES and self._rooted_in_jwt(node.value):
            self.hits.append(node.lineno)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        # ``PyJWT``/``PyJWS`` imported directly and used anywhere.
        if node.id in self.encoder_class_aliases:
            self.hits.append(node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "encode":
            if self._rooted_in_jwt(func.value):
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
        "pyjws_class.py": "import jwt\nT = jwt.PyJWS().encode(C, K)\n",
        "pyjws_imported.py": "from jwt import PyJWS\nT = PyJWS().encode(C, K)\n",
        "dotted_submodule.py": "import jwt\nT = jwt.api_jwt.encode(C, K)\n",
        "dotted_jws.py": "import jwt\nT = jwt.api_jws.encode(C, K)\n",
        "nested_import.py": "import jwt.api_jwt\nT = jwt.api_jwt.encode(C, K)\n",
        "submodule_from.py": "from jwt import api_jwt\nT = api_jwt.encode(C, K)\n",
        # Not a JWT mint: a same-named method on something that is not PyJWT.
        "innocent.py": "def to_bytes(text):\n    return text.encode('utf-8')\n",
    }
    for name, source in modules.items():
        (tmp_path / name).write_text(source)

    offenders, scanned = find_jwt_encode_calls(tmp_path)

    assert scanned == 13
    assert set(offenders) == {
        "plain.py",
        "aliased_module.py",
        "direct_name.py",
        "aliased_name.py",
        "pyjwt_class.py",
        "pyjwt_imported.py",
        "pyjws_class.py",
        "pyjws_imported.py",
        "dotted_submodule.py",
        "dotted_jws.py",
        "nested_import.py",
        "submodule_from.py",
    }
