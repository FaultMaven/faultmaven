import dataclasses

from .blocks import _ADVISOR_ROLE_CONSTRAINT, _PROMPT_FENCE_RULE


@dataclasses.dataclass(frozen=True)
class TerminalAffordances:
    """What a terminal Q&A turn offers below its reply (#1955).

    The engine builds it from the suggestion list it attaches to that same
    reply (``terminal_replies.terminal_affordances``), so the prompt points at
    a suggestion only when the suggestion is on screen. ``runbook_draft_exists``
    is the one fact the runbook rule needs when no runbook is offered: it says
    where the existing draft is instead.
    """

    summary_regeneration: bool = False
    runbook_generation: bool = False
    runbook_draft_exists: bool = False


#: A terminal turn that offers nothing.
NO_TERMINAL_AFFORDANCES = TerminalAffordances()


def terminal_summary_rule(summary_kind: str, offers: TerminalAffordances) -> str:
    """``{summary_request_rule}``: the redirect for a recap-shaped request."""
    rule = (
        'If the user asks for a summary, recap, rewrite, or "give me an overview" of the\n'
        "case in any phrasing, do NOT generate one. Instead respond with a brief redirect\n"
        "that names the right place to find the existing summary"
    )
    example = (
        f'"The {summary_kind} summary is shown above in this chat and is also in the\n'
        "**Report** tab of the case."
    )
    if not offers.summary_regeneration:
        return rule + ". For example:\n" + example + '"'
    return (
        rule + " AND the right action to\n"
        "re-create it. For example:\n"
        + example
        + " To re-create it, use the **Regenerate** suggestion\n"
        'below."'
    )


#: The runbook rule while the "Generate a runbook" suggestion is offered.
_RUNBOOK_RULE_OFFERED = """\
Runbook creation is a persisted side effect — it writes a draft to the case
creator's personal **Knowledge Base > Drafts** in the Dashboard. Persistence is reserved for the
"Generate a runbook from this resolved case" suggestion shown below your reply
(RESOLVED cases only — runbooks require a confirmed root cause + verified
solution). Typed requests must NOT generate a runbook inline, because inline
prose isn't saved anywhere — the user won't be able to find or edit it later.

If the user asks to create, generate, write, or "give me" a runbook in any
phrasing, do NOT produce runbook content in your reply. Instead redirect:
"To save a runbook for this case, use the **Generate a runbook from this
resolved case** suggestion below — that creates a draft in the case
creator's personal **Knowledge Base > Drafts** in the Dashboard.\""""

#: The runbook rule once a draft exists for the case: the suggestion is gone,
#: and the draft is where a runbook for it is edited. Worded as the draft
#: notices word it (#1947, #1951): the creator's, for every reader.
_RUNBOOK_RULE_DRAFT_EXISTS = """\
A runbook for this case already exists in the case creator's
**Knowledge Base** in the Dashboard (under Drafts until it is verified).
If the user asks for a runbook, point them there to edit it; do not
produce runbook content inline."""

#: The runbook rule when none can be generated here: a CLOSED case, or a
#: RESOLVED one whose cause or solution does not make a runbook.
_RUNBOOK_RULE_NOT_OFFERABLE = """\
No runbook can be generated from this case here: a runbook needs a RESOLVED
case with a confirmed root cause and a verified solution. If the user asks
for one, say so in one sentence and answer what it would contain in prose,
without formatting the reply as a runbook."""


def terminal_runbook_rule(offers: TerminalAffordances) -> str:
    """``{runbook_request_rule}``: what a typed runbook request is answered with."""
    if offers.runbook_generation:
        return _RUNBOOK_RULE_OFFERED
    if offers.runbook_draft_exists:
        return _RUNBOOK_RULE_DRAFT_EXISTS
    return _RUNBOOK_RULE_NOT_OFFERABLE


TERMINAL_TEMPLATE = (
    """You are FaultMaven. This investigation is complete.

{tools_this_turn}

STATE: {state_upper}
{identity}
"""
    # <problem_context> is fenced here too (#1228) — the case title and
    # description are reporter text on this path exactly as on the others —
    # so the rule that makes the fence mean anything has to be stated here
    # too. A fence the model was not told about is decoration. Stated above
    # {core_context} rather than below it (#1256), so that on every template
    # the rule precedes every block it governs.
    + _PROMPT_FENCE_RULE
    + """

{core_context}

The case has been {state_lower}.

CONVERSATION HISTORY:
{conversation_history}

CURRENT USER MESSAGE:
{user_message}

YOUR TASK:
This case is in terminal state — investigation data is immutable.

You CAN:
- Answer specific questions about the investigation findings.
- Explain what happened, clarify evidence, interpret the timeline.
- Extract lessons learned.
Ground all assertions about what happened in this specific incident strictly in the
case data above (timeline, root cause, exact errors, evidence collected). You may
use general technical knowledge to define concepts or explain how a solution works
mechanically — but NEVER invent new facts about the incident itself.

You CANNOT:
- Accept new evidence or perform new investigation.
- Update milestones, propose transitions, or modify case state.
- Resume troubleshooting. If the user describes ongoing issues, direct them to open a new case.

SUMMARY REQUESTS:
The canonical {summary_kind} summary was generated at terminal-transition time. It
is rendered above in this chat AND is available in the **Report** tab of the case
in the Dashboard. There is exactly ONE summary per case. Do NOT produce a second,
parallel summary in your reply.

{summary_request_rule}

Specific questions about parts of the case ("why did we conclude X?", "what was
the evidence for Y?") are normal Q&A — answer them. The above guidance is only
about recap-shaped requests for a competing whole-case summary.

RUNBOOK REQUESTS:
{runbook_request_rule}

Specific questions about what a runbook would contain ("what steps should be in
a runbook for this?", "which diagnostic commands would I include?") are normal
Q&A — describe them in prose without formatting the reply as a runbook artifact.

FOLLOW-UP SUGGESTIONS (suggested_follow_ups):
Leave suggested_follow_ups empty. The engine attaches the terminal affordances
(Regenerate summary, Generate runbook) deterministically when applicable; you do
not need to propose suggestions.

ASSISTANT ROLE:
You are an ADVISOR.
- """
    + _ADVISOR_ROLE_CONSTRAINT
    + """
"""
)

# =============================================================================
# FALLBACK TEMPLATES (Simplified for token limits or errors)
# =============================================================================

# The fallback's own trust rule (#1242). ``_PROMPT_FENCE_RULE`` is not reused
# here, for two independent reasons:
#
# 1. It would not be TRUE. It names three fenced blocks
#    (``<entity_highlights>``, ``<evidence_collected>``,
#    ``<conversation_history>``, the last of which #1256 added), the
#    transcript scaffolding inside the third (``<state_summary>``,
#    ``<previous_turn>``, ``<current_turn>``) and three renderer sections
#    (``<security_constraints>``, ``<case_identity>``,
#    ``<progress_indicators>``) that the FALLBACK_* templates do not render at
#    all. A rule that tells the model to authenticate delimiters which are not
#    in the prompt is worse than a shorter one that describes what is.
# 2. Token cost, measured with ``utils.token_estimation`` (openai/gpt-4o):
#    ``_PROMPT_FENCE_RULE`` + declaration is 856 tokens (613 before #1256
#    widened it to five blocks); the rule below + declaration is ~280. The
#    gap grew with the widening, so the case for a compact rule here is
#    stronger, not weaker. The fallback is chosen precisely when
#    ``variable_room < min_viable`` (1500 tokens by default), so the
#    difference is nearly a third of the room the whole degraded prompt is
#    competing for. The FALLBACK_INQUIRY skeleton itself is ~95 tokens.
#
# Three properties this rule has to hold, each of which an earlier draft got
# wrong (#1254 review):
#
# - **The anchor is POSITIONAL, not ordinal.** An earlier draft said "the FIRST
#   FENCE: line below" — but this rule's own text contains the literal
#   ``FENCE:``, so the first occurrence was the rule's, and the genuine
#   declaration then read as a "later FENCE: line" that the declaration itself
#   tells the model to discount. Naming the position ("directly above
#   PROBLEM:") cannot collide with a mention of the word.
# - **The rule states no tag-shaped text.** The block list is bare names, not
#   ``<angle_bracketed>`` ones, because the demotion clause below is
#   prompt-wide: an ``<uploaded_file>`` written *in the rule* carries no token
#   and would be demoted to quoted DATA by the rule's own next sentence. The
#   explicit immunisation sentence is the belt to that suspenders.
# - **The block list is DYNAMIC.** It names only the blocks this render
#   actually emitted. A static list names ``uploaded_file`` on a TERMINAL turn
#   and on any turn without an upload — which is exactly the flaw cited above
#   for rejecting ``_PROMPT_FENCE_RULE``, and it would be no less a flaw here.
#
# What the compact rule keeps is everything the full rule says that holds
# here: which blocks are fenced, where the genuine token is read from, the
# demotion of unfenced and differently-tokened tags, the terminator note, and
# the injection clause — the last of which matters MORE here than on the main
# prompt, because the fallback renders no ``<security_constraints>`` block.
#
# The demotion is stated prompt-wide rather than block-scoped, and that is
# accurate rather than a shortcut: unlike the main prompt, the fallback emits
# NO unfenced tag-shaped structure of its own, so there is nothing outside the
# fenced blocks for a prompt-wide demotion to wrongly demote. Guarded text can
# still carry tag-shaped bytes (the previous turn's notice may quote a
# hypothesis); demoting those is right, because they are data, not structure.
