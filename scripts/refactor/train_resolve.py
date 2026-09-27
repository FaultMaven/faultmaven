#!/usr/bin/env python3
"""Resolve merge-train conflicts across interlocking decomposition branches, conservatively.

PROVES/DOES: resolves a diff3-style conflict hunk only when the resolution is
mechanically re-derivable afterwards, and otherwise leaves it for a human —
never guesses at a hunk with real, non-import edits on both sides.

WHY: several decomposition branches touching the same file (each moving a
different symbol out of it) conflict on nearly every merge, almost always on
import lines only — each branch rewrote the same `from D import ...`
differently because each is fixing up its own moved names. Resolving those by
hand, one merge-train step at a time, does not scale past two or three
branches; but a resolver that is *not* conservative (that also swallows a
hunk with a real logic change) silently drops a fix. This tool exists to be
the boring middle: automatic where the outcome is re-derivable by re-running
clean_refs.py --rewrite afterwards, refused everywhere else.

For each conflict hunk:
  * if one side changed ONLY import lines relative to base, take the OTHER
    side (import rewrites are re-derived afterwards by re-running the import
    codemod of every merged module — they are deterministic);
  * if both sides changed only import lines -> left UNRESOLVED. A
    line-level union is not safe: a hunk can cut through a parenthesized
    multi-line import and the union breaks the syntax (it did on the #1707
    train). train_step.sh resolves .py conflicts per FILE instead;
  * .md hunks: take each line from whichever side changed it when the three
    versions are line-aligned, else merge per character when edits do not
    overlap;
  * anything else -> left for a human, and the file is reported UNRESOLVED.

USAGE: train_resolve.py FILE [FILE ...]  (rewrites in place)

Requires diff3-style conflict markers (the ``|||||||`` base section), which
git does not add by default: run with ``git -c merge.conflictstyle=diff3
merge ...``, or set it once with ``git config merge.conflictstyle diff3``.
A file with only the plain 3-marker style (no ``|||||||``) raises rather than
silently under-resolving.

EXIT CODES: 0 every hunk in every file resolved; 1 at least one hunk was left
UNRESOLVED (conflict markers remain in the file for a human).
"""

import difflib
import re
import sys

IMPORT_LINE = re.compile(
    r"^\s*(from\s+\S+\s+import\b.*|import\s+\S+.*|[A-Za-z_][A-Za-z0-9_]*(\s+as\s+\w+)?,?|\)|\(|)\s*$"
)


def only_imports_changed(base, side):
    sm = difflib.SequenceMatcher(None, base, side, autojunk=False)
    for op, a, b, c, d in sm.get_opcodes():
        if op == "equal":
            continue
        for ln in base[a:b] + side[c:d]:
            if not IMPORT_LINE.match(ln):
                return False
    return True


def charmerge(b, o, t):
    eo = [
        (a, b2, o[c:d])
        for op, a, b2, c, d in difflib.SequenceMatcher(
            None, b, o, autojunk=False
        ).get_opcodes()
        if op != "equal"
    ]
    et = [
        (a, b2, t[c:d])
        for op, a, b2, c, d in difflib.SequenceMatcher(
            None, b, t, autojunk=False
        ).get_opcodes()
        if op != "equal"
    ]
    edits = sorted(eo + et)
    for (a1, b1, _), (a2, b2, _) in zip(edits, edits[1:]):
        if not (b1 <= a2):
            return None
    res = b
    for a, bb, rep in sorted(edits, reverse=True):
        res = res[:a] + rep + res[bb:]
    return res


def resolve_md(o, b, t):
    if len(o) == len(b) == len(t):
        out = []
        for oo, bb, tt in zip(o, b, t):
            if oo == tt:
                out.append(oo)
            elif oo == bb:
                out.append(tt)
            elif tt == bb:
                out.append(oo)
            else:
                m = charmerge(bb, oo, tt)
                if m is None:
                    return None
                out.append(m)
        return out
    return None


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    bad = 0
    for path in sys.argv[1:]:
        L = open(path).read().split("\n")
        out, i, res, unres = [], 0, 0, 0
        is_md = path.endswith(".md")
        while i < len(L):
            if not L[i].startswith("<<<<<<< "):
                out.append(L[i])
                i += 1
                continue
            j = i + 1
            o = []
            while not L[j].startswith("||||||| "):
                o.append(L[j])
                j += 1
            j += 1
            b = []
            while L[j] != "=======":
                b.append(L[j])
                j += 1
            j += 1
            t = []
            while not L[j].startswith(">>>>>>> "):
                t.append(L[j])
                j += 1
            hunk = L[i : j + 1]
            pick = None
            if is_md:
                pick = resolve_md(o, b, t)
            else:
                oi, ti = only_imports_changed(b, o), only_imports_changed(b, t)
                if oi and ti:
                    # Both sides rewrote imports in the same hunk. A line-level
                    # union is NOT safe: a hunk can cut through a parenthesized
                    # multi-line import, and the union then breaks the syntax
                    # (it did on the #1707 train). Leave it for train_step.sh,
                    # which resolves .py conflicts per FILE (keep the side
                    # whose edits go beyond imports, re-derive the other
                    # side's imports with the codemods), or for a human.
                    pick = None
                elif oi:
                    pick = t
                elif ti:
                    pick = o
            if pick is None:
                out.extend(hunk)
                unres += 1
            else:
                out.extend(pick)
                res += 1
            i = j + 1
        open(path, "w").write("\n".join(out))
        print(f"{path}: resolved {res}, unresolved {unres}")
        bad += unres
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
