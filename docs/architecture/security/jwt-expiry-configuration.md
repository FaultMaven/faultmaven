# JWT Expiry Configuration — Single Source

**Status:** Current
**Component:** `faultmaven/config/settings.py` (`AuthSettings`), token generators, `AuthService`
**Issue:** fm#888

## The knob

Token lifetimes are configured by exactly one pair of environment variables,
effective in **every** auth mode (HS256/local and RS256/oauth-cloud alike):

| Variable | Meaning | Bounds |
|---|---|---|
| `JWT_ACCESS_TOKEN_EXPIRY_MINUTES` | Access token lifetime, minutes | 1–`MAX_ACCESS_TOKEN_EXPIRY_MINUTES`, default 15 |
| `JWT_REFRESH_TOKEN_EXPIRY_DAYS` | Refresh token lifetime, **days** | 1–`MAX_REFRESH_TOKEN_EXPIRY_DAYS`, default 7 |

They live on `settings.auth` (`AuthSettings`), bound by explicit
`validation_alias`, with bounds that make an implausible value fail at boot.
The names carry their unit deliberately (#832): unsuffixed parallel names once
invited "10080" (7 days in minutes) into the days field.

`settings.security` carries **no** expiry fields. Every path that mints or
advertises a token lifetime reads the same source:

- The token generators (HS256 and RS256) take the two lifetimes as **explicit
  constructor parameters**; they do not reach into a settings object for them.
  Expiry was in fact the *only* thing either generator read off its `settings`
  argument (keys, issuer and audience were already explicit), so that argument
  is gone: no generator holds a settings object, and which half a caller holds
  can never decide a token's lifetime again.
- Both construction sites pass `settings.auth` values:
  `modules/auth/api/auth.py` (HS256/local) and
  `container/providers/services.py::create_jwt_token_generator` (RS256/cloud —
  keys, issuer and audience still come from `settings.security`; only the
  lifetimes come from auth).
- `AuthService`'s internal mint properties and its
  `_longest_token_lifetime_seconds` (the #769 revocation-watermark bound) read
  `settings.auth`. With one source, "the watermark outlives every mintable
  token" is structural rather than a `max()` across halves.
- `OAuthService` and the SSO login service advertise `expires_in` from the same
  values the generator mints with, so advertised and actual lifetimes cannot
  diverge.

## The retired spelling fails the boot

The `EXPIRE` spelling (`JWT_ACCESS_TOKEN_EXPIRE_MINUTES` /
`JWT_REFRESH_TOKEN_EXPIRE_DAYS`) formerly reached the security half by
field-name binding and was documented in the installation guide. It is retired:
settings construction **rejects** an environment that sets either name, with an
error naming the `EXPIRY_*` replacement. A silently-inert knob is the failure
mode this design exists to remove — an operator who sets the old name gets a
boot error, not a deployment whose token lifetimes quietly ignore them.

## The defect this replaced

Both settings halves declared the same two field names. The auth half bound
them via `validation_alias` (`EXPIRY_*`); the security half bound them by field
name (`EXPIRE_*`). Which half a minting path was built from decided which env
spelling reached it:

- HS256/local minted from `settings.auth` → `EXPIRY_*` worked, `EXPIRE_*` inert.
- RS256/cloud minted from `settings.security` → `EXPIRE_*` worked, `EXPIRY_*`
  inert — while CLAUDE.md and four other docs presented `EXPIRY_*` as *the*
  knob, and the installation guide presented `EXPIRE_*`.
- OAuth/SSO surfaces advertised `expires_in` from the auth half regardless of
  which half minted, so under cloud the advertised lifetime and the actual
  lifetime came from different configuration.

The same split had already produced #769 (revocation watermark read one half,
minting read the other) and #832 (units trap duplicated across halves); this is
the third defect from the class, which is why the fix deletes the duplicate
rather than aliasing it (**rejected alternative:** adding `validation_alias` to
the security half too — cheaper, but leaves two fields that must be kept in
sync by convention, i.e. the trap, for the next field and the next reader).

## Test obligations

Asserting on settings objects is what let this hide — a value can land on a
settings half no minting path reads. The guards therefore assert on **minted
tokens**:

- With `JWT_ACCESS_TOKEN_EXPIRY_MINUTES` / `JWT_REFRESH_TOKEN_EXPIRY_DAYS` set
  to non-default values (values where a default-equality pass is impossible),
  a token minted by the **RS256 generator built through the container factory
  path** decodes to `exp − iat` equal to the configured lifetime — access and
  refresh both, swept over more than one value. Same property for the
  HS256/local generator.
- Setting either retired `EXPIRE_*` name fails settings construction with a
  message naming the replacement.
- `_longest_token_lifetime_seconds` equals the configured (single-source)
  refresh lifetime.

Mutation checks: rebinding the RS256 generator's lifetimes to a hardcoded
default (simulating the old security-half read) must turn the cloud-mint test
red; deleting the retired-spelling guard must turn the rejection test red.
