# Changing the API contract

`docs/reference/api/openapi.json` is the contract between this API and its
clients — `faultmaven-copilot` and `faultmaven-dashboard`. It is generated from
the code, but that does not make it a build artifact: it is the document both
ends are written against, and it changes **by agreement, in cooperation**, not
as a side effect of editing a handler.

This page is how that works in practice.

## The two questions, kept apart

Most confusion here comes from running two different questions together:

| Question | Answered by | When |
|---|---|---|
| Does the document describe the server? | `api-contract-drift` CI job | Every PR, automatically |
| Have the clients agreed to this contract? | The pinned ref in each client repo | When the client bumps it |

The first must always be yes — a document that lies about the server is worse
than a stale one, which is why the drift job regenerates and diffs on every PR
and why `openapi.json` is never edited by hand.

The second is the actual contract change, and it belongs to the clients. They
pin a ref of this repository and fetch the contract from it, so **merging here
does not reach them**. Adoption is a pull request in their repo that moves the
pin. That is where cooperation lives.

## The three kinds of change

Every API change is one of three, and the *ordering* is what keeps both ends in
contract at all times:

| Kind | Order | Why |
|---|---|---|
| **Add** — new endpoint, new optional field, additionally accepted encoding | server first, client after | Existing calls stay valid; the client adopts when ready |
| **Remove** — endpoint, field, accepted encoding | client first, server after | Never remove something still being read |
| **Change** — rename, status code, response shape | not directly: express as add, then remove | A change is two operations wearing one hat |

That third row is the whole technique. There are no in-place changes, only
**expand** and **contract**:

1. **Expand.** Serve both forms. A renamed field is populated under both names;
   a new error shape is returned as a superset of the old one. Nothing breaks,
   and no coordination is needed because nothing was taken away.
2. **Migrate.** The clients move to the new form and adopt the contract that
   describes it.
3. **Contract.** Remove the old form, once nothing reads it.

### The case that cannot expand

Some values are singular — a **status code** is the usual one. A response
cannot be 400 and 401 at the same time, so the order inverts:

1. Make the clients **tolerant** of both, and confirm that is deployed.
2. Flip the server.
3. Optionally tighten the clients afterwards.

Client tolerance is the "add" step when the server has no room for one. This is
the one situation where the client genuinely moves first.

### Knowing when it is safe to contract

The remove step is a guess unless you measure it. Count arrivals of the old
form — a Prometheus counter on requests in the legacy shape — and contract when
it reads zero across a full deploy cycle of every client. "Nobody should still
be using it" is not evidence.

## Publishing a change

1. Make the change in the code.
2. Decide what it is. **MINOR** if every existing client survives it; **MAJOR**
   if one can break. This judgement is what the clients are being asked to
   accept, so it is a person's call, not a script's.
3. Bump `API_CONTRACT_VERSION` in `faultmaven/api/contract_version.py`.
4. Regenerate, with the lockfile installed — the document depends on FastAPI's
   and Pydantic's versions as well as on the code, so an ambient environment
   produces a valid-looking artifact CI rejects:

   ```bash
   ./scripts/sync-venv.sh dev
   .venv-dev/bin/python scripts/generate_api_docs.py
   git add docs/reference/api/openapi.json docs/reference/api/README.md
   ```

5. Include the contract delta in the PR description. `scripts/check_contract_version.py`
   prints it; paste it, so the reviewer and the client owners see what is being
   amended rather than a 400-line generated diff.
6. Merge. **Nothing has reached the clients yet.**
7. Open the pin bump in each client repo. That PR is the acceptance.

`scripts/check_contract_version.py` refuses a PR whose structural surface moved
while `info.version` stayed put, so step 3 cannot be skipped by accident. It
compares structure only — a reworded description is not something a client can
break on, and demanding a bump for prose would train people to bump without
reading.

## Adopting a change (client side)

Each client repo pins the ref it fetches the contract from:

```yaml
# .github/workflows/ci.yml
env:
  FM_SPEC_REF: <commit sha>
```

To adopt: move the ref, regenerate the typed client, fix whatever the compiler
and tests now object to, and open it as one PR. That PR is where the client
says yes.

Before the backend merges, a client can prepare against the proposed contract
without adopting it — the generator takes an explicit spec:

```bash
pnpm generate:api-types --spec ../faultmaven/docs/reference/api/openapi.json
```

so the client can build and test against a change while the contract it pins
is still the old one.

## What this deliberately does not do

- **It does not classify MINOR versus MAJOR for you.** A tool that decided
  whether clients can survive a change would be making the agreement on their
  behalf.
- **It does not stop a breaking change.** Breaking changes are legitimate; they
  are what MAJOR is for. What it stops is a breaking change reaching a client
  that never agreed to it.
- **It does not version the product.** `GET /` reports the product version, and
  the two move on unrelated cadences — most releases change no route, no schema
  and no status code.
