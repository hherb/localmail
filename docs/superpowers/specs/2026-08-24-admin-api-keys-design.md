# Admin-issued API keys

**Status:** design approved 2026-08-24.
**Problem owner:** onboarding. A downstream process ("my_mail_bot") has no
cheap way to authenticate against localmail today.

## The problem

Every credential localmail issues is minted *by the principal that will use
it*. `POST /v1/auth/login` takes a username and password and returns a bearer
token with a 30-day TTL; the OAuth authorization server takes a browser round
trip through a consent screen. Both assume a human at a keyboard.

A machine consumer — a mail bot, a cron job, an agent speaking MCP — has
neither. Onboarding one today means creating an `api_users` row with a password
that exists only to be exchanged for a token, storing that password somewhere so
the bot can re-login when its token expires in 30 days, and granting accounts by
CLI. The password is a second long-lived secret that buys nothing: it is used
once per month by a script, and it is a full interactive credential for a
principal that should never have one.

What is wanted is the ordinary API-key workflow: an admin names a consumer,
receives an opaque key once, and hands it to that consumer. The key
authenticates until it is revoked.

## Non-goals

- **Scopes or per-key permissions beyond the existing account ACL.** A key
  reads exactly the accounts its principal is granted, through the same
  `user_accounts` join every other credential uses. A finer permission model is
  a separate design and would have to cover the human credentials too.
- **Overlapping keys during rotation.** One principal holds exactly one key
  (see "The principal behind a key"), so rotation is revoke-then-re-mint under
  the same name: grants survive, but there is a gap rather than a cut-over
  window. Overlapping keys is what would close the gap, and it needs a second
  name per key — a bot name *and* a key label — which is surface this design
  deliberately does not buy yet.
- **Expiring keys.** Decided explicitly: keys live until revoked. The
  alternative — a fixed TTL — stops unattended bots at an arbitrary moment with
  no warning surface to announce it, and localmail has no notification channel
  to build that warning on.
- **A second authentication header.** Keys ride `Authorization: Bearer`.
  See "Wire format" below.
- **The Tauri desktop GUI panel.** The JSON routes are bearer-capable by
  construction, so the desktop panel is later frontend-only work. The GUI's
  Users tab is still a placeholder; building it is its own phase.

## Design

### The principal behind a key

A key is minted against a dedicated `api_users` row — a **service user** — whose
username is the key's name. `my_mail_bot` the key implies `my_mail_bot` the
principal.

This is the choice that keeps the rest of the design small. The per-account ACL
(`user_accounts`), the disable switch (`disabled_at`), the bulk revocation lever
(`sessions_invalidated_at`), and every ACL-scoped accessor in `api/` are already
keyed on a user id. A key that is its own principal would need a parallel
implementation of all four, and CLAUDE.md's standing rule — that session
revocation is only terminal if it covers *every* credential kind — would then
have a fifth kind to reach that nothing reaches today.

**A name collision with a non-service user is an error, never an attachment.**
Attaching to an existing row would mean that minting a key named after a human
administrator hands out an administrator's credential, which defeats "Rule 1"
below at the front door. `is_service` is what the rule is decided on, so it has
to be a column rather than an inference from "has a key" — a bot whose key was
just revoked has no key and is still not a person.

A collision with a **service** user is different, and is how a bot gets re-keyed
after a revocation: if that bot currently holds no key, `create_key` mints one
against the existing principal, **grants intact**. If it already holds one, that
is an error naming the revoke step (the 1:1 index would reject it regardless,
but a constraint violation is not an operator-facing message).

**The pairing is 1:1 and enforced in the database**: one API-key row per
service user, one service user per key. That is what lets the whole surface
address a key and its principal interchangeably — the panel says "this key may
read these accounts" while the grant is stored against the user id, and neither
the operator nor the reader has to hold two identities in mind. Relaxing it to
many-keys-per-principal is the rotation feature named in the non-goals, and it
would need a key label distinct from the bot name before any of it makes sense.

### Wire format

The raw key is `lmk_` followed by the existing 32-byte URL-safe token from
`auth.generate_token()`, presented as `Authorization: Bearer lmk_…`.

The header is the existing one because it is the only one that works
everywhere: every current client sends it, and the MCP Streamable HTTP auth the
agent case depends on is built around it. A second `X-API-Key` path would ship
anyway *in addition*, not instead — and two authentication paths are two things
every future auth change has to reach.

The `lmk_` prefix is not consulted during verification; there remains exactly
one lookup path. It exists so a leaked key is recognisable — greppable in logs,
matchable by secret scanners, and distinguishable from a session token by eye.

The key is stored as SHA-256 like every other credential, so it is displayed
**once** at creation and is unrecoverable afterwards. The remedy for a lost key
is to revoke it and mint a replacement under the same name, which keeps the
bot's grants — note the order, since the 1:1 pairing means the replacement
cannot be minted first.

### Data model — migration `0036_api_keys.sql`

```sql
ALTER TABLE api_tokens ADD COLUMN api_key_name TEXT;
ALTER TABLE api_tokens ALTER COLUMN expires_at DROP NOT NULL;
ALTER TABLE api_tokens ADD CONSTRAINT api_tokens_only_keys_are_immortal
  CHECK (api_key_name IS NOT NULL OR expires_at IS NOT NULL);
CREATE UNIQUE INDEX api_tokens_one_key_per_service_user
  ON api_tokens (user_id) WHERE api_key_name IS NOT NULL;

ALTER TABLE api_users ADD COLUMN is_service BOOLEAN NOT NULL DEFAULT FALSE;
```

**`api_key_name IS NOT NULL` is the credential kind.** There is no second
boolean beside it that could disagree. The column is `api_key_name` rather than
`name` deliberately: a future "let users label their sessions" feature must add
its own column instead of inheriting API-key semantics — immortality and the
admin bar — by writing to a field that sounds general.

**The CHECK is the load-bearing half of the migration.** Dropping `NOT NULL`
from `expires_at` on its own would let a *login* token be minted with no expiry:
an immortal interactive credential, produced by a one-line bug, with nothing
failing and no query that would look wrong. The constraint scopes "may live
forever" to API keys, in the database, where no code path routes around it.

The partial unique index is what enforces the 1:1 pairing, and it is keyed on
`user_id` alone rather than on `(user_id, api_key_name)`: the pair would permit
several differently-named keys on one principal, which is precisely the
many-keys model the non-goals defer. Key *names* are unique globally for free,
via the existing `api_users.username` unique constraint.

`api_users.is_service` is what makes a bot distinguishable from a person — for
the Users screen, for the login bar in Rule 2, and for reasoning about
`delete_user`'s blast radius. Existing rows default to `FALSE`.

### Minting

`api/admin/api_keys.py::create_key(conn, *, name, account_ids) -> CreatedKey`

1. Validate `name` through the pure `api_key_name_error(name) -> str | None`
   (blank / length), shaped like `account_names.py::account_name_error`.
2. Resolve the principal, one of three outcomes: no such username → insert the
   `api_users` row (`username = name`, `is_service = TRUE`, `is_admin` false,
   `password_hash` = argon2 of 32 random bytes nobody retains); an existing
   service user holding no key → reuse it, grants untouched; anything else — a
   non-service user, or a service user that already holds a key → raise
   `ApiKeyFieldError`.
3. Apply grants via the existing `acl.grant_account`. On the reuse branch this
   is additive only, so re-keying never silently narrows what a bot could read.
4. Insert the token: `expires_at = NULL`, `api_key_name = name`.

The whole of `create_key` runs in one transaction: a failure at step 4 must not
leave behind a principal that the operator's retry would then collide with.

`CreatedKey` is the only object that ever carries the raw key.

**A key is addressed by its principal's id** (`api_users.id`) everywhere — in
`revoke_key`, in the routes, and in the panel. The 1:1 pairing makes that
unambiguous, and `api_tokens` offers no alternative: its primary key is
`token_sha256`, which is credential material and must never travel in a URL or
a log line. Per #33 the id is a string on the wire and passes through
`api.ids.parse_int_id` at the boundary. The CLI addresses keys by name and
resolves the id itself.

### Verifying

`api.auth.verify_token` changes in exactly two places, and it is the only place
either change is needed — `mcp.auth.LocalmailTokenVerifier` and
`mcp.oauth.access.load_access` both delegate their expiry decision to it:

- the expiry predicate becomes `(t.expires_at IS NULL OR t.expires_at > now())`;
- the SELECT gains `t.api_key_name IS NOT NULL`, projected onto a new
  `AuthenticatedUser.is_api_key` field.

`is_api_key` is a **required keyword field with no default** (#234's shape).
`False` is the permissive value — it means "allowed at admin routes" — so it
must not be reachable by forgetting to write it. There is one production
construction site and zero in tests, so the requirement costs nothing now and
cannot be silently skipped later.

`last_used_at` maintenance already lives in `verify_token`, so the panel's "last
used" column needs no new write path.

### Rule 1 — a key never reaches an admin route

`serve/admin/dependencies.py::require_admin()` is the single bearer admin gate.
Its bearer branch refuses `user.is_api_key` with 403 **before** consulting
`is_admin`.

The guard sits at the point of use rather than at mint time because a service
user can be promoted through the Users panel *after* its key was minted; a
mint-time-only check would be correct at the moment it ran and wrong forever
after. `users.py` additionally refuses the `is_admin` toggle on a service row so
the two rules agree, but the runtime gate is what carries the invariant.

The consequence worth stating: a bot key cannot mint another bot key, disable a
user, or touch account configuration. A leaked key reads granted mail and
nothing else.

### Rule 2 — a service user cannot log in

Three separate lookups verify a password against `api_users`, all today
carrying the identical `WHERE username = %s AND disabled_at IS NULL` wording by
copy:

| Site | Reached by |
| --- | --- |
| `api/auth.py::login` | `POST /v1/auth/login` |
| `api/admin/auth.py::authenticate_admin` | `/admin/login` cookie session |
| `serve/oauth/consent_router.py` | the OAuth consent screen |

The pure `login_eligible_sql(user=…)` fragment — a sibling of
`credential_valid_sql`, for the same one-authority reason — is spliced into all
three, adding `AND <u>.is_service IS FALSE`. #241 was exactly a rule applied to
one site and not its sibling, and the wording is what must not drift.

Timing parity is unaffected: a service username falls to the existing
dummy-hash branch, indistinguishable from an unknown username.

`api/admin/users.py` also refuses password-reset on a service row. Without it
the Users panel can hand a bot an interactive login, which is the one path that
makes the unusable password hash usable again.

### Revocation

The key is an `api_tokens` row precisely so that every existing lever reaches
it, none of them modified:

| Lever | Effect |
| --- | --- |
| `revoke_key` (DELETE the token row) | kills the credential; the bot and its grants survive, ready to be re-keyed |
| `delete_key_principal` (DELETE the service user) | removes the bot entirely — token and grants go with it by `ON DELETE CASCADE` |
| bump the service user's `sessions_invalidated_at` | kills the key without naming it — `credential_valid_sql` compares `t.created_at`, unchanged |
| set `disabled_at` on the service user | stops everything, `/mcp` included, reversibly |

The first two are both offered, in the panel and the CLI, because they answer
different questions. "This key leaked" wants the credential gone and the
twelve account grants kept; "this bot is retired" wants the principal gone.
Collapsing them would force one of those two operators to do avoidable work,
and the destructive one is the wrong default.

### Surfaces

Each admin feature in this codebase is a pair of routers plus a nav entry; this
follows that shape exactly.

| Layer | Module | Notes |
| --- | --- | --- |
| service | `api/admin/api_keys.py` | transport-free: `list_keys`, `create_key`, `revoke_key`, `delete_key_principal` |
| pure | `api/admin/api_key_names.py` | `api_key_name_error` |
| pure | `api/login_eligible_sql.py` | Rule 2's shared fragment |
| pure | `serve/admin/api_key_forms.py` | form parsing/validation, unit-tested standalone |
| JSON | `serve/admin/api_keys_router.py` at `/v1/admin/api-keys` | `require_admin()`; `GET`, `POST` (201, raw key once), `DELETE /{id}` (revoke key), `DELETE /{id}/principal` (delete bot), `POST /{id}/grants` — `{id}` is the principal's id, per "Minting" |
| HTML | `serve/admin/api_keys_panel_router.py` at `/admin/api-keys` | HTMX, templates under `templates/api_keys/`, nav link in `base.html`, dashboard card |
| static | `serve/admin/static/api-keys-panel.js` | the copy button; the `/admin` CSP is `script-src 'self'` with no `unsafe-inline`, so inline JS is not an option |
| CLI | `cli.py` | `add-api-key NAME [--grant ACCOUNT]…`, `list-api-keys`, `revoke-api-key NAME`, `remove-api-key NAME` |

Grants reuse `acl.grant_account`/`revoke_account` and the account-checkbox
component the Users screen already renders — no second grant path.

Every mutating control carries a method-bound CSRF token via
`csrf_token_for_method`, so a token minted for `POST` cannot replay against
`DELETE` (#122).

CLI verb mix is deliberate: `add-`/`remove-` mirror `add-api-user` and
`remove-api-user` and act on the principal, while `revoke-` mirrors
`revoke-account` and `revoke-admin-sessions` and acts on the credential. All
four commands take `_dsn(ctx)`, so `--config` reaches them (#245) and
`tests/test_cli_config_path.py` covers them without being extended.

`add-api-key` prints **only the key on stdout**; everything human-readable goes
to stderr. Same reasoning as `--version`: stdout is what a provisioning script
captures.

### Errors

`ApiKeyFieldError(ValueError)` mirrors `AccountFieldError` and `UserFieldError`,
mapping to 400 in both routers. Three inputs take that path: a blank or
over-long name, a name belonging to a **non-service** user, and a name
belonging to a service user that **already holds a key** — the last worded so it
names the revoke step, since the operator's intent was almost certainly to
re-key. The fourth case, a service user holding no key, is not an error at all:
it is the re-key path. Revoking or deleting an unknown key is 404. An unknown
`--grant` account is a `ClickException`, not a traceback.

## Testing

Grouped by what each test actually pins.

**By-construction (DB).** The CHECK rejects a session token with
`NULL expires_at` — the pin that stops the immortal-login-token regression, and
one that fails against a migration which merely drops `NOT NULL`. The partial
unique index rejects a second key for a service user that already has one, and
`api_users.username` rejects a second bot of the same name.

**The credential path.** `verify_token` accepts a NULL-expiry key, still rejects
an expired session token, and reports `is_api_key` correctly for both kinds.
Each of the three revocation levers independently stops the key — three tests,
because revocation is only terminal if every lever holds.

**The principal lifecycle.** Minting against a name held by a human user is
refused — the test that stops Rule 1 being defeated at the front door, and it
asserts the refusal specifically for an `is_admin` human. Revoking a key leaves
the principal and its grants in place; re-minting under the same name then
succeeds and **the grants are still there**, which is the whole reason revoke
and delete are separate operations. `delete_key_principal` removes the token and
the grants with it, by cascade.

**The admin bar (load-bearing).** `require_admin()` refuses an API-key bearer
**whose service user has been promoted to `is_admin`**. The promotion is done
by direct SQL in the test, since `users.py` refuses that toggle through the UI —
which is the point: the gate has to hold for a state the UI will not produce
today but a migration, a repair script, or a relaxed toggle could. Mutation-
pinned: it must fail when the guard is deleted, which a non-admin service user
cannot demonstrate, since that 403s for the ordinary reason. Positive control: a
real admin bearer still passes.

**Containment.** One test per password-verifying site refusing a service user —
three tests, not one parametrised over the helper. The drift the shared fragment
exists to prevent is a site *not using* it, and a test that calls the fragment
directly cannot see that.

**Secrecy.** The raw key appears in the create response and nowhere else: not in
`list_keys`, not in any `api_tokens` column, not in the panel's list HTML.

**Reach.** An API key against `/v1/search` returns only granted accounts. The
MCP verifier accepts it and resolves the correct subject.

## Documentation

README gains an onboarding section (mint a key, grant accounts, hand it to the
consumer). `docs/mcp-usage.md` gains the key as the answer to "how does my agent
authenticate", replacing the hand-pasted login token in the quick-start.
