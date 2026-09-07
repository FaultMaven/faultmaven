"""``CLAUDE.md``'s reasoning-intent call-site table must match the code (#1357).

The table under *"A caller can declare what a call needs from reasoning"* is
hand-maintained, and it drifted the first time the set changed: fm#1116 added a
fifth call site declaring ``ReasoningIntent.INFERENCE`` on a **structured**
call (#1313/#1314/#1316) and the doc kept saying there were four, all
``EXTRACTION``, and that "no structured call ships with an intent at all".

That is not a tidiness defect. The stale text described a strictly weaker
design than the one that shipped — read literally it said the engine never asks
for reasoning, when what actually happens is that reasoning is *routed*:
suppressed for grounded extraction, requested for inference over candidates,
with an output floor making the lift safe. CLAUDE.md is the first file every
contributor and coding agent reads, so a claim like that propagates.

Unlike the alembic head (#1246, the sibling guard next door), the durable fix
here is NOT to delete the number. The set is small, changes rarely, and *is*
the information — "only these five calls declare an intent" is what tells a
reader the mechanism is narrow and deliberate. So the table stays and this
pins it.

**Table paths are relative to** ``faultmaven/`` (``core/investigation/…``, not
``faultmaven/core/investigation/…``). A row written repo-relative reports the
same filename under both "undocumented" and "claimed but not in the code",
which reads as a contradiction but is just the two spellings failing to match.

The code side is an AST scan, deliberately not a grep: ``black`` reflows call
sites across lines, so a text anchor written today silently stops matching
after the next format pass. The scan keys on a keyword argument
``reasoning_intent=`` whose value *names* an intent, in any of the spellings
the runtime actually honours (see ``_declared_intent``).
"""

import ast
import re
from collections import Counter
from pathlib import Path

import pytest

from faultmaven.infrastructure.llm.providers.base import ReasoningIntent

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_CLAUDE_MD = _PROJECT_ROOT / "CLAUDE.md"
_PACKAGE = _PROJECT_ROOT / "faultmaven"
_ENGINE = _PACKAGE / "core" / "investigation" / "milestone_engine.py"

# The knobs the table's "Declares" column records, in the order it lists them.
_KNOBS = ("reasoning_intent", "min_output_tokens")

# Member names, and the wire spellings ``ReasoningIntent.coerce`` accepts for
# them. Derived from the enum rather than restated, so a renamed member cannot
# leave this guard matching a name the runtime no longer knows.
_MEMBER_NAMES = {member.name for member in ReasoningIntent}
_VALUE_TO_NAME = {member.value: member.name for member in ReasoningIntent}

_TABLE_HEADER = "| Call site | Declares |"
_BACKTICKED = re.compile(r"`([^`]+)`")
# A prose count of call sites. Emphasis markers between the number and the noun
# are tolerated: the shipped text bolds this region, and tightening
# "**five call sites**" to the more idiomatic "**five** call sites" must not
# disarm the guard. Only checked when the captured word is itself a number —
# "the call sites" must not read as a stale count.
_CALL_SITE_COUNT = re.compile(r"([A-Za-z0-9]+)[*_`]*\s+call\s+sites", re.IGNORECASE)
_NUMBER_WORDS = {
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
    11: "eleven",
    12: "twelve",
}


def _declared_intent(node: ast.AST) -> str | None:
    """The intent this ``reasoning_intent=`` value names, or ``None``.

    Every spelling the runtime honours has to be recognised, or the guard is
    trivially bypassed by writing the declaration a different way:

    - ``ReasoningIntent.INFERENCE`` — the canonical form. Matched on the
      *attribute* alone, not on the qualifier, so ``RI.INFERENCE`` from an
      aliased import and ``base.ReasoningIntent.INFERENCE`` are covered too.
    - ``"inference"`` — a bare string. NOT hypothetical: ``coerce`` accepts it
      and ``router.route`` coerces (router.py:456) *before* the
      INFERENCE-requires-a-floor check on the next line, so the string behaves
      identically to the member. mypy is no backstop here (``ignore_errors``).
    - ``ReasoningIntent["INFERENCE"]`` — subscript lookup, same object.

    The one form left uncovered is a **bare name** (``reasoning_intent=_CONST``
    or a forwarded parameter). It is deliberate and unfixable at this level: a
    bare ``Name`` is syntactically identical whether it forwards a parameter or
    dereferences a module constant, and forwarding is what ``router.py`` and
    ``milestone_engine._generate_structured_output`` actually do. Treating
    names as declarations would demand table rows for plumbing and get the
    guard switched off; treating them as plumbing loses a rare declaration
    style nothing in the tree uses. The trade is stated rather than hidden.
    """
    if isinstance(node, ast.Attribute) and node.attr in _MEMBER_NAMES:
        return node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _VALUE_TO_NAME.get(node.value)
    if isinstance(node, ast.Subscript):
        key = node.slice
        if isinstance(key, ast.Constant) and key.value in _MEMBER_NAMES:
            return str(key.value)
    return None


def _declares_in(source: str) -> list[str]:
    """Every declaring call in ``source``, rendered as the table renders it.

    A declaration is a call keyword ``reasoning_intent=<names an intent>``.
    When the same call also passes ``min_output_tokens``, that is recorded too
    — the pair is the unit that matters, since ``INFERENCE`` without a floor is
    a ``ValueError`` at the router and a refused lift at the Gemini provider.
    """
    declares: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        if "reasoning_intent" not in keywords:
            continue
        intent = _declared_intent(keywords["reasoning_intent"])
        if intent is None:
            continue  # forwarded parameter, not a declaration
        parts = [f"reasoning_intent={intent}"]
        floor = keywords.get("min_output_tokens")
        if floor is not None:
            parts.append(f"min_output_tokens={ast.unparse(floor)}")
        declares.append(", ".join(parts))
    return declares


def _declared_call_sites() -> Counter:
    """``(path relative to faultmaven/, declares)`` → how many such calls.

    A count rather than a set: ``out_of_band.py`` declares the same knobs at
    two separate call sites (triage and answer), and the table lists both.

    Every module is parsed. An earlier version skipped files not containing
    the literal ``ReasoningIntent``, which meant a module declaring purely by
    string spelling was never even read.
    """
    found: Counter = Counter()
    for path in sorted(_PACKAGE.rglob("*.py")):
        for declares in _declares_in(path.read_text(encoding="utf-8")):
            found[(str(path.relative_to(_PACKAGE)), declares)] += 1
    return found


def _documented_call_sites(text: str) -> Counter:
    """The same shape, parsed out of the ``| Call site | Declares |`` table.

    Anchored on the header row rather than on surrounding prose: the prose is
    rewritten far more often than the column names are.
    """
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == _TABLE_HEADER)
    except StopIteration:  # pragma: no cover - the guard below reports it
        return Counter()

    documented: Counter = Counter()
    for line in lines[start + 2 :]:  # skip the header and its separator
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 2:
            break
        site, declares = cells
        path = _BACKTICKED.search(site)
        assert path, f"call-site cell names no path in backticks: {line}"
        knobs = [
            token
            for token in _BACKTICKED.findall(declares)
            if token.split("=", 1)[0] in _KNOBS
        ]
        documented[(path.group(1), ", ".join(knobs))] += 1
    return documented


def _engine_constant(name: str) -> int:
    """Read a module-level ``int`` constant out of ``milestone_engine`` by AST.

    Parsed rather than imported: the engine pulls in most of the application,
    and this guard needs one integer.
    """
    tree = ast.parse(_ENGINE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return int(node.value.value)
    raise AssertionError(f"{name} is not a module-level int in {_ENGINE.name}")


@pytest.mark.unit
@pytest.mark.architecture
def test_the_scanner_finds_the_shipped_call_sites() -> None:
    """Positive control: the AST scan is not silently finding nothing.

    Without this, an import rename or a parse that quietly returned zero would
    make the guard below pass against any CLAUDE.md at all — the same vacuity
    the sibling #1246 guard has to defend against.
    """
    assert _declared_call_sites(), "the AST scan found no declaring call site"


@pytest.mark.unit
@pytest.mark.architecture
def test_the_scanner_separates_declarations_from_plumbing() -> None:
    """Positive control: the discriminator actually discriminates.

    ``router.py`` and ``milestone_engine``'s ``_generate_structured_output``
    both forward the parameter, and every translating provider compares against
    the enum members. If any of those read as a declaration the guard would
    demand table rows for plumbing and be turned off; if a real declaration did
    not read as one it would never fire at all.
    """
    plumbing = """
def route(reasoning_intent=None, min_output_tokens=None):
    if reasoning_intent is ReasoningIntent.INFERENCE and min_output_tokens is None:
        raise ValueError("floor required")
    return provider.generate(
        reasoning_intent=reasoning_intent,
        min_output_tokens=min_output_tokens,
    )
"""
    assert _declares_in(plumbing) == []

    declaration = """
response = await router.route(
    messages=messages,
    reasoning_intent=ReasoningIntent.INFERENCE,
    min_output_tokens=TOOLLESS_INFERENCE_OUTPUT_FLOOR,
)
"""
    assert _declares_in(declaration) == [
        "reasoning_intent=INFERENCE, "
        "min_output_tokens=TOOLLESS_INFERENCE_OUTPUT_FLOOR"
    ]

    no_floor = "router.route(reasoning_intent=ReasoningIntent.EXTRACTION)\n"
    assert _declares_in(no_floor) == ["reasoning_intent=EXTRACTION"]


@pytest.mark.unit
@pytest.mark.architecture
def test_the_scanner_reads_every_honoured_spelling() -> None:
    """A declaration written any way the runtime honours is still a declaration.

    Each of these reaches the provider as the same member, so a guard that saw
    only the canonical attribute could be bypassed — accidentally or not — by
    writing the call differently. The string form is the live one: ``coerce``
    accepts it and the router coerces before its floor check.
    """
    by_string = 'router.route(reasoning_intent="inference", min_output_tokens=N)\n'
    assert _declares_in(by_string) == [
        "reasoning_intent=INFERENCE, min_output_tokens=N"
    ]

    by_subscript = 'router.route(reasoning_intent=ReasoningIntent["EXTRACTION"])\n'
    assert _declares_in(by_subscript) == ["reasoning_intent=EXTRACTION"]

    aliased = "router.route(reasoning_intent=RI.INFERENCE, min_output_tokens=N)\n"
    assert _declares_in(aliased) == ["reasoning_intent=INFERENCE, min_output_tokens=N"]

    # A string that is not a member spelling names no intent, and must not be
    # reported as one.
    assert _declares_in('router.route(reasoning_intent="banana")\n') == []


@pytest.mark.unit
@pytest.mark.architecture
def test_the_table_parser_reads_the_shipped_table() -> None:
    """Positive control: the table is where the parser looks for it.

    A renamed header, a reflowed row or a table moved out from under this
    anchor would otherwise show up as "the code declares five sites and the doc
    documents none" — a real failure, but reported as drift rather than as the
    parser losing its footing.
    """
    documented = _documented_call_sites(_CLAUDE_MD.read_text(encoding="utf-8"))
    assert documented, (
        f"no rows parsed under the {_TABLE_HEADER!r} header in CLAUDE.md — "
        "the table was renamed, moved or reshaped; update this parser with it"
    )


@pytest.mark.unit
@pytest.mark.architecture
def test_claude_md_documents_every_reasoning_intent_call_site() -> None:
    """The guard: the table lists exactly the calls that declare an intent."""
    declared = _declared_call_sites()
    documented = _documented_call_sites(_CLAUDE_MD.read_text(encoding="utf-8"))

    missing = declared - documented
    stale = documented - declared
    assert not missing and not stale, (
        "CLAUDE.md's reasoning-intent call-site table has drifted from the "
        f"code (#1357).\n  undocumented in CLAUDE.md: {sorted(missing)}\n"
        f"  claimed by CLAUDE.md but not in the code: {sorted(stale)}\n"
        "Paths in the table are relative to faultmaven/. Add, remove or "
        "correct the row — and re-read the prose around the table, which "
        "asserts what the declared intents are and whether any of them lifts "
        "a starvation guard."
    )


@pytest.mark.unit
@pytest.mark.architecture
def test_claude_md_counts_the_call_sites_correctly() -> None:
    """Every prose count of "N call sites" matches the table.

    Two sentences carry the count — one in the Gemini shape-rule paragraph, one
    opening the knobs section — and both were wrong before #1357. Correcting
    the table alone would leave the sentence a reader hits first still saying
    "four".
    """
    total = sum(_declared_call_sites().values())
    assert total in _NUMBER_WORDS, (
        f"{total} declaring call sites — extend _NUMBER_WORDS so the prose "
        "count can still be checked"
    )
    expected = {_NUMBER_WORDS[total], str(total)}
    recognised = set(_NUMBER_WORDS.values())

    wrong = [
        match.group(0)
        for match in _CALL_SITE_COUNT.finditer(_CLAUDE_MD.read_text(encoding="utf-8"))
        if (word := match.group(1).lower()) in recognised or word.isdigit()
        if word not in expected
    ]
    assert not wrong, (
        f"CLAUDE.md counts the reasoning-intent call sites as {wrong}; the code "
        f"declares {total} ({_NUMBER_WORDS[total]})."
    )


@pytest.mark.unit
@pytest.mark.architecture
def test_claude_md_states_the_output_floor_correctly() -> None:
    """The two numeric claims the #1357 prose added are pinned to the code.

    The table records the constant's *name*, so on its own it would let the
    value drift under the sentence that quotes it — the same defect class
    #1357 exists to close, re-introduced by the fix for it. The prose asserts
    both a literal ("(2048)") and a relation ("well under
    ``STRUCTURED_OUTPUT_MAX_TOKENS``"); the relation is what makes the floor
    safe, because a floor at or above the cap would raise the cap rather than
    merely forbidding a starvable partition.
    """
    floor = _engine_constant("TOOLLESS_INFERENCE_OUTPUT_FLOOR")
    cap = _engine_constant("STRUCTURED_OUTPUT_MAX_TOKENS")
    text = _CLAUDE_MD.read_text(encoding="utf-8")

    quoted = re.search(r"TOOLLESS_INFERENCE_OUTPUT_FLOOR``?\s*\((\d+)\)", text)
    assert quoted, (
        "CLAUDE.md no longer quotes TOOLLESS_INFERENCE_OUTPUT_FLOOR's value; "
        "either restore the '(N)' form or drop this guard with it"
    )
    assert int(quoted.group(1)) == floor, (
        f"CLAUDE.md says TOOLLESS_INFERENCE_OUTPUT_FLOOR is "
        f"{quoted.group(1)}; milestone_engine.py says {floor}."
    )
    assert floor < cap, (
        f"CLAUDE.md says the floor sits 'well under' "
        f"STRUCTURED_OUTPUT_MAX_TOKENS, but {floor} is not below {cap} — the "
        "floor would raise the generation cap instead of only forbidding a "
        "starvable partition."
    )
