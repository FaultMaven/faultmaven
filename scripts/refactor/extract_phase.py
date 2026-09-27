#!/usr/bin/env python3
"""Extract phases of one long method into private methods of the same class.

DOES: step A of a function-body split. Each phase (validated by
phase_flow.py's rules) becomes ``def <name>(self, *, <inputs>)`` inserted after
the owner method, with the phase's statements moved VERBATIM (re-indented only;
lines inside multi-line strings are left alone). The owner gets the call:

  STRAIGHT  ``<outputs> = [await] self.<name>(<input>=<input>, ...)``
            (the method ends with ``return <outputs>``; no outputs -> a bare call)
  TAIL      ``return [await] self.<name>(<input>=<input>, ...)``

A comment block directly above the phase's first statement stays in the owner
as the section header; comments inside the phase move with it.

Refuses (exit 1, nothing written) any phase phase_flow.py reports as INVALID,
with UNDEFINED outputs, MAYBE-UNBOUND inputs or CONFLICTS. Prove the result with
verify_inline.py (independent of this tool).

USAGE: extract_phase.py --file F --func Class.method --phase NAME:FIRST-LAST [...] [--apply]
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import io
import sys
import tokenize
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "phase_flow", Path(__file__).with_name("phase_flow.py")
)
pf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pf)


def string_interior_lines(src: str) -> set[int]:
    out: set[int] = set()
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.STRING and tok.start[0] != tok.end[0]:
            out.update(range(tok.start[0] + 1, tok.end[0] + 1))
    return out


def analyse(f, lo, hi):
    """phase_flow.analyse_phase, reshaped for this tool (one analysis, no drift)."""
    r = pf.analyse_phase(f, lo, hi)
    if not r["found"]:
        return None, [], [], False, r["problems"]
    shape = r["shape"] if r["shape"] in ("STRAIGHT", "TAIL") else None
    return shape, r["inputs"], r["outputs"], r["awaits"], r["problems"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--func", required=True)
    ap.add_argument("--phase", action="append", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    path = Path(a.file)
    src = path.read_text()
    lines = src.splitlines(keepends=True)
    interior = string_interior_lines(src)
    tree = ast.parse(src)
    cls, meth = a.func.split(".")
    C = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    f = next(m for m in C.body if getattr(m, "name", None) == meth)
    existing = {
        m.name for m in C.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    method_indent = " " * (f.col_offset + 4)

    plans = []
    bad = False
    for spec in a.phase:
        name, rng = spec.split(":")
        lo, hi = (int(x) for x in rng.split("-"))
        if name in existing:
            print(f"{name}: the class already has a member of that name")
            bad = True
            continue
        shape, inputs, outputs, awaits, problems = analyse(f, lo, hi)
        print(
            f"{name}: L{lo}-{hi} shape={shape} {'async' if awaits else 'sync'} inputs={inputs} outputs={outputs}"
        )
        for p in problems:
            print(f"   REFUSED: {p}")
        if problems or shape is None:
            bad = True
            continue
        plans.append((name, lo, hi, shape, inputs, outputs, awaits))
    spans = sorted((lo, hi) for _, lo, hi, *_ in plans)
    for (a1, b1), (a2, b2) in zip(spans, spans[1:]):
        if a2 <= b1:
            print(f"phases L{a1}-{b1} and L{a2}-{b2} overlap")
            bad = True
    if bad:
        return 1
    if not a.apply:
        return 0

    new_methods = []
    edits = []  # (first_line, last_line, replacement text) over source lines
    for name, lo, hi, shape, inputs, outputs, awaits in sorted(
        plans, key=lambda p: p[1]
    ):
        stmt_indent = len(lines[lo - 1]) - len(lines[lo - 1].lstrip(" "))
        delta = stmt_indent - len(method_indent)
        body_lines = []
        for ln in range(lo, hi + 1):
            text = lines[ln - 1]
            if ln in interior or not text.strip():
                body_lines.append(text if ln in interior else "\n")
            elif delta >= 0 and text[:delta].strip() == "":
                body_lines.append(text[delta:])
            else:
                body_lines.append(method_indent + text.lstrip(" "))
        kw = "".join(f"{p}, " for p in inputs)
        sig = (
            f"{method_indent[:-4]}{'async ' if awaits else ''}def {name}(self, *, {kw.rstrip(', ')}):\n"
            if inputs
            else f"{method_indent[:-4]}{'async ' if awaits else ''}def {name}(self):\n"
        )
        doc = f'{method_indent}"""TODO: one-line docstring."""\n'
        ret = ""
        if shape == "STRAIGHT" and outputs:
            ret = f"{method_indent}return {', '.join(outputs)}\n"
        new_methods.append("\n" + sig + doc + "".join(body_lines) + ret)
        call = f"self.{name}(" + ", ".join(f"{p}={p}" for p in inputs) + ")"
        call = ("await " if awaits else "") + call
        ind = " " * stmt_indent
        if shape == "TAIL":
            repl = f"{ind}return {call}\n"
        elif outputs:
            repl = f"{ind}{', '.join(outputs)} = {call}\n"
        else:
            repl = f"{ind}{call}\n"
        edits.append((lo, hi, repl))
    out = list(lines)
    insert_at = f.end_lineno  # after the owner method's last line
    out.insert(insert_at, "".join(new_methods))
    for lo, hi, repl in sorted(edits, reverse=True):
        out[lo - 1 : hi] = [repl]
    path.write_text("".join(out))
    print(f"wrote {path}: {len(plans)} phase(s); run black, then verify_inline.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
