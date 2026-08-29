"""The one root-anchored containment check every path subsystem shares.

A path handed to the filesystem is safe when the path it *resolves to* is
inside a tree we own — not when it happens to lack two textual markers. This
module owns that check; #1213/#1215/#1225 established the discipline for the
runbook tree and #1235 lifted it here so the evidence/object-storage backend
uses the same implementation rather than a second one that drifts.

Three consumers today:

- ``faultmaven.utils.runbook_id.resolve_runbook_path`` — the runbook tree, which
  keeps its own ``RunbookPathEscape`` subclass and its own wording because
  callers there catch precisely that type in order to *degrade* (a listing skips
  one bad row, the scan skips one bad file) rather than fail.
- ``faultmaven.infrastructure.storage.filesystem.FilesystemStorageBackend`` —
  the evidence blob tree, where a refusal is a hard error. It re-raises with a
  REDACTED message, because its exception reaches a client and the LLM context;
  see ``_get_full_path``.
- ``faultmaven.bootstrap.kb_pack.KbPack.load`` — the shipped-runbook pack, whose
  ``pack.json`` names each runbook's file with a relative path.

The generic checker is deliberately parameterised on *wording* and *error type*
rather than being copied: what must be identical between subsystems is the
containment rule, and what must differ is what a log line and an ``except``
clause see.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["PathEscape", "resolve_within_root"]


class PathEscape(ValueError):
    """A path that is not, or cannot be shown to be, inside its root.

    Covers both halves of "this path is not safe to touch": it resolved outside
    the root, or it could not be resolved at all (a symlink loop, a permission
    error, a name too long, an embedded NUL). Callers act on both the same way
    — refuse the filesystem operation — so distinguishing them at the type level
    would buy nothing and invite one of the two to be forgotten.

    Subclasses ``ValueError`` because that is the shape both call sites already
    raised before this was lifted, and because "the caller passed a path it may
    not touch" is a value error.

    The message carries the resolved absolute paths, which are useful in a
    server log and must never reach a client. Service callers translate it to a
    typed domain exception whose client-facing message names only the resource.
    """


def resolve_within_root(
    path: str | Path,
    *,
    root: Path,
    source: str,
    subject: str,
    tree: str,
    error: type[PathEscape] = PathEscape,
) -> Path:
    """Resolve ``path``, refusing anything not strictly inside ``root``.

    Call this before ANY filesystem operation on the path — read, write, mkdir,
    or unlink — and act on the returned resolved path.

    Anchored on the ROOT of the tree, never on the directory the path is in:
    anchoring on the containing directory is circular, since an escaped
    directory trivially contains its own child. That circularity is the exact
    defect #1215's first round shipped, and it passed its own test.

    The result is STRICTLY inside the root — the root itself is refused. It is a
    directory, so a write to it would fail anyway, but a caller that first
    creates the *parent* of what it was handed would be creating a directory
    outside the tree.

    **Symlinks are resolved, so a link pointing out of the tree is refused.**
    That is the security property a substring check cannot have — containment is
    about where a path *lands*, not what it is named — and it has a deployment
    consequence worth stating plainly: a tree assembled out of symlinks
    (``data/knowledge/team_x -> /mnt/share``) worked before this guard and does
    not work now.

    ``Path.resolve()`` can also fail outright — ``RuntimeError`` on a symlink
    loop (measured on CPython 3.11), ``OSError`` for permissions, a too-long
    name, or a filesystem that is gone, and ``ValueError`` on an embedded NUL
    (``a\\x00b``). An unresolvable path is not a containable path, so all three
    become ``error`` rather than escaping as themselves: letting a bare
    ``RuntimeError`` through kills callers that only guard against the
    documented failure, and a bare ``ValueError`` — the type ``PathEscape``
    itself subclasses — would sail straight past a caller catching only the
    typed escape.

    The two resolves are separated so the error can name **which** path failed.
    The ``root`` failing is an operator's misconfiguration and names the tree;
    the ``path`` failing is the untrusted value and names the caller's subject.
    Conflating them sends an operator chasing the innocent file when the root
    was at fault.

    Args:
        path: The candidate path. May be relative; it is resolved against the
            process cwd exactly as ``root`` is, so containment is unaffected by
            where the process runs.
        root: The tree the path must be inside. Required, never defaulted: a
            default root is a bypass no test can see, because a caller that
            simply omits it checks against the wrong tree and passes anyway.
            Resolved against the process cwd like ``path``, every call — NOT
            cached. A cached anchor plus a freshly resolved candidate disagree
            the moment the process chdirs, and the shipped storage root
            (``./data/storage``) is relative, so that is reachable rather than
            theoretical. The saving would be ~57us per call (measured); the
            consistency is worth more.
        source: Where the path came from, carried into the error. For a value
            read back from the database that means naming the row — "some draft
            is bad" is not an actionable message.
        subject: What the path *is*, in the error prose ("runbook path",
            "storage key"). Named so a log line says which subsystem refused.
        tree: The tree's name in the error prose ("knowledge", "storage").
        error: The ``PathEscape`` subclass to raise, for subsystems whose
            callers catch a narrower type.

    Raises:
        error: the path is outside the root, or cannot be resolved.
    """
    try:
        # ``Path(root)``, not ``root.resolve()``: the annotation says ``Path``
        # but nothing enforces it, and a ``str`` root raised ``AttributeError``
        # — escaping as itself, past every caller that catches the typed error.
        # Normalising both sides identically is the point of a shared primitive.
        resolved_root = Path(root).resolve()
    except (RuntimeError, OSError, ValueError, TypeError) as exc:
        raise error(
            f"refusing to resolve against an unresolvable {tree} root: "
            f"{root!r} ({type(exc).__name__}: {exc}) (source: {source})"
        ) from exc
    try:
        resolved = Path(path).resolve()
    except (RuntimeError, OSError, ValueError) as exc:
        raise error(
            f"refusing to touch an unresolvable {subject}: {path!r} "
            f"({type(exc).__name__}: {exc}) (source: {source})"
        ) from exc
    if resolved == resolved_root or not resolved.is_relative_to(resolved_root):
        raise error(
            f"refusing to touch a {subject} outside the {tree} tree: "
            f"{resolved} is not under {resolved_root} (source: {source})"
        )
    return resolved
