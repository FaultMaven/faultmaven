"""Every ID-reference field coerces a bare integer to ``new_index_N``.

Regression for the Gemini shape failure (Variant E): the model emits an
unquoted integer where the schema declares ``str``. Gemini's function-calling
tool spec carries ``type`` as advisory metadata rather than a decoding
constraint, so the wire protocol accepts the integer and Pydantic 500s the turn
client-side.

The fix is the ``IdRef`` annotated type. These tests pin the *property* rather
than a hand-maintained field list: a field whose documented contract admits a
``new_index_N`` placeholder must carry ``IdRef``, and vice versa. That
bidirectional invariant is what stops the coercion drifting out of sync with
the schema again — three causal-graph fields (``produces``, ``cause``,
``effect``) were already missing it, and no name-based sweep would have found
them.
"""

import inspect
import typing

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from faultmaven.core.investigation import schemas as s

pytestmark = pytest.mark.unit

_COERCER = "_coerce_bare_int_to_new_index"


def _is_id_ref(field) -> bool:
    """True when the field's annotation carries the IdRef BeforeValidator.

    Covers the bare (``IdRef``), optional (``Optional[IdRef]``) and list
    (``List[IdRef]``) spellings — Pydantic hoists metadata to ``.metadata``
    only for the bare form, leaving it inline in the annotation otherwise.
    """
    return _COERCER in str(field.annotation) + str(field.metadata)


def _documents_new_index(field) -> bool:
    return "new_index" in (field.description or "")


def _all_models():
    """Every BaseModel in the schema module, including nested ones.

    Several response schemas define their ``*StateUpdate`` payloads as classes
    nested inside the response class, so a sweep over module-level names alone
    would silently skip them — and a field added there would escape both
    direction tests below.
    """
    seen, stack = {}, [
        obj
        for obj in vars(s).values()
        if inspect.isclass(obj) and issubclass(obj, BaseModel)
    ]
    while stack:
        model = stack.pop()
        if model.__qualname__ in seen:
            continue
        seen[model.__qualname__] = model
        stack.extend(
            obj
            for obj in vars(model).values()
            if inspect.isclass(obj) and issubclass(obj, BaseModel)
        )
    return seen


def _all_fields():
    """(model_name, field_name, FieldInfo) for every schema model."""
    for model_name, model in _all_models().items():
        for field_name, field in model.model_fields.items():
            yield model_name, field_name, field


def _adapter_for(field) -> TypeAdapter:
    """A validator for the field's declared type, in isolation.

    Validating the field alone keeps these tests free of every host model's
    required fields and cross-field invariants, which are irrelevant to the
    coercion and would otherwise dominate the fixtures.
    """
    annotation = field.annotation
    if field.metadata:
        annotation = typing.Annotated[tuple([annotation, *field.metadata])]
    return TypeAdapter(annotation)


_ID_REF_FIELDS = [(m, f, fld) for m, f, fld in _all_fields() if _is_id_ref(fld)]


def test_the_schema_actually_has_id_ref_fields():
    """Guard the sweep itself: an empty set would make every test below vacuous."""
    assert len(_ID_REF_FIELDS) >= 15


@pytest.mark.parametrize(
    "model_name,field_name,field",
    _ID_REF_FIELDS,
    ids=[f"{m}.{f}" for m, f, _ in _ID_REF_FIELDS],
)
def test_id_ref_field_coerces_bare_int(model_name, field_name, field):
    """A bare integer becomes the ``new_index_N`` placeholder."""
    adapter = _adapter_for(field)
    value = [3] if "List" in str(field.annotation) else 3
    expected = ["new_index_3"] if isinstance(value, list) else "new_index_3"
    assert adapter.validate_python(value) == expected


@pytest.mark.parametrize(
    "model_name,field_name,field",
    _ID_REF_FIELDS,
    ids=[f"{m}.{f}" for m, f, _ in _ID_REF_FIELDS],
)
def test_id_ref_field_passes_real_ids_through(model_name, field_name, field):
    """Coercion must not touch a well-formed existing ID."""
    adapter = _adapter_for(field)
    is_list = "List" in str(field.annotation)
    value = ["hyp_real123abc12"] if is_list else "hyp_real123abc12"
    assert adapter.validate_python(value) == value


def test_every_field_documenting_new_index_is_an_id_ref():
    """The contract in the description must be backed by the annotation.

    A field whose description promises the LLM it may emit ``new_index_N``
    but which lacks ``IdRef`` is exactly the drift this consolidation removed:
    the promise is made, the bare-int form 500s.
    """
    missing = [
        f"{m}.{f}"
        for m, f, fld in _all_fields()
        if _documents_new_index(fld) and not _is_id_ref(fld)
    ]
    assert not missing, (
        "these fields document a new_index_N contract but are not IdRef: " f"{missing}"
    )


def test_every_id_ref_documents_its_new_index_contract():
    """The converse: no silent coercion the prompt never told the LLM about.

    Coercion is only sound where a same-turn reference is meaningful. If a
    field carries IdRef, its description has to say so — otherwise the model
    is never told the placeholder form exists, and the annotation is either
    dead or papering over a field that should stay strict.
    """
    undocumented = [
        f"{m}.{f}"
        for m, f, fld in _all_fields()
        if _is_id_ref(fld) and not _documents_new_index(fld)
    ]
    assert (
        not undocumented
    ), f"these IdRef fields do not document new_index_N: {undocumented}"


def test_fields_whose_consumer_cannot_resolve_a_placeholder_stay_strict():
    """Fields whose consumer only matches a persisted id must NOT coerce.

    Membership in ``IdRef`` is decided by the consumer, not the field name.
    ``_guard_source_file`` demotes an unresolvable ``source_file_id`` to
    USER_DESCRIPTION, and the solution apply-path keeps ``node_ref`` only when
    it is already in ``case.causal_nodes`` — neither resolves ``new_index_N``,
    so annotating them would advertise a form the engine cannot honour.

    (``RootCauseConclusionUpdate.names_root_node_id`` is deliberately NOT in
    this list: milestone_engine B2 (#695) does resolve its placeholder, so it
    carries ``IdRef``.)
    """
    for model_name, field_name in (
        ("EvidenceToAdd", "source_file_id"),
        ("SolutionToAdd", "node_ref"),
    ):
        field = getattr(s, model_name).model_fields[field_name]
        assert not _is_id_ref(field), f"{model_name}.{field_name} must stay strict"
        with pytest.raises(ValidationError):
            _adapter_for(field).validate_python(3)


def test_booleans_are_not_coerced():
    """``bool`` is an ``int`` subclass — it must not become ``new_index_True``."""
    field = s.HypothesisEvidenceLinkToAdd.model_fields["hypothesis_id_ref"]
    with pytest.raises(ValidationError):
        _adapter_for(field).validate_python(True)


def test_sentinel_node_ref_survives():
    """``'D'`` (the problem node) is a valid ref and must pass through."""
    field = s.CausalEdgeToAdd.model_fields["cause"]
    assert _adapter_for(field).validate_python("D") == "D"
