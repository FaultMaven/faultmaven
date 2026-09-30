"""``env_var_spellings`` is the one statement of "which keys are this variable".

pydantic-settings binds a variable case-insensitively, the last spelling in the
environment winning, so every key whose ``.upper()`` equals the name's is the
same variable to it. ``set_env_var`` removes exactly those keys before setting
the name, and ``tests/utils.delenv_every_spelling`` clears exactly those. The
rule used to be written three times (#1790); these tests pin the one copy.

``PROBE`` is the plan's mechanism probe (#1815): three real spellings beside
three near-misses that share their letters (a trailing extra character, a
leading one, and a hyphen for the underscore). A prefix, suffix, substring or
separator-normalising match would take one of the near-misses.
"""

import os

import pytest

from faultmaven.config.settings import env_var_spellings, set_env_var
from tests.utils import delenv_every_spelling

PROBE = {
    "DATABASE_URL": "1",
    "database_url": "2",
    "Database_Url": "3",
    "DATABASE_URLX": "4",
    "XDATABASE_URL": "5",
    "DATABASE-URL": "6",
}
SPELLINGS = ["DATABASE_URL", "database_url", "Database_Url"]
NEAR_MISSES = {"DATABASE_URLX": "4", "XDATABASE_URL": "5", "DATABASE-URL": "6"}


def _set_env_var_before_1815(env, name, value):
    """``set_env_var`` as it stood before the predicate was extracted.

    Stated here, not imported, so the extraction is compared with the code it
    replaced rather than with itself.
    """
    for key in [key for key in env if key.upper() == name.upper()]:
        del env[key]
    env[name] = value


@pytest.mark.unit
def test_every_letter_case_spelling_and_no_near_miss_in_env_order():
    assert env_var_spellings(PROBE, "DATABASE_URL") == SPELLINGS


@pytest.mark.unit
def test_the_order_is_the_environments_not_a_sort():
    reversed_env = dict(reversed(list(PROBE.items())))

    assert env_var_spellings(reversed_env, "DATABASE_URL") == list(reversed(SPELLINGS))


@pytest.mark.unit
@pytest.mark.parametrize("name", ["DATABASE_URL", "database_url", "dAtAbAsE_uRl"])
def test_the_name_itself_matches_in_any_letter_case(name):
    assert env_var_spellings(PROBE, name) == SPELLINGS


@pytest.mark.unit
def test_no_spelling_is_an_empty_list_and_the_env_is_not_touched():
    env = dict(PROBE)

    assert env_var_spellings(env, "JWT_SECRET_KEY") == []
    assert env == PROBE


@pytest.mark.unit
def test_set_env_var_leaves_the_exact_name_as_the_only_spelling():
    env = dict(PROBE)

    set_env_var(env, "DATABASE_URL", "new")

    assert env == {**NEAR_MISSES, "DATABASE_URL": "new"}
    assert list(env) == [*NEAR_MISSES, "DATABASE_URL"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "env",
    [
        PROBE,
        dict(reversed(list(PROBE.items()))),
        {},
        {"database_url": "only-lowercase"},
        NEAR_MISSES,
    ],
    ids=["probe", "probe-reversed", "empty", "lowercase-only", "near-misses-only"],
)
def test_set_env_var_removes_the_same_keys_it_did_before_the_extraction(env):
    ours, before = dict(env), dict(env)

    set_env_var(ours, "DATABASE_URL", "new")
    _set_env_var_before_1815(before, "DATABASE_URL", "new")

    assert list(ours.items()) == list(before.items())


@pytest.mark.unit
def test_delenv_every_spelling_clears_the_spellings_and_keeps_the_near_misses(
    monkeypatch,
):
    for key, value in PROBE.items():
        monkeypatch.setenv(key, value)

    delenv_every_spelling(monkeypatch, "DATABASE_URL")

    assert [key for key in PROBE if key in os.environ] == list(NEAR_MISSES)
