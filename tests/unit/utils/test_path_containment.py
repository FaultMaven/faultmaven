"""The shared root-anchored containment primitive (#1235).

``resolve_within_root`` is the rule #1213/#1215/#1225 established for the
runbook tree, lifted so the evidence-storage backend uses the same
implementation instead of a second one that drifts. Two things are pinned here:

1. The **rule** — resolve, then require strict containment — including the
   symlink case a substring denylist cannot see.
2. The **seam** — that lifting it did not change what the runbook subsystem
   raises or says. ``tests/unit/modules/knowledge/test_conversion_path_containment.py``
   covers the runbook behaviour itself; these tests pin the message text so a
   future edit to the generic wording cannot silently reword it.
"""

import pytest

from faultmaven.utils.path_containment import PathEscape, resolve_within_root
from faultmaven.utils.runbook_id import RunbookPathEscape, resolve_runbook_path


def _resolve(path, root, **kwargs):
    kwargs.setdefault("source", "test")
    kwargs.setdefault("subject", "test path")
    kwargs.setdefault("tree", "test")
    return resolve_within_root(path, root=root, **kwargs)


# =============================================================================
# The rule
# =============================================================================


class TestContainmentRule:
    def test_path_inside_the_root_resolves(self, tmp_path):
        resolved = _resolve(tmp_path / "a" / "b.txt", tmp_path)

        assert resolved == (tmp_path / "a" / "b.txt").resolve()
        assert resolved.is_absolute()

    def test_path_need_not_exist(self, tmp_path):
        """Containment is checked before a write, so the file is not there yet."""
        assert _resolve(tmp_path / "not" / "yet" / "here.txt", tmp_path)

    def test_directory_symlink_out_of_the_root_is_refused(self, tmp_path):
        """The case a ``".." in key`` denylist admits.

        No traversal sequence, no leading separator — and it still lands
        outside. This is the whole reason the guard resolves.
        """
        root = tmp_path / "root"
        outside = tmp_path / "outside"
        root.mkdir()
        outside.mkdir()
        (root / "linked").symlink_to(outside, target_is_directory=True)

        candidate = "linked/file.txt"
        assert ".." not in candidate and not candidate.startswith("/")

        with pytest.raises(PathEscape, match="outside the test tree"):
            _resolve(root / candidate, root)

    def test_file_symlink_out_of_the_root_is_refused(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("private")
        (root / "peek.txt").symlink_to(secret)

        with pytest.raises(PathEscape, match="outside the test tree"):
            _resolve(root / "peek.txt", root)

    def test_symlink_staying_inside_the_root_is_allowed(self, tmp_path):
        """Containment is about where the path lands, not that it is a link."""
        root = tmp_path / "root"
        (root / "real").mkdir(parents=True)
        (root / "linked").symlink_to(root / "real", target_is_directory=True)

        resolved = _resolve(root / "linked" / "f.txt", root)

        assert resolved == (root / "real" / "f.txt").resolve()

    def test_traversal_out_of_the_root_is_refused(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()

        with pytest.raises(PathEscape, match="outside the test tree"):
            _resolve(root / ".." / "escaped.txt", root)

    def test_absolute_path_outside_the_root_is_refused(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()

        with pytest.raises(PathEscape, match="outside the test tree"):
            _resolve("/etc/passwd", root)

    def test_the_root_itself_is_refused(self, tmp_path):
        """Strictly inside: a caller that mkdirs the PARENT of what it was
        handed would otherwise create a directory outside the tree."""
        with pytest.raises(PathEscape, match="outside the test tree"):
            _resolve(tmp_path, tmp_path)

    def test_sibling_with_the_root_as_a_name_prefix_is_refused(self, tmp_path):
        """``/x/root-evil`` is not under ``/x/root`` — a string prefix test
        would say it is."""
        root = tmp_path / "root"
        root.mkdir()
        (tmp_path / "root-evil").mkdir()

        with pytest.raises(PathEscape, match="outside the test tree"):
            _resolve(tmp_path / "root-evil" / "f.txt", root)

    def test_relative_root_and_relative_path_are_contained(self, tmp_path, monkeypatch):
        """Both sides resolve against the same cwd, so a relative root works."""
        from pathlib import Path

        monkeypatch.chdir(tmp_path)
        Path("root").mkdir()

        assert _resolve("root/f.txt", Path("root")) == (tmp_path / "root/f.txt")

        with pytest.raises(PathEscape, match="outside the test tree"):
            _resolve("elsewhere/f.txt", Path("root"))


# =============================================================================
# Unresolvable is not containable
# =============================================================================


class TestUnresolvable:
    def test_embedded_nul_becomes_a_typed_escape(self, tmp_path):
        """``resolve()`` raises ValueError here; a bare ValueError would sail
        past every caller that catches only the typed escape."""
        with pytest.raises(PathEscape, match="unresolvable test path"):
            _resolve(tmp_path / "a\x00b", tmp_path)

    def test_symlink_loop_becomes_a_typed_escape(self, tmp_path):
        """``resolve()`` raises RuntimeError/OSError on a loop, not ValueError."""
        root = tmp_path / "root"
        root.mkdir()
        (root / "a").symlink_to(root / "b")
        (root / "b").symlink_to(root / "a")

        with pytest.raises(PathEscape):
            _resolve(root / "a", root)

    def test_unresolvable_root_names_the_root_not_the_path(self, tmp_path):
        from pathlib import Path

        with pytest.raises(PathEscape) as exc:
            _resolve(tmp_path / "innocent.txt", Path("bad\x00root"))

        assert "unresolvable test root" in str(exc.value)
        assert "innocent.txt" not in str(exc.value)

    def test_escape_is_a_value_error(self):
        """Callers written against the pre-lift ``ValueError`` still work."""
        assert issubclass(PathEscape, ValueError)

    def test_a_str_root_raises_the_typed_error_not_attribute_error(self, tmp_path):
        """``root`` is annotated ``Path``, and nothing enforces annotations.

        A ``str`` root used to reach ``root.resolve()`` and raise
        ``AttributeError``, which escapes as itself past every caller that
        catches only the typed error — in the one primitive this module tells
        every new subsystem to use.
        """
        with pytest.raises(PathEscape):
            _resolve("/etc/passwd", str(tmp_path))

    def test_a_str_root_still_contains_correctly(self, tmp_path):
        """And it is normalised, not merely tolerated."""
        assert (
            _resolve(tmp_path / "in.txt", str(tmp_path))
            == (tmp_path / "in.txt").resolve()
        )


# =============================================================================
# Per-subsystem wording and error type
# =============================================================================


class TestSubsystemParameterisation:
    def test_wording_names_the_calling_subsystem(self, tmp_path):
        with pytest.raises(PathEscape) as exc:
            _resolve(
                "/etc/passwd",
                tmp_path,
                subject="storage key",
                tree="storage",
                source="key='x'",
            )

        message = str(exc.value)
        assert "refusing to touch a storage key outside the storage tree" in message
        assert "(source: key='x')" in message

    def test_error_type_is_the_callers(self, tmp_path):
        class Custom(PathEscape):
            pass

        with pytest.raises(Custom):
            _resolve("/etc/passwd", tmp_path, error=Custom)


# =============================================================================
# The runbook subsystem is unchanged by the lift
# =============================================================================


class TestRunbookDelegationUnchanged:
    def test_runbook_escape_is_a_path_escape_and_a_value_error(self):
        assert issubclass(RunbookPathEscape, PathEscape)
        assert issubclass(RunbookPathEscape, ValueError)

    def test_the_default_error_type_is_the_base_not_a_subsystems(self, tmp_path):
        """A caller that names no ``error`` gets the base type.

        This is a statement about the PRIMITIVE's default, and nothing more.
        The invariant that matters operationally — that
        ``FilesystemStorageBackend`` never raises ``RunbookPathEscape``, because
        the knowledge callers catch that type in order to skip a row and carry
        on — has to be asserted against the backend itself, and is:
        ``tests/infrastructure/test_storage_backends.py::
        TestFilesystemPathContainment::
        test_a_storage_refusal_is_never_a_runbook_refusal``.

        Asserting it here instead was the defect: with the default error type
        the property holds by construction, so the pin survived a mutation that
        made the backend raise the runbook type.
        """
        with pytest.raises(PathEscape) as exc:
            _resolve("/etc/passwd", tmp_path, subject="storage key", tree="storage")

        assert type(exc.value) is PathEscape
        assert not isinstance(exc.value, RunbookPathEscape)

    def test_outside_message_is_byte_for_byte_what_1225_shipped(self, tmp_path):
        root = tmp_path / "knowledge"
        root.mkdir()
        outside = tmp_path / "escaped.md"

        with pytest.raises(RunbookPathEscape) as exc:
            resolve_runbook_path(outside, source="draft 7", root=root)

        assert str(exc.value) == (
            f"refusing to touch a runbook path outside the knowledge tree: "
            f"{outside.resolve()} is not under {root.resolve()} "
            f"(source: draft 7)"
        )

    def test_unresolvable_path_message_is_byte_for_byte_unchanged(self, tmp_path):
        bad = tmp_path / "a\x00b.md"

        with pytest.raises(RunbookPathEscape) as exc:
            resolve_runbook_path(bad, source="draft 7", root=tmp_path)

        assert str(exc.value).startswith(
            f"refusing to touch an unresolvable runbook path: {bad!r} (ValueError: "
        )
        assert str(exc.value).endswith("(source: draft 7)")

    def test_unresolvable_root_message_is_byte_for_byte_unchanged(self, tmp_path):
        from pathlib import Path

        bad_root = Path("bad\x00root")

        with pytest.raises(RunbookPathEscape) as exc:
            resolve_runbook_path(tmp_path / "x.md", source="draft 7", root=bad_root)

        assert str(exc.value).startswith(
            f"refusing to resolve against an unresolvable knowledge root: "
            f"{bad_root!r} (ValueError: "
        )
        assert str(exc.value).endswith("(source: draft 7)")

    def test_root_is_still_required(self, tmp_path):
        """The keyword-only, no-default ``root`` is a guard in its own right."""
        with pytest.raises(TypeError):
            resolve_runbook_path(tmp_path / "x.md", source="draft 7")
