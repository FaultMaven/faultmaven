"""019_verified_by_is_the_display_name

A verified runbook's frontmatter ``verified_by`` names its verifier by their
CURRENT display name, never their username (#1948, owner-approved 2026-10-12).

Until #1942, verify wrote the request principal's username into the runbook's
frontmatter. An SSO username is the email's local part, and that text is served
with the runbook to every reader at its scope (``GET /knowledge/documents/{id}``,
keyword search). #1942 made new verifications write the verifier's display name
(which follows #1924's rule); this revision rewrites the runbooks verified
before it. Since #1950 a runbook's text lives only in the database — the
published item in ``knowledge_items.content``, its draft in
``conversion_drafts.content`` — so a data migration reaches all of it, on
standalone and Cloud alike.

Which rows change
-----------------

The runbooks are identified by who verified them (the verifier foreign key),
never by matching text; within them, only a VALUE derived from the verifier's
account is rewritten. ``verified_by`` is also a free "name or team" field
(kb-toolkit writes it so, and an owner can edit a published runbook), so a value
someone chose — ``SRE Team`` — stays.

1. **Runbooks verified through FaultMaven** (``knowledge_items.verified_by``, the
   verifier's user id; only verify sets it). An account-derived value becomes
   the name verify writes today: the ``display_name`` of that user's row in the
   runbook's enterprise, or ``""`` when the user is anchored in another
   enterprise (verify reads the verifier within the job's enterprise, and the
   item carries the job's). A value is account-derived when it

   * equals the verifier's current ``username``, ignoring case and surrounding
     whitespace;
   * contains ``@``; or
   * equals the local part of the verifier's REAL email, ignoring case and
     surrounding whitespace. An email the store synthesized for an account
     registered without one (``<username>@faultmaven.example``) is not real
     (``is_synthesized_email``, frozen below as 016 froze it).

2. **Runbooks whose verifier account is gone**: the foreign key cleared it
   (``ON DELETE SET NULL``), but the row is still recognisable as verified
   through FaultMaven — ``verification_level = 1`` (COMMUNITY, which verify
   derives from a verifier) and ``source_url LIKE 'conversion:%'`` (verify's
   source). With no account, the username and email are unknown, so only a
   value containing ``@`` counts as account-derived; it becomes ``""``, what
   verify writes when it finds no account.
3. Each such runbook's **verified draft** (``conversion_drafts`` with
   ``status = 'verified'`` and that ``knowledge_item_id``) gets the same rewrite.
   The rewrite is a function of the text and the verifier, so a draft holding
   the same text as its item (#1950's invariant) ends holding the same text as
   its item.

Any other value is left untouched, and the ids of those runbooks are logged.

Known false negative: a username renamed since the verification. The runbook
holds the old username, which is neither the current username nor (unless it
was) the email's local part, so it stays. Its account is not recorded anywhere
to match on.

How the text changes
--------------------

Only the value's own characters change. The frontmatter block is found with the
application's grammar (``utils/frontmatter.FRONTMATTER_RE``, frozen here: an
optional BOM, ``---`` delimiter lines with LF or CRLF endings, anchored at the
start), parsed with ``yaml.compose`` to find where the ``verified_by`` value
starts and ends, and the new value is spliced into that span as a one-line YAML
double-quoted scalar from YAML's own emitter (so control characters, NEL and
U+2028 are escaped and the value re-reads exactly). Re-serialising the frontmatter instead would rewrite every other line of a
runbook its owner edited (``PUT /knowledge/documents/{id}`` stores the text
verbatim): comments dropped, keys re-sorted, quoting and timestamps
re-spelled, CRLF and the trailing newline lost. A block scalar's trailing line
breaks stay. A runbook whose frontmatter has no ``verified_by`` key is left
alone (there is nothing derived to remove), as is one that already holds the
target name (so a re-run changes nothing).

Every splice is re-read before it is written: the block must keep its
boundaries, ``verified_by`` must read back as the new name, and every other key
must read back unchanged; otherwise the row is left and its id logged. A value
held through a YAML anchor or alias is shared with another key, so it is left
(counted with the values not derived from the account). Any failure to parse a
frontmatter (a YAML error, an impossible date such as ``2024-02-30``, nesting
too deep) leaves the row and logs its id: owner text is stored unvalidated, and
none of it may stop the migration (a failed migration stops a standalone boot).

Verify's old fallback, for a frontmatter that did not parse, replaced the
literal ``verified_by: ""`` with ``verified_by: "<username>"``. Such a text still
does not parse, so its rewrite is that same literal replacement in reverse,
keyed on the verifier's username and confined to the frontmatter block (the
body may quote the line); a text that does not parse and holds no such line in
its frontmatter (or whose verifier is gone) is left, and its id is logged.

Rows left untouched
-------------------

* **Values not derived from the account** (above): a name or team the owner
  chose, a display name the verifier has since changed, a renamed username.
* **Uploaded runbooks** (``upload_document``: ``knowledge_items.verified_by``
  NULL, ``verification_level = 0``) and their drafts: their frontmatter is the
  uploader's own text, not something FaultMaven wrote. Their ``verified_by``
  values holding an ``@`` are counted and logged for the operator, never
  changed.
* **The KB pack** (built-ins, ``verified_by`` NULL, no ``conversion:`` source):
  shipped text, re-ingested by content hash at boot.
* **Discarded drafts, and verified drafts whose item is gone**: deleting a
  published runbook discards its draft, and nothing serves a discarded draft's
  text (018).
* ``updated_at``: the rewrite is not an edit, and the reranker's freshness
  signal reads that column.

The vectors need no re-embedding: frontmatter is stripped before chunking
(``content_chunker``) and is not chunk metadata. Keyword search scores the stored
text, so it now matches the display name, not the username.

Dialects
--------

The same Python on both. On PostgreSQL ``knowledge_items`` and
``conversion_drafts`` are enrolled in row-level security, so the reads and
writes run under ``SET LOCAL row_security = off`` (restored to ``DEFAULT``
after), the choice and reasoning of revisions 006-008, 013 and 018: the rewrite
is tenant-wide by construction, and a role the policy would filter raises
rather than settling one enterprise's rows. ``users`` is not enrolled (016).

Offline (``--sql``)
-------------------

The rewrite parses each runbook's frontmatter, which a generated script cannot
do. Both dialects render a SQL comment saying so in place of the rewrite, and
nothing else: a script loses nothing, it only leaves the old values. The
upgrade is idempotent, so it can be applied later, online, on its own:
``alembic stamp c4441b83ba24``, then ``alembic upgrade 82bc95a1c02f``, then
``alembic stamp head``. Stamping, not downgrading, so no later revision's
``downgrade()`` runs. (On SQLite a whole-chain render already stops at 018.)

``downgrade()``
---------------

A no-op. The old values are not kept anywhere to restore from, and restoring
them would re-publish the username this revision removes.

Revision ID: 82bc95a1c02f
Revises: c4441b83ba24
Create Date: 2026-10-12 15:00:00
"""

import logging
import re
from typing import Optional, Sequence, Union

import yaml
from sqlalchemy import text

from alembic import op

revision: str = "82bc95a1c02f"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = (
    "c4441b83ba24"  # pragma: allowlist secret
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_log = logging.getLogger("alembic.runtime.migration")

#: Rule 2: verified through FaultMaven (COMMUNITY, from a ``conversion:``
#: source), its verifier since deleted. Frozen: migrations are history.
_VERIFIER_GONE = (
    "(k.verified_by IS NULL AND k.verification_level = 1 "
    "AND COALESCE(k.source_url, '') LIKE 'conversion:%')"
)

#: Rules 1 and 2: each runbook verified through FaultMaven, with the name verify
#: would write today (NULL → ``""``) and the verifier's username and email (NULL
#: when the account is gone).
SELECT_VERIFIED_ITEMS = text(
    "SELECT k.item_id, k.content, "
    "CASE WHEN v.enterprise_id = k.enterprise_id THEN v.display_name END, "
    "v.username, v.email "
    "FROM knowledge_items AS k LEFT JOIN users AS v ON v.user_id = k.verified_by "
    f"WHERE k.verified_by IS NOT NULL OR {_VERIFIER_GONE} "
    "ORDER BY k.item_id"
)

#: Rule 3: the verified drafts published as those runbooks.
SELECT_VERIFIED_DRAFTS = text(
    "SELECT id, knowledge_item_id, content FROM conversion_drafts "
    "WHERE status = 'verified' AND knowledge_item_id IS NOT NULL ORDER BY id"
)

#: The rest, for the operator's count of ``@`` values left in place.
SELECT_OTHER_ITEMS = text(
    "SELECT k.item_id, k.content FROM knowledge_items AS k "
    f"WHERE k.verified_by IS NULL AND NOT {_VERIFIER_GONE} ORDER BY k.item_id"
)

UPDATE_ITEM = text("UPDATE knowledge_items SET content = :content WHERE item_id = :id")
UPDATE_DRAFT = text("UPDATE conversion_drafts SET content = :content WHERE id = :id")

ROW_SECURITY_OFF = "SET LOCAL row_security = off"
ROW_SECURITY_RESTORED = "SET LOCAL row_security TO DEFAULT"

#: What an offline script carries in place of the rewrite.
OFFLINE_NOTICE = (
    "-- WARNING (019): this script does NOT rewrite runbook verified_by values: "
    "the rewrite parses each runbook's frontmatter and needs a connection. "
    "Usernames written by verify before #1942 stay published. To apply it, run "
    "online: alembic stamp c4441b83ba24, then alembic upgrade 82bc95a1c02f, then "
    "alembic stamp head"
)


#: ``faultmaven/utils/frontmatter.FRONTMATTER_RE``, frozen: the app's one
#: frontmatter grammar (an optional BOM, ``---`` lines with CRLF or LF endings,
#: the block anchored at the start of the text). Group 1 is the YAML.
_FRONTMATTER_RE = re.compile(
    "^\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n", re.DOTALL
)

#: ``infrastructure/auth/synthesized_email.SYNTHESIZED_EMAIL_DOMAIN``, frozen.
_SYNTHESIZED_EMAIL_SUFFIX = "@faultmaven.example"


class _Unparseable(Exception):
    """The text's frontmatter does not parse and holds no line to rewrite."""


class _NotDerived(Exception):
    """The value is not derived from the verifier's account: it stays."""


class _Unsafe(Exception):
    """The spliced frontmatter would not read back as the same mapping with
    only ``verified_by`` changed: the row is left as it was."""


def is_account_derived(
    value: object, username: Optional[str], email: Optional[str]
) -> bool:
    """Whether a stored ``verified_by`` value was derived from the verifier's
    account (see the module docstring). ``username`` and ``email`` are ``None``
    when the account is gone, leaving only the ``@`` rule."""
    if value is None:
        return False
    text_value = str(value)
    if "@" in text_value:
        return True
    folded = text_value.strip().lower()
    if username is not None and folded == username.strip().lower():
        return True
    if email and not email.lower().endswith(_SYNTHESIZED_EMAIL_SUFFIX):
        if folded == email.split("@", 1)[0].strip().lower():
            return True
    return False


def _frontmatter_block(content: str) -> Optional[re.Match]:
    """The frontmatter block at the start of ``content`` (group 1), or ``None``."""
    return _FRONTMATTER_RE.match(content)


def _verified_by_pair(block: str) -> Optional[tuple]:
    """The ``(key, value)`` nodes of ``verified_by`` in a frontmatter block (the
    last pair, as a load keeps the last of a repeated key), or ``None``. Raises
    whatever the parser raises for a block that does not compose."""
    root = yaml.compose(block, Loader=yaml.SafeLoader)
    if not isinstance(root, yaml.MappingNode):
        return None
    found = None
    for key, value in root.value:
        if isinstance(key, yaml.ScalarNode) and key.value == "verified_by":
            found = (key, value)
    return found


def _quoted(name: str) -> str:
    """``name`` as a YAML double-quoted scalar on one line, from YAML's own
    emitter: it escapes what YAML itself would not read back (``\\x7f``,
    ``\\x85``, U+2028, line breaks), so the value re-reads as exactly ``name``."""
    emitted = yaml.dump(
        name,
        Dumper=yaml.SafeDumper,
        default_style='"',
        allow_unicode=True,
        width=float("inf"),
    )
    if emitted.endswith("\n...\n"):
        emitted = emitted[: -len("...\n")]
    return emitted.rstrip("\n")


def _without_verified_by(metadata: dict) -> dict:
    return {key: value for key, value in metadata.items() if key != "verified_by"}


def _checked(content: str, offset: int, block: str, new_block: str, name: str) -> str:
    """The text with ``new_block`` in place of ``block``, once it is shown to
    read back as intended: the same frontmatter boundaries, ``verified_by``
    equal to ``name``, every other key's value unchanged. Raises ``_Unsafe``
    otherwise, so no splice can write a frontmatter that no longer parses."""
    new = content[:offset] + new_block + content[offset + len(block) :]
    try:
        match = _frontmatter_block(new)
        old_metadata = yaml.safe_load(block)
        new_metadata = yaml.safe_load(new_block)
    except Exception as exc:
        raise _Unsafe() from exc
    if (
        match is None
        or match.group(1) != new_block
        or not isinstance(new_metadata, dict)
        or not isinstance(old_metadata, dict)
        or new_metadata.get("verified_by") != name
        or _without_verified_by(new_metadata) != _without_verified_by(old_metadata)
    ):
        raise _Unsafe()
    return new


def rewrite_verified_by(
    content: str, name: str, username: Optional[str], email: Optional[str]
) -> Optional[str]:
    """``content`` with its frontmatter ``verified_by`` VALUE replaced by
    ``name``, or ``None`` when nothing changes.

    Only the value's own characters change: the frontmatter is parsed to find
    where the value starts and ends, and ``name`` is spliced in, double-quoted.
    Line endings, comments, key order, the other values' quoting and the body
    stay byte for byte (an owner-edited runbook is stored verbatim). The result
    is re-read before it is returned (``_checked``).

    Raises ``_NotDerived`` for a value not derived from the verifier's account
    (or held through a YAML anchor or alias, which a splice would break),
    ``_Unparseable`` for a frontmatter that does not parse and does not hold the
    line verify's old fallback wrote for ``username``, and ``_Unsafe`` for a
    splice that would not read back as intended.
    """
    match = _frontmatter_block(content)
    if match is None:
        return None
    block, offset = match.group(1), match.start(1)
    try:
        pair = _verified_by_pair(block)
        current = yaml.safe_load(block).get("verified_by") if pair else None
    except Exception:
        # Anything the parser raises means the frontmatter does not parse: a
        # YAMLError, a ValueError for an impossible date (2024-02-30), a
        # RecursionError for deep nesting. Owner text is stored unvalidated,
        # so none of it may stop the migration.
        #
        # Verify's old fallback wrote this exact line into a frontmatter that
        # did not parse. Replaced inside the frontmatter only, never the body.
        written = f'verified_by: "{username}"' if username is not None else None
        if written is None or written not in block:
            raise _Unparseable()
        fixed = block.replace(written, f"verified_by: {_quoted(name)}", 1)
        if fixed == block:
            return None
        return content[:offset] + fixed + content[offset + len(block) :]
    if pair is None:
        return None
    key, node = pair
    if current == name or (current is None and name == ""):
        return None
    if not isinstance(node, yaml.ScalarNode) or not is_account_derived(
        current, username, email
    ):
        raise _NotDerived()
    span = block[node.start_mark.index : node.end_mark.index]
    if node.start_mark.index < key.end_mark.index or span.startswith("&"):
        # An alias (the node is the anchored one, earlier in the text) or an
        # anchored value another key refers to: the value is shared, and a
        # splice would rewrite the wrong span or drop the anchor.
        raise _NotDerived()
    # A block scalar's span runs through its trailing line breaks: keep them.
    kept_tail = span[len(span.rstrip()) :]
    new_block = (
        block[: node.start_mark.index]
        + _quoted(name)
        + kept_tail
        + block[node.end_mark.index :]
    )
    return _checked(content, offset, block, new_block, name)


def _holds_an_address(content: str) -> bool:
    match = _frontmatter_block(content)
    if match is None:
        return False
    try:
        metadata = yaml.safe_load(match.group(1))
    except Exception:
        # Owner text: an impossible date or deep nesting is not a crash.
        return False
    if not isinstance(metadata, dict):
        return False
    value = metadata.get("verified_by")
    return value is not None and "@" in str(value)


def _rewrite() -> None:
    bind = op.get_bind()
    targets: dict = {}
    unparseable: list = []
    kept: list = []
    unsafe: list = []
    items_rewritten = 0
    emptied = 0
    for item_id, content, display_name, username, email in bind.execute(
        SELECT_VERIFIED_ITEMS
    ).fetchall():
        name = display_name or ""
        targets[item_id] = (name, username, email)
        try:
            new = rewrite_verified_by(content, name, username, email)
        except _NotDerived:
            kept.append(item_id)
            continue
        except _Unparseable:
            unparseable.append(item_id)
            continue
        except _Unsafe:
            unsafe.append(item_id)
            continue
        if new is None:
            continue
        bind.execute(UPDATE_ITEM, {"content": new, "id": item_id})
        items_rewritten += 1
        emptied += name == ""

    drafts_rewritten = 0
    for draft_id, item_id, content in bind.execute(SELECT_VERIFIED_DRAFTS).fetchall():
        if item_id not in targets:
            continue
        name, username, email = targets[item_id]
        try:
            new = rewrite_verified_by(content, name, username, email)
        except _NotDerived:
            continue
        except _Unparseable:
            unparseable.append(draft_id)
            continue
        except _Unsafe:
            unsafe.append(draft_id)
            continue
        if new is None:
            continue
        bind.execute(UPDATE_DRAFT, {"content": new, "id": draft_id})
        drafts_rewritten += 1

    _log.info(
        "019: rewrote verified_by in %d published runbook(s) and %d verified "
        "draft(s); %d of those runbooks have no verifier account and now hold ''",
        items_rewritten,
        drafts_rewritten,
        emptied,
    )
    if kept:
        _log.info(
            "019: left %d verified runbook(s) whose verified_by is not derived "
            "from the verifier's account (a name the owner chose): %s",
            len(kept),
            ", ".join(kept),
        )
    if unparseable:
        _log.warning(
            "019: left %d runbook text(s) whose frontmatter does not parse and "
            "holds no verified_by line verify wrote: %s",
            len(unparseable),
            ", ".join(unparseable),
        )
    if unsafe:
        _log.warning(
            "019: left %d runbook text(s) whose rewrite would not read back as "
            "intended: %s",
            len(unsafe),
            ", ".join(unsafe),
        )
    addresses = [
        item_id
        for item_id, content in bind.execute(SELECT_OTHER_ITEMS).fetchall()
        if _holds_an_address(content)
    ]
    if addresses:
        _log.info(
            "019: left %d runbook(s) not verified through FaultMaven (uploads, "
            "the pack) whose own verified_by holds an '@': %s",
            len(addresses),
            ", ".join(addresses),
        )


def upgrade() -> None:
    """Rewrite every verified runbook's ``verified_by`` to its verifier's
    current display name (``""`` with no verifier account)."""
    context = op.get_context()
    if context.as_sql:
        op.execute(OFFLINE_NOTICE)
        return
    if context.dialect.name == "postgresql":
        op.execute(ROW_SECURITY_OFF)
        _rewrite()
        op.execute(ROW_SECURITY_RESTORED)
        return
    _rewrite()


def downgrade() -> None:
    """Nothing to restore: the old values are not kept, and restoring them would
    re-publish the usernames this revision removes."""
