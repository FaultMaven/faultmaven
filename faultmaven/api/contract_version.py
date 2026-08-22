"""The version of the API contract, moved by hand.

``docs/reference/api/openapi.json`` is the contract between this API and its
clients (faultmaven-copilot, faultmaven-dashboard). A contract changes only by
agreement, so this number moves only when someone decides to publish a change —
never automatically, and never as a side effect of shipping.

It is deliberately **not** the product version. The product ships on its own
cadence, and most releases change no route, no schema and no status code; a
number that moved with them would carry no information. ``GET /`` still reports
the product version, which is a different fact about a different thing.

Bump this in the same PR as the change it describes:

* **MINOR** — the surface grew in a way every existing client survives: a new
  endpoint, a new optional field, an additionally accepted content type.
* **MAJOR** — an existing client can break: an endpoint or field removed, a
  status code changed, a response shape replaced, a request field that was
  optional made required.
* **PATCH** — available for a re-publication that changes no structure at all.
  Not required for one: a reworded description is not something a client can
  break on, and demanding a bump for prose would train people to bump without
  reading.

``scripts/check_contract_version.py`` fails a pull request whose structural
surface differs from the base branch while this stays put, so a surface change
cannot reach clients silently. What that check deliberately does **not** do is
decide MINOR versus MAJOR: that judgement is the thing the clients are being
asked to accept, and it belongs to a person.
"""

API_CONTRACT_VERSION = "1.0.0"
