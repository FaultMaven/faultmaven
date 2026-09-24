"""Out-of-range confidence from a provider that does not enforce the bound (fm#1502).

Every probability-shaped field the engine asks the model for is
``Field(ge=0, le=1)``, and that bound reaches the wire (fm#355, pinned by
``test_schema_constraints_reach_the_provider.py``). STRICT providers enforce
it. FUNCTION_CALLING and BEST_EFFORT providers do not, so a model can still
answer ``likelihood: 90`` meaning 90%. Before this module Pydantic rejected
that, the engine's backstop pruned the record carrying it, and a value on a
single-object sub-record (no list index to prune) dropped **every**
``state_updates`` — valid siblings included. One bad number could cost an
otherwise-good turn.

The ruling (fm#1502, 2026-09-19 as amended 2026-09-24) is asymmetric by the
field's ROLE, never by its value:

- **ADD-shaped** — the record has no prior value, so the alternative to a
  repair is losing the whole record. A value in ``(1, 100]`` is read as a
  percentage and rescaled (``90 -> 0.90``); a ``bool`` is coerced to
  ``1.0``/``0.0``. Anything else (negative, ``> 100``, NaN, infinity, a
  non-number) prunes **that record only**.
- **UPDATE-shaped** — the field's absence means "keep the stored value", so the
  field is dropped and the record kept. Only where absence means *keep*:
  dropping a field whose absence means a *default* silently writes the default,
  which is the clamp the ruling rejected.
- **Links** — absence means full confidence on a NEW link and "keep" on a
  re-emitted one, and only ingest knows which it is. So validation sets the
  value aside (outside the schema, the bound left intact) and ingest decides:
  a new link is rescaled or coerced when it can be, otherwise pruned — never
  given the default, which on a REFUTES link would be a decisive
  disconfirmation manufactured from garbage; a re-emitted link keeps its stored
  value — and so does one whose confidence was simply omitted, on both axes.
  "Re-emitted" means the SAME CLAIM: the same evidence at the same stance. A re-emission that flips the stance is a new claim, and the stored
  value is confidence in the old one — keeping it would, for example, turn a
  confident REFUTES into a confident SUPPORTS built from garbage — so it is
  decided as a new link, and pruning it leaves the stored link as it was.

Why a percentage rescale and not a clamp or a sentinel: it is the only repair
that keeps what the model meant, so it weakens neither a support nor a
refutation. Clamping overstates (a falsely certain hypothesis steers the
investigation); a sentinel understates, and on a refutation understating is the
unsafe direction. Both were built and rejected on #1498.

Every action is observable twice: a per-field Prometheus counter
(``faultmaven_schema_field_repairs_total``) and a note on the turn's
``validation_repairs``. Validators cannot see the engine, so they report through
the validation context (``model_validate_json(..., context=...)``) under
:data:`CONFIDENCE_REPAIRS_CONTEXT_KEY`; the degradation ladder supplies a fresh
sink per attempt, so an attempt that failed reports nothing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from faultmaven.core.investigation import reliability_metrics

#: Validation-context key a confidence validator appends its
#: :class:`ConfidenceRepair` records to. Absent context (a direct construction
#: in code or a test) repairs identically and simply reports nowhere.
CONFIDENCE_REPAIRS_CONTEXT_KEY = "fm_confidence_repairs"

#: Pydantic error ``type`` raised for an ADD-shaped confidence nothing can
#: repair. The ladder keys its per-field ``pruned`` count on it, reading the
#: field from the error's ``ctx`` — so the prune is attributed to the field that
#: caused it rather than inferred from the ``loc``.
CONFIDENCE_UNREPAIRABLE = "confidence_unrepairable"

#: How much of a raw value a note or a log line quotes. A model can put
#: anything in a number's place, including a paragraph.
_RAW_REPR_MAX = 40

#: The private attribute a link schema keeps a set-aside confidence on. Private
#: so that no JSON schema, strict rewrite or provider adapter can render it —
#: the field's own ``ge=0, le=1`` stays exactly as fm#355 left it.
LINK_SET_ASIDE_ATTR = "_stance_confidence_set_aside"


class ConfidenceAction(str, Enum):
    """What happened to one out-of-range confidence value."""

    RESCALED = "rescaled"  # (1, 100] read as a percentage
    COERCED = "coerced"  # bool -> 1.0 / 0.0
    DROPPED = "dropped"  # field removed; the stored value stands
    PRUNED = "pruned"  # the record carrying it was removed
    # A link's value set aside for ingest to decide. Internal to validation: it
    # never reaches the field counter, because ingest counts what it decided.
    SET_ASIDE = "set_aside"


#: Actions that keep what the model meant. A body whose every confidence action
#: is one of these is ``repaired``; anything else lost part of what the model
#: said.
MEANING_PRESERVING_ACTIONS = frozenset(
    {ConfidenceAction.RESCALED, ConfidenceAction.COERCED}
)


@dataclass(frozen=True)
class ConfidenceRepair:
    """One confidence value the engine had to act on."""

    schema: str  # the model class that owns the field, e.g. "HypothesisToAdd"
    field: str  # e.g. "likelihood"
    action: ConfidenceAction
    raw: Any
    value: Optional[float] = None
    where: Optional[str] = None  # where the value sat, when it is known

    def note(self) -> str:
        """The line this repair contributes to the turn's ``validation_repairs``."""
        head = f"{self.schema}.{self.field}: {short_repr(self.raw)}"
        if self.action is ConfidenceAction.RESCALED:
            return f"{head} read as a percentage, rescaled to {self.value:.4g}"
        if self.action is ConfidenceAction.COERCED:
            return f"{head} coerced to {self.value:.1f}"
        if self.action is ConfidenceAction.DROPPED:
            return (
                f"{head} is not a confidence in [0, 1]; field dropped, "
                "stored value kept"
            )
        if self.action is ConfidenceAction.PRUNED:
            at = f" at {self.where}" if self.where else ""
            return f"{head} unrepairable{at}; the record carrying it was pruned"
        return f"{head} set aside for ingest"


def short_repr(raw: Any) -> str:
    text = repr(raw)
    if len(text) > _RAW_REPR_MAX:
        text = text[: _RAW_REPR_MAX - 3] + "..."
    return text


def classify(raw: Any) -> tuple[str, Optional[float]]:
    """Read a confidence value. Returns ``(kind, value)``.

    ``kind`` is one of:

    - ``"conforming"`` — a number in ``[0, 1]`` (a numeric string included,
      which is how hand-parsed BEST_EFFORT output routinely spells one).
    - ``"rescaled"`` — a number in ``(1, 100]``, returned divided by 100.
    - ``"coerced"`` — a ``bool``, returned as ``1.0`` / ``0.0``. Checked before
      the number test because ``bool`` is an ``int``; Pydantic's lax mode would
      otherwise coerce it silently and the UPDATE-shaped fields could not
      tell ``true`` from a deliberate ``1.0``.
    - ``"unrepairable"`` — everything else: negative, above 100, NaN, infinity,
      or not a number at all. ``value`` is ``None``.

    ``None`` is not classified here: it means absent and every caller handles
    it before asking.
    """
    if isinstance(raw, bool):
        return "coerced", 1.0 if raw else 0.0
    if not isinstance(raw, (str, int, float)):
        return "unrepairable", None
    try:
        number = float(raw.strip() if isinstance(raw, str) else raw)
    except (ValueError, OverflowError):
        # OverflowError: JSON integers are unbounded, and ``float(10**400)``
        # raises rather than returning inf. It must become a validation error
        # like any other unusable value — an exception Pydantic does not wrap
        # would escape the degradation ladder and 500 the turn.
        return "unrepairable", None
    if not math.isfinite(number):
        return "unrepairable", None
    if 0.0 <= number <= 1.0:
        return "conforming", number
    if 1.0 < number <= 100.0:
        return "rescaled", number / 100.0
    return "unrepairable", None


def decide_link_at_ingest(
    raw: Any, *, re_emitted: bool
) -> tuple[ConfidenceAction, Optional[float]]:
    """Decide a set-aside link confidence once ingest knows whether the link
    re-states a stored one.

    ``re_emitted`` means a stored link for the same evidence asserts the SAME
    stance. A stance flip is not a re-emission: the stored value is confidence
    in a different claim, so absence cannot mean "keep" it.

    - Re-emitted: ``(DROPPED, None)`` — the caller keeps the stored value. Even
      a rescalable ``90`` is dropped here: the record is UPDATE-shaped, and a
      stored value is what absence already promises.
    - New, or a stance flip, repairable: ``(RESCALED | COERCED, value)``.
    - New, or a stance flip, unrepairable: ``(PRUNED, None)`` — the caller
      skips the link, so a stored link is left exactly as it was. The field's
      default must never stand in: ``1.0`` on a REFUTES link is a decisive
      disconfirmation nobody asserted.
    """
    if re_emitted:
        return ConfidenceAction.DROPPED, None
    kind, value = classify(raw)
    if kind == "rescaled":
        return ConfidenceAction.RESCALED, value
    if kind == "coerced":
        return ConfidenceAction.COERCED, value
    # "conforming" cannot reach here — conforming values are never set aside —
    # so anything else is a value with no magnitude to recover.
    return ConfidenceAction.PRUNED, None


def set_aside_link_confidence(link: Any) -> Any:
    """The raw confidence a link schema set aside, or ``None``.

    Read through ``__pydantic_private__`` rather than ``getattr`` because ingest
    is duck-typed and its tests pass ``SimpleNamespace``/``Mock`` links, on
    which a plain attribute read would invent a value. Never ``None`` when
    something was set aside: only a non-``None`` value is ever moved there.
    """
    private = getattr(link, "__pydantic_private__", None)
    if isinstance(private, dict):
        return private.get(LINK_SET_ASIDE_ATTR)
    return None


def settle_set_aside_link(
    link: Any,
    *,
    stored_stance: Any,
    where: str,
    notes: Optional[list] = None,
) -> Optional[tuple[ConfidenceAction, Optional[float]]]:
    """Ingest's half of a link confidence the schema set aside.

    ``stored_stance`` is the stance of the stored link for the same evidence,
    or ``None`` when there is none.

    ``None`` when nothing was set aside — the link's own field stands.
    Otherwise decides it (:func:`decide_link_at_ingest`), counts the decision,
    appends its note to ``notes`` when given, and returns ``(action, value)``:
    ``PRUNED`` means do not write the link; ``DROPPED`` (value ``None``) means
    keep the stored value. The ONE implementation both link paths call — the
    causal-node ingest and the hypothesis-link apply step.
    """
    raw = set_aside_link_confidence(link)
    if raw is None:
        return None
    re_emitted = stored_stance is not None and stored_stance == getattr(
        link, "stance", None
    )
    action, value = decide_link_at_ingest(raw, re_emitted=re_emitted)
    repair = ConfidenceRepair(
        schema=type(link).__name__,
        field="stance_confidence",
        action=action,
        raw=raw,
        value=value,
        where=f"link {where}",
    )
    count(repair)
    if notes is not None:
        notes.append(repair.note())
    return action, value


def report(context: Any, repair: ConfidenceRepair) -> None:
    """Append ``repair`` to the validation context's sink, if one was supplied."""
    if isinstance(context, dict):
        sink = context.get(CONFIDENCE_REPAIRS_CONTEXT_KEY)
        if isinstance(sink, list):
            sink.append(repair)


def count(repair: ConfidenceRepair) -> None:
    """One increment on ``faultmaven_schema_field_repairs_total``.

    ``SET_ASIDE`` is not counted: it is a deferral, and the ingest decision it
    defers to is counted when it is made.
    """
    if repair.action is ConfidenceAction.SET_ASIDE:
        return
    reliability_metrics.schema_field_repairs_total.labels(
        schema=repair.schema, field=repair.field, action=repair.action.value
    ).inc()
