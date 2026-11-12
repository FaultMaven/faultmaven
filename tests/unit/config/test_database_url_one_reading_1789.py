"""A ``DATABASE_URL`` with surrounding whitespace is refused, never stripped (#1789).

The persistence gate used to strip the value while the engine and the migration
URL did not, so ``" sqlite+aiosqlite:///./p.db"`` passed the gate, wrote durable
state, then failed to parse. A trailing newline even parses, as another database
(``p.db\\n``). Every reader now refuses the padded value, with a message that
never contains it: it can hold a password.
"""

from __future__ import annotations

import json
import os

import pytest

from faultmaven.config.settings import (
    DatabaseSettings,
    DatabaseUrlWhitespaceError,
    FaultMavenSettings,
    configured_database_url,
    describe_database_url,
    env_var_spellings,
    get_settings,
    persistent_database_configured,
    reset_settings,
)
from faultmaven.models.exceptions import ConfigurationError

pytestmark = pytest.mark.unit

PASSWORD = "FakePw1789"
URL = f"postgresql+asyncpg://fm_app:{PASSWORD}@127.0.0.1:1/p1789"
PADS = {
    "leading-space": " {}",
    "trailing-space": "{} ",
    "lf": "{}\n",
    "crlf": "{}\r\n",
    "tab": "\t{}",
}
MESSAGE = "DATABASE_URL has leading or trailing whitespace"


@pytest.fixture
def database_env(monkeypatch):
    """Set the database variables as the only spellings in the environment."""
    for name in ("DATABASE_URL", "DATABASE"):
        for key in env_var_spellings(os.environ, name):
            monkeypatch.delenv(key)
    monkeypatch.setenv("FAULTMAVEN_SKIP_DOTENV", "1")
    reset_settings()
    yield monkeypatch.setenv
    reset_settings()


def _assert_names_the_variable_not_the_value(error: BaseException) -> None:
    text = str(error)
    assert MESSAGE in text, text
    assert URL not in text and PASSWORD not in text, text


@pytest.mark.parametrize("pad", PADS.values(), ids=PADS.keys())
def test_the_settings_refuse_a_padded_url_without_echoing_it(database_env, pad):
    database_env("DATABASE_URL", pad.format(URL))

    with pytest.raises(DatabaseUrlWhitespaceError) as plain:
        DatabaseSettings()
    _assert_names_the_variable_not_the_value(plain.value)

    with pytest.raises(DatabaseUrlWhitespaceError) as alembic_reader:
        configured_database_url()
    _assert_names_the_variable_not_the_value(alembic_reader.value)

    with pytest.raises(ConfigurationError) as built:
        get_settings()
    _assert_names_the_variable_not_the_value(built.value)


@pytest.mark.parametrize("pad", PADS.values(), ids=PADS.keys())
def test_a_padded_url_in_a_nested_database_value_is_refused_too(database_env, pad):
    database_env("DATABASE", json.dumps({"database_url": pad.format(URL)}))

    with pytest.raises(DatabaseUrlWhitespaceError) as nested:
        FaultMavenSettings(_env_file=None)
    _assert_names_the_variable_not_the_value(nested.value)


def test_an_unpadded_url_reads_byte_identically(database_env):
    database_env("DATABASE_URL", URL)

    assert DatabaseSettings().database_url == URL
    assert configured_database_url() == URL
    assert get_settings().database.database_url == URL


@pytest.mark.parametrize("blank", [" ", "\n", " \t "])
def test_a_blank_url_is_still_no_database(database_env, blank):
    """Pinned as on ``main``: blank is not padding around a URL."""
    database_env("DATABASE_URL", blank)

    assert DatabaseSettings().database_url == blank
    assert configured_database_url() == blank
    assert persistent_database_configured(blank) is False
    assert describe_database_url(blank) == repr(blank)


def test_the_predicate_judges_the_value_as_given():
    """A direct call no longer strips: a leading space does not parse."""
    assert persistent_database_configured("sqlite+aiosqlite:///./p.db") is True
    assert persistent_database_configured(" sqlite+aiosqlite:///./p.db") is False
