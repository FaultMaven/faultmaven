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

The code side is an AST scan, deliberately not a grep: ``black`` reflows call
sites across lines, so a text anchor written today silently stops matching
after the next format pass. The scan keys on the one thing that distinguishes a
*declaring* call site from the plumbing around it — a keyword argument
``reasoning_intent=`` whose value is a literal ``ReasoningIntent`` member.
``route(reasoning_intent=reasoning_intent)`` forwards a name and is not a
declaration; ``if intent is ReasoningIntent.INFERENCE`` is a comparison, not a
call keyword. Both are excluded by construction rather than by an exemption
list that could itself drift.
"""

import ast
import re
from collections import Counter
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_CLAUDE_MD = _PROJECT_ROOT / "CLAUDE.md"
_PACKAGE = _PROJECT_ROOT / "faultmaven"

# The enum whose members a call site names to declare its intent.
_INTENT_CLASS = "ReasoningIntent"
# The knobs the table's "Declares" column records, in the order it lists them.
_KNOBS = ("reasoning_intent", "min_output_tokens")

_TABLE_HEADER = "| Call site | Declares |"
_BACKTICKED = re.compile(r"`([^`]+)`")
# A prose count of call sites, wrap-tolerant. Only checked when the captured
# word is itself a number — "the call sites" must not read as a stale count.
_CALL_SITE_COUNT = re.compile(r"([A-Za-z]+)\s+call\s+sites", re.IGNORECASE)
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


def _declares_in(source: str) -> list[str]:
    """Every declaring call in ``source``, rendered as the table renders it.

    A declaration is a call keyword ``reasoning_intent=ReasoningIntent.X``.
    When the same call also passes ``min_output_tokens``, that is recorded too
    — the pair is the unit that matters, since ``INFERENCE`` without a floor is
    a ``ValueError`` at the router and a refused lift at the Gemini provider.
    """
    declares: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        intent = keywords.get("reasoning_intent")
        if not (
            isinstance(intent, ast.Attribute)
            and isinstance(intent.value, ast.Name)
            and intent.value.id == _INTENT_CLASS
        ):
            continue  # forwarded parameter, not a declaration
        parts = [f"reasoning_intent={intent.attr}"]
        floor = keywords.get("min_output_tokens")
        if floor is not None:
            parts.append(f"min_output_tokens={ast.unparse(floor)}")
        declares.append(", ".join(parts))
    return declares


def _declared_call_sites() -> Counter:
    """``(path relative to faultmaven/, declares)`` → how many such calls.

    A count rather than a set: ``out_of_band.py`` declares the same knobs at
    two separate call sites (triage and answer), and the table lists both.
    """
    found: Counter = Counter()
    for path in sorted(_PACKAGE.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if _INTENT_CLASS not in source:
            continue
        for declares in _declares_in(source):
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
        "Add, remove or correct the row — and re-read the prose around the "
        "table, which asserts what the declared intents are and whether any "
        "of them lifts a starvation guard."
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
