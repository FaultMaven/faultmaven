"""`FaultMavenSettings.model_config` is declared once, and its consequences hold.

A class body binds the LAST assignment, so a second `model_config` silently
overrode the first and nothing warned. The two happened to agree on everything
they shared, but the winner also carried `use_enum_values`, which is why
enum-annotated settings fields hold plain `str` at runtime — the divergence that
put an enum repr into an append-only audit column in #827.

These pin the duplication away and pin the runtime behaviour it obscured, so the
next person reading the annotation is not misled by it.
"""

import ast
from pathlib import Path

import pytest

from faultmaven.config.settings import DeploymentMode, FaultMavenSettings, get_settings

SETTINGS_SOURCE = Path(__file__).resolve().parents[3] / "faultmaven/config/settings.py"


@pytest.mark.unit
def test_model_config_is_declared_exactly_once():
    """A second declaration would silently win and nothing would warn."""
    tree = ast.parse(SETTINGS_SOURCE.read_text())
    class_def = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "FaultMavenSettings"
    )
    declarations = [
        target.id
        for stmt in class_def.body
        if isinstance(stmt, ast.Assign)
        for target in stmt.targets
        if isinstance(target, ast.Name) and target.id == "model_config"
    ]

    assert len(declarations) == 1, (
        f"FaultMavenSettings declares model_config {len(declarations)} times; "
        "the last one silently wins and the others are dead."
    )


@pytest.mark.unit
def test_use_enum_values_is_still_set():
    """The documented behaviour below depends on this flag being on.

    If it is ever turned off, the read sites that unwrap defensively become
    unnecessary — and, more importantly, anything relying on the field being a
    plain `str` starts receiving members instead.
    """
    assert FaultMavenSettings.model_config.get("use_enum_values") is True


@pytest.mark.unit
def test_enum_annotated_field_holds_the_value_not_the_member():
    """`deployment_mode: DeploymentMode` is a plain `str` at runtime.

    Pinned because the annotation says otherwise. Code that calls `.value` on it
    raises AttributeError; code that calls `str()` on a *member* (from a test
    stub or a directly-constructed Settings) gets "DeploymentMode.STANDALONE".
    Both mistakes have been made; see #827.
    """
    mode = get_settings().deployment_mode

    assert isinstance(mode, str)
    assert not isinstance(mode, DeploymentMode)
    assert mode in {m.value for m in DeploymentMode}


@pytest.mark.unit
def test_defensive_unwrap_handles_both_shapes():
    """The `getattr(x, "value", x)` idiom is correct for member and str alike.

    This is what `is_cloud` uses and what call sites should copy.
    """
    for candidate in (DeploymentMode.STANDALONE, DeploymentMode.STANDALONE.value):
        assert str(getattr(candidate, "value", candidate)) == "standalone"
