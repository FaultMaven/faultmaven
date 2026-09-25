# Local development troubleshooting

Symptoms a contributor hits on a checkout, and the first thing to run. Script
and port problems specific to `faultmaven.sh` / `faultmaven-dev.sh` are in the
[script usage guide](./script-usage-guide.md#troubleshooting); user-facing
setup problems are in the [quickstart](../getting-started/quickstart.md#troubleshooting).

## Import errors

```bash
pip install -e ".[dev]"            # the editable install is what makes `import faultmaven` resolve
export PYTHONPATH="${PYTHONPATH}:$(pwd)"   # only if you cannot install
```

## Async tests do not run

`asyncio_mode = auto` is set in `pytest.ini`, so a plain `async def test_…`
runs; an explicit `@pytest.mark.asyncio` is accepted but not required.
`--strict-markers` is on: an unregistered marker fails collection — the list is
`pytest.ini`.

## Port conflicts

```bash
./faultmaven.sh stop
lsof -i :8090      # API
lsof -i :3333      # Dashboard
```

## Database issues

```bash
rm -rf data/faultmaven.db          # standalone SQLite; the next start re-runs migrations and re-seeds
alembic upgrade head
alembic heads                      # the current head — never copy a revision id from prose
```

## Architecture violations

```bash
lint-imports
```

The output names the import that violates a contract in `.importlinter`. Fix by
importing from the module's `contracts.py` instead of its internals; see the
Architecture section of `CLAUDE.md`.

## LLM provider issues

```bash
echo $CHAT_PROVIDER
echo $OPENAI_API_KEY               # or the relevant provider key

# $TOKEN from POST /api/v1/auth/dev-login {"username":"admin"} — the bootstrap
# admin holds the operator roles.
curl -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/v1/admin/llm/config

# The debug router when mounted (platform-admin only since #1474: no header
# -> 401, a non-operator token -> 403)
curl -H "Authorization: Bearer $TOKEN" http://localhost:8090/debug/llm-providers

./faultmaven.sh logs api
```

Provider capabilities and the shipped defaults: `.claude/rules/llm-providers.md`,
`docs/reference/llm-model-capabilities.md`.

## JWT / auth issues

```bash
echo $AUTH_MODE
python scripts/generate_oauth_keys.py       # oauth mode needs the RSA key pair
curl -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/v1/auth/me
```
