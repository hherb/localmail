# CLAUDE.md

Guidance for Claude Code sessions working in this repo. See [README.md](README.md)
for the end-user view.

## What this is

`localmail` mirrors one or more IMAP accounts (password or Gmail OAuth2) into a
PostgreSQL database. **Read-only with respect to upstream**: localmail never
deletes, modifies, or sends mail. Downstream agents consume the DB and the
attachment tree without touching IMAP.

## Stack (locked in)

- Python ≥ 3.12, managed by `uv` (`uv sync`, `uv run …`).
- Postgres access: `psycopg` v3 + raw SQL + numbered `.sql` files in
  `migrations/`. **No ORM.** Migrations are tracked in `schema_migrations`.
- IMAP: `imapclient` (sync, blocking). Gmail OAuth2 uses XOAUTH2 via
  `google-auth` + `google-auth-oauthlib`.
- Secrets: two interchangeable backends behind `secrets.py`'s seven functions,
  keyed identically (service `"localmail"`, username = `<account.name>` for
  passwords, `<account.name>:refresh` for OAuth refresh tokens). `[secrets]
  backend` selects: **`keyring`** (default — macOS Keychain on darwin, Secret
  Service on Linux) or **`file`** (a 0600 JSON file at `[secrets] file_path`).
  The **file backend is mandatory for headless hosts**: a lingering systemd
  *user* service starts at boot with no PAM session, so the gnome-keyring
  `login` collection is locked and nothing can unlock it — the daemon then
  crash-loops on `KeyringLocked` forever. That is not a keyring
  misconfiguration; it is what a login keyring is. See
  [docs/superpowers/specs/2026-08-02-headless-secrets-design.md](docs/superpowers/specs/2026-08-02-headless-secrets-design.md).
  - Modules: pure [src/localmail/secrets_store.py](src/localmail/secrets_store.py)
    (username scheme, JSON encoding, `mode_is_private`, `directory_exposure`), IO
    [src/localmail/secrets_file.py](src/localmail/secrets_file.py)
    (`FileSecretStore`), dispatcher `secrets.py`, pure planner
    [src/localmail/secrets_migrate.py](src/localmail/secrets_migrate.py).
  - **`config.load_config()` calls `secrets.configure()`** — a deliberate side
    effect. `load_config` is the only place that sees the resolved config
    (including `--config PATH`), and every process that touches a secret loads
    config first; the alternative was threading a store through
    `open_connection` → `sync` → `idle`/`poller` → `Daemon` and the whole admin
    layer. An autouse conftest fixture calls `secrets.reset_to_default()` after
    every test so a config-loading test cannot leak its backend.
  - **An install from an operator-named config is pinned**
    (`configure(..., named_config=True)`); a later load of the *default* config
    leaves it alone. Without the pin, an incidental default-path read could swap
    a headless host's `file` backend back to `keyring` mid-command and silently
    reintroduce the boot-time `KeyringLocked` failure the backend exists to
    remove. **#245 — the nine CLI commands that ignored `--config` — is fixed**
    (see `_dsn(ctx)` under Commands), and the pin is **kept anyway**:
    `search.create_searcher(cfg=None)` still falls back to a no-path
    `load_config()` for library callers, so the pin is what makes the invariant
    hold by construction rather than by everyone remembering.
  - **File permissions are the only protection, and are enforced on read**: any
    group/other bit raises `InsecureSecretsFile` naming the `chmod` to run.
    Refusing (rather than warning, or self-healing with a `chmod`) is the
    deliberate call — the daemon stops until an operator acts, which is correct
    for a leaked-credential file and is one command to clear.
  - **The parent directory is graded separately (#246)** by the pure
    `secrets_store.directory_exposure(mode) -> DirectoryExposure`, a **sibling**
    of `mode_is_private`, not a reuse — the two read different bits and carry
    different costs, and merging them is how one rule ends up applied to the
    wrong thing. Directory *write* access permits `unlink`/`rename` of entries
    **regardless of their own modes**, so a writable parent can swap the 0600
    file for an attacker-written 0600 file and every file-mode check still
    passes. `WORLD_WRITABLE` → `InsecureSecretsDirectory` (refuse, naming
    `chmod o-w`); `GROUP_WRITABLE` → **one WARNING per process** and proceed;
    read/execute bits ignored. Group-write only warns because the umask-002 +
    per-user-private-group default of the Debian/Ubuntu and RHEL families puts a
    user-created directory at 0775 with that user's own group — refusing would
    wedge a stock install over a distro default (the DGX's own
    `~/.config/localmail` is 0775). The warning is deduped on a
    `FileSecretStore` instance flag because every `get` re-reads the file and
    the daemon reads a secret per reconnect; `configure()` installs one store
    per process, so instance state is process state. The check runs **before**
    the file check and **even when no file exists** — the substitution works
    just as well by planting a file where none was.
  - Writes go through an `O_EXCL` temp in the same directory + `os.replace`, so
    no reader sees a partial file and the secret is never briefly at a wider
    mode, then `fsync` the file **and** its directory — `os.replace` orders the
    rename against readers but is not durable, and a store that exists to
    survive reboots cannot lose a write to an unclean shutdown. An **existing**
    directory is left at whatever mode it has (the default path is the
    operator's config dir): the 0600 file is the protection.
  - `O_EXCL` on a fixed temp name means one write killed where no handler runs
    (SIGKILL, power loss) wedges every later one. That raises
    `StaleSecretsTempFile` naming the `rm` to run, rather than a bare
    `FileExistsError` for a dotfile the operator has never seen. The stray temp
    is never clobbered — it may hold the secret from the interrupted write.
  - `localmail migrate-secrets [--dry-run]` copies keyring → file for every DB
    account. It always reads the *keyring* and writes the *file*, ignoring the
    configured backend, because the realistic order of operations is flip the
    config → watch it fail → migrate. It never deletes from the keyring, so it
    is re-runnable and the switch is reversible.
- **That username scheme is why account names may not contain `:` (#217)** — a password account named
  `gmail:refresh` would otherwise `store_password` straight over the `gmail`
  account's OAuth refresh token, and `gmail`'s next token refresh would fail.
  The rule is the pure
  [src/localmail/account_names.py](src/localmail/account_names.py)`::account_name_error`
  (blank / length / separator), applied at **both** create boundaries —
  `api.admin.accounts._validate_create_fields` (admin UI, JSON API, CLI) and
  `config.Config._reject_unusable_account_names` (the `init-db` TOML seed,
  which reaches `create_account`). Names are not editable after creation
  (`_UPDATABLE` has no `name`), so create is the whole surface. Rejecting the
  one separator character was chosen over a conservative allowlist because an
  allowlist would retroactively break existing configs on a re-seed.
  **The TOML half sits on `Config`, not on the `AccountConfig` field**, because
  `AccountConfig` doubles as the DB-row adapter
  (`daemon_accounts.account_config_from_row`,
  `api.admin.accounts._open_imap_connection`). A pre-#217 release could seed a
  colon-carrying name — `create_account` only checked blank/length — and a
  field validator would gate that existing row on *read*, turning a latent
  keyring collision into a `ValidationError` that stops every account's sync
  thread (`Daemon._spawn_account` is unguarded) and 500s `probe_connection`,
  with no remedy since `name` is not updatable. Pinned by
  `test_daemon_accounts.py::test_legacy_name_from_the_db_maps_without_raising`.
- Config: TOML, validated by `pydantic` v2.
- CLI: `click`.
- Tests: `pytest` (in-memory `keyring` backend; real Postgres at
  `LOCALMAIL_TEST_DSN`, defaults to a separate `localmail_test` database so
  tests can't clobber the live archive).

## Commands

```bash
uv sync                          # install deps
uv run pytest                    # full test suite (skips DB tests if PG unreachable)
uv run localmail --version       # installed version; reads no config, no DB (#279)
                                 #   stdout = the version line; stderr names the
                                 #   remedy when it cannot be read (#291), the
                                 #   exception that stopped it (#296) and the chain
                                 #   it was raised from (#303). EVERY command logs
                                 #   that line at startup (#295, #304); `run`/`serve`
                                 #   do it themselves, the group callback does it for
                                 #   the other 36 (cli.SELF_REPORTING_COMMANDS).
uv run localmail init-db         # apply pending migrations
uv run localmail list-accounts   # show config'd accounts and whether a secret is stored
uv run localmail add-account N   # store password for account N (must exist in config.toml)
uv run localmail remove-account N  # drop stored secrets for account N
uv run localmail migrate-secrets [--dry-run]   # copy keyring → file backend (headless hosts)
uv run localmail enable-account N    # resume syncing account N (sync_enabled = TRUE)
uv run localmail disable-account N   # pause syncing account N (sync_enabled = FALSE)
uv run localmail oauth-login N   # Gmail desktop OAuth flow → refresh token in the secret store
uv run localmail sync [--account N] [--limit-per-folder K]   # one-shot incremental sync
uv run localmail run             # foreground daemon (IDLE on INBOX + periodic poll)
uv run localmail list-failed [--account N] [--limit K]   # show messages sync skipped
uv run localmail retry-failed [--account N]    # re-attempt every failed message
uv run localmail extract-backfill              # one-shot extraction backfill for all blobs
uv run localmail lang-backfill [--retry-declined] [--relabel [--yes]]  # one-shot body_lang detection
uv run localmail backfill-internal-date [--account N]  # IMAP INTERNALDATE for legacy rows
uv run localmail list-failed-extractions [--limit K]   # show blobs extraction skipped
uv run localmail retry-failed-extractions      # re-attempt every failed extraction
uv run localmail list-failed-fetches [--account N] [--limit K]   # UIDs whose BODY[] sync gave up on
uv run localmail retry-failed-fetches [--account N] [--forget] [--dry-run]  # rewind uidnext to re-fetch them
uv run localmail sweep-blob-temps [--dry-run] [--max-age-seconds S]  # collect stranded blob temps
uv run localmail estimate-upgrade [--format text|json]   # pre-flight size/duration for lock-heavy migrations
# see docs/operations/upgrade-runbook.md
# search-status reports Phase 2 attachment_text/attachment_chunks counts and
# body_lang_populated / body_lang_pending / body_lang_declined; its four
# blobs_{extracted,no_text,gave_up,pending} partition blobs_eligible, and
# blobs_claimable is the worker's real queue depth (allowlist-blind) (#277).
# Sub-second on a 127k-message archive since #280 decorrelated the eligibility
# lookup — it used to take 13½ minutes.
```

GUI server (Phase: gui-server):

```bash
uv run localmail add-api-user USERNAME       # create an API user (no grants by default)
uv run localmail list-api-users [--with-grants]
uv run localmail remove-api-user USERNAME
uv run localmail add-api-key NAME [--grant ACCOUNT]…  # mint a never-expiring key (stdout = the key)
uv run localmail list-api-keys                        # keys, grants, last-used
uv run localmail revoke-api-key NAME                   # kill the credential, keep the bot + grants
uv run localmail remove-api-key NAME                   # delete the bot entirely
uv run localmail grant-account USERNAME ACCOUNT_NAME   # per-user ACL (migration 0016)
uv run localmail revoke-account USERNAME ACCOUNT_NAME
uv run localmail rotate-tls --cert PATH --key PATH
uv run localmail serve [--bind 127.0.0.1] [--port 8443] \
                       [--tls-cert PATH] [--tls-key PATH] [--no-tls]
```

**`--config` reaches every command via `ctx.obj["config_path"]` (#245).** The
shared `cli._dsn(ctx)` takes the click context; it used to be zero-argument and
call `load_config()` with no path, so **nine** commands (`extract-backfill`,
`embed-backfill`, `lang-backfill`, `search-status`, `estimate-upgrade`,
`list-failed-embeddings`, `retry-failed-embeddings`, `list-failed-extractions`,
`retry-failed-extractions` — the issue named five, the four `_dsn`-only ones
were equally affected) silently ran against `~/.config/localmail/config.toml`:
a different database, a different attachment root. Making the context a
parameter means a new command cannot omit it without a `TypeError`. Pinned for
every command by `tests/test_cli_config_path.py`, which probes the DSN that
reaches `open_pool`/`psycopg.connect`. `search.create_searcher(cfg=None)` still
falls back to a no-path `load_config()` — that is the library default, where no
click context exists, and is why `secrets.configure`'s pin is kept.

**`--version` is the second reader of `localmail.__version__`, after
`/v1/version` (#279).** The manual's *install-verification* step tells users to
run it, and it printed a usage error — failing at the one point where a user
cannot tell a broken install from a missing flag. It also closes a real
diagnostic gap: it is the only `localmail` command that reports the version, so
on a host running just the sync daemon reading it meant starting `serve` for
`/v1/version`. (`uv pip show localmail` still works — the claim is about the
CLI's own surface, not about the value being unobtainable.) **The version is
never detected by click**: a bare `@click.version_option()` makes click read
the distribution metadata a *second* time, independently of `__version__`, and
the two disagree exactly where the resolution guards earn their keep — on a
tree that was never installed click raises `RuntimeError` where every other
reader degrades to `UNKNOWN_VERSION`.

**#291 replaced the decorator outright**, so `@click.version_option` is now
forbidden in `cli.py` **in every spelling**, not merely required to carry
`__version__`. Two independent reasons, and the first is the new one: click's
own callback prints and exits without ever consulting *why* the version is what
it is, so even the compliant `@click.version_option(__version__)` printed
`0.0.0+unknown` and said nothing. See the `version_report` section below.

**Three pins, each of which was weaker than it read** (review of #289 — every
one was proven by mutation, so do not relax them back):

- The value assertions anchor the *tail* of the output (`_printed_version`),
  because `__version__ in output` is also satisfied by `0.3.0-dev` and
  `0.3.0+local` on a `0.3.0` install — the wrong answers the flag exists to
  rule out.
- The derivation pin was a source regex ending `[,)]`, not `\b` — `\b` stops at
  the identifier and ignores a trailing ` + "-dev"`. **#291 retired the regex
  for a behavioural pin**: it rebinds `localmail.cli.__version__` and asserts
  the flag prints the rebound value. That is why `_print_version` reads the
  module attribute at call time; the decorator froze it at *decoration* time,
  which made it a literal from the callback's point of view (the mutation
  proves it — the pin fails against the old decorator with
  `assert '<installed version>' == '9.9.9+sentinel'`). It catches an
  f-string-assembled version too, which no regex reliably does.
- The config-free pin's `list-accounts` negative control asserts the
  `FileNotFoundError`'s **filename**, not just its class. `list-accounts` raises
  that from the *default* path too, so on any host without
  `~/.config/localmail/config.toml` — i.e. CI — the class check passed whether
  or not `$LOCALMAIL_CONFIG` was read at all. The DB half is asserted
  structurally by the `forbid_db` fixture; `exit_code == 0` tested nothing while
  Postgres was reachable, which it is on CI and both deployments.

**An unresolvable version is reported, not passed off as an answer (#291).**
`localmail --version` printed `0.0.0+unknown` with exit 0 and nothing on
stderr — "the version could not be determined", in a format indistinguishable
from success, at the one moment an operator is diagnosing a broken install. The
sentinel existed in `__init__.py` and was surfaced nowhere.

- The rule is [src/localmail/version_report.py](src/localmail/version_report.py):
  `UNKNOWN_VERSION` (named, not repeated — it had been written out twice and
  quoted in a comment), `VersionSource`, `resolve_version`, and the pure
  `unknown_version_diagnostic`. `__init__.py` resolves **once** and exports the
  three projections of that resolution (`__version__` / `__version_source__` /
  `__version_diagnostic__` — see the bullet below); re-deriving any of them per
  reader is the footgun the bare decorator carries. Everything else
  `version_report` exports is aliased private there and `_resolved` is `del`'d,
  so `localmail` gains no public second way to ask the same question.
- **The three failure causes are kept apart because the remedies differ**, which
  is the only reason to read the line: `NOT_INSTALLED` (no dist-info) wants an
  install, `METADATA_INCOMPLETE` (dist-info present, no `Version:`) wants a
  reinstall *over* it, `METADATA_UNREADABLE` (#296 — the read itself raised)
  wants the cause line read **first**, since the catch behind it is broader than
  any single remedy can speak to. `uv sync` does not repair the second. They
  used to collapse to one string.
- **The unreadable remedy defers to the cause line, deliberately.** `MemoryError`,
  `RecursionError` and anything a third-party `sys.meta_path` finder raises are
  all `Exception` subclasses reaching that branch, and none of them is a corrupt
  METADATA. The wording asserted a faulty filesystem for every one of them, which
  would send an OOMing host to `fsck` a healthy volume — the "causes are kept
  apart because remedies differ" principle inverted at the point #296 added a
  cause. Do not restore an unconditional filesystem claim here; the honest move
  when the catch is broad is to name the observation and let `cause:` diagnose.
- **`NOT_INSTALLED` is not reached by `python -m localmail` from a checkout** —
  the src layout makes that a `ModuleNotFoundError` first, and the 2B.4
  supervisor runs `sys.executable -m localmail` against an interpreter where the
  package is installed. The reachable shapes are the sources imported without
  their metadata (`PYTHONPATH=src`, a vendored copy) and a dist-info removed by
  a partial sync. The earlier claim that the supervisor path exercised this was
  wrong in three places and is corrected; do not reinstate it.
- **The remedy lives on the `VersionSource` member, not in a dict beside it.**
  A `dict[VersionSource, str]` read with `.get()` returns `None` for an unmapped
  member, and `None` is also how the module says "healthy, stay quiet" — a cause
  added without a message would report a broken install as fine, i.e. #291 one
  level up. Declared as a member payload, omitting it raises `TypeError` at
  **class creation**, so the common slip cannot reach CI at all. Same
  by-construction call as `ExtractedText.__post_init__` (#249/#266) and
  `_HttpJsonRewriter.base_url_setting` (#235).
  `test_every_unknown_source_has_a_diagnostic` remains as the backstop for the
  one thing construction cannot catch: a member written `("x", None)` on purpose.
  The member *values* are debugging aids, **not** a wire contract — nothing
  serialises or parses them (contrast `rewrite_note_code`).
- **The version line stays on stdout; the diagnostic goes to stderr.** That is
  why the fix is not `version_option(message=…)`, whose message is echoed to
  stdout: `--version` is scripted (it is the manual's install-verification
  step), and a warning on stdout breaks every naive parser of it. Pinned by
  `test_cli_version_flag_keeps_the_diagnostic_off_stdout`.
- **Exit stays 0 on the unknown path** — an explicit decision. A non-zero status
  would break every script using `--version` as a liveness check, and it argues
  against the deliberate choice to degrade gracefully rather than raise the way
  click's own lookup does. The stderr line carries the diagnosis.
- **stdout's `%(prog)s, version %(version)s` shape is pinned separately** by
  `test_cli_version_line_keeps_click_s_documented_format`, and that separation
  is load-bearing: `_printed_version` uses `rpartition`, which returns the whole
  string when the separator is absent, so a bare `0.3.0`, the real line, and
  `nonsense, version 0.3.0` all reduce to the same token. Every value assertion
  in the module survives deleting the documented prefix; only that one test does
  not.
- **The `resilient_parsing` early return is pinned** by
  `test_cli_version_flag_stays_silent_during_completion`. click sets that flag
  while resolving shell completions; without the guard the version line is
  echoed into the completion protocol stream, so `localmail --version <TAB>`
  offers it as a candidate. Deleting the clause otherwise leaves the suite green.
- **`_mentions_version_option` walks the AST, not the text.** The rationale for
  banning that decorator necessarily quotes its spelling — in a comment beside
  the replacement option *and* in `_print_version`'s docstring — and #279's
  regex-over-stripped-comments approach handled the comment but not the
  docstring, so writing the reason down broke the pin that enforces it (it did,
  once, during #291). Prose is not code, and the AST is where that distinction
  already lives. It covers `daemon_cli.py` as well as `cli.py`: that module
  defines a second click group mounted by `main.add_command(daemon_group)`, so a
  `version_option` there would attach to the same CLI from outside the pin.
- **Test helpers take `result.stdout`, never `result.output`.** Since click
  **8.2** `output` interleaves stdout and stderr in write order, so
  `_printed_version`'s tail anchor started reading from whichever stream spoke
  last — and the diagnostic contains the word "version". 8.2 is also why
  `pyproject.toml` floors `click>=8.2`: `CliRunner` gained separately-captured
  stderr there, and on 8.1 the four stderr assertions raise `ValueError: stderr
  not separately captured` instead of failing honestly. The runtime is fine on
  8.1 — this floor is the *tests'*.
- **`import must not fail` is enforced against every exception, not one (#296).**
  `importlib.metadata.version` reads `METADATA` as UTF-8 through a
  `suppress(...)` list covering neither `UnicodeDecodeError` nor a generic
  `OSError`, so a file in another encoding — or an EIO on a network-mounted
  `site-packages` — propagated out of `import localmail` and killed **every**
  entry point with a bare traceback: CLI, `serve`, daemon, MCP, and `--version`
  itself, the one command whose purpose is diagnosing a broken install.
  Reproduced end-to-end with a latin-1 byte in a
  `localmail-9.9.9.dist-info/METADATA` placed ahead on `sys.path`. The shipped
  `METADATA` is pure ASCII today (`[project]` declares no `readme`), so the
  truncation variant was latent; the encoding and `OSError` variants were live.
  - **The broad `except Exception` is defensible only because it reports what it
    caught.** `ResolvedVersion.unreadable(exc)` renders it onto `.detail` and the
    line carries it as `cause:`. The rendering is `traceback.format_exception_only`,
    **not** `type(exc).__name__`: a bare `OSError` cannot separate EIO (hardware)
    from ESTALE (remount) from EACCES (`chmod`) — three different remedies, i.e.
    exactly the distinction this module exists to preserve — and the type name
    also loses the filename and the decode offset that #296's own reproduction
    turns on. It retains **no frame references**, which matters because `detail`
    becomes a module global at import; a traceback would pin frames for the
    process's life. The type name still leads, which is `failure_pacing.py`'s
    point (`str(exc)` alone is empty for much of what fails here). A discarded
    exception would be a silent catch, i.e. #291 wearing a third hat.
  - **The rendering rule lives on `ResolvedVersion.unreadable`, not at the catch
    site**, so a second catch cannot re-decide it — the one-authority argument
    `pgtext.strip_nuls` and `text_empty.is_blank` already make.
  - **It renders the whole `__cause__`/`__context__` chain, not the outermost
    exception (#303).** `format_exception_only` formats one exception, so
    `raise RuntimeError("finder failed") from OSError(5, …, "/nfs/…/METADATA")`
    rendered as `cause: RuntimeError: finder failed` — the errno and filename
    gone, i.e. the rendering discarding exactly what it was chosen over a type
    name to keep, under a remedy that says "read the cause below first". A
    wrapper is the normal shape for the third-party `sys.meta_path` finder the
    docstring names as reachable. The rule is the pure
    `version_report.render_exception_chain`, outermost first, joined by
    `_CHAIN_SEPARATOR`.
    - **Three bounds, because this runs on the import path inside a handler that
      may not fail**: `_MAX_CHAIN_LINKS` (5) ends a pathological wrapper stack,
      an identity set ends a `__context__` cycle (reachable — an exception
      re-raised while its own cause is handled), and `_MAX_DETAIL_CHARS` still
      applies **to the joined result**, so the ceiling is not silently five
      times looser. All four are mutation-pinned. The bound test asserts against
      `_MAX_DETAIL_CHARS + len(_TRUNCATION_MARKER)`, **not** a round number: at
      the shipped values a per-link ceiling renders 1014 characters, so the
      literal `1_000` it used to carry caught that regression by 14 characters —
      a margin shortening `_CHAIN_SEPARATOR` would silently spend.
    - **A walk cut short by either of the first two ends in
      `_TRUNCATION_MARKER`** (review follow-up to #303). The end it drops is the
      *innermost*, which is where the errno and the filename are, so an unmarked
      truncation hands over a degraded cause in a shape indistinguishable from a
      complete one — under a remedy line that says to read it first, i.e. #291's
      defect one layer down. The marker is **named once** and shared with the
      character ceiling: one that appeared for one truncation and not the other
      would teach the reader that an unmarked line is complete. A chain ending
      naturally gains nothing, which keeps the common unwrapped rendering
      byte-identical.
    - **`__suppress_context__` is honoured**, so `raise X from None` prints no
      chain: the author detached it deliberately and `traceback` agrees. The
      walk follows `__cause__` first, then `__context__` unless suppressed —
      following only `__cause__` would miss most real wrappers, since a bare
      `raise` inside an `except` sets the context, not the cause.
    - **The link test is `is not None`, never `or`** (review follow-up to #303).
      An exception whose class defines `__bool__`/`__len__` is falsy while being
      perfectly present, and `or` skipped it — this rendering dropping the
      exception that names the fault, which is the whole of #303, reintroduced
      by the walk added to fix it. The fallback cannot cover for it either:
      assigning `__cause__` sets `__suppress_context__`, so a skipped cause is
      lost rather than replaced by the context. Read lazily, so a hostile
      `__context__` is not touched once a cause has answered.
    - **Each link is rendered in its own `try`** (`render_one_exception`), so one
      hostile link costs its own detail rather than the whole chain's; the outer
      guard in `unreadable` remains, because reading `__cause__` off a hostile
      object can raise too. An unwrapped exception gains **no** separator and no
      empty link — pinned, since both shapes #296 reproduced are unwrapped and a
      chain walk must not change every real rendering to buy an unseen case.
  - **`Exception`, never `BaseException`**, and `PackageNotFoundError` must stay
    **ahead** of it: it is a `ModuleNotFoundError` subclass, so reordering the
    two silently reclassifies every uninstalled tree as a corrupt one and sends
    the operator to `fsck` instead of `uv sync`. Both pinned.
  - **This site carries no `# noqa: BLE001`, and neither do most of its
    siblings.** Of the 79 `except Exception` sites in `src/`, **14 carry the
    directive and 65 do not** — so there is no convention here in either
    direction, and this site is with the majority rather than diverging from
    anything. (An earlier wording said "fourteen sibling broad catches do,
    that is a divergence", which counted correctly and framed it backwards.)
    Nor is the directive inert on principle: BLE001 is **not** in ruff 0.11's
    default set but **is** from 0.16, and this tree has been run with both
    (`.ruff_cache/` holds each) — so "inert under the pinned ruff but live under
    newer ones" was self-refuting, since being live under newer ones *is* it
    entering the default set. **Nothing pins a ruff version**: there is no ruff
    in `pyproject.toml`, none in `uv.lock`, no `[tool.ruff]` section, and no
    lint step in either CI workflow. #285 (open) decides whether ruff gates CI
    at all; revisit this bullet with it.
    The earlier claim that writing `# noqa` inside this comment would itself
    create a directive is **wrong as applied**: a directive binds to its own
    line, and this comment is not on the `except` line. A *trailing* comment on
    that line would suppress.
  - **The reporting step is guarded too, and that is not decoration.**
    `traceback.format_exception_only` is not total — it calls `.rstrip()` on
    `SyntaxError.text` unconditionally, so an exception carrying a non-`str`
    there makes the renderer raise — and it allocates (a `TracebackException`,
    a `StackSummary` per chain link, `linecache` reads), which is what fails
    again under the very `MemoryError` the remedy text names. Running unguarded
    *inside* the handler, it propagated straight back out and killed `import
    localmail`: #296's defect restored by #296's own fix, and violently — the
    unguarded form takes the interpreter down with "lost sys.stderr", and takes
    pytest itself down with an `INTERNALERROR`. `unreadable` therefore renders
    in a `try`, falling back to the bounded pre-#296 `type(exc).__name__`, so
    the cause degrades rather than being lost. The rendering is also **capped**
    (`_MAX_DETAIL_CHARS`, 500): `detail` becomes a module global for the life of
    the process and is logged in full at every startup, while
    `format_exception_only` embeds the whole of `str(exc)` plus every PEP 678
    note — both chosen by whatever raised.
  - **`ResolvedVersion.__post_init__` enforces three pairings, not one.** The
    `detail` ⟺ `METADATA_UNREADABLE` biconditional is the one #301 shipped; the
    other two are rules the field's own comment already claimed and nothing
    checked. A **blank** detail is rejected (`is not None` admitted `""`, which
    rendered a bare `cause:` line — verbatim the "reads as if a detail were
    withheld" outcome the guard exists to prevent). And a **failed source must
    carry the sentinel**: `unresolvable(INSTALLED)` otherwise yielded
    `__version__ = "0.0.0+unknown"` with `__version_diagnostic__ = None`, which
    is #291's shape exactly, in the module written to end it. That rule is
    **one-directional on purpose** — the converse would fail `import localmail`
    for a pyproject that ever declared `0.0.0+unknown`, over a cosmetic
    collision, and import not failing is this module's first rule.
    - **Because that blank check *raises*, `unreadable`'s fallback tests
      `rendered.strip()`, not truthiness** (review follow-up to #303). A bare
      `or` catches only `""`; `"   "` is truthy and sails through to the raise —
      `import localmail` dying inside the handler written to stop `import
      localmail` dying. Unreachable through the real renderer today, and pinned
      anyway, because what makes it unreachable is that `_CHAIN_SEPARATOR`
      happens to contain letters: the guard's correctness rested on a constant it
      does not own. Mutation-pinned (restoring the `or` raises `ValueError`).
  - **An empty remedy on a `VersionSource` member is rejected at class
    creation** by the module-level `reject_empty_diagnostic`, which
    `VersionSource.__new__` is the only caller of. A member written
    `("new-cause", "")` supplies both payload elements, so the signature is
    satisfied and no `TypeError` fires — and `log_version_diagnostic`'s falsy
    guard then discards it, reporting a broken install as healthy. That is #291
    one level up, i.e. precisely what declaring the remedy on the member is
    supposed to make impossible. It is a **module-level function rather than an
    inline check** because enum machinery replaces `__new__` after class
    creation, so no test can reach the production one to prove the rule fires
    for a *future* member.
- **Every entry point reports an unresolvable version at startup (#295, #304).**
  #291 fixed `--version` and only that, leaving `__version_source__` with exactly
  one reader — so on a headless host `serve` and `run` shipped the sentinel in
  silence, `/v1/version` answered `0.0.0+unknown` as if it were a version, and
  the GUI rendered it. #291 verbatim, one reader over.
  - The rule is `version_report.log_version_diagnostic(log, diagnostic)` — one
    ERROR, or nothing — called by `serve_cmd`, `create_app`, `run_cmd`,
    `Daemon.__init__`, and the `main` **group callback**. Shared so they cannot
    drift to different levels or wordings. The CLI's `--version` is deliberately
    **not** a caller: its stderr goes through click because its stdout is the
    machine-readable line, and its option is eager and exits inside its own
    callback, so the group callback never runs for it (pinned).
  - **#304 was the reach gap, and it was the one that mattered operationally.**
    #295 wired the report to the two entry points it named; the other **36** of
    38 commands caught an unresolvable version and surfaced it nowhere. Since
    #296 deliberately traded a loud crash for graceful degradation, that made a
    cron `localmail sync` on a host with a failing `site-packages` mount run to
    completion with exit 0 and say nothing — where before #296 it failed loudly
    on the first night. The RED test reproduced the headline exactly: **36
    failures**, one per command.
    - **`cli.SELF_REPORTING_COMMANDS` (`{"run", "serve"}`) is what the group
      callback steps aside for — and the two are in it for different reasons.**
      Only `run`'s is about formatting: `run_cmd` calls `basicConfig` before it
      reports, so reporting for it in the group callback would win the
      per-process dedup with an *earlier, unformatted* line and silently
      downgrade it. **`serve` does not configure logging first** — it reports as
      the first statement in its body, ahead of the deferred `import uvicorn`,
      so its line goes through `logging.lastResort` exactly as the group
      callback's would; what it keeps is the `localmail.serve` **logger name**,
      which says *which* process is broken on a host running both planes.
      Verified by mutation: dropping `serve` from the set fails three existing
      ordering pins, and the failure names `localmail` vs `localmail.serve`. An
      earlier wording here (and in `cli.py`) said both commands "configure
      logging first"; that was false for `serve` and contradicted
      `log_version_diagnostic`'s own docstring two sections down, which reasons
      from `serve` *not* having configured logging. Do not restore it.
    - **Only one drift direction is survivable, so the set is derived and
      compared, never trusted.** A command listed but not reporting goes
      **silent** (#304 reopened for exactly the long-running processes #295 was
      about); a command reporting but not listed merely loses its logger name
      (and, for `run`, its formatting).
      `test_the_skip_set_is_exactly_the_commands_that_report_themselves` reads
      the **live** `main.commands` registry — so a command added later is in
      scope without anyone updating a list — and decides by walking each
      callback's **AST**, not its text: the reason for the rule is written in
      comments beside it, and #291 already paid for the lesson that a text match
      cannot tell prose from code. Mutation-pinned in both directions, plus a
      third mutation proving a prose mention does *not* count.
    - **A help request is skipped, and all four shapes are now pinned (#307).**
      The callback used to fire for `localmail <cmd> --help` — click resolves
      the subcommand before applying its `--help` — while bare `localmail`,
      `localmail --help` and an unknown command stayed silent, because
      `no_args_is_help` short-circuits ahead of it. Help does no archive work
      and touches neither config nor DB, and the line landed ahead of the text
      the operator had explicitly asked to read; the decision was to quieten
      the odd one out rather than make the other three loud. `--version` still
      reports, being the command whose whole job is diagnosing the install.
      - The rule is the pure
        [src/localmail/cli_help_request.py](src/localmail/cli_help_request.py)`::is_help_request`,
        shaped like `account_names.py::account_name_error`. It reads
        `ctx.help_option_names` rather than spelling `--help` (click lets a
        project add `-h`) and stops at a bare `--`, since click would not treat
        a later token as the option either. **Known imprecision, deliberate**:
        a help token consumed as an option *value* reads as a help request,
        because judging otherwise means knowing every option's arity — a second
        parser to keep in step with the first, for the cost of one suppressed
        diagnostic on a pathological invocation.
      - **The callback cannot answer this itself**, which is why there is a
        `_HelpAwareGroup`: by the time click runs the group callback the
        resolved subcommand's own arguments are off the context (`ctx.args` is
        empty whether or not `--help` was typed). They are still in place one
        frame out, in `Group.invoke`, so the *question* is asked there and the
        verdict passed via `ctx.meta`; the **report stays in the callback**,
        beside the `SELF_REPORTING_COMMANDS` skip it shares a decision with. A
        nested group's help (`localmail daemon status --help`) arrives as the
        root's pending `['status', '--help']`, which the scan covers and a rule
        reading only the first pending argument would miss.
      - **The three shapes that were already quiet are pinned too**, since
        their silence is a side effect of click's parse order rather than a
        choice — an `invoke_without_command=True` would flip them loud with
        nothing failing. And a **positive control** (`localmail sync` still
        reports) guards the other direction: a rule matching too broadly
        reopens #304 for the 36 commands it was filed about, and every
        quiet-shape assertion would still pass. Mutation-pinned — forcing
        `is_help_request` to `True` fails 43 tests, including the reach pins.
  - **The severity word is derived from the level, not written beside it
    (#302).** All three remedies opened with `warning:` while the record was an
    ERROR, so journald showed `ERROR … warning: …` and an operator told to grep
    for one found the other. `_SEVERITY_PREFIX` is now
    `logging.getLevelName(_REPORT_LEVEL).lower()`, so the two cannot be changed
    apart — the one-authority call again. The pin reads the word back off a
    record the module actually emitted rather than comparing against a literal
    `"error"`, which would pass against a remedy set and a level that agree with
    the literal and not with each other.
    - **Most paths print no level at all, which is why the word carries weight.**
      `run_cmd` has called `basicConfig`; `serve`, and the group callback that
      covers the other 36, have not, and nothing they import does — so those
      records go out through `logging.lastResort`: stderr, message only, no
      level, no timestamp, no logger name. Configuring logging in the group
      callback to fix that was **rejected**: it precedes all 38 commands, and
      installing a root handler for every one of them changes far more than the
      line it would format. The level still matters where it is not decoration —
      `run` after `basicConfig`, `create_app` under uvicorn's `dictConfig`, and
      any embedder constructing `Daemon` directly.
  - **`--version` still dies on the *other* broken install (#305, open).** What
    the broad catch buys is an unreadable METADATA, and only that: `cli.py`
    imports the daemon — and so `sqlparse`, `psycopg`, `keyring` — at module
    scope, so a partial `uv sync` that dropped any third-party dependency kills
    the command with a bare traceback before click parses the flag. Reproduced by
    blocking one module on `sys.meta_path`: `import localmail` succeeds and
    resolves its version, `import localmail.cli` does not. The module docstring
    states the scope rather than overclaiming; making it survivable means
    deferring those imports into the command bodies, which belongs with the
    `cli.py` refactor already owed.
  - **ERROR, not WARNING.** `localmail run --log-level ERROR` is an offered
    `click.Choice` and `run_cmd` calls `basicConfig` with it *before* constructing
    the daemon, so at WARNING the line was filtered out entirely — and
    `basicConfig`'s root handler also removes the `logging.lastResort` escape that
    saves the `serve` path. A report the process can be told to discard is not a
    report. Pinned by a test that reads the choices off `run_cmd` itself, so
    adding a quieter one fails rather than silently reopening the hole.
  - **Reported once per process** (`_REPORTED`, with `reset_version_reports()` and
    an autouse conftest fixture — the `embed_worker._FAILURE_LOG` shape), because
    `serve` and `run` each report at two layers. Under `uvicorn --workers N` each
    worker is its own process and still reports. **Keyed on the diagnostic
    string, not on a "have we said anything yet" flag** — the flag form silences
    a *different* second problem for the life of the process, a suppression bug
    inside the module written to end suppression bugs. Both halves are pinned
    now; neither was, and deleting the dedup outright left the whole suite green,
    which is what let the `cli.py` comment assert "create_app's call below stays
    silent" with nothing behind it. (The `embed_worker._FAILURE_LOG` comparison
    is about the module-global-plus-reset shape only: that one also takes a
    `failure_log=` parameter defaulting to the global, and this has no such
    seam — three callers, all entry points, so there is nothing to inject.)
  - **Every call runs before the gate it precedes**, and that ordering is pinned,
    not just commented: the daemon reports before `retry_with_backoff` waits on
    Postgres (a host broken enough to lose its version may well have a DB down
    too, and that wait is unbounded); `create_app` reports before the
    `state_signing_key` check raises; and **`serve_cmd` reports before
    `pending_migrations`**, which is the one that actually mattered — that check
    raises `ClickException("… Is Postgres reachable?")` and `create_app` is never
    reached, so on an unreachable DB `serve` was still silent after #295's first
    pass. A diagnostic emitted after a raise is a diagnostic never emitted.
  - **`load_config` is a gate too, and it took a third pass to cover it.** The
    rule above was stated absolutely but applied to one gate per command:
    `serve_cmd` reported *after* `load_config`, and `run_cmd` reported only from
    inside `Daemon.__init__` — one gate later still. A missing or malformed
    config raises at `load_config`, so on a host mid-deploy neither command said
    anything. `serve_cmd`'s call is now the **first statement in its body**,
    ahead of the deferred `import uvicorn` (a partial `uv sync` that dropped the
    dist-info can equally have dropped `uvicorn`), the `--no-tls` usage check,
    `load_config`, and the schema check; `run_cmd` gained its own call between
    `basicConfig` and `load_config`, on `localmail.daemon` so the grep target is
    unchanged. The dedup is what makes the extra call sites free. The gap
    survived #295 because the test that pinned the schema gate drives the
    `LOCALMAIL_DSN_OVERRIDE` branch, which **skips `load_config` entirely** — a
    reminder that a pin proves only the path it takes.
  - **`/v1/version` gained no field in #295, deliberately — and gained three in
    #278/#300.** The reasoning then: the GUI's connect probe decodes
    `server_version` as a non-optional String — which is *why* the sentinel
    exists rather than a null — and a new key nothing renders was #278 from the
    other end (the About tab declared a `build_hash` the server never emitted,
    with four test files and a Rust `#[cfg(test)]` module mocking it into
    looking covered). Reversible; removing a shipped wire key is not. #300
    tracked the consequence: an unresolvable version was legible to a human on
    every entry point and to a *machine* on none.
    **Both are closed now** — see the build-provenance bullet below. What
    changed is not the objection but the fact it rested on: the new keys have a
    renderer, which is exactly the test #295 set ("a new key nothing renders").
    The machine-consumer case is stated in README for external monitoring and
    has **no in-tree reader** — do not cite it as though it were shipped. The
    `--version` half of #300 needed no flag: stderr is non-empty iff the version
    is unresolvable, now stated in README and pinned. And `__version_source__`'s
    retention paid off as predicted — `version_source` is derived from it.
- **`__init__.py` exports three attributes, and `__version_diagnostic__` is
  rendered there rather than by each reader.** The exception type behind a
  `METADATA_UNREADABLE` resolution is known only at resolution time, so a reader
  handed just `__version_source__` drops it silently — and there are three
  reader *modules* now (`cli.py` reads it twice since #304: `_print_version` and
  the group callback). `unknown_version_diagnostic`'s `detail` is keyword-only **with no
  default** (#234's shape) for the same reason one layer down; there is exactly
  one production call site, which is what makes that free rather than noisy.
  Each reader imports with `from … import`, so a test must rebind the *reader's*
  binding — `localmail.__version_diagnostic__` reaches none of them.
- **The build identity is resolved from the checkout, not stamped at build time
  (#278, #300).** `/v1/version` now carries `build_hash`, `build_source` and
  `version_source`. Design:
  [docs/superpowers/specs/2026-08-15-build-provenance-design.md](docs/superpowers/specs/2026-08-15-build-provenance-design.md).
  - **There is no build**, which is why. Both CI workflows are test-only, there
    are no tags, nothing publishes, and *both* deployments run editable installs
    from a git checkout — so a hash stamped into a wheel would be absent on the
    only machines the row is ever read on. `STAMPED` is **reserved and
    unreachable**: nothing reads a `_build_info.py` and nothing writes one, so
    do not go hunting for the branch — the member exists only to settle the
    wire value before a release pipeline does. Implement the hatchling hook
    when there is one, not before.
  - **No source carries a remedy, and only `GIT_FAILED` logs** — where it
    deliberately parts from `version_report`. An unresolvable *version* is
    always a fault; an unresolvable *build hash* usually is not, since
    `NOT_A_REPO` is the correct state of an installed artifact, so copying
    `VersionSource`'s forced-remedy rule across would put an ERROR in front of
    an operator for a healthy install, i.e. #291 inverted.
    - **The silence is scoped, not absolute, and the scope is load-bearing.**
      `GIT_FAILED` is the one member that is a fault by construction, and
      `capture_output=True` means git's own account of it is already in hand.
      Discarding it is precisely the silent catch this codebase forbids of
      `version_report`'s identical broad catch — *"defensible only because it
      reports what it caught"* — so `_git_failed` logs one WARNING carrying
      either git's stderr and exit code or the rendered exception chain
      (`version_report.render_exception_chain`, reused so there is one
      rendering rule). WARNING, not the sibling's ERROR: a broken *version*
      breaks the install, while a broken *build hash* degrades one diagnostic
      field on a server that otherwise works. One line per process, since
      resolution is cached — no dedup machinery needed. It stays out of the
      response body for the reason the human diagnostic does: the route is
      unauthenticated and git's stderr carries filesystem paths.
    - **`NOT_A_REPO` is exit 128 and nothing else.** `!= 0` routed a
      signal-killed git (OOM reports `-9`) and a usage error from a future
      flag change into the *healthy* category — a broken host reported as an
      installed artifact, which is the collapse this whole feature exists to
      end, one probe in. The dirty probe's `not in (0, 1)` had the rule right
      and the two disagreed; they ask the same question now.
  - **Resolution is lazy and cached, never at import.** `import localmail` runs
    for all 38 CLI commands and a `git` subprocess there can hang on a stale
    mount — the #296 scenario. Caching also gives the semantics the row wants:
    pinned for the life of the process, so it reports what the process is
    *running*, not what the tree says now. That is live on an editable install,
    where a `git pull` moves the tree under a daemon that keeps executing the
    code it already imported. `reset_build_info()` + an autouse conftest
    fixture, the `reset_version_reports()` shape.
  - **The repo it finds must be ours**, checked by requiring
    `<toplevel>/src/localmail/__init__.py` to resolve to the file we imported.
    Containment is not enough: a virtualenv inside a dotfiles repo *is*
    contained, and would report that project's SHA as localmail's build. Its
    own test, because it fails silently.
  - **`-dirty` measures tracked files only** (`git diff --quiet HEAD`). Scratch
    files would make every deployment read dirty forever, and a marker that is
    always on carries no information. A single
    `git describe --always --dirty` would halve the subprocess count and was
    **rejected**: the day someone tags a release it silently returns
    `v0.4.0-3-geec8e09-dirty`, changing the field's format under us.
  - **The probe is parsed with `splitlines()`, never `.split()`.** `git rev-parse
    --show-toplevel --short HEAD` emits two *lines*, and the first is a path:
    `.split()` splits on any whitespace, so a checkout under a directory
    containing a space yielded 3+ tokens and reported a healthy tree as
    `GIT_FAILED`. Found by review before it shipped, and pinned by
    `test_a_repo_path_containing_a_space_still_resolves`.
  - **The wire strings are declared, never derived.** `BuildSource`'s value IS
    its wire string; `VersionSource` carries a separate `wire_name`, because its
    own values are hyphenated debugging aids (`"not-installed"`) while this
    API's wire enums are underscored (`rewrite_note_code` ships
    `not_configured`). `reject_empty_wire_name` enforces non-emptiness at class
    creation, beside `reject_empty_diagnostic` and for the same reason.
  - **The diagnostic text is deliberately NOT on the wire.** `/v1/version` is
    unauthenticated and `__version_diagnostic__` embeds rendered exception text
    carrying errno values and filesystem paths (#303). `version_source` is an
    identifier; the human line stays in the logs where #295 put it. If a
    machine-readable *reason* beyond the enum is ever wanted, it belongs on an
    authenticated endpoint.
  - **`--version` gained no flag.** stderr is non-empty iff the version is
    unresolvable — true since #291, now stated in README and pinned by
    `tests/test_cli_version_stderr_contract.py`. stdout stays the single line
    and exit stays 0, so neither is a failure signal.

Common gotcha when running ad-hoc commands: shells often have `VIRTUAL_ENV`
set to some other pyenv venv, which makes `uv run` warn and (with `--active`)
pick the wrong interpreter. Prefix with `unset VIRTUAL_ENV && …` to be safe.

**Three `uv` footguns met while landing #314.** Each degrades a *running* archive
without announcing itself; the second and third were reproduced on this host, the
first is uv's documented resolution behaviour:

- **Every sync must carry `--extra extraction --extra mcp`.** uv installs only
  the default dependency set and prunes the rest, so omitting them uninstalls
  `docling`, `mcp` and `rapidocr` — the last arriving transitively via
  `extraction`, and being the OCR engine `auto` finds on Linux — costing MCP and
  every scanned-PDF extraction. uv 0.11 has no `default-extras`, so there is no
  declarative fix.
- **`uv sync --dry-run` is not read-only.** On uv 0.11.32 it *replaced* the
  project environment while reporting only what it "would" do, leaving the wrong
  interpreter **and no extras** — observed with the extra flags present, so the
  flags are no protection. Do not reach for it to "just check".
  - **`uv lock --dry-run` is a different command and *is* read-only** —
    verified, not assumed, because of the sibling above: `uv.lock`,
    `pyproject.toml` and the installed versions all compare byte-identical
    across `uv lock --upgrade-package <pkg> --dry-run`. That is the command to
    reach for when sizing a dependency bump.
  - **A branch checkout re-resolves the environment.** Switching to `main` and
    back mid-session silently downgraded `pypdf`/`icalendar` to the versions
    the *other* branch's lock names, and the next `uv run` re-installed them
    again. Re-sync deliberately after any checkout you intend to measure
    against; do not assume the venv followed you.
- **`.python-version` (`3.13`) beats `UV_PYTHON`.** Every uv command reads it,
  including `uv run`, and only an explicit `--python` overrides it. That is why
  `python-ci.yml` passes `--python ${{ matrix.python }}` to both `uv sync` and
  `uv run`: without it the 3.12 matrix leg silently syncs and tests 3.13 and the
  label lies. The matrix carries **both** interpreters because #314's defect is
  an interpreter difference and `requires-python` still admits 3.12.

## Layout

```
src/localmail/
  cli.py            # click entry point
  cli_help_request.py # pure: is_help_request — keeps the version line off help (#307)
  config.py         # pydantic models + TOML loader
  db.py             # connection pool + migration runner
  secrets.py        # keyring wrapper
  oauth_gmail.py    # OAuth2 desktop flow + token refresh
  imap_client.py    # open_connection() context manager (password / XOAUTH2)
  parser.py         # bytes -> ParsedMessage (pure; no IO; NUL-strip + empty->None)
  pgtext.py         # pure: strip_nuls / strip_nuls_all — the one NUL rule
  ocr_policy.py     # pure: plan_ocr / unknown_engine_message (#248)
  version_report.py # resolve_version + pure unknown_version_diagnostic (#291)
                    #   + METADATA_UNREADABLE (#296) + log_version_diagnostic (#295)
                    #   + pure render_exception_chain (#303) + _SEVERITY_PREFIX (#302)
  build_report.py   # pure enum + BuildInfo, lazy git resolution (#278, #300)
  attachments.py    # write_attachments(conn, parsed, root) -> JSONB rows (content-addressable)
  blob_temps.py     # writer temp naming (new_temp_path) + its collector (sweep_blob_temps) (#237)
  fetch_retry.py    # bounded BODY[] hold (#222A) + give-up tombstones/rewind planner (#239)
  sync.py           # upsert_*, process_one_message, sync_mailbox, sync_account,
                    #   record_failed_message, retry_failed_messages, folders_to_sync
  worker.py         # WorkerContext shared by daemon threads
  idle.py           # run_inbox_idle_loop, _one_inbox_session, _idle_step
  poller.py         # run_poll_loop, _one_poll_pass
  daemon.py         # Daemon class: signal handling, per-account thread spawn
  shutdown_budget.py # remaining_seconds/supervisor_kill_after (pure) +
                    #   wind_down_threads — the one shutdown budget (#221 A)
  search/           # hybrid search subsystem (Phases 1 + 2)
    __init__.py     # public API: create_searcher, Searcher, SearchPage, SearchResult
    arms.py         # retrieval arms: arm_bm25_messages, arm_bm25_chunks, arm_vector_chunks, arm_vector_attachment_chunks
    chunking.py     # chunk_message() -> ChunkSpec list; chunk_attachment_text() -> ChunkSpec list
    embed_worker.py # run_embed_worker_once, run_embed_worker (background thread)
    embeddings.py   # FastEmbedBackend + EmbeddingBackend ABC
    attachment_kind.py # pure: extension_of / is_allowlisted / preferred_filename / is_pdf (#216)
    extractor.py    # LightweightExtractor (11 formats) + ExtractorBackend ABC; DoclingExtractor via [extraction] extra
    extract_worker.py # run_extract_worker_once, run_extract_worker (background thread)
    extract_queue.py  # the one claim/eligibility predicate + fetch_queue_counts (#277)
                    #   + QueueCounts (4-bucket partition + allowlist-blind claimable)
                    #   + decorrelated extension join (#280) + misfiled guard (#284)
    lang_detect.py  # LinguaDetector + FixedDetector + run_lang_detect_pass for messages.body_lang
                    #   + CLAIMABLE/DECLINED/RELABELABLE_WHERE_SQL, retry_declined (#251),
                    #   reopen_all (#255)
    lang_text.py    # pure: normalize_for_detection — the one detector-input rule (#255)
    text_empty.py   # pure: is_blank — the one "nothing to index" rule (#266)
    page_cache.py   # in-process LRU cache for paginated result pools
    sweep_pacing.py # pure: SweepOutcome + idle-streak/sleep arithmetic (#259)
    failure_pacing.py # pure: how often a repeating batch failure reports (#267)
    query.py        # parse_query() -> ParsedQuery, SearchFilters, filter DSL
    reranker.py     # FastEmbedReranker + Reranker ABC
    rewriter.py     # Phase 4 --smart: build_rewrite_prompt/parse_rewrite_response/apply_rewrite (pure) + PEP562 back-compat re-exports
    rewriter_backends.py # _HttpJsonRewriter base + Ollama/OpenAI/Anthropic backends + build_rewriter() factory
    rewriter_url.py # pure: base_url_error — the one base-URL rule (#235)
    searcher.py     # Searcher orchestrator, rrf_fuse(), make_snippet(), SearchResult
    sort_axes.py    # pure: SortMode/SortOrder, their defaults, and
                    #   resolve_sort/sort_applicability_error — one
                    #   authority per axis (#312, #324)
    date_keyset.py  # pure: the date walk's ORDER BY, keyset predicates,
                    #   undated top-up and the one SQL emitter (#323)
    keyset_walk.py  # pure: walk_for_text / keyset_walk_error — a text cursor
                    #   needs its query back (#326)
    argument_errors.py # SearchArgumentRefused + its four subclasses — the
                    #   one family every api boundary maps to 400 (#344)
migrations/         # 0001_init.sql … 0036_api_keys.sql (0023_daemon_heartbeats.sql also applied)
tests/
  acceptance/       # standalone eval harnesses — FIVE (run_recall_eval.py,
                    # run_attachment_eval.py, run_rrf_k_sweep.py,
                    # run_browse_explain.py, run_chunk_insert_bench.py);
                    # browse_explain_lib.py is a library, not an entry point
    _harness_lock.py  # harness_db_lock + the AST rule requiring it (#337)
  conftest.py       # memory_keyring fixture, db_dsn/db_conn fixtures
  _eml.py           # MIME fixture builders (no .eml files on disk)
  _fake_imap.py     # in-memory IMAP fake with IDLE support
  _gated_supervisor.py  # DaemonSupervisor whose stop() parks — busy-guard pins
                    #   hold their window open instead of racing a timer (#299)
  _multilingual_corpus.py  # synthetic 50-message corpus for multilingual eval
  fixtures/         # multilingual_queries.example.json
  test_*.py
config.example.toml
```

User-facing config lives at `~/.config/localmail/config.toml` (override with
`$LOCALMAIL_CONFIG` or `localmail --config PATH …`).

## Schema essentials

Tables: `accounts`, `mailboxes`, `messages`, `message_labels`,
`attachment_blobs`, `attachment_text`, `attachment_chunks`,
`failed_messages`, `failed_extractions`, `transient_extractions`,
`api_users`, `api_tokens`, `channel_subscriptions`, `transient_fetches`,
`user_accounts`, `schema_migrations`. Migration `0020_accounts_canonical.sql`
extended `accounts` with `folder_allow`, `folder_deny`, `folder_deny_flags`,
`sync_enabled`, `updated_at`, lifted the `NOT NULL` constraint from
`imap_host`/`imap_port`, widened `auth_method` to include `'archive'`, and
added the `accounts_live_requires_host` check constraint (live accounts must
have a host). Migration `0036_api_keys.sql` extended `api_tokens` with
`api_key_name` (NULL = a session token; non-NULL = an API key, and the column
*is* the credential kind), dropped `NOT NULL` from `api_tokens.expires_at`
under the `api_tokens_only_keys_are_immortal` CHECK, added the partial unique
index `api_tokens_one_key_per_service_user`, and extended `api_users` with
`is_service` (default FALSE — an API key's principal, never a person). Dedup
model:

- **Messages — per-account, by `Message-Id`**: same Message-Id in INBOX + 3
  Gmail labels produces one `messages` row + four `message_labels` rows. The
  same Message-Id on a different account is a separate `messages` row
  (provenance preserved).
- **Messages — fallback when no Message-Id**: dedup by SHA-256 of the raw
  RFC822 bytes (`messages.raw_sha256`, partial unique index when
  `message_id IS NULL`).
- **Attachments — content-addressable, global**: identical bytes appear on
  disk and in `attachment_blobs` exactly once across the whole archive
  regardless of account/message. `messages.attachments` JSONB stores
  `[{"filename": "<original-name-from-this-email>", "sha256": "<hex>",
    "content_id": "<cid-without-brackets>"}, …]` — `content_id` is only
  present on inline parts (HTML bodies reference them via `cid:`), omitted
  otherwise. The original filename is preserved per-message so files can be
  restored with the names they had when received; the bytes, mime type, size,
  and on-disk path live on the `attachment_blobs` row.

On-disk path: `<attachments.root>/blobs/<aa>/<bb>/<full-sha256-hex>` (two-level
hex fan-out). The path is opaque — never derive filenames from it; always go
through the JSONB.

**Nullability**: only `raw_bytes`, `size_bytes`, `headers`, and `attachments`
are `NOT NULL` on `messages`. `subject`, `body_text`, `body_html`, `from_addr`,
`to_addrs`, etc. are all nullable — real mail occasionally lacks any of them.
The parser normalizes empty strings to NULL so `WHERE body_text IS NULL` is
the canonical "no body" query.

**`body_lang` / `body_lang_attempted_at`**: `body_lang` is the detected ISO
639-1 code, NULL when unknown — that is the only thing it means, and the `lang:`
filter depends on it. "The detector has already run on this body and declined"
is recorded separately in `body_lang_attempted_at` (migration `0035`), which is
what keeps a declined row out of the claim. See the #251 notes under Search
subsystem before touching either column.

**Date columns** (`date_sent`, `date_received`, `internal_date`):
- `date_sent` — email header `Date:`. Sender-supplied, may be wrong/future,
  usually accurate. Nullable.
- `internal_date` — IMAP server INTERNALDATE (RFC 3501), "when this email
  arrived at the mailbox". Populated by `sync.py:upsert_message` on insert;
  legacy rows (pre-migration-0018) are NULL until backfilled via
  `localmail backfill-internal-date`. Nullable.
- `date_received` — local sync timestamp, `NOT NULL`. Not a meaningful
  "received" date; reflects "when localmail wrote this row". Used by
  `/v1/changes` as a safe-horizon filter (`< now() - changes_safe_horizon_s`)
  and for audit.

The canonical "show me recent mail" ordering is
`ORDER BY COALESCE(internal_date, date_sent) DESC NULLS LAST, id DESC`,
backed by the expression index `messages_recent_idx`. Used by the
`/v1/changes` initial-fetch branch, the new keyset browse path
(`api.list_messages` → `/v1/messages`), and `Searcher._date_keyset_search`
— the one date-ordered keyset walk, which serves `sort=date`, any
blank-query search, and (see **Browse & search pagination** below) both
the descending direction above and its exact reverse.

**Planner choice under the per-user ACL filter (#72, resolved)**:
`messages_recent_idx` does *not* include `account_id`, but the
planner uses it anyway as a date-ordered walk and applies the
`account_id = ANY(...)` predicate as a per-tuple filter. The
acceptance harness in [tests/acceptance/run_browse_explain.py](tests/acceptance/run_browse_explain.py)
proves this across 200,000-row synthetic archives in balanced /
skewed / tail multi-account distributions: every probe picks
`Index Scan using messages_recent_idx`, never a bitmap heap scan
or full sort. No covering index keyed on `account_id` is needed
(or warranted — it would duplicate the existing
`messages_acct_date_idx` without solving anything the LIMIT
short-circuit doesn't already solve). The index-eligibility
regression is pinned by `tests/test_api_browse_plan.py` —
specifically that the COALESCE expression, the `DESC NULLS LAST`,
and the secondary `id DESC` are all load-bearing for the plan,
and that the index alone can serve the query when competing
indexes are temporarily hidden.

**Mid-keyset perf (#75, resolved)**: deep-keyset pagination
(`cursor.ts` not None) used to walk ~`total_rows / 2` tuples per
51-row page because the cursor predicate (`expr < X OR (expr = X
AND id < Y) OR COALESCE IS NULL`) was treated as a post-walk
`Filter:` rather than an `Index Cond:`. Two interacting causes:
the `OR COALESCE IS NULL` disjunct admitted NULL-tail rows but
prevented any range bound; even after removing it, the OR-form
keyset (`expr < X OR (expr = X AND id < Y)`) still degraded to
a Filter at production scale (Postgres refuses to decompose a
mixed-column OR into an index range bound when an Index Scan
alternative is on the table).

The shipped fix uses SQL **row comparison** —
`ROW(COALESCE(internal_date, date_sent), m.id) < ROW(%s, %s)` —
which Postgres composes as a single `Index Cond` on
`messages_recent_idx`. The scan starts AT the cursor and only
emits matching rows. NULL-tail rows are reached via a separate
"top-up" query in `list_messages` when the dated portion runs
short of `limit + 1`; the response cursor transitions to the
NULL-tail flavour (`ts=None`) naturally via `page_rows[-1]`.

200k-row, ACL=1 heavy, skewed distribution, mid-keyset 51-row
LIMIT: **100,014 → 13 rows removed by filter; 28.3ms → 0.072ms
execution; ~500k → 424 buffer hits**. The residual filter rows
are bounded by the per-tuple ACL cost (~`page_size /
acl_fraction`), not by table size. Tracked by
`tests/test_api_browse_plan.py::test_dated_cursor_predicate_composes_index_range_bound`
(unit-scale eligibility) and
`tests/acceptance/run_browse_explain.py` (operational
`--predicate-form {current,pre75}` before/after). Do NOT
rewrite the predicate as the OR-form even though it's
semantically equivalent — the planner does not optimize it.

**Canonical browse SQL emitter (#77, simplified by #85)**:
`BROWSE_ROW_SQL_TEMPLATE`, `compose_browse_sql(where=…)`, and
`build_where(account_ids=…, folder_ids=…, cursor=…,
null_tail_only=…)` in
[src/localmail/api/browse.py](src/localmail/api/browse.py) are
the only authoritative SQL emitter for the browse path. Both
the unit-scale eligibility tests
(`tests/test_api_browse_plan.py`) and the EXPLAIN harness
(`tests/acceptance/run_browse_explain.py`) compose the
production SQL via these primitives — there is no duplicate
inline SQL to drift against. Any refactor of the SELECT /
FROM / ORDER BY shape or of the WHERE-clause emitter
automatically lands in the tests + harness. The `pre75`
predicate variant in the harness is the one deliberate
exception: it reuses `BROWSE_ROW_SQL_TEMPLATE` for the
SELECT / FROM / ORDER BY shape but substitutes a local
buggy `_PRE75_BUGGY_WHERE` so the operator can reproduce
the pre-fix planner choice.

**Folder-filter shape (#78, simplified by #85 — EXISTS semi-join)**:
the `folder_ids` branch of `list_messages` adds
`AND EXISTS (SELECT 1 FROM message_labels ml WHERE
ml.message_id = m.id AND ml.mailbox_id = ANY(%s))` inside
the WHERE clause; there is **no** `JOIN message_labels` in
the FROM clause and **no** `SELECT DISTINCT`. EXISTS short-
circuits the labels scan on the first matching row per outer
message, so there is no row multiplication and no DISTINCT
is required. Pre-#85 the production SQL used `SELECT
DISTINCT … JOIN message_labels …`, which forced a post-join
Sort+Unique pass over every projected column on top of the
Nested Loop; the EXISTS rewrite turns that 3-node chain
(`Nested Loop + Incremental Sort + Unique`) into a single
`Nested Loop Semi Join`. The #85 benchmark at 200k rows ×
broad folder showed ~45-50% fewer buffer hits per page
across every folder-filter probe; the operationally
significant signal is the buffer-hit reduction, not the
sub-ms execution time delta (synthetic data fits in cache).
The planner's choice for the *messages* side of the
semi-join is still selectivity-dependent — at narrow
selectivities (~5% labelled) it can correctly start from
`message_labels`; at broad selectivities (~50%) it prefers
the date-ordered `messages_recent_idx` walk. At production
scale every folder-filter probe picks `Index Scan using
messages_recent_idx`. The acceptance harness exercises this
via `run_browse_explain.py --folder-filter`, which seeds two
mailboxes per account (`selective` ~5%, `broad` ~50%) and
appends four folder-filter probes: ACL=1+selective,
ACL=1+broad, ACL=1+broad mid-keyset, ACL=all+broad-across-
accounts. The SQL-shape eligibility regression is pinned by
`tests/test_api_browse_plan.py` —
`test_messages_recent_idx_is_eligible_for_{narrow,broad,multi}_folder_filter`
prove that with every competing `messages` index hidden,
`Index Scan using messages_recent_idx` still serves the
messages side under the semi-join. Those tests do NOT
forbid Sort nodes — at fixture scale the planner correctly
inverts the semi-join (starts from `message_labels`, looks
up messages by PK via `messages_recent_idx`, then Sorts to
restore the ORDER BY); the DISTINCT-regression signature
(`Unique` node + Sort over every projected column) only
surfaces at scales where the date-ordered walk is preferred,
which the acceptance harness covers.

**The wire `date` field MUST match this sort key.** Every paginated
list endpoint (`/v1/messages`, `/v1/search`, `/v1/changes`) emits
`date = COALESCE(internal_date, date_sent)` — never raw `date_sent`.
Emitting `date_sent` while the SQL sorts by COALESCE makes the
displayed order look broken on any row whose two dates disagree
(forwarded mail, mailing lists, sender clock skew, mid-rollout
backfill). Tests in `test_serve_browse_route.py`,
`test_serve_search_route.py`, `test_serve_changes_route.py`
enforce this invariant — keep them green when touching wire
serialisation.

Folder filtering supports `folder_allow`, `folder_deny`, and **`folder_deny_flags`**
(by RFC 6154 IMAP special-use flag, e.g. `\Trash`, `\Junk`, `\All`). Prefer
flag-based denial — it survives provider locales (`[Gmail]/Bin` vs `Trash`).

## Sync model

- One-shot via `localmail sync`: useful for cron and smoke testing.
  `--limit-per-folder K` caps how many UIDs are processed per mailbox per run;
  the next run resumes from `mailboxes.uidnext`.
- Daemon via `localmail run`: per account, **two threads** — one IDLE on INBOX,
  one periodic poll on every other folder. **All daemon threads share a single
  `psycopg_pool.ConnectionPool`** (`Daemon.pool`): IDLE + poll per account,
  plus the optional `embed_worker` and `extract_worker` threads. They
  coordinate via a `threading.Event` stop signal and reconnect with
  exponential backoff (1s → 60s cap) on failure. SIGTERM/SIGINT cleanly stop
  IDLE and join threads.

  Pool sizing: by default `compute_daemon_pool_size(...)` in `db.py` derives
  the cap from `(2 * n_accounts) + workers + headroom`, floored at
  `POOL_BASELINE_MIN`. Set `daemon.pool_max_size` in `config.toml` to
  override for tight Postgres `max_connections` budgets or higher concurrency.
  The chosen value is logged at startup ("daemon pool sizing: max_size=…").

  **Startup backoff (#133)**: `Daemon.__init__` does DB IO during
  construction — `_load_syncable_accounts` (a one-shot `psycopg.connect`,
  before the pool opens, since pool sizing needs the account count) then
  `open_pool`. The **synchronous** `_load_syncable_accounts` touch goes
  through `retry.retry_with_backoff` so a briefly unreachable Postgres at
  launch (DB still coming up under systemd, transient blip) makes the daemon
  *wait* — bounded exponential backoff between
  `daemon.startup_backoff_initial_s` (default 1.0) and
  `daemon.startup_backoff_max_s` (default 60.0) — rather than crashing on
  construction. `open_pool` is **not** wrapped: it opens with `wait=False`
  (returns immediately, fills lazily on background threads) and so never
  raises synchronously on an unreachable DB — wrapping it would catch only
  config errors, which aren't transient. By the time `_load_syncable_accounts`
  returns, Postgres has answered; a blip in the window before a worker first
  acquires a connection is absorbed by the IDLE/poll loops' own 1s→60s
  backoff. The shared `retry.next_backoff` (pure: `min(current*factor, max)`)
  plus `retry_with_backoff` (respects the stop event; first attempt is
  immediate; a stop signal during a wait raises `RetryAborted`) live in
  [src/localmail/retry.py](src/localmail/retry.py). Signal handlers install in
  `run_forever` *after* construction, so during a startup-backoff wait
  SIGTERM/SIGINT fall to the default handler (process exits) — the
  `RetryAborted` escape is for an injected `stop_event` (tests, future daemon
  control), not the systemd path, where the supervisor owns kill semantics.

`sync_mailbox` checkpoints `mailboxes.uidnext` after each 50-message batch, so
a crash mid-run loses at most one batch of progress. Re-running is safe — the
existing-id check + `ON CONFLICT DO NOTHING` make inserts idempotent.

### Failure handling (poison-pill messages)

Per-message work runs inside a Postgres `SAVEPOINT msg` so a single bad row
(unexpected encoding, NUL byte the parser missed, etc.) only loses itself,
not the surrounding 49 messages in the batch. On exception:

1. `ROLLBACK TO SAVEPOINT msg` — discards just this message's writes.
2. `record_failed_message` inserts the full RFC822 bytes + error class +
   message + traceback into `failed_messages` (its own nested SAVEPOINT so a
   logging failure can't kill the outer transaction). Re-failures upsert and
   bump `retry_count`.
3. `mailboxes.uidnext` still advances past the failed UID — we don't get
   stuck retrying the same poison pill on every run.

Recovery flow: fix the parser bug, run `localmail retry-failed`. The retry
path calls the same `process_one_message` as live sync, so any fix that works
for new messages also works for the backlog.

The parser itself does two pre-emptive sanitizations to keep poison pills
rare: NUL bytes are stripped from every text field (Postgres `TEXT` rejects
them), and attachment-only messages get synthesized
`subject = "{attachments only}"` / `body_text = "{attachments: name1, name2}"`
so they remain searchable and visible (original bytes/filenames are intact
in `messages.attachments` + the blobs tree).

### Concurrent writers: the IDLE thread vs the poll thread (#231)

The two per-account threads are **not** partitioned by message. The IDLE thread
owns INBOX and the poll thread owns every other folder, but Gmail delivers one
Message-Id to INBOX *and* several labels at once, so both threads routinely
process the same message concurrently, on separate pool connections. Three
invariants follow; all are pinned by
[tests/test_sync_concurrent_writers.py](tests/test_sync_concurrent_writers.py).

- **`upsert_message` is check-then-INSERT, so the check can lose.** The INSERT
  carries `ON CONFLICT DO NOTHING`; on no `RETURNING` row it re-reads the
  winner's id and reports `inserted=False`. Without this the loser raised
  `UniqueViolation`, which `process_one_message` recorded in `failed_messages`
  **as if the message were malformed** — polluting `list-failed` with healthy
  mail.

  **The re-read depends on READ COMMITTED** (psycopg's default). The conflicting
  INSERT blocks on the speculative-insert lock until the winner commits or
  aborts, and the SELECT then takes a fresh per-statement snapshot that includes
  it. Under REPEATABLE READ the INSERT raises `SerializationFailure` instead —
  do **not** raise the isolation level on the sync path without revisiting
  `upsert_message`.

  `DO NOTHING` carries **no conflict target**, because the dedup key spans two
  partial unique indexes (`messages_acct_msgid_uniq`,
  `messages_acct_rawsha_uniq`) and one target can't cover both. It therefore
  also swallows a violation of some *other* unique constraint (a desynced
  `messages_id_seq` after a restore without `setval`), so the no-match branch
  raises a named `RuntimeError` rather than asserting — a bare `AssertionError`
  would be a *worse* `failed_messages` entry than the `UniqueViolation` it
  replaced, and vanishes under `python -O`.

- **Attachment blob temps are per-writer.** Each writer uses
  `<sha>.<pid>.<uuid>.tmp`, never a shared `<sha>.tmp`. With a shared name one
  writer could truncate (open-for-write) the temp another was about to
  `replace()` into place, installing a **short or zero-length blob at the
  canonical content-addressed path**. Both writers hold identical bytes, so
  whichever `replace` wins is correct. Do not "simplify" this back to a shared
  name.

  The cost is that a hard kill (SIGKILL/OOM/power loss) between write and
  replace strands a temp — the old shared name was accidentally self-limiting,
  since the next writer of that blob reopened the identical path. **#237 adds
  the collector**: [src/localmail/blob_temps.py](src/localmail/blob_temps.py)
  owns *both* the minting (`new_temp_path`, called by `write_attachments`) and
  the matching (`is_writer_temp`), so a rename of the format can never silently
  strand every future orphan. `sweep_blob_temps` runs at `Daemon.start_workers`
  (best-effort — a leaked temp costs disk, a raise costs the daemon) and on
  demand via `localmail sweep-blob-temps [--dry-run]`.

  **The gate is age, not pid liveness**, even though the pid is right there in
  the name: that pid's process may be long gone *and* its number recycled, and
  unlike `import_jobs.owner_pid` (#162) there is no row recording the owning
  host to disambiguate. `[attachments] temp_max_age_s` (default 86400) is what
  keeps a live writer's temp safe, so keep it generous — a real write finishes
  in milliseconds and there is no upside to tightening it. Because it is the
  *only* protection it is **floored at 1s** (`Field(ge=1)`, and
  `--max-age-seconds` is a `click.IntRange(min=1)`): `is_expired` is
  `now - mtime > max_age_s`, so at 0 the sweep deletes a temp whose `replace()`
  has not run yet, failing that write and poison-pilling a healthy message. The
  name match is
  deliberately strict (`<64 hex>.<digits>.<32 hex>.tmp`), never a `*.tmp` glob:
  the sweep deletes without asking, and the blob tree is a directory operators
  poke at. A temp that vanishes *before* `stat` is **not** counted as removed —
  the age gate never judged it, so claiming it (especially under `--dry-run`,
  whose whole job is to say what would happen) reports an intent the sweep never
  formed. A temp that vanishes at `unlink` **is** counted, since by then the
  sweep had decided; its bytes are not, because it did not free them.

- **Every blocking IMAP call is bounded** by `[daemon] imap_timeout_s`
  (default 60s), threaded through `WorkerContext.imap_timeout_s`. Unbounded,
  imapclient blocks forever on a network black-hole and the worker holds its
  pool connection, never observes the stop event, and gets respawned as a
  duplicate on the next reconcile — the IMAP analogue of the Postgres bounds in
  `daemon.db_*_timeout*` (#140/#142). It is **operator-tunable rather than a
  constant** because it is a per-recv bound: a slow-but-progressing FETCH is
  safe, but a server-side stall with nothing on the wire (a Gmail SEARCH over a
  very large `\All` folder) is indistinguishable from a black-hole, and the
  resulting `socket.timeout` makes the IDLE/poll loops livelock in
  reconnect-with-backoff. IDLE waits are unaffected — imapclient's
  `idle_check()` drops the socket to non-blocking and polls on its own timeout,
  restoring this one in a `finally`. The admin "Test connection" probe uses its
  own much shorter `accounts.PROBE_TIMEOUT_SECONDS` because it runs
  synchronously inside a request and holds a threadpool slot.

### UID numbering: `src/localmail/uids.py` (#215, #222A)

`message_labels` carries `UNIQUE (mailbox_id, uid)`. For IMAP mail the UID is
the server's truth; for **archive imports it is invented**. All UID arithmetic
lives in the pure [src/localmail/uids.py](src/localmail/uids.py)
(`next_uid_after`, `should_reallocate_uid`, `checkpoint_uidnext`, plus the one
thin read `max_label_uid`), shared by `sync.py` and `importer/runner.py`.

- **`message_labels.uid` has exactly one reader: `sync.backfill_internal_date`,**
  which uses it as an IMAP FETCH key. Every *read* surface — search
  (`search/arms.py`), browse (`api/browse.py`), account listing
  (`api/accounts.py`), message fetch (`api/messages.py`) — keys on `mailbox_id`
  alone and never sees the uid. Re-allocation is safe only because the two can
  never meet: it is gated on `auth_method == 'archive'`, archive accounts carry
  no `imap_host`, and `backfill-internal-date` requires a live connection.
  **Widening `should_reallocate_uid` (or the importer) to a live account would
  make `backfill_internal_date` FETCH a synthetic UID against the real server
  and write another message's INTERNALDATE onto this row.** Re-check both
  claims before touching that gate.
- **Imports continue from `MAX(uid) + 1` per mailbox, resolved at first touch of
  that mailbox in the run** — never a counter restarting at 0. Mailboxes resolve
  on `(account_id, name)` and the importer names them from the source's filename
  stem, so `2023/Inbox.mbox` and `2024/Inbox.mbox` land in the *same* mailbox;
  a per-run counter recycled committed UIDs and every collision poison-pilled a
  perfectly good message into `failed_messages` (`upsert_label`'s
  `ON CONFLICT (message_id, mailbox_id)` arbitrates the PK, not the uid index).
  Re-import stays idempotent at the message level; only the label's uid churns
  upward, which nothing reads.
- **`retry_failed_messages` re-allocates the UID for archive accounts only**, and
  re-records a still-failing row under its *stored* uid so the
  `UNIQUE (account_id, mailbox_id, uid)` row upserts instead of multiplying.
  This is the recovery path for rows a pre-fix import already poisoned —
  replaying their stored uid collides forever. A live account's UID is replayed
  verbatim: a collision there is a genuine invariant violation worth surfacing,
  which is also why `upsert_label` was **not** made collision-tolerant.
- **An empty `BODY[]` is probed, not assumed.** `_uid_still_on_server` runs one
  `SEARCH UID n:n`. Gone → expunged between our SEARCH and this FETCH; advance
  (holding the watermark would pin the mailbox forever for a message that no
  longer exists). Still present, **or the probe itself raises** → transient; set
  `hold_at` and clamp the checkpoint through `checkpoint_uidnext`. The clamp is
  load-bearing: `highest_seen` is a running max, so a later UID in the same run
  would otherwise carry the watermark past the stuck one. `failed_messages` is
  **not** an option here — `raw_bytes` is `NOT NULL` and retry re-parses it, so
  an empty-body record would fail forever or insert a bogus empty message.

  **The probe is skipped once `hold_at` is set.** UIDs ascend, so from the first
  hold onward `min(highest_seen + 1, hold_at) == hold_at` and the answer cannot
  change the outcome. That matters because whatever empties one `BODY[]` tends
  to empty the whole tail, and each probe is a round trip bounded only by
  `imap_timeout_s` against an already-sick server while the worker pins its pool
  connection.

  **The hold is bounded** by `[daemon] max_body_fetch_hold_s` (default 1800),
  tracked per `(mailbox_id, uid)` in `transient_fetches` (migration `0033`) via
  [src/localmail/fetch_retry.py](src/localmail/fetch_retry.py). *Still present*
  is not *will ever be fetchable*: a **zero-length message** reads as no-body
  (`raw = b""`) yet the probe finds it, and a corrupt store entry can omit the
  body indefinitely — either would pin the mailbox permanently. Unbounded that is
  worse than the bug it fixes, because the tail is re-fetched on every run and
  `idle.py::_sync_inbox` runs on **every IDLE notification**, i.e. per new mail.
  Past the window sync logs a distinct *"giving up"* WARNING and advances; a
  successful fetch calls `clear_attempts`, so it measures one **continuous**
  outage. The per-run `load_attempts` preload means the common no-history path
  costs no per-message DELETE, and `record_attempt` uses a nested SAVEPOINT with
  the SAVEPOINT **outside** the try (like `record_failed_message`, so `ROLLBACK
  TO` is always valid), reporting a fresh hold on failure so bookkeeping trouble
  makes sync hold rather than give up on a history it could not read.

  **A duration, not an attempt count — do not "align" it with #153.**
  `transient_extractions`' consecutive-failure cap is the obvious analogue, but
  that counter is driven by a timer-paced sweep, so there a count *is* a
  duration. Here the pace is event-driven, so a count would be spent at the
  mailbox's traffic rate: five IDLE notifications in ten seconds (another client
  toggling flags is enough) would exhaust a 5-attempt budget and drop a message
  over a blip that resolved a minute later, while the poll plane got 25 minutes
  from the same number. Nor would a count bound the re-fetch traffic — that comes
  from holding the watermark, which happens per pass regardless of counting.

  **Two lifecycle rules keep the table honest.** A UIDVALIDITY reset calls
  `clear_mailbox` alongside `clear_mailbox_labels`: the UID space is renumbered,
  and a surviving near-expiry row would make sync give up on an unrelated new
  message at its first sighting. Each checkpoint calls `reclaim_below(resume_at)`:
  rows under the resume point are dead by construction (sync never revisits those
  UIDs), and without it every expunged-but-recorded-as-held UID leaks a row
  forever — which the probe skip below makes routine.

  **Giving up leaves the row in place, now as a tombstone (#239).** Two separate
  reasons, both load-bearing. (1) When a *lower* held UID keeps the watermark
  below an expired one, the expired UID stays reachable and is re-seen every
  pass; clearing its history at give-up time would re-mint it a fresh window on
  the next pass, silently undoing the give-up for as long as any lower hold
  lasts. (2) The row is the **only queryable record that the message is
  permanently absent** — every sibling failure path here keeps one
  (`failed_messages`/`retry-failed`,
  `failed_extractions`/`retry-failed-extractions`) and this one used to keep
  nothing but a WARNING. `sync_mailbox` calls `fetch_retry.mark_gave_up`, which
  stamps `transient_fetches.gave_up_at` (migration `0034`) via
  `COALESCE(gave_up_at, now())` — **never restamping**, so `gave_up_at` keeps
  meaning "since when has this message been missing" across re-sightings, and
  expiry stays sticky because `first_seen_at` is untouched.
  **`reclaim_below` therefore skips `gave_up_at IS NOT NULL`** — the watermark
  passing the UID is exactly when the record must survive. It still collects
  live holds orphaned above the old watermark (a held UID later expunged drops
  out of SEARCH and is never re-seen).

  **`mark_gave_up` runs in a nested SAVEPOINT**, like `record_attempt` beside it
  and `record_failed_message` before both. It is bookkeeping *about* a failure
  on the same branch of the same batch transaction, so unguarded a failure in it
  poisons the transaction, the checkpoint right after it fails too, and the
  whole pass dies into the worker's reconnect backoff — 49 healthy messages lost
  to the row that was only reporting on the 50th. The realistic trigger is a
  deploy landing the code before migration `0034` (`UndefinedColumn`).

  **Ops surface:** `localmail list-failed-fetches [--account N] [--limit K]` and
  `localmail retry-failed-fetches [--account N] [--forget] [--older-than-days D]
  [--dry-run]`. Retry rewinds each affected mailbox's `uidnext` to its **lowest**
  tombstoned UID via the pure `fetch_retry.plan_uidnext_rewind` (which skips a
  mailbox whose watermark is already at or below that UID — rewinding *forward*
  would skip mail); the re-scan of everything above is idempotent through
  `upsert_message`'s existing-id check + `ON CONFLICT DO NOTHING`, just not free.
  A tombstone otherwise clears on a successful re-fetch (`clear_attempts`, via
  the `held_attempts` preload) or a UIDVALIDITY reset (`clear_mailbox` — the UID
  space is renumbered, so the recorded uid is no longer actionable).

  **Retry *arms* the tombstone (`fetch_retry.arm_for_retry`); it does not purge
  it.** Both halves of that are load-bearing. It must **reopen the hold window**
  (`attempt_count = 0, first_seen_at = now()`) because `load_attempts` does not
  filter tombstones — a rewound row still carrying its original `first_seen_at`
  expires on sight, so sync gives up again without ever re-fetching and the
  command is a no-op that costs a full re-scan. And it must **keep the row**
  because `mailboxes.uidnext` is not ours alone: `update_mailbox_progress` writes
  it unconditionally from a resume point the daemon computed at the *top* of its
  pass, so a daemon already mid-pass on that mailbox overwrites the rewind at its
  next checkpoint. Purging there would make that silent — no re-fetch, no record,
  and a command that reported success; armed, the same race costs one re-run,
  which the command's own output says. `gave_up_at` is untouched by the arming
  (the message is still absent, and has been since the original give-up), so a
  re-driven row stays in `list-failed-fetches` until it actually arrives — which
  is why the CLI labels the reopened `first_seen_at` **`held_since`**, not
  `first_seen`. **Do not "simplify" this back to a purge.**

  **Retention is manual, deliberately** (`--forget`, optionally scoped by
  `--older-than-days`). A tombstone is written once per distinct unfetchable UID
  and upserted thereafter, so growth is bounded by the number of genuinely
  broken messages — not a runaway. An automatic sweep would trade that
  negligible growth for silently deleting the sole record of permanently lost
  mail, which is the failure #239 exists to end; `failed_messages` and
  `failed_extractions` make the same call. Do not add a background expiry.

  **The probe skip is keyed on `server_emptying_bodies`, not `hold_at`.**
  `hold_at` is only assigned on the still-holding branch, so on the run where
  every held UID has finally expired it stays `None` — and using it as the guard
  would re-probe the entire tail one UID at a time, on precisely the worst run.

### Degenerate `Message-Id` (#222B)

`parser.normalize_message_id` collapses an identity-free `Message-Id` to `None`
so the `raw_sha256` dedup fallback engages. The form that matters is the **empty
angle-addr** (`<>`, `< >`) — `email.policy.default` already reduces a
whitespace-only header body to `""`, which the old `if message_id` guard caught,
but `<>` survived as a truthy, non-unique string and collapsed distinct messages
onto one row (discarding the second's body and attachments). The fix is
prospective; an already-collapsed pair cannot be recovered.

**The header *read* is guarded too, and that half is interpreter-dependent
(#314).** `normalize_message_id` never ran on the form it was written for:
`email.policy.default` parses structured headers on **read**, and that parser is
**not total** — CPython 3.12.3 raises `IndexError` out of `get_obs_local_part`
for `<>`, and other malformed forms raise `AttributeError`. The exception
escaped `parse_message`, which runs under a per-message SAVEPOINT, so one broken
header sent otherwise-healthy mail to `failed_messages` **permanently**: the same
bytes re-parse the same way on every `retry-failed`. CPython **3.13 guards the
empty case**, which is why one archive can behave differently on two hosts.

- The rule is `parser._header_text`, which reports a header the stdlib cannot
  parse as **absent** — for `Message-Id` that is the same answer `<>` gets on an
  interpreter that *can* parse it, so the `raw_sha256` fallback engages either
  way. **The catch is broad because the failure is**: `IndexError`,
  `AttributeError` and `HeaderParseError` all arrive from different malformed
  forms, and a narrower tuple lets the next one through as a poison pill.
- **It is two sites, not one.** `_headers_dict` reads every header through
  `msg.items()`, which parses each value, so guarding the `Message-Id` read alone
  left the message poisoned one line later. It iterates `raw_items()` — the same
  sequence, unparsed — and parses per occurrence, so a failing one falls back to
  its raw text and degrades **only that header**, never the whole `headers`
  column.
- **`In-Reply-To` / `References` need no guard**: the plural `parse_message_ids`
  already catches `HeaderParseError` per id. Verified against 3.12.3 — do not add
  one "for symmetry".
- **The test raises at the policy seam, not from a fixture.** On 3.13 no real
  input provokes the failure, so a fixture-driven test would silently stop
  exercising the guard on exactly the stdlib that still needs it. Same shape as
  `version_report`'s renderer-fallback pin, and for the same reason.
- **The guard belongs here, not in a pinned interpreter.** A pin is undone by a
  single `uv sync` (see the uv footguns under Commands) while the archive keeps
  ingesting.

## Search subsystem (Phases 1 + 2 shipped)

Hybrid lexical (tsvector) + vector (pgvector) search over messages and
attachment text. See
[docs/superpowers/specs/2026-05-16-hybrid-search-design.md](docs/superpowers/specs/2026-05-16-hybrid-search-design.md)
for the full design,
[docs/superpowers/plans/2026-05-16-hybrid-search-phase1.md](docs/superpowers/plans/2026-05-16-hybrid-search-phase1.md)
for the Phase 1 plan, and
[docs/superpowers/specs/2026-05-16-hybrid-search-phase2-design.md](docs/superpowers/specs/2026-05-16-hybrid-search-phase2-design.md) /
[docs/superpowers/plans/2026-05-16-hybrid-search-phase2.md](docs/superpowers/plans/2026-05-16-hybrid-search-phase2.md)
for the Phase 2 plan.

- Code lives under `src/localmail/search/` — `chunking.py`, `embeddings.py`,
  `reranker.py`, `query.py`, `rewriter.py`, `searcher.py`, `arms.py`,
  `page_cache.py`, `embed_worker.py`, `sweep_pacing.py`, `failure_pacing.py`,
  `extractor.py`, `extract_worker.py`.
  Public API: `localmail.search.create_searcher`.
- All numeric tunables in `LocalmailConfig.search` (`SearchConfig`).
  **No magic numbers elsewhere in search code.**
- Lexical retrieval via PostgreSQL built-in `tsvector` + `ts_rank_cd` with
  `setweight()` — no third-party extension required. Arms 1 and 2 (whole-message
  and chunk-level FTS) use `plainto_tsquery('simple', ...)` for language-neutral
  tokenisation. The docstrings in `arms.py` still use "BM25" as shorthand;
  the actual implementation is `tsvector`/`ts_rank_cd` throughout.
- Vector retrieval via pgvector HNSW + `halfvec(768)`. Default embedder:
  EmbeddingGemma-300M via fastembed (Gemma Terms — runtime download).
- One embed_worker thread per process (account-agnostic; backend-bound).
  Lazily chunks messages it sees without chunks. Failure model mirrors
  `sync.py`:
    - **Per-message SAVEPOINT** around chunking — poison messages land in
      `failed_chunkings` (keyed on `message_id`) and are skipped on
      subsequent sweeps once `retry_count >= embed_worker_max_chunk_retries`.
    - **Per-chunk SAVEPOINT** around the embedding UPDATE — poison chunks
      land in `failed_embeddings` and are skipped likewise.
    - **Both failure-recording paths use a nested SAVEPOINT** so a logging
      failure can't abort the outer transaction.
    - **Batch-level backend errors do NOT mark chunks as failed.** Transient
      errors (network blips, model load failures) just roll back and back
      off; chunks get re-claimed next sweep. Permanently-broken backends
      surface via repeated WARNINGs rather than silently poisoning the
      entire queue.
- Phase 2 (attachment search) — **shipped**, see
  [docs/superpowers/specs/2026-05-16-hybrid-search-phase2-design.md] and
  [docs/superpowers/plans/2026-05-16-hybrid-search-phase2.md].
  Phase 5 (polish) — separate design + plans.

**The sweep result names both queues, and the loop's backoff reads both
(#259).** `run_embed_worker_once` returned a bare count of embedded chunks,
which `run_embed_worker` read as "did this sweep do work". But the sweep also
runs one `body_lang_detect_batch_size` slice of language detection, so a sweep
that laboured through 200 rows reported `0`, the loop concluded the queue was
empty, and it slept the full backoff — **~340 rows/min**, against the far
higher rate `localmail lang-backfill` achieves on the same queue. Harmless in
steady state (new mail arrives far below one lang batch per sweep, so the
backoff is correct), but on the 100k-row backlog #251 unwedged it was the
difference between ~5 hours and ~25 minutes.

- The rule is the pure
  [src/localmail/search/sweep_pacing.py](src/localmail/search/sweep_pacing.py),
  which owns **both** halves — `SweepOutcome.made_progress` (what counts as
  work) and `next_idle_streak` / `sweep_sleep_seconds` (how long to sleep on
  it). Co-located for the same reason as `blob_temps.py`'s minting-beside-
  matching and `shutdown_budget.py`'s two budgets: writing them apart is what
  produced the defect.
- **`SweepOutcome.__bool__` raises `TypeError`.** `LangDetectPass` merely
  declines to define one, which leaves `if not result:` silently always-False;
  raising makes the implicit read that caused #251 *and* #259 impossible rather
  than just discouraged. `lang_visited` counts rows **visited**, not labelled —
  a declined batch still leaves the queue for good.
- The backoff ceiling is `search.embed_worker_idle_backoff_max_steps`
  (default 6 → 35 s at a 5 s interval), not the hardcoded `6` it used to be;
  `next_idle_streak` takes `max_steps` keyword-only **with no default** so the
  config stays the one authority. `0` disables the backoff.
- **`embed-backfill` still breaks its first loop on `sweep.embedded`**, not on
  `made_progress`: it has a second tight loop that drains the language queue
  and reports `visited`/`labelled` separately. The three acceptance harnesses
  *do* break on `made_progress` — they want a fully-populated corpus, and the
  loop cannot spin because every visited row is stamped attempted.
- **The sweep's third pass — lazy chunking — is deliberately not counted.** It
  feeds the embedding queue rather than draining one of its own, so under a
  working backend its output is already reported as `embedded` in the same
  sweep — counting it would double-report. And both chunking passes return rows
  *selected*, not rows drained, which is what made them untrustworthy as a
  progress signal while a zero-chunk row could be re-selected on every sweep
  (#266 — see the blank-text bullet below).
- **A broken embedding backend no longer paces the loop while a language
  backlog drains.** `_embed_table` catches batch-level backend errors itself
  and returns 0 rather than raising, so `run_embed_worker`'s `except` never
  sees them; with `lang_visited > 0` the streak resets and the backend is
  retried once per base poll interval instead of once per ceiling. Deliberate
  (the sweep really is doing work), but it is why the loop's docstring no
  longer claims the backoff throttles a broken backend.

**The log volume that retry pace implies is bounded separately (#267).** A
traceback for each of the two chunk tables every ~5 s — ~24/min, for as long as
the backlog lasts — is what that pace costs unthrottled. The rule is the pure
[src/localmail/search/failure_pacing.py](src/localmail/search/failure_pacing.py):
**report a failure, with its traceback, when it is the first on record for that
table, when the exception type changes, or when
`search.embed_worker_failure_report_interval_s` (default 300, `0` disables) has
elapsed since the last report; otherwise stay silent and count.** The next
report names how many it swallowed, so nothing is lost, only deferred.

- **This is not the shape #153 and #239 settled on, and should not be aligned
  with it.** Those log one *terminal* "giving up" line, bounded by a cap and
  backed by a queryable row (`transient_extractions` / `transient_fetches`) with
  a `retry-…` command to clear it. Nothing here is terminal — the backend is
  retried forever and nothing is written to the database on that path, so the
  log is the whole record and it has to keep re-arming.
- **The record holds the exception type, not just a count.** A count alone
  cannot tell a continuing failure from a *different* one arriving mid-incident;
  the second would be suppressed and, worse, reported as a continuation of the
  first, leaving the one traceback on record naming the wrong problem.
- **Success does not clear the record**, which is why the rule is a duration
  rather than a consecutive-failure streak. A backend alternating 200/503 — the
  "network blip" the batch-level handler exists for — makes every failure the
  first of a fresh streak under reset-on-success, so every one carries a
  traceback and the throttle buys nothing. Recovery is expressed by the interval
  instead. An empty-claim sweep likewise touches nothing: only the failure
  branch writes, since the log records what has been *said*, not what the
  backend is doing.
- **Every report carries the traceback**, including the periodic ones. Logging
  it only once per process leaves a long incident undiagnosable from a rotated
  log or the supervisor's `deque(maxlen)` ring buffer, with no way back short of
  restarting the daemon — which also destroys the failing state.
- **The mapping is process state (`embed_worker._FAILURE_LOG`), and
  `run_embed_worker_once`'s `failure_log=` defaults to it.** Throttling log
  output is a process concern, and making the *default* correct is what removes
  the footgun rather than merely making it loud: the four looping callers (the
  daemon loop, `embed-backfill`, three acceptance harnesses) pass nothing and
  are throttled anyway. #234's keyword-only-no-default shape is for a parameter
  whose safe value cannot be the default; here it can. `reset_failure_log()` +
  an autouse conftest fixture keep one test's broken backend from silencing the
  next test's WARNING, the same shape as `secrets.reset_to_default()`.
- **The line names `type(exc).__name__`** and does not leave it to the
  traceback: `str(exc)` is empty for `ConnectionError()`, `MemoryError()` and
  much of what a backend raises, and the message line is what a log grep shows.
  (`version_report` cites this reasoning but renders more — see #296 there: the
  exceptions it *reproduces* come from the OS and the codec machinery, which
  populate `errno`/`filename`/`reason`, so the premise that `str(exc)` adds
  nothing does not hold for them. Not "always", though — that site's own
  docstring names `MemoryError` and `RecursionError` as reachable through the
  same branch, and `format_exception_only(MemoryError())` is the bare type name.
  The type name leading is what covers both cases.)

**Phase 4 (`--smart` query rewriter) — shipped**, see
[docs/superpowers/specs/2026-06-07-smart-query-rewriter-design.md](docs/superpowers/specs/2026-06-07-smart-query-rewriter-design.md)
and [docs/superpowers/plans/2026-06-07-smart-query-rewriter.md](docs/superpowers/plans/2026-06-07-smart-query-rewriter.md).
Opt-in (`search.rewriter_enabled_by_default` + the per-call `--smart`/`smart=`
flag). [search/rewriter.py](src/localmail/search/rewriter.py) is pure helpers
(`build_rewrite_prompt`, `parse_rewrite_response`, `apply_rewrite`); the IO
backends live in [search/rewriter_backends.py](src/localmail/search/rewriter_backends.py)
— a template-method base `_HttpJsonRewriter` (does prompt-build + parse;
subclasses implement only `_request`) plus three `httpx`-only backends selected
by `search.rewriter_backend` (`ollama` default | `openai` | `anthropic`) via the
`build_rewriter(cfg)` factory. `OllamaLLMRewriter` → Ollama `/api/generate`
(`format`-constrained JSON); `OpenAICompatRewriter` → any OpenAI-compatible
`/chat/completions` (`response_format` json_object); `AnthropicRewriter` →
Anthropic `/v1/messages` (assistant `"{"` prefill forces JSON, no tool-use). All
use `temperature=0`. The cloud backends read their API key at construction from
the env var named by `rewriter_openai_api_key_env` / `rewriter_anthropic_api_key_env`
(never config/DB); a missing key raises `MissingApiKey`, which `create_searcher`'s
guard turns into graceful "no `--smart`". `rewriter.py` keeps the old deep import
path working via a PEP 562 `__getattr__`.

**The base URL is validated at construction, beside the API key (#235).** A
malformed `ollama_host` / `rewriter_openai_base_url` / `rewriter_anthropic_base_url`
used to surface *per request* as `rewrite_note_code: unreachable`, "could not
reach the rewriter service" — a permanent `config.toml` typo reported in
transient wording, on every search forever, sending the operator to the network.
`InvalidRewriterUrl` is a sibling of `MissingApiKey` (same `RewriteParseError`
base, same guard, same degradation), so the wire says `not_configured` and
`smart_available` is correctly `False`. **No wire contract changed** — the
alternative, a new `invalid_config` note code, would have added a value to an
enum documented across CLAUDE.md, the MCP tool docstrings, and the HTTP schema.

- The rule is the pure
  [src/localmail/search/rewriter_url.py](src/localmail/search/rewriter_url.py)`::base_url_error`,
  shaped like `account_names.py::account_name_error` — a message, or `None`.
- **`httpx.URL` alone is not a validator.** It is permissive: only an
  unparseable port raises `InvalidURL`. The far more common mistake —
  omitting the scheme — parses happily as `scheme='localhost'`, and the request
  then fails as an `HTTPError`, i.e. the same misleading "unreachable". The
  check is therefore scheme-in-`{http,https}` **and** non-empty host, *plus*
  the httpx parse so nothing that passes here can still raise at request time.
- Each backend declares `base_url_setting`, the `SearchConfig` attribute name.
  Stringly-typed on purpose: the name is what lets the error tell the operator
  which key to edit, and a subclass that omits it makes
  `_HttpJsonRewriter.__init__` raise a named `TypeError` rather than silently
  skipping the check (a `raise`, not an `assert` — asserts vanish under
  `python -O`, the same reasoning as `upsert_message`'s named `RuntimeError`).
- **Keep the `httpx.InvalidURL` catch in `Searcher.search`** (added by #229).
  It is the backstop for a rewriter constructed some other way, and costs
  nothing.

No new uv extra (`httpx` is already a dep). The rewriter produces
`rewritten_text` (vector arm +
reranker), `expansion_terms` (OR-ed into the lexical arms — see below), and
`extracted_filters` (NL → structured). **`apply_rewrite` merge precedence:
explicit operators win** — the LLM fills only the scalar filter slots
(`after`/`before`/`from`/`to`/`subject`/`has_attachment`) the user left `None`;
it never sets account/folder/lang. **Failure policy lives in the Searcher, not
the rewriter**: the backends raise typed exceptions
(`httpx.HTTPError` subclasses, `RewriteParseError` — incl. a 200-with-missing-
`response`-key); `Searcher.search` catches `(httpx.HTTPError, RewriteParseError)`,
keeps the un-rewritten query, logs `smart rewrite skipped: …`, and surfaces it
on **`SearchPage.rewrite_skipped`** (the CLI prints a `note:`). Relative dates
are resolved LLM-side via an injected `today` (deterministic prompt; testable).
Expansion terms OR into the lexical arms through
`arms.build_lexical_tsquery(free_text, expansion_terms)` →
`plainto_tsquery('simple', %s) [ || … ]`; **with no expansion terms it returns
the bare single-tsquery form byte-for-byte**, so the non-smart path is
unchanged. The multi-term fragment is **parenthesised** because `@@` binds
tighter than `||` in Postgres. `rewriter_max_expansion_terms` (default 8) caps
the OR fan-out. No new migration, **no new uv extra** (`httpx` is already a dep;
Ollama is an external HTTP service). `continue_page`/`grow_pool` reuse the
cached enriched `parsed` and do not re-rewrite (`rewrite_skipped` is a page-1
signal).

**`--smart` over the wire (HTTP + MCP):** the rewriter is also exposed on the
network read surfaces — `POST /v1/search` accepts a `smart` body field and the
MCP `search` tool a `smart` param; both responses carry `rewrite_skipped`
(always present, default `false`). `api.search.run_search` gates it via the
public **`Searcher.smart_available`** property (`self._rewriter is not None`) —
never reaching into `searcher._rewriter` (#71). It computes `effective_smart =
smart and searcher.smart_available` so the Searcher's "no rewriter configured"
`RuntimeError` is never triggered: when `smart` is requested but unavailable,
the un-rewritten query runs and `rewrite_skipped` is `true` (**graceful
degradation** — unlike the CLI, which hard-errors, being interactive). `smart`
applies on the page-1 branch only (`cursor is None`); continuation/keyset pages
report `rewrite_skipped=false`. See
[docs/superpowers/specs/2026-06-08-smart-over-mcp-http-design.md](docs/superpowers/specs/2026-06-08-smart-over-mcp-http-design.md).

**Structured rewrite outcome (#176, #175):** every search response also carries
`rewrite_status` — a 5-value enum (`applied` / `unavailable` / `failed` /
`not_attempted` / `not_requested`) — and an optional curated `rewrite_note`
(actionable detail, e.g. `rewriter model '…' is not available; pull it with:
ollama pull …`). `rewrite_skipped` is **kept but now derived**
(`rewrite_skipped_for_status(status) == status in {unavailable, failed}`). The
pure module [search/rewrite_status.py](src/localmail/search/rewrite_status.py)
holds the constants, the `classify_rewrite_failure(exc, *, model)` classifier
(curated messages only — no raw exception text on the wire; model name is the
sole interpolated value), and `rewrite_skipped_for_status`. `Searcher.search`
classifies its own page-1 outcome onto `SearchPage.rewrite_status` /
`.rewrite_note` (the `rewrite_skipped` *field* is gone from `SearchPage`);
`api.search.run_search` owns the layer-specific statuses — `unavailable` (smart
requested, no rewriter), `not_attempted` (continuation page — **the #176 fix**
for the silent-no-op), and `not_requested` (smart off, or the empty-ACL
short-circuit). The empty-ACL short-circuit also reports `total_estimate: None`
(uniform with the normal path — **#175**; never `0`). See
[docs/superpowers/specs/2026-06-08-rewrite-outcome-status-design.md](docs/superpowers/specs/2026-06-08-rewrite-outcome-status-design.md).
Every response also carries a machine-readable **`rewrite_note_code`** (1:1 with
the curated note, `null` when the note is `null`): `missing_model` / `unreachable`
/ `unparseable` (the three `failed` causes), `not_configured` (`unavailable`),
`continuation_page` (`not_attempted`). The **code is canonical** —
`rewrite_status.classify_rewrite_failure(exc)` returns the code (no `model` arg)
and the pure `note_for_code(code, *, model=None)` renders the human note from it,
so the two cannot drift. See
[docs/superpowers/specs/2026-06-15-rewrite-note-code-design.md](docs/superpowers/specs/2026-06-15-rewrite-note-code-design.md).

**Phase 2 notes**:
- `LightweightExtractor` handles 11 formats (PDF, DOCX, XLSX, PPTX, ODT, RTF,
  TXT, Markdown, HTML, CSV, ICS). `DoclingExtractor` is optional, enabled via the
  `[extraction]` uv extra.
- **The extension half of the allowlist reads the *original filename*, never
  `attachment_blobs.path` (#216).** That path is content-addressable
  (`blobs/<aa>/<bb>/<sha256hex>`) and has **no extension by construction**, so
  every `Path(path).suffix` comparison was against `""` — silently reducing
  "MIME *or* extension" to "MIME only" and leaving mis-typed attachments
  (`application/octet-stream` from mobile clients) permanently unindexed. The
  rule is the pure
  [src/localmail/search/attachment_kind.py](src/localmail/search/attachment_kind.py)
  (`extension_of`, `is_allowlisted`, `preferred_filename`, `is_pdf`), shared by
  the worker gate, the docling-fallback decision, and both extractors'
  `extract`/`supports`. `extract` takes a keyword-only `filename=`; filenames
  come from `messages.attachments` via `extract_worker._blob_filenames`, whose
  containment predicate is served by `messages_attachments_gin`. A blob is
  content-addressable and global, so it can carry several original names —
  **any** one with an allowlisted extension admits it.
- **A turned-away blob gets a `type-skipped` sentinel row, and that is
  load-bearing (#216).** The gate used to `continue` with no row and no log.
  Two consequences, the second severe. The skip was invisible — no
  `failed_extractions`, nothing queryable. And the blob never gained an
  `attachment_text` row, so it stayed eligible and was re-claimed every sweep:
  since `_claim_batch` is `ORDER BY first_seen_at LIMIT
  extract_worker_batch_size` (default 20), **one full batch of images ahead of
  everything else made every sweep return `touched=0`**, which the CLI backfill
  loop and the daemon worker both read as "queue drained". Extraction then
  stopped for the whole archive. This was live on the Mac deployment: 16,542
  unprocessed blobs, 19 extracted, 0/20 allowlisted in the next claim. The
  sentinel makes the blob ineligible, so the queue advances. It also means
  widening an allowlist does **not** re-open skipped blobs — clear them with
  `DELETE FROM attachment_text WHERE extractor = 'type-skipped'`
  (`retry-failed-extractions` deliberately does not, it is about *failures*).
- `extract_worker` uses `conn_factory` (not pool) so each sweep gets a fresh
  connection — prevents server-side idle timeouts on long extractions.
- `extract_worker` spawn is gated by `cfg.search.run_extract_worker`.
- There is no `failed_attachment_chunkings` table (intentional Phase 2 scope
  decision); persistent attachment-chunk failures surface as repeated WARNING logs.
- `_extract_xlsx` blob-path workaround: openpyxl detects format by file extension,
  so the worker passes `io.BytesIO(path.read_bytes())` instead of the
  extension-free blob path. No other Office extractor has this issue.
- **The OCR engine is configurable and defaults to `auto` (#248).**
  `DoclingExtractor` used to hardcode `ocr_options=EasyOcrOptions(...)`. EasyOCR
  is **not** a docling dependency, so on any install without it every scanned PDF
  raised `ImportError` out of `convert()` — on the **poison-pill** path, burning
  `retry_count` until the blob was given up on. Scanned PDFs are precisely what
  the docling fallback exists for; 743 such rows accumulated on the live Mac
  archive within hours of #216 making the path reachable. The hardcoding also
  **overrode a better default**: docling's own `PdfPipelineOptions.ocr_options`
  is `OcrAutoOptions`, which probes ocrmac → rapidocr → easyocr and, when none
  is installed, passes pages through **without raising** — an honest
  `lightweight-empty` sentinel instead of a failure.
  - The pure [src/localmail/ocr_policy.py](src/localmail/ocr_policy.py)
    (`plan_ocr`, `unknown_engine_message`, `OCR_AUTO`, `OCR_DISABLED`) maps
    `search.extractor_ocr_engine` to `(do_ocr, engine_kind)`. It sits at the
    **top level, not under `search/`**, because `config.py` imports `OCR_AUTO`
    as the field default and `localmail.search`'s `__init__` imports `config` —
    same reason `account_names.py` and `fetch_retry.py` live there.
  - The config value **is docling's own registry kind**, resolved through
    `factory.create_options(kind=…)`, so there is no mapping table to drift
    against a docling upgrade. Validation is against the **live**
    `factory.registered_kind` rather than a `Literal` in our config — engines get
    added and renamed (this build registers `tesserocr`, not `tesseract_cli`).
    The one value we own is `"none"` (disable OCR; docling has no such kind).
  - **A missing/unknown engine is an `ExtractorConfigurationError`, which
    subclasses `TransientExtractorError`.** That subclassing is the load-bearing
    part: `_is_transient` already recognises it, so `retry_count` is never burned
    and the bound becomes the #153 transient budget — which exists for exactly
    this shape (not-the-blob's-fault, possibly permanent). Detection is
    `_exc_chain_has_import_error` — matching the **type**, never the message
    text, since each engine words its own. A dedicated `attachment_text` sentinel
    was rejected: it would make the blob ineligible for re-claim, so fixing the
    config would silently *not* re-open the documents it was fixed for (the
    one-way door `type-skipped` documents). Recovery is
    `localmail retry-failed-extractions`.
  - `pyproject.toml`'s `[extraction]` extra installs **`ocrmac ; sys_platform ==
    'darwin'`** — a thin Apple Vision wrapper, no torch and no model downloads.
    Linux gets no engine and degrades via `auto`; install `easyocr`/`rapidocr`
    there to opt in. Measured on the live Mac archive: cold pipeline init ~100 s,
    then **~1.7 s/page warm** — docling caches the pipeline internally, so the
    per-blob `DocumentConverter()` construction is not worth caching ourselves.
- **`ExtractedText` strips NUL bytes on construction, and that is a fix not a
  nicety (#249).** Postgres `TEXT` rejects `\x00` and `attachment_text.extracted_text`
  is the type's only consumer, so a NUL surviving to the INSERT aborted it,
  escaped `_process_blob` into the worker's outer safety net, and was recorded as
  a poison pill under the extractor name **`'unexpected'`** — permanently, since
  the same bytes always re-extract to the same NUL. 128 blobs on the live Mac
  archive (112 PDFs, 10 `text/plain`, 5 `octet-stream`, 1 html) had been given up
  on this way. Normalising in `__post_init__` rather than in each of the eleven
  `_extract_*` methods means a twelfth cannot forget (same by-construction
  reasoning as #67's unconditional ACL check). The rule itself is the pure
  [src/localmail/pgtext.py](src/localmail/pgtext.py)`::strip_nuls`, now the single
  implementation shared by `parser.py`, `extract_worker.py`'s failure logging, and
  this boundary — it had been copy-pasted into the first two and simply missing
  from the third.
- **`ExtractedText` also collapses whitespace-only text to the `''` sentinel
  (#266).** `_chunk_attachments_lazily` claims `extracted_text <> ''`, but
  `chunk_attachment_text` returns `[]` for text whose `normalize_whitespace` is
  empty — so a stored whitespace-only row (a scanned page of nothing, layout
  that extracted to spaces) passed the claim, produced no chunk, and was
  **re-claimed on every sweep forever**. Enough of them sorting low in the
  `ORDER BY sha256` claim fill the batch and attachment ingestion stops
  archive-wide: the #216 shape, with the same invisibility (blob chunking has
  no failure table, by design). Placed in `__post_init__` beside the #249 NUL
  strip, and for the same by-construction reason.
  - The rule is the pure
    [src/localmail/search/text_empty.py](src/localmail/search/text_empty.py)`::is_blank`,
    shared with the worker's backstop below. It is `not text or text.isspace()`
    — allocation-free and short-circuiting, where `not text.strip()` copies a
    possibly-megabyte string whenever there is leading/trailing whitespace.
    `tests/test_text_empty.py` pins `is_blank(t) == (normalize_whitespace(t) ==
    '')` over every character Python calls whitespace, including the
    `str.splitlines()` boundaries (`\x0b`, `\x1c`–`\x1e`, `\x85`, U+2028).
  - **The backstop is gated on `is_blank`, NOT on the chunker's bare `[]`.**
    `_chunk_attachments_lazily` heals a claimed row that is blank by stamping
    `extracted_text = ''` in place (one INFO line), which is how rows stored
    before the boundary drain out — on first claim, with no migration. Healing
    on `[]` alone would be shorter and today means the same thing, but the
    UPDATE is destructive and **one-way** (`_claim_batch` skips any blob that
    already has an `attachment_text` row, so nothing re-extracts it): a future
    chunker rule that returned `[]` for text with substance would silently
    delete real extracted text archive-wide. Gated this way that case logs a
    WARNING and stays claimable — a loud wedge, which is recoverable, over a
    quiet one-way door, the same call as `type-skipped` and the rejected `'und'`
    sentinel. Healed rows are `extracted_text = '' AND extractor NOT IN
    ('size-skipped', 'type-skipped', 'lightweight-empty')`.
  - **It changes what `_process_blob` sees**, deliberately: step 3's gate is
    `if lw_text.text`, so a whitespace-only lightweight result now falls to step
    4 — the docling/OCR fallback for PDFs (usually right: a lightweight
    extraction of pure space is a scanned page, exactly what the fallback exists
    for, at the cost of an OCR pass on those blobs — see #248 for the timings),
    and the `lightweight-empty` sentinel for everything else.
  - Consequence for `search-status`, **fixed by #277** (see the extraction
    queue bullet below): a healed row used to move into `blobs_pending`, which
    never drained. Pre-existing for every other sentinel; healing added to that
    bucket rather than creating it.
- **The extraction queue has one authority, and `search-status` composes it
  (#277).** `search-status` derived `blobs_pending = blobs_eligible -
  blobs_extracted`, where *extracted* meant `attachment_text.extracted_text <>
  ''`. But `_claim_batch` skips a blob the moment **any** `attachment_text` row
  exists, so every empty-text sentinel — `type-skipped` (#216),
  `lightweight-empty`, `size-skipped`, a #266-healed row — counted as
  outstanding work **forever**, as did every blob parked at a retry cap (#153,
  which writes no `attachment_text` row at all). On the live Mac archive that
  was `blobs_pending 288` against a genuinely empty queue: 106 sentinels + 182
  capped-out failures, none of which any worker would ever claim. Exactly the
  drift #251 found on the language half of the same command.
  - The rule is [src/localmail/search/extract_queue.py](src/localmail/search/extract_queue.py),
    which owns the claim predicate (`CLAIMABLE_WHERE_SQL`), the join shape it
    reads (`QUEUE_FROM_SQL`), the SQL mirror of the allowlist
    (`ALLOWLISTED_WHERE_SQL`), and the one thin read `fetch_queue_counts` —
    co-located for the same reason as `blob_temps.py`'s minting-beside-matching.
    **`_claim_batch` composes the same constants**, so the report cannot again
    describe a queue the worker disagrees with.
  - **Four buckets partition `blobs_eligible`**: `blobs_extracted` (a row with
    text), `blobs_no_text` (a row with `''` — every sentinel flavour),
    `blobs_gave_up` (no row, a retry budget exhausted), `blobs_pending` (no row,
    still claimable). `QueueCounts.__post_init__` **raises** when they fail to
    sum, since a gap can only come from a predicate bug and the number an
    operator reads is the command's whole product. `attachment_text.extracted_text`
    is `NOT NULL`, which is what makes the rowed pair exhaustive; `retry_count`
    and `transient_count` are too, which is what keeps `NOT (…)` from going
    three-valued and dropping a row out of *every* bucket. All three joined
    tables key on `sha256 PRIMARY KEY`, so no join can multiply a blob.
  - **`blobs_pending` is not the worker's queue depth — `blobs_claimable` is
    (review follow-up).** The four buckets are allowlist-scoped and
    `CLAIMABLE_WHERE_SQL` deliberately is not (#216 applies the allowlist in
    Python, after the claim), so a non-allowlisted un-rowed blob is real work
    the worker will claim and appears in **no** bucket. Shipping only the four
    would have left `blobs_pending 0` reading as "queue empty" on exactly the
    archive #216 was filed about — 16,542 blobs, 0/20 allowlisted in the next
    claim — i.e. #277's defect inverted, and the under-report is the quieter
    half. `CLAIMABLE_TOTAL_SQL` is its **own statement**, because the honest
    number must not inherit the allowlist, and it composes `QUEUE_FROM_SQL`
    rather than `QUEUE_COUNTS_FROM_SQL` so it never touches `messages` at all.
    It is therefore a different snapshot and is **excluded from the partition
    check** — a worker committing between the two statements can briefly put
    `claimable` below `pending`, and crashing over a race that resolves itself
    would be worse than reporting it.
  - **`ALLOWLISTED_WHERE_SQL` is a hand-maintained restatement of
    `attachment_kind.is_allowlisted`, and the two had drifted.** Pinned now by a
    differential test over the same inputs. Two divergences, both found in
    review: the SQL compared MIME **case-sensitively** and never lowered the
    *configured* values, so a `config.toml` carrying `"Application/PDF"` or
    `".PDF"` matched in the worker and in no counter — which makes SQL
    *under*-count uniformly, so the partition still sums and nothing raises,
    #277's failure mode wearing a different hat. And `'\.[^.]+$'` matched the
    whole of a bare dotfile (`.txt`) where `Path(".txt").suffix` is `""`; the
    regex is `'.(\.[^.]+)$'` now, requiring a character ahead of the final dot.
    Both operands are lowered — the config half in `allowlist_params`, so the
    fragment can assume it. **#280 moved the extension half of both details onto
    `EXTENSION_MATCH_JOIN_SQL`**; `ALLOWLISTED_WHERE_SQL` keeps only the MIME
    comparison's `lower()`, so grepping the named constant for the regex now
    finds nothing.
  - **`blobs_extracted` is now scoped to allowlisted blobs**, where it used to
    be a global `attachment_text` count. That is what lets the four sum; the two
    agreed on the live archive (9202) chiefly because only an allowlist
    *narrowed* after extraction separates them — the case-folding divergence
    above and a blob whose referencing messages were all deleted do too. (#280
    quotes 9203 for the same counter: a later snapshot, one more extraction, not
    a contradiction.)
  - **`blobs_gave_up` is recoverable and `blobs_no_text` is not.** The former
    clears with `localmail retry-failed-extractions`; the latter is terminal by
    design — no `retry-…` command reopens it, the deliberate escape hatch being
    the `DELETE` documented under `SKIPPED_EXTRACTOR` — so a steady non-zero
    reading is **normal**, the same shape as `body_lang_declined`. Break it down
    with `SELECT extractor, count(*) FROM attachment_text WHERE extracted_text =
    '' GROUP BY extractor`; a flat int payload was kept over a nested
    per-extractor map so `--format text` stays one number per line.
    **Only half of `blobs_gave_up` is listable**: the transient budget (#153)
    writes no `failed_extractions` row, so `list-failed-extractions` shows the
    poison-pill half only and a misconfigured OCR engine reads as "nothing
    wrong". Query `transient_extractions` directly for the rest.
  - **The invariant failure is operator-legible, and does not take the report
    with it.** `QueueCountsInconsistent` is a named `ValueError` so
    `search_status` can catch exactly it; the blob read is deliberately the
    **last** one the command makes, so an attachment-side gap still prints the
    embedding and `body_lang_*` counters (nulled blob keys, then a
    `ClickException`) rather than discarding eleven healthy numbers and a raw
    psycopg traceback. `QueueCounts.status_field_names()` / `.status_fields()`
    derive the payload keys from the dataclass fields, so a bucket added to the
    type cannot go missing from the command that exists to report it — the
    hand-copied projection was the last place this drift could hide, apart from
    `misfiled` — see #284 below, which is excluded on purpose. `__bool__`
    raises, like `SweepOutcome`'s (#259).
  - **The partition is one aggregate pass, not five queries.** **Do not split
    it**: one statement is one snapshot under READ COMMITTED, which is the only
    reason `__post_init__` can treat a gap as a predicate bug rather than as a
    worker committing mid-read — split, it becomes an intermittent crash on a
    live archive.
  - **The eligibility lookup is decorrelated, and that is #280's whole fix.**
    `search-status` measured **13:28.45** on the 127k-message Mac archive and
    now measures **0.97 s**, with every counter byte-identical (9491 / 9203 /
    106 / 182 / 0). That 0.97 s covers `CLAIMABLE_TOTAL_SQL` too, closing the
    measurement #277 left open. (Session 21 separately clocked the pre-fix
    eligibility counter *alone* at **13:04** and the four-bucket pass at
    **14:07**; those are components of the same 13:28, not rival totals.) The
    extension half reads original filenames out of `messages.attachments`
    (#216); written as a correlated `EXISTS` it was a `SubPlan` re-executed
    once per blob, because `messages_attachments_gin` needs a **constant**
    operand and correlating on `b.sha256` costs a `Seq Scan on messages`
    instead.
    - `EXTENSION_MATCH_JOIN_SQL` resolves it once for the whole archive as a
      `LEFT JOIN` over `SELECT DISTINCT`, and hangs off `QUEUE_COUNTS_FROM_SQL`
      rather than `QUEUE_FROM_SQL` — the latter is shared with `_claim_batch`,
      whose join keys must stay three primary keys. The shipped form does
      **not** restore the index plan: it is one `Seq Scan on messages` +
      `HashAggregate` for the whole archive, i.e. the scan paid once rather
      than once per blob. `messages_attachments_gin`'s remaining user is
      `extract_worker._blob_filenames`, which does pass a constant.
    - **A `LEFT JOIN`, not an uncorrelated `IN (SELECT …)`.** The subquery form
      reads better and plans the same way until the planner *estimates* the
      hashed subplan will not fit `work_mem`, at which point it plans the
      per-row form and the fix is undone on precisely the archives it was
      written for. The estimate is made at plan time from statistics — Postgres
      does not detect overflow at runtime and switch — so bad statistics can
      choose that form on an archive that would have fit. A hash join spills to
      disk instead.
    - **`jsonb_typeof(m.attachments) = 'array'` guards the expansion.** The
      correlated form carried `m.attachments @> …`, a single-relation qual the
      planner pushed below the lateral, so `jsonb_array_elements` only ever saw
      arrays; decorrelated there is no restriction on `messages` and every row
      is expanded. `jsonb_array_elements` raises `22023` on an object or scalar
      and the column is `JSONB NOT NULL DEFAULT '[]'` with no `CHECK`, so one
      malformed row — a restore, a hand `UPDATE` — would abort the statement,
      escape `search_status`'s narrow catch, and take the eleven healthy
      counters with it. No writer produces one today; the guard keeps the
      failure mode where the read ordering put it.
    - **`DISTINCT` is load-bearing, and no runtime guard covers it.** A blob is
      content-addressable and global, so every message carrying those bytes
      names it independently; without it a blob several messages named
      admissibly fans out into one row per message, inflating every counter.
      The partition check does **not** catch this — the fan-out multiplies
      `eligible` and the buckets equally, so each duplicated row still matches
      exactly one bucket, the sum holds and `misfiled` stays `0`. The only
      symptom that reaches an operator is `pending` diverging from `claimable`,
      i.e. #277's failure mode returning.
      `test_a_blob_two_messages_both_named_admissibly_is_counted_once` is the
      sole pin; it is load-bearing, not redundant.
    - The regression pin is a **plan** assertion, because nothing about the
      answers changes, and it is two assertions because either alone has a
      hole. `test_extract_queue_sql.py` walks `EXPLAIN (FORMAT JSON)` and
      requires that no scan of `messages` sit under a `SubPlan` — structural,
      so it holds at any scale, but blind to a `LEFT JOIN LATERAL`
      re-correlation, which keeps the join and merely makes it per-blob. So it
      also walks `EXPLAIN (ANALYZE)` and requires `Actual Loops == 1` on a
      **seeded** fixture; on the empty tables `db_conn` yields every form
      reports `0` loops and that assertion would be vacuous. Both keep the
      pre-#280 predicate verbatim as a negative control — the same role
      `--predicate-form pre75` plays in `run_browse_explain.py`.
  - **The buckets have one authority: `BUCKET_WHERE_SQL` (#284).** The SELECT's
    aggregates, `__post_init__`'s sum, and the `misfiled` guard are all derived
    from that mapping, so a fifth disposition cannot reach one and miss
    another. `misfiled_count_sql` closes the gap the sum check leaves — a sum
    is *implied by* a partition but does not *imply* one, so a blob counted
    twice plus a blob counted not at all adds up correctly. It casts each
    predicate to `int` and demands exactly `1`; `IS DISTINCT FROM` rather than
    `<>` because the total goes SQL `NULL` as soon as any predicate does, which
    is what relaxing one of the `NOT NULL` columns the predicates pivot on
    would produce, and a `NULL` filter condition counts nothing and reports the
    archive healthy. It takes its buckets as a **parameter** so the detector can
    be tested against contrived predicates — the production four are
    structurally incapable of overlapping, which is why nothing tested this.
    `misfiled` is the one field `status_field_names()` excludes: its only
    non-zero value raises, so reporting it would put a permanently-`0` line in
    front of an operator. **Its scope is one blob against the four buckets** —
    it cannot see a blob duplicated by a join fan-out (see `DISTINCT` above),
    because each duplicate still lands in exactly one bucket. Because the field
    defaults to `0`, an aggregate that stopped arriving — deleted, or degraded
    to a constant — would leave `class_row` filling that default and the guard
    silently off, reporting every archive healthy. The pin therefore asserts on
    `QUEUE_COUNTS_SQL` **without calling `misfiled_count_sql`**: each bucket
    predicate must appear exactly twice (its own `FILTER`, and the misfiled
    sum). Comparing the function's output against the SQL built from it is a
    tautology that survives replacing the whole expression with
    `0 AS misfiled` — as does the substring `"AS misfiled"`.
- **Transient classification of third-party docling failures (#47)**:
  `extract_worker._is_transient` recognises only the narrow builtin
  `_TRANSIENT_EXC_TYPES` (`ConnectionError`/`TimeoutError`/`MemoryError`) plus
  `TransientExtractorError` — broadening it (e.g. `OSError`) would mis-classify
  permanent `ENOENT`/`EACCES`. docling's network errors are *third-party*
  classes (`requests`/`httpx`/`urllib3`/`huggingface_hub`/`aiohttp`) outside
  that hierarchy, so `DoclingExtractor.extract` opts them in **at the wrapper**:
  a `convert()` failure whose cause/context chain contains a package in
  `extractor._TRANSIENT_THIRD_PARTY_MODULES` is re-raised as
  `TransientExtractorError` (retried next sweep, not poison-pilled). The
  chain walk is the shared pure `extractor.iter_exc_chain` generator, reused
  by both `_is_transient` and `_exc_chain_has_transient_module`. To add a
  newly-observed transient package, extend the frozenset — never widen the
  builtin `_TRANSIENT_EXC_TYPES`. **Transient retry cap (#153, resolved)**: the
  transient path deliberately never touches `retry_count` (reserved for
  poison-pill semantics), so a *permanently* failing third-party network error
  (`huggingface_hub` 401/403 from a misconfigured token, 404 for a removed
  model) used to re-attempt every sweep forever. The fix adds a **separate**
  counter table `transient_extractions` (migration `0025`, keyed on `sha256`,
  independent of `failed_extractions`): `extract_worker` bumps
  `transient_count` on each transient classification via
  `_record_transient_safely` (nested SAVEPOINT, like `_record_failure_safely`),
  the `_claim_batch` query excludes a blob once `transient_count >=
  cfg.extract_worker_max_transient_retries` (default 5 — larger than the
  poison-pill cap of 3 because transients are often genuinely recoverable, but
  now bounded), and a successful extraction calls `_clear_transient` so the cap
  measures **consecutive** failures only (the claim returns the prior
  `transient_count` as a 5th column so the reset DELETE is skipped on the common
  no-history path). At the cap the worker logs one distinct *"giving up"*
  WARNING instead of repeating the per-sweep retry line. Recovery: `localmail
  retry-failed-extractions` now clears **both** `failed_extractions` and
  `transient_extractions` rows (per-blob with `--sha256`, else all). The pure
  boundary `transient_budget_exhausted(count, cap)` (`count >= cap`) matches the
  SQL `transient_count < cap` filter.

**Language detection: a declined row leaves the queue (#251).** `body_lang`
detection had stopped **archive-wide on both deployments** — the Mac frozen at
7744 labelled against 100020 pending, for weeks. `run_lang_detect_pass` claimed
`body_lang IS NULL AND body_text IS NOT NULL ORDER BY id LIMIT N`, and a row the
detector *declined* stayed NULL, so it kept satisfying the predicate and, under
a stable ordering, was re-claimed **in the same position forever**. Once the
first `body_lang_detect_batch_size` rows were all unlabelable — separator
blocks, bare URLs, bodies under the 20-char floor — nothing behind them was ever
reached. Same shape as #216's un-rowed blob.

- **The record lives in its own column, not in `body_lang`.** Migration `0035`
  adds `messages.body_lang_attempted_at`; the claim gains
  `AND body_lang_attempted_at IS NULL`. `body_lang` keeps meaning exactly
  "detected language, else unknown", so **no reader changes**. A sentinel value
  (`'und'`) was rejected: it would have needed four readers to learn to exclude
  it (`arms.py`'s `lang:` filter, `searcher._maybe_warn_unpopulated_body_lang`,
  `search-status`, migration 0015's index) and would have repeated the
  **one-way door** CLAUDE.md already documents for `type-skipped` — lowering
  `body_lang_min_confidence` would silently not re-open the rows it was lowered
  for. `localmail lang-backfill --retry-declined` (→ `lang_detect.retry_declined`)
  is the escape hatch the sentinel cannot have.
- **`CLAIMABLE_WHERE_SQL` / `DECLINED_WHERE_SQL` are the one authority**, shared
  by the claim, `search-status`'s two counters, and (by hand, with a test)
  migration 0035's index predicate. The drift they prevent is what hid the bug:
  `search-status` reported 100020 rows "pending" that the worker would never
  claim. A test pins that the two predicates are disjoint and jointly exhaustive.
- **`0035` replaces 0017's index under a NEW name** (`messages_body_lang_pending_idx`
  → `messages_body_lang_claimable_idx`). `CREATE INDEX IF NOT EXISTS` matches on
  **name only**, so recreating the old name with the new predicate would have
  silently no-opped on every host that already had it, leaving the worker on an
  index that no longer matches its claim.
- **The return type carries both counts** — `LangDetectPass(visited, labelled)`.
  This is the second half of the bug, and it is load-bearing: the function used
  to return *labelled*, and both drain loops broke on 0, so skipping declined
  rows alone would still have stopped them on the first unlabelable batch.
  Loops terminate on `visited == 0`. There is deliberately **no `__bool__`** —
  an implicit reading of this exact value is the defect.
- **Poison rows are stamped too.** The exception branch rolls back to its
  savepoint, discarding the stamp, so `_mark_attempted_safely` rewrites it under
  a *second* nested savepoint — `SAVEPOINT` outside the `try`, like
  `record_failed_message` and `record_attempt`. Without it a body that reliably
  crashes the detector starves the queue exactly as a declined one did.
- **`body_lang_pending` was redefined** to mean claimable work only; the
  turned-away remainder is the new `body_lang_declined`. Rows labelled before
  0035 keep a NULL `attempted_at` — legal, and never consulted, since the claim
  excludes `body_lang IS NOT NULL` first.

**The detector sees the body with URLs stripped (#255).** Unwedging detection
in #251 immediately exposed the next defect: **17% of all labels** on the live
Mac archive (17129 of 100922) named a language with no plausible presence in
it — Yoruba the second most common at 7593 rows, plus `fi`, `eo`, `et`, `cy`,
`la`, `az`. The mail is English marketing/newsletter traffic whose bodies are
mostly tracking URLs with high-entropy path segments; lingua scores that soup
above `body_lang_min_confidence` and lands on a low-resource language. The
errors are **correlated**, so `lang:en` was excluding ~7600 English newsletters
— the inverse of the filter's purpose.

- **The rule is the pure
  [src/localmail/search/lang_text.py](src/localmail/search/lang_text.py)`::normalize_for_detection`**,
  applied *only* inside `LinguaDetector.detect`. `messages.body_text`, the FTS
  tsvector, chunking and embeddings all still see the original body — this
  changes what the detector reads, never what the archive stores.
- **The length floor measures the normalised text**, and that ordering is
  load-bearing. A body of pure tracking URLs clears the 20-char floor when
  measured raw and earns a confident wrong label; normalised it is empty and
  correctly declines.
- **Two changes were needed, not one.** URL-stripping alone resolved 69% of the
  bad rows, full-accuracy mode alone 48%, together **99%**. The issue framed
  them as alternatives. `body_lang_low_accuracy` now defaults **False**, and
  `LinguaDetector.__init__`'s own default was aligned to match so the two
  cannot drift.
- **Three assumptions measured false, all recorded so they are not re-tried.**
  Full accuracy is **227 MB peak RSS, not ~1 GB** (low is 239 MB — full is
  *cheaper* and 2.3× faster; lingua loads per-language models lazily either
  way), so the old config comment was simply wrong. Raising
  `body_lang_min_confidence` 0.65 → 0.90 moved implausible labels 64 → 62 of
  500 — low-accuracy lingua is *confidently* wrong, so a confidence floor
  cannot discriminate. And the U+034F preheader padding the issue highlights
  contributes **nothing**: an ablation shows invisible-character, email,
  HTML-tag and separator-rule stripping each add **zero** once URLs are gone.
  **Do not add normalisation steps here without a measurement.**
- **`reopen_all` / `lang-backfill --relabel` is the escape hatch
  `retry_declined` cannot be.** A row carrying a wrong label is neither
  claimable nor declined, so `--retry-declined` cannot reach it; only a policy
  change needs this and it discards every label, hence the confirmation prompt.
  `RELABELABLE_WHERE_SQL` joins the other two as one authority per predicate —
  claimable and declined are disjoint subsets of it.
- **A "detected but implausible" sentinel was rejected**, for the reason #251
  rejected `'und'` and #216's `type-skipped` documents: it is a one-way door.

**`bm25_field_boosts` weight normalization**: `arms.py` normalises the raw
boost values by `max(raw)` to satisfy `ts_rank_cd`'s `[0, 1]` weight
requirement. Config values above 1.0 are therefore treated as *relative*
weights, not absolute — e.g. `{"subject": 3.0, "from": 2.0, "body": 1.0,
"to": 0.5}` is equivalent to `{"subject": 1.0, "from": 0.67, "body": 0.33,
"to": 0.17}` after normalization.

**`body_html` in FTS (migration 0006)**: the generated column `fts_v2` on
`messages` includes `body_html` concatenated with `body_text` at weight C.
This deviates slightly from the plan (which had only `body_text`). HTML
markup tokens (tags, attribute names) may dilute ranking slightly for
heavily-marked-up messages; this can be revisited in a later migration if
needed. The current approach is fine for plain-text–heavy archives.

**`_split_statements` in `db.py`**: the migration runner delegates to
`sqlparse.split` so dollar-quoted bodies (`$$ ... $$` / `$tag$ ... $tag$`),
single-quoted string literals, and `--` / `/* */` comments don't trip the
splitter on embedded semicolons. Pure-comment fragments after the final
statement are dropped; comments attached to a real statement are preserved.

`pyproject.toml` floors `sqlparse>=0.6`, and that floor is **security-driven**
rather than functional: three HIGH advisories (dollar-quote ReDoS,
`TokenList.__init__` CPU DoS, quadratic `group_comments`) and one medium
string-breakout advisory all land on `<= 0.5.5`. Nothing attacker-controlled
reaches the parser — this function is its only consumer and it reads our own
numbered migration files — so the bump is hygiene, not incident response,
which is why it was **verified rather than assumed**: splitting every
migration on disk yields byte-identical statement lists under 0.5.5 and 0.6.0.

**`pypdf>=6.16.1` and `icalendar>=7.1.3` are the security floors where the
input IS attacker-controlled**, which is the case the sqlparse note above
contrasts against. Both are parsed straight out of email attachments —
`extractor._extract_pdf` calls `PdfReader`/`page.extract_text()`, and
`_extract_ics` calls `Calendar.from_ical` — so the four DoS advisories they
carry (Dependabot #66–#69: an infinite loop in pypdf's
`TreeObject.insert_child`, unbounded runtime/memory retrieving outlines and
extracting XForm objects, and algorithmic complexity in icalendar's
`Component.__eq__`) are reachable by anyone who can email the archive.

- **They are still floors and not an incident**, because the blast radius was
  already bounded: a hostile attachment costs one extraction slot and then
  poison-pills under #153's transient budget. What it cannot do is stall the
  archive, which is what makes this a bump rather than a rollback.
- **The icalendar floor is the fix, not a formality.** Its vulnerable range is
  `>= 7.1.0, < 7.1.3` — *above* the old `>=6.0` floor — so the declaration
  read as unaffected while the resolver, picking the newest compatible
  release, landed squarely inside the window (the lock held 7.1.0). A floor
  that a vulnerable version satisfies is not a floor. Do not read a
  `vulnerable_version_range` against the declared floor; read it against
  `uv.lock`.
- **Verified rather than assumed**, the same way sqlparse was: extraction over
  six PDF/ICS shapes (native, multipage, outlined, scanned, a two-event
  calendar, an empty one) is **byte-identical** across the bump, and the 99
  extraction tests pass. The script is not kept — it builds its fixtures with
  reportlab/PIL exactly as `test_extractor.py` does, which is where a
  permanent regression gate for this already lives.

**`transformers>=5.10.0` is the third security floor, and the one that is
*not* on an attacker-controlled path** (Dependabot #70, HIGH, CVE-2026-9856,
range `< 5.10.0`). It arrives via docling under the `[extraction]` extra,
`grep -rn transformers src/` is empty, and the advisory is a path traversal in
`save_pretrained` via chat-template names — a **write** path localmail never
calls. So this is the sqlparse case (hygiene), not the pypdf/icalendar one.

- **It is declared even though nothing imports it**, which is the corollary of
  the lesson `icalendar` taught: the lock is the state, the floor is the
  constraint, and a lock-only fix says nothing about what a re-resolution may
  pick. Listing a package we do not import follows `ocrmac` beside it — that
  one is there for the same reason, to shape what docling does.
- **The advisory was reachable in the lock and invisible in the manifest**:
  `transformers` appeared in neither `[project.dependencies]` nor any extra,
  so there was no floor to read against at all. Read
  `vulnerable_version_range` against `uv.lock`.
- **Verified end to end, not by the test suite**, because the suite cannot see
  this: every docling test mocks the converter, so nothing in it loads the
  layout models a transformers bump moves. A one-off probe built an image-only
  PDF (PIL + reportlab, as `test_extractor.py` does), confirmed
  `LightweightExtractor` finds no text in it, and ran a **real** OCR pass
  through `DoclingExtractor` before and after. The extracted string is
  byte-identical (`"Invoice 4711 total 250 EUR"`), and the run reports
  `Loading weights: 770`, which is what proves the bumped package is on the
  path rather than merely installed. The probe is not kept, for the reason the
  pypdf/icalendar one was not.
- **The bump was larger than the pre-measurement**: `uv lock --dry-run` had
  reported 5.8.1 → 5.15.1 plus one `safetensors` bump and "nothing else". The
  real re-lock moved **three** packages — `transformers 5.16.1`,
  `safetensors 0.8.0`, `tokenizers 0.23.1` — partly because the declared floor
  changes the resolution and partly because a release landed in between. A
  dry-run measures the resolution at the moment it runs; it is not a promise
  about the one that ships.

**Acceptance eval harness**: `tests/acceptance/run_recall_eval.py` seeds the
synthetic multilingual corpus, runs the embed worker, and reports recall@K +
MRR@K per language. Phase-1 gates: recall@20 >= 80% and MRR@20 >= 0.5 for
de/en/es/ja. Norwegian is reported but not gated. Requires the fastembed model
`google/embeddinggemma-300m` to be in the local fastembed cache (downloaded
on first invocation, ~250 MB).

**RRF sweep harness**: `tests/acceptance/run_rrf_k_sweep.py` (added for #35)
seeds the chosen corpus + drains the workers exactly once, then re-runs the
query suite for each candidate `rrf_k` against the same chunk pool — only
fusion varies between sweeps. Use `--corpus {multilingual,attachment}`,
`--rrf-ks`, `--candidates-per-arm`. The #35 measurement showed that both
synthetic corpora are insensitive to `rrf_k` across [1, 1000] — fusion is
dominated by a single arm — so the default `rrf_k=60` is fine until
production data or an adversarial corpus exposes the bias hypothesised in
#35.

**Chunk-insert benchmark (#5, closed not-fixed)**:
`tests/acceptance/run_chunk_insert_bench.py` seeds N multi-chunk messages and
times the chunking loop's per-chunk `cur.execute` (production) against a batched
`cur.executemany` candidate, both inside the same per-message SAVEPOINT. #5
hypothesised that row-by-row chunk INSERTs were a backfill bottleneck. The
measurement (localhost Postgres, 1500 msgs × ~12 chunks) showed the loop is
**tokenization-bound** — ~880 chunks/s regardless of INSERT strategy, because
`chunk_message` spends its time in tiktoken `encode`, not INSERT round-trips. On
localhost `executemany` is ~4% *slower* (per-call batching overhead with no
round-trip latency to amortise). localmail is **single-host**, so Postgres is
always local — the remote-DB scenario where `executemany` would win never
applies. The production loop **stays row-by-row**; #5 is closed on this evidence.
Per-message poison isolation at the INSERT layer is pinned by
`test_embed_worker.py::test_insert_failure_isolates_poison_message_per_savepoint`
(NUL-byte chunk text → INSERT rejected → only that message rolls back).

## GUI server (Phase 1 of GUI)

Network-reachable HTTPS API exposing the same functionality as the search
subsystem, plus account/folder/message/attachment read paths and bearer-token
auth. See [docs/superpowers/specs/2026-05-17-localmail-gui-design.md](docs/superpowers/specs/2026-05-17-localmail-gui-design.md)
for the full design.

- Code lives under `src/localmail/api/` (transport-free service library) and
  `src/localmail/serve/` (FastAPI HTTP wrapper).
- The MCP server (planned) will import `localmail.api` directly — no HTTP hop.
- Migration `0014_api_users.sql` adds `api_users` + `api_tokens`. Tokens are
  stored as SHA-256 hashes; raw bearer is only returned at login/refresh.
- Migration `0016_user_accounts.sql` adds the per-user `(user_id, account_id)`
  ACL join table. Every service-layer accessor under `src/localmail/api/`
  takes a required keyword-only `allowed_account_ids: list[int]` so the SQL
  boundary applies `WHERE account_id = ANY(%s)` on every read. Routes
  resolve the list once per request via `localmail.api.acl.allowed_account_ids`.
  See [docs/superpowers/specs/2026-05-18-per-user-account-acl-design.md](docs/superpowers/specs/2026-05-18-per-user-account-acl-design.md).
- **Login rate-limiting (Postgres-backed, #7)**: migration
  `0019_api_login_attempts.sql` adds an append-only audit table read by
  three sliding-window caps — global, per-IP, per-user. Every login
  attempt (success + failure) is one INSERT; the check is a single SELECT
  with three `FILTER (...)` aggregates. Caps + windows + retention live
  in `LocalmailConfig.auth` so there are no module-level magic numbers in
  `api/auth.py`. The in-memory dicts that preceded this design were
  per-process and lost the security promise the moment `uvicorn
  --workers N` came into scope; the DB-backed table keeps the limits
  consistent across workers and across `serve` restarts. Cleanup is
  best-effort, gated by a Postgres advisory lock
  (`_SWEEP_ADVISORY_LOCK_KEY`) so concurrent workers don't pile up
  DELETEs. **Reverse-proxy support**: `auth.trusted_proxies` (CIDR list)
  + `auth.trusted_proxies_max_hops` enable right-to-left peeling of
  `X-Forwarded-For` for the per-IP cap. Empty default = unchanged
  behaviour (the socket peer is used). The same CIDR list governs both
  admission (is the immediate peer a trusted proxy?) and peeling
  (which XFF entries to skip). Design + threat model in
  [docs/superpowers/specs/2026-05-21-trust-proxy-headers-design.md](docs/superpowers/specs/2026-05-21-trust-proxy-headers-design.md).
  Do NOT also set `uvicorn --forwarded-allow-ips`; it rewrites
  `request.client.host` before our admission check and collapses it.
- **Session revocation covers all four credential kinds**: bumping
  `api_users.sessions_invalidated_at` (via `localmail
  revoke-admin-sessions USERNAME`, the `/admin/users` panel, or
  `POST /v1/admin/users/{id}/revoke-sessions`) is enforced in **four**
  independent SELECTs, each comparing the credential's own issue time
  against the cutoff. All four are required for revocation to be
  terminal; drop any one and the operator's "I cut off that leaked
  credential" belief is false:
  - **admin cookies** — `get_admin_user` (`to_timestamp(issued_at) <
    sessions_invalidated_at` → `SessionInvalidated` → 303 to
    `/admin/login`). See the #113 bullet below.
  - **bearer tokens** — `api.auth.verify_token` (`t.created_at >=
    u.sessions_invalidated_at`). Covers every `/v1/*` endpoint, `/mcp`
    (`mcp.auth.LocalmailTokenVerifier` and `oauth.access.load_access` both
    wrap `verify_token`), and the desktop GUI. Without it a leaked bearer
    stayed valid for its full TTL — up to 30 days.
  - **OAuth refresh tokens** — `mcp.oauth.refresh.load_refresh`
    (`r.created_at >= u.sessions_invalidated_at`). Without it the bearer
    check above buys only ~1 hour: the client presents its refresh token,
    `access.mint_access` stamps the successor `created_at = now()` (past the
    cutoff, so valid), and a fresh 30-day sliding refresh comes with it. A
    revoked token lands on `rotate_refresh`'s **`unknown`** outcome, not
    `reuse` — revocation is an operator action, not evidence of a stolen
    copy, so the family is left intact.
  - **OAuth authorization codes** — `mcp.oauth.codes.load_code`
    (`c.created_at >= u.sessions_invalidated_at`, plus `u.disabled_at IS
    NULL`), added by **#236**. The window is only
    `oauth_authorization_code_ttl_s` (default 60 s) wide and codes are
    single-use + PKCE-bound, but exchanging one mints an access + refresh
    pair stamped `created_at = now()` — past the cutoff, hence valid — so
    an honoured code hands back exactly the credentials the operator just
    cut off. The `disabled_at` half was the older gap of the two:
    `load_refresh` has mirrored `verify_token` on it since the M1
    hardening (#182); `load_code` never did.

    **The load-time check is no longer the only one (#241, resolved).** The SDK
    drives load and exchange as two separate calls, so the load's verdict is
    already stale by exchange time. A *disabled* user was caught indirectly on
    the second leg (the minted refresh reads back absent through `load_refresh`
    → `user_vanished`), but `sessions_invalidated_at` was not: the successor
    carries `created_at = now()`, past the cutoff, so the exchange completed and
    handed back exactly the credentials the operator had cut off.

    The fix re-decides validity **inside the burn**. `codes.consume_code` is one
    CTE — `DELETE … RETURNING user_id, created_at, expires_at` joined LEFT to
    `api_users` — returning `ConsumeResult(burned, still_valid)` under a single
    snapshot, so no revocation can land between the two halves. `still_valid` is
    **one** field covering every reason (expiry, missing/disabled user,
    revocation) rather than one per reason: the caller's question is "may I
    honour this?", and splitting the answer invites honouring a burn that
    satisfied two conditions out of three — the safe-by-default shape of #234
    and #67. **Expiry is re-decided here too**, for the same staleness reason as
    revocation; the window is much narrower (a code can only cross its own
    deadline, never be revoked mid-round-trip), so that conjunct is defence in
    depth, but it is what lets the burn stand alone rather than assume its
    caller checked.

    A missing user row is closed by an explicit `u.id IS NOT NULL`, and that
    line is **not** redundant. The intuitive reading — LEFT JOIN misses, so the
    predicate is NULL, so `COALESCE(…, FALSE)` fails closed — is wrong, and this
    shipped that way first: against the all-NULL row both `disabled_at IS NULL`
    and `sessions_invalidated_at IS NULL` are TRUE, so the predicate returns
    TRUE and no COALESCE ever fires. `ON DELETE CASCADE` on
    `oauth_authorization_codes.user_id` makes the branch unreachable today (a
    deleted user takes its codes with it, so nothing is burned), which is
    exactly why the guard reads as unnecessary until someone relaxes that FK.
    Pinned by `test_consume_of_an_orphaned_code_reports_it_invalid`, which drops
    the FK inside the test transaction to construct the orphan.

    **Burning is unconditional, and that is the point.** Making the DELETE itself
    conditional on the user — the shape #241's issue text first proposed — would
    leave a revoked user's code *unburned* and replayable for the rest of its
    TTL, breaking the single-use invariant #219 established
    (`test_failed_exchange_still_burns_the_code`). Single-use and user-validity
    are separate concerns; the code always dies, validity is reported beside it.

    The **rotation** path needed a narrower fix. `rotate_refresh` already re-ran
    the full `load_refresh` predicate at the top of the exchange leg, so the
    SDK's stale load was re-decided — but the claim UPDATE and the mint are
    separate statements after it, each taking its own READ COMMITTED snapshot.
    The claim now carries the revocation predicate as an `EXISTS`, making the
    whole decision one statement. A failed claim is then **disambiguated** by
    re-reading `_raw_state`: consumed → `reuse` (theft, delete the family),
    otherwise → `unknown`. Conflating them would delete a token family the
    operator never targeted, over an action they took on purpose.

    The predicate itself is now the pure
    [src/localmail/api/revocation_sql.py](src/localmail/api/revocation_sql.py)`::credential_valid_sql(user=, credential=)`,
    shared by all five sites (`verify_token`, `load_refresh`, `load_code`,
    `consume_code`, the rotation claim). #241 *was* a place where the wording had
    been applied to the load but not the consume, so one authority for it is what
    stops the next such gap from being invisible.

  `NULL` means "never revoked" and is the default, so nothing changes for a
  user who has never been revoked. The cutoff is a moment, not a ban:
  credentials minted *after* it authenticate normally, which is what makes
  "revoke, then log in again" work. Login-issued tokens
  (`oauth_refresh_family_id IS NULL`) and OAuth-issued ones are treated
  identically here.
- **API keys are a fifth credential kind, and deliberately not a fifth code
  path (migration `0036`).** A key is an `api_tokens` row with `api_key_name`
  set and `expires_at NULL`, minted against a dedicated **service user**
  (`api_users.is_service`). Because the principal is an ordinary `api_users`
  row, the per-account ACL, `disabled_at` and `sessions_invalidated_at` all
  reach it with no code of their own — which is the whole reason it is not its
  own table. Design:
  [docs/superpowers/specs/2026-08-24-admin-api-keys-design.md](docs/superpowers/specs/2026-08-24-admin-api-keys-design.md).
  - **The CHECK is the load-bearing half of the migration.** Dropping
    `NOT NULL` from `expires_at` alone would let a *login* token be minted with
    no expiry — an immortal interactive credential, produced by a one-line bug,
    with nothing failing. `api_tokens_only_keys_are_immortal` scopes "may live
    forever" to API keys, in the database.
  - **The pairing is 1:1**, enforced by the partial unique index
    `api_tokens_one_key_per_service_user` — keyed on `user_id` alone, because
    `(user_id, api_key_name)` would permit the many-keys-per-principal model
    that overlapping-key rotation needs and this design defers. Everything
    therefore addresses a key by its **principal's id**: `api_tokens`' primary
    key is `token_sha256`, a hash *of* the credential — not presentable as a
    bearer, since verification hashes what is presented and compares, but still
    not something to put in a URL or a log line.
  - **Rule 1 — a key never reaches an admin route.** `require_admin()`'s bearer
    branch refuses `user.is_api_key` **before** consulting `is_admin`. The
    guard sits at the point of use, not at mint time, because a service user can
    be promoted after its key was minted. `users.set_admin` also refuses to
    promote a service row, but the runtime gate is what carries the invariant —
    its test promotes by direct SQL precisely because the UI will not.
    - **The test must assert the refusal's wording, not its 403.** A key on a
      *non-admin* principal is refused by the pre-existing `is_admin` branch
      too, so `test_an_api_key_cannot_mint_another` passed with Rule 1 deleted
      outright; only `test_api_key_admin_bar.py`'s admin-principal test caught
      it. Both assert the detail string now. Mutation-proven, in both
      directions.
    - **`grant-admin` was the unguarded half of the promotion surface, and the
      promotion was not the damage.** `admin.auth.grant_admin` (the CLI) had no
      `is_service` check, and `active_admin_count` counted the bot — so the
      last-admin guard then permitted demoting the only human, leaving an
      archive with **no usable administrator**: Rule 1 refuses the bot's key
      everywhere and Rule 2 refuses its login, and recovery is shell-side only.
      Excluding service rows from the count is what makes the guard hold
      whatever put `is_admin` on the row, which is why that is the fix rather
      than the guard alone; `grant_admin` shares `users.reject_service_row`
      (renamed from `_reject_service_row`, imported inside the function because
      `users` imports `UserNotFound` from `auth`) rather than restating it.
  - **The point-of-use gate is necessary and not sufficient, because the
    credential can change kind under it — the mint is where that is closed.**
    Rule 1 judges the credential in hand. `verify_token` accepts keys, so
    `refresh_token` handed one back an *ordinary session token* — a different
    credential, of a different kind, reporting `is_api_key = False` — and
    deleted the key. Three consequences, and the design asserted none of them
    could happen because it had reasoned about the *lookup* rather than its
    callers ("`verify_token` changes in exactly two places, and it is the only
    place either change is needed"). Revocation stopped being terminal
    (`revoke_key` and the panel's Revoke button both key on
    `api_key_name IS NOT NULL`, so the operator saw a keyless bot and was
    offered nothing to revoke); Rule 1 itself fell for any service row carrying
    `is_admin`, the laundered token walking through `require_admin()` and able
    to mint further keys; and a well-behaved bot doing the documented refresh
    destroyed its own unrecoverable credential.
    - The guard is therefore in **`auth.issue_token`**, whose INSERT is an
      `INSERT … SELECT` filtered on `is_service IS FALSE`, raising the named
      `SessionCredentialRefused` (a `ValidationFailed`, so `/v1` answers 400)
      when it matches no row — never a bare `assert`, which vanishes under
      `python -O`. It has exactly two production callers, `login` (already
      closed by Rule 2) and `refresh_token`, so one guard closes both **by
      construction** and a third caller cannot rediscover the hole. Adding an
      `is_api_key` check inside `refresh_token` alone was rejected for that
      reason. Same one-authority call as `login_eligible_sql`.
    - `refresh_token` **also** refuses a key up front, so a bot gets "API keys
      do not expire and must not be refreshed" rather than a message about
      minting. The mint guard is what makes it safe; this is what makes it kind.
    - **`logout` refuses a key too.** It deletes the presented row, so a
      generic client's shutdown-logout silently destroyed an unrecoverable
      credential — the same class of surprise, and the reason it is a refusal
      rather than a documented self-revoke path. Retiring a key is an
      administrator's act (`revoke_key`), which is also the only one that
      leaves the principal and its grants standing for a re-key.
    - **`revoke_key` sweeps every token the principal holds**, not just the
      `api_key_name IS NOT NULL` row. Under the 1:1 model a service user holds
      zero or one credential and it is always a key, so anything else there is
      a laundered token. Migration 0036 and `issue_token`'s guard shipped
      together, so no upgrading archive can hold one and the sweep is defence
      in depth — note the panel's
      Revoke button is gated on `has_key` and would not offer it in that state,
      while the CLI and the JSON route both do. Its `is_service` predicate is
      load-bearing for the
      same reason `delete_key_principal`'s is: without it, sweeping becomes a
      second, unguarded way to cut off a *person's* sessions.
  - **Rule 2 — a service user cannot log in.** Four lookups verify a password
    against `api_users` (`api.auth.login`, `api.admin.auth.authenticate_admin`,
    `serve.oauth.consent_router`'s inline consent check, and
    `api.auth.change_password`), and they carried the `disabled_at IS NULL`
    wording by copy. The pure
    [src/localmail/api/login_eligible_sql.py](src/localmail/api/login_eligible_sql.py)
    is now the one authority, adding `is_service IS FALSE`. The unusable random
    password hash is *not* the protection — `users.set_password` is one admin
    click away from making it usable, which is why that too refuses a service
    row.
    - **The consent-router site had no behavioural test, and the reason given
      for that was false.** It was pinned by an AST check that the route
      *calls* `login_eligible_sql`, which a mutation calling it and discarding
      the result satisfies — the whole file stayed green, while the same
      mutation on `api.auth.login` failed. The comment said driving the real
      route needs a full PKCE client; `test_serve_oauth_consent.py` already
      POSTs `/oauth/consent` end to end, and that is where the pin lives now.
      Honouring the consent mints an authorization code, which exchanges into
      an access + refresh pair for a principal that must never hold one.
  - **The mint guard covers two writers, not one.** `issue_token`'s docstring
    claimed to be "the one place a session credential comes into existence";
    `mcp.oauth.access.mint_access` is the other, reaching `api_tokens` with a
    bare `user_id`. It was closed only by Rule 2's consent gate three modules
    away — defence in depth, not the construction the claim needs — so it
    carries the same `is_service IS FALSE` predicate now.
    - **`access.revoke_access` is hardened for the reason `logout` is.**
      `verify_token` accepts keys, so `load_access_token` resolves one and the
      OAuth revocation endpoint reaches it, destroying an unrecoverable
      credential on a machine client's routine shutdown. The SDK's
      `token.client_id == client.client_id` check blocks it today only because
      DCR ids are `uuid4` and a key resolves to the `localmail` sentinel — a
      coincidence of two constants. The `api_key_name IS NULL` predicate is the
      rule.
  - **CSRF on `/v1/admin/api-keys` was unexercised, on every route.** Every
    test there authenticates with a *bearer*, and `check_csrf` returns
    immediately for bearer — so replacing all four calls with `pass` left the
    file green, on a cross-site key-mint and key-delete surface reachable with
    the admin session cookie. The pins are cookie-driven now, including the
    method binding (#122), matching `test_serve_admin_users.py`.
  - **A key that no longer authenticates must not read "active".**
    `set_disabled` and `revoke_sessions` both kill a key through
    `credential_valid_sql`, and neither is refused — both are legitimate
    operator actions. What was wrong is that the panel rendered only `has_key`,
    so the operator saw "active" while the bot got a bare 401 with nothing to
    diagnose. `ApiKeySummary` carries `disabled` and `revoked` **separately**,
    because the remedies differ (re-enable the principal; revoke and re-mint,
    a key being unrecoverable). The two are a hand-maintained restatement of
    `credential_valid_sql`'s halves, pinned against `verify_token` by
    `test_reported_state_matches_whether_the_key_verifies` — the
    `ALLOWLISTED_WHERE_SQL` arrangement.
  - **`CreatedKey.raw_key` is `field(repr=False)`.** It is the credential's
    only plaintext existence, and the default dataclass repr renders it in
    full — so one `logging.info("%s", created)`, a debugging `{{ created }}`,
    or a frame-locals error reporter leaks it with nothing failing. The four
    call sites are disciplined; this is what makes discipline unnecessary.
  - **Revoke and delete are separate operations, deliberately.** `revoke_key`
    drops the token and keeps the principal, so re-minting under the same name
    restores service with the grants intact — that is the rotation path, and it
    is why `list_keys` is driven from `api_users` rather than `api_tokens` (a
    revoked bot holds no token row and must stay visible).
    `delete_key_principal` removes the bot; its `is_service IS TRUE` predicate
    is load-bearing, since the route is addressed by user id and would
    otherwise become a second way to delete a person.
  - **`create_key` runs in one transaction.** A failure after the principal is
    created would leave a row that the operator's retry then collides with.
    Its check-then-INSERT is still not atomic, so a concurrent mint (a
    double-clicked Create) loses at the partial unique index; that
    `UniqueViolation` maps to `ApiKeyFieldError` rather than escaping as a 500
    that bypasses the routers' 400 contract and reads as an inert button.
    **Its name validation had nothing behind it** — deleting the
    `api_key_name_error` call left all 44 API-key tests green, because the
    panel's own blank check answers one layer up and neither the JSON route
    (`name: str`, no `min_length`) nor the CLI validates at all.
  - **A service row is visible as one wherever users are listed.**
    `UserSummary`/`UserDetail` carry `is_service`, `/admin/users` badges it,
    and `localmail list-api-users` marks it `[service]`. They stay **listed** —
    hiding them trades one false impression for another — but the two controls
    that dead-end at `reject_service_row`'s 400, Reset password and Promote,
    render disabled through `action_flags`. **Promote reached the wire as a
    500 until the routers learned to catch it**: `UserFieldError` is a bare
    `ValueError`, and while `POST /users/{id}/password` mapped it to 400,
    `PATCH /v1/admin/users/{id}` and the panel's `admin-toggle` caught only
    `UserNotFound`/`LastAdminError` — so the guard `action_flags`' own
    docstring calls "UX only; not enforcement" was an unhandled exception on
    the path that carries it, and under HTMX that is #148's inert button. Its `is_service` parameter is
    keyword-only with no default (#234's shape): `False` re-enables both, so
    it must not be reachable by forgetting to write it.
  - **The panel's account checklist adds; it never replaces.** Re-keying an
    existing bot only ever grants (`create_key`'s reuse branch is additive by
    design, so re-keying cannot silently narrow), while a form of unticked
    boxes reads as a replacement. The fix is a note under the checklist, not a
    grants-editing route — take a grant away with `localmail revoke-account`.
- **Admin session revocation (#113)**: migration
  `0022_api_users_sessions_invalidated_at.sql` adds a nullable
  `sessions_invalidated_at TIMESTAMPTZ` column on `api_users`. The
  admin-cookie dependency (`localmail.serve.admin.dependencies.require_admin_session`)
  passes the session token's `issued_at` into `get_admin_user`; the
  service does `to_timestamp(issued_at) < sessions_invalidated_at`
  in the same SELECT and raises `SessionInvalidated` when the token
  predates the revocation moment — translated to a 303 redirect to
  `/admin/login`. NULL means "never revoked" and is the default.
  Operators bump the column shell-side via
  `localmail revoke-admin-sessions USERNAME`; admin privileges are
  untouched (use `revoke-admin` for that). The **cookie** check is
  opt-in: callers that don't pass `issued_at` (CLI lookups, smoke
  paths) skip the comparison entirely so they keep working on a
  revoked user. The bearer and refresh checks above are *not* opt-in
  — they are unconditional predicates in the token lookup itself.
- **DB-canonical accounts + admin CRUD (Sub-plan 2A)**: migration
  `0020_accounts_canonical.sql` makes `accounts` the write-authoritative
  store for IMAP configuration — adding `folder_allow`, `folder_deny`,
  `folder_deny_flags` (RFC 6154 flag-based denial), `sync_enabled`,
  `updated_at`; lifting NOT NULL from `imap_host`/`imap_port`; extending
  `auth_method` to include `'archive'`; and adding the
  `accounts_live_requires_host` check constraint so live accounts always
  carry a host. The v1 daemon does not yet honour `sync_enabled` (deferred
  to Sub-plan 2A.2 along with TOML→DB seed and CLI rewiring). The service
  layer in
  [`src/localmail/api/admin/accounts.py`](src/localmail/api/admin/accounts.py)
  exposes `list_accounts`, `get_account`, `create_account`,
  `update_account`, `delete_account` (cascade-or-refuse: refuses when
  messages exist unless `force=True`), `store_password`,
  `clear_secret`, and `probe_connection` (renamed from `test_connection`
  to avoid pytest auto-collection). The web OAuth flow for Gmail accounts
  lives in
  [`src/localmail/api/admin/oauth.py`](src/localmail/api/admin/oauth.py)
  — `start_oauth` returns a Google consent URL and writes a stateless
  HMAC-signed state token via
  [`src/localmail/api/admin/oauth_state.py`](src/localmail/api/admin/oauth_state.py)
  (`encode_state`/`decode_state`: JSON payload + `base64url(hmac_sha256(key,
  payload))`); `complete_oauth` verifies the state, exchanges the code,
  and persists the refresh token — closes #114 (`[serve].state_signing_key`
  now has a real consumer). HTTP routes for CRUD + password + test-connection
  live under `/v1/admin/accounts` (the test-connection URL keeps the
  `test-connection` name for API consistency even though the Python function
  is `probe_connection`); OAuth routes are `POST
  /v1/admin/accounts/{id}/oauth/start` and `GET /admin/oauth/callback`. The
  callback reads `state`/`code` via `get_unscrubbed_query_params(request)`
  because `ScrubSensitiveQueryParamsMiddleware` would otherwise redact them.
  Cookie `Path` is `"/"` — required so the admin session cookie reaches
  `/v1/admin/*` routes; SameSite=Lax + per-route CSRF tokens
  (`X-CSRF-Token` header) remain the primary CSRF defences. The JSON-router
  CSRF token is bound to `(user_id, "<METHOD>:<action-url>")` —
  `check_csrf` derives the method from `request.method` via
  `serve/admin/csrf.py::csrf_action`, so a token minted for `PATCH` on a
  shared URL can't be replayed against `DELETE` (#122). No `/v1/*` machine
  endpoint reads cookies (machine clients use `Authorization: Bearer …`),
  so the broader cookie scope adds no smuggling surface — pinned by
  [tests/test_session_cookie_scope.py](tests/test_session_cookie_scope.py),
  which walks the FastAPI dependant tree and fails if any non-`/v1/admin/*`
  route under `/v1/` reads the session cookie or depends on
  `require_admin_session` (#121). The OAuth flow's
  `gmail_oauth.client_secrets_file` is threaded in from
  `app.state.gmail_client_secrets_file` (set by `create_app`'s
  `gmail_client_secrets_file=`) — the service layer never calls
  `load_config()` per request (#120). When that path is unset,
  `oauth.py::_build_flow` raises `OAuthNotConfigured` (a
  `RuntimeError` subclass), which `oauth_start` maps to a clean
  **503** "Gmail OAuth is not configured" rather than letting a bare
  `RuntimeError` escape as a 500 (#126); the callback's broad
  `except Exception` already catches it as a failed-redirect.
  Account-row reads use psycopg
  `class_row` (name-based column→field mapping, not positional unpack), and
  `AccountInUse` subclasses `ValueError` like its sibling
  `AccountFieldError` (#119, #123).
- **TOML→DB seed (Sub-plan 2A.2 slice 1, shipped):** `init-db` now merges
  `config.toml` `[[accounts]]` into the `accounts` table after migrations
  apply — idempotent, keyed by `name`, **DB-canonical** (existing rows are
  never overwritten; a drifted TOML value logs a WARNING naming the fields
  and is otherwise ignored). Implemented as a pure planner
  (`account_seed.plan_account_seed`) + thin IO wrapper
  (`account_seed.seed_accounts`, inserting via `create_account` to reuse
  validation, reading existing rows via the new public
  `api.admin.accounts.list_accounts_full`); `init-db` echoes
  `seeded accounts: inserted=N skipped=M drifted=K` and maps a malformed
  block's `AccountFieldError` to a clean non-zero `ClickException` (whole
  seed runs in one uncommitted transaction, so a failure leaves no partial
  rows). **Sub-plan 2A.2b shipped (DB-canonical daemon):** the daemon now
  reads its account set from the `accounts` table — `Daemon.__init__`
  enumerates live, `sync_enabled` accounts via
  `api.admin.accounts.list_syncable_accounts` and maps each row to an
  `AccountConfig` through the pure `daemon_accounts.account_config_from_row`
  adapter (archive + `sync_enabled = FALSE` rows spawn no threads — 2A.2c
  folded in). The DB `account_id` is carried on `WorkerContext`, so the
  IDLE/poll workers use `ctx.account_id` and no longer call `upsert_account`.
  Per-account `poll_seconds` TOML overrides are no longer honoured
  by the daemon (no DB column); the daemon-wide `cfg.daemon.poll_seconds`
  applies to every account. **The account set is read once at `Daemon.__init__`**
  (a one-shot `psycopg.connect`, before the pool opens, since pool sizing
  depends on the count) — admin-UI/CLI account changes take effect on the next
  daemon restart, not live; hot reload is deferred to daemon control (2B).
  **Sub-plan 2A.2d shipped (DB-canonical CLI):** `list-accounts`,
  `add-account`, `oauth-login`, `remove-account`, and the one-shot `localmail
  sync` now read/write the `accounts` table via `api.admin.accounts` instead of
  `cfg.accounts`. `sync.py:upsert_account` is **deleted** (no callers remain);
  `sync.sync_account` now takes an explicit `account_id: int` resolved by the
  caller (it never creates the account row). `add-account` / `oauth-login`
  resolve a name via the pure `cli_account_resolve`
  (`Found`/`SeedThenUse`/`NotFound`); a name absent from the DB but present in
  `config.toml` is seeded via `create_account` + the shared
  `account_seed.account_create_kwargs` mapping (CLI helper
  `cli._resolve_account_row`). `remove-account` is **secrets-only by default**
  (DB row untouched, back-compat); `--delete-row` deletes the row, `--force`
  cascades when messages reference it. One-shot `sync` (bare) iterates
  `list_syncable_accounts` like the daemon; `--account NAME` resolves via
  `get_account_by_name` and syncs even a paused (`sync_enabled = FALSE`)
  account, rejecting archive accounts. `backfill-internal-date` remains
  TOML-driven (`_account_or_die`) — out of 2A.2d scope. No new migration.
- **`sync_enabled` CLI setter (follow-up to 2A.2):** `enable-account NAME` /
  `disable-account NAME` toggle `accounts.sync_enabled` via the pure planner
  `cli_sync_toggle.plan_sync_toggle` (reject / noop / apply). Name resolution is
  DB-only (no TOML seed — toggling presupposes the row exists); archive rows are
  rejected (the daemon never syncs them); an account already in the target state
  is a no-op that leaves `updated_at` untouched. Both commands share the
  `cli._apply_sync_toggle` helper, which only calls `update_account` on the
  `apply` branch. No new migration (`sync_enabled` ships in `0020`).
- **Account CRUD admin screens (Sub-plan 2A.3, shipped — closes #125):**
  server-rendered HTMX screens at `/admin/accounts` for list, create, edit,
  delete, store-password, test-connection, enable/disable sync, and Gmail OAuth
  "Connect". Code: thin HTML router
  [`serve/admin/accounts_panel_router.py`](src/localmail/serve/admin/accounts_panel_router.py)
  (~330 lines) + pure form logic
  [`serve/admin/account_forms.py`](src/localmail/serve/admin/account_forms.py)
  (unit-tested in isolation via `tests/test_account_forms.py`). Templates under
  `serve/admin/templates/accounts/` (`list.html`, `form.html`,
  `_form_fields.html`, `_row.html`, `_test_result.html`, `_secret_status.html`,
  `_delete_confirm.html`);
  auth-method field toggle in the served static file
  [`serve/admin/static/accounts-panel.js`](src/localmail/serve/admin/static/accounts-panel.js)
  (CSP `script-src 'self'`, no inline JS). Each mutating action carries a
  **method-bound** CSRF token via `csrf_token_for_method` — the explicit closure
  of #125 (the shared mint from 2B.5 now has its first non-daemon consumer).
  Backend change: `probe_connection` now supports `oauth2` accounts — threads
  `gmail_client_secrets_file` into the existing XOAUTH2 path; a missing refresh
  token surfaces as a clean `AccountFieldError` (→ inline error fragment), never a 500.
  Validation errors render **inline beside the offending field** (`_form_fields.html`,
  HTTP 400 + HTMX swap); successful create/update returns `HX-Redirect` to the
  edit page. On OAuth completion the callback redirects to
  `/admin/accounts/{id}?oauth=success`. Archive accounts are rejected by
  test-connection (same as before). **No new migration** (reuses `sync_enabled`
  from `0020`).
- **Friendly test-connection failures (#158, resolved):** a *genuine* connect
  failure (wrong host/port/password, DNS, TLS) raises `OSError` /
  `imaplib.IMAP4.error` / `imapclient.exceptions.IMAPClientError`, which used to
  escape both `probe_connection`'s narrow `except RuntimeError` and the routes'
  `except AccountFieldError` as a **500**. The classification tuple
  `accounts.CONNECT_FAILURE_EXC_TYPES` names exactly those types and lives next
  to `probe_connection`; the service still does **not** catch it (its contract is
  to raise on connect failure — the broadening is deliberately **at the
  transport routes**). The HTML route
  (`accounts_panel_router.py::test_connection`) renders the `_test_result.html`
  error fragment (HTTP 200, `ctx["error"]`); the JSON `/v1` route
  (`accounts_router.py::test_connection`) mirrors it as a clean **400** with the
  error detail (uniform with the existing `AccountFieldError → 400` mapping).
  Both paths keep `probe_connection`'s builtin transient-classification
  narrowness intact.
- **User-management admin screens (Sub-plan 2A.4, shipped):** server-rendered
  HTMX screens at `/admin/users` + a JSON `/v1/admin/users` router, sharing one
  service layer
  [`src/localmail/api/admin/users.py`](src/localmail/api/admin/users.py):
  list/create/delete users, per-account ACL grant/revoke (a checklist over every
  account on the edit screen), `is_admin` toggle, admin session revocation,
  admin password reset (no old password), and enable/disable (`disabled_at`).
  Two lock-out guards: the **count-based last-admin** rule lives in the service
  (the pure `would_orphan_last_admin` predicate + an IO wrapper reading
  `count(*) WHERE is_admin IS TRUE AND disabled_at IS NULL`; raises
  `LastAdminError`), and the **identity-based self-action** rule (no self-demote,
  no self-delete) lives in the routers (compared `uid == admin.id`, returns
  **409**). Both guards map to **409** (mirroring the accounts cascade-refuse
  409); validation maps to **400**. The edit screen also renders unsafe controls
  `disabled` server-side via `action_flags` — UX only; a hand-crafted POST still
  hits the guards. Pure form logic in
  [`serve/admin/user_forms.py`](src/localmail/serve/admin/user_forms.py)
  (unit-tested in `tests/test_user_forms.py`). Method-bound CSRF throughout (a
  PATCH token can't replay on DELETE). **No new migration** — reuses
  `is_admin`/`disabled_at`/`sessions_invalidated_at` + `user_accounts` (0016).
  Closes the `/admin/users` 404.
- **Imports admin screens (Sub-plan 2A.5, shipped):** server-rendered HTMX
  screens at `/admin/imports` + a JSON `/v1/admin/imports` router, sharing the
  service layer
  [`src/localmail/api/admin/imports.py`](src/localmail/api/admin/imports.py):
  list/create/cancel import jobs, per-job status, and `reconcile_orphaned_jobs`
  (called at serve startup to move any `running` jobs left over from a crash into
  `failed`). Closes the last 404 admin nav link (`/admin/imports`).
  The new `src/localmail/importer/` package contains:
  `paths.py` (`resolve_import_path` — config-allowlist guard using `realpath`;
  empty `roots` = imports disabled, raises `ImportNotAllowed`),
  `sources.py` (`iter_mbox`/`iter_maildir` → `ImportedMessage` named-tuples;
  received-date from the mbox `From_` envelope line for mbox sources, maildir
  file mtime for maildir sources),
  `job_state.py` (pure predicates `is_stale`/`is_terminal`/`should_checkpoint`),
  `runner.py` (`run_import` — streams a source through `sync.process_one_message`
  with per-message SAVEPOINT isolation, periodic progress flush +
  `last_progress_at` heartbeat, cooperative cancel via the `cancel_requested`
  column, and guaranteed terminal status write on exit).
  Migration `0026_import_jobs.sql` adds the `import_jobs` table (columns:
  `id`, `account_id`, `source_kind`, `source_path`, `status`, `inserted`,
  `skipped`, `failed`, `error_msg`, `created_at`, `last_progress_at`) plus a
  partial unique index `ON import_jobs ((TRUE)) WHERE status IN ('pending','running')`
  — the single-active busy-guard that prevents two concurrent imports.
  Imports target a pre-created **archive** account (dropdown on the create form);
  the service layer is admin-global (NOT per-user ACL-scoped, consistent with the
  accounts and users admin services). Source paths must reside under a directory
  in `[imports].roots` (empty = imports disabled); paths are resolved server-side
  only. Received date from the source (mbox envelope / maildir mtime) is stored
  in `messages.internal_date`. Three-layer mid-import failure visibility: runner
  sets terminal `failed` + `error_msg` on unhandled errors; `last_progress_at`
  stall detection (panel shows red past `[imports].stale_seconds`); and
  `reconcile_orphaned_jobs` at serve startup clears any `running` rows from a
  prior crash. The import worker runs in-serve as a plain thread started by
  `start_job`; `localmail import <path> --account NAME --kind {mbox,maildir}`
  invokes the same `run_import` synchronously from the CLI. Re-import is
  idempotent — already-imported messages are skipped via the existing per-account
  Message-Id / raw-SHA256 dedup. **Migration `0026_import_jobs.sql`** (2A.5).
  **Checkpoint cadence (#163, resolved):** the runner used to flush progress +
  poll cancel only on `c.processed % checkpoint_every == 0`, so a sub-`checkpoint_every`
  import showed `0/0/0/0` until the terminal write and its Cancel button was inert,
  and a small-count-but-slow import (few huge attachments) was unresponsive for a
  long time. The flush/poll decision now lives in the pure predicate
  `job_state.should_checkpoint(processed, processed_at_last_checkpoint,
  seconds_since_checkpoint, checkpoint_every, checkpoint_seconds)`, which fires on
  three independent triggers: the **first** processed message (immediate progress +
  cancellability), the **count** cadence (`checkpoint_every`, unchanged), and a new
  **time** cadence (`[imports].checkpoint_seconds`, default 2 — decouples
  responsiveness from per-message cost). `<= 0` disables a cadence; the first-message
  flush always fires. `run_import` tracks `processed_at_last_checkpoint` +
  `last_checkpoint_at` and takes an injectable `clock` (default `time.monotonic`)
  so the time branch is deterministically unit-tested. `checkpoint_seconds` threads
  from config through `start_job` and all three callers (CLI, JSON router, HTML panel
  router). No new migration.
  **Concurrent-CLI-safe reconcile (#162, resolved):** `reconcile_orphaned_jobs`
  ran at serve startup and flipped **every** active (`pending`/`running`) row to
  `failed`, on the assumption that an active row could only be an orphaned
  in-serve worker thread. But `localmail import` runs the same `run_import`
  **synchronously in a separate process** with its own `running` row — so a serve
  restart mid-CLI-import clobbered the live job's status *and* released the
  single-active busy-guard (`import_jobs_single_active_uniq`), opening a window
  for a panel-initiated import to run concurrently. Migration
  `0027_import_jobs_owner.sql` adds nullable `owner_host` / `owner_pid`, recorded
  at `create_job` time — the creating process is the running process for both the
  CLI (one process) and the in-serve panel (the worker thread runs in the serve
  process), so `os.getpid()` at create is the pid whose liveness reconcile must
  check. `reconcile_orphaned_jobs(conn, *, current_host=None, pid_alive=...)` now
  selects active rows and reaps one only when its owner is verifiably gone, via
  the pure predicate `importer/ownership.py::should_reap` (reap iff `owner_pid IS
  NULL` — legacy/never-started; else keep when `owner_host != current_host`; else
  reap iff `not pid_alive`). `pid_is_alive` is the single liveness syscall
  (`os.kill(pid, 0)`), isolated so `should_reap` stays pure and unit-tested;
  `current_host` / `pid_alive` are injectable for deterministic DB tests. A live
  CLI import (pid alive) now survives a serve restart, keeping the busy-guard
  held; orphaned serve **and** CLI jobs (pid dead) are still reaped. **Accepted
  limitation:** pid reuse can rarely keep a dead job's row until the next restart
  (self-heals; low probability on single-host). **Migration
  `0027_import_jobs_owner.sql`** (#162).
- **DaemonSupervisor + HTTP + CLI (Sub-plan 2B.4, shipped):** two control
  planes for the sync daemon. **Plane B** (process lifecycle) lives in
  [src/localmail/serve/daemon_supervisor.py](src/localmail/serve/daemon_supervisor.py):
  `DaemonSupervisor` owns `localmail run` via `subprocess.Popen`
  (`start`/`stop`/`restart`/`status`/`recent_log_lines`), a state machine
  `stopped → starting → running → stopping → stopped` with `crashed` for an
  unexpected child exit (detected by the stdout reader thread hitting EOF while
  state is still `running`), and a bounded ring buffer (`deque(maxlen)`) of the
  child's combined stdout/stderr. `stop()` is SIGTERM → wait
  `daemon.shutdown_grace_seconds` → SIGKILL, and deliberately **releases the
  lock before waiting** so the reader thread can never deadlock against the
  grace wait. `ExternalDaemonSupervisor` is the stub for
  `[serve] supervise_daemon = false` (systemd deploy): `status()` reports
  `external`; lifecycle ops raise `SupervisorUnavailable`. Pure helpers
  (`resolve_runtime_dir`, `socket_path`, `default_daemon_argv`,
  `status_to_dict`) are shared by serve + CLI so both derive the same socket
  path / launch argv / wire shape. The child is launched as
  `python -m localmail run` (portable — `src/localmail/__main__.py` shim, no
  PATH dependence). The **control socket**
  ([src/localmail/serve/daemon_control_socket.py](src/localmail/serve/daemon_control_socket.py))
  is newline-delimited JSON over a Unix socket at
  `${runtime_dir}/localmail-supervisor.sock` (mode 0600): `handle_control_request`
  is a pure dispatcher (supervisor in, dict out, never raises),
  `ControlSocketServer` wraps it with an accept loop, `send_control_request` is
  the client half the CLI uses. `create_app` builds the supervisor on
  `app.state.daemon_supervisor` (real when `supervise_daemon`, stub otherwise)
  **side-effect-free** — the child spawns only on an explicit `start()`, and the
  control socket binds only in the lifespan when `enable_control_socket=True`
  (the `serve` CLI path), so TestClient apps never bind a shared socket. HTTP
  routes ([src/localmail/serve/admin/daemon_router.py](src/localmail/serve/admin/daemon_router.py),
  admin-gated, method-bound CSRF): `GET /v1/admin/daemon` fuses supervisor
  process state + `daemon_heartbeats` + recent log (`supervise_daemon_externally`
  derives from the supervisor's own `state == external`, not config, so a
  swapped stub reports correctly); `POST /v1/admin/daemon/{start,stop,restart}`
  (Plane B; 409 on the external stub); `POST /v1/admin/daemon/reload` and `POST
  /v1/admin/accounts/{id}/restart-sync` (Plane A → `enqueue_command` reusing 2B.3,
  not re-implemented; restart-sync 404s an unknown account before enqueue). CLI
  ([src/localmail/daemon_cli.py](src/localmail/daemon_cli.py), registered via
  `main.add_command(daemon_group)`): `localmail daemon {status,reload,restart-account}`
  work against the DB planes even when externally supervised;
  `{start,stop,restart}` go over the socket and exit non-zero with a clear note
  when `supervise_daemon=false` (external) or the socket is unreachable (serve
  not running). `status` always prints heartbeats; an unreachable socket is
  reported, not a failure. **No new migration** (reuses 0023 heartbeats + 0024
  commands).
- **Async lifecycle + admin panel (Sub-plan 2B.5, shipped — closes the 2B arc):**
  lifecycle ops no longer block a request/socket worker (#146). `DaemonSupervisor`
  grows `request_start()`/`request_stop()`/`request_restart()` that set the
  **transitional** state synchronously (`starting`/`stopping`) under `_lock`,
  then run the existing blocking `start()`/`stop()`/`restart()` body on **one
  dedicated lifecycle thread**; a second lifecycle op while one is in flight
  raises `SupervisorUnavailable` (the **busy-guard**, keyed on
  `_lifecycle_thread.is_alive()`, not state). The blocking variants stay (used by
  `close()` on serve shutdown — teardown must block — and by tests).
  `ExternalDaemonSupervisor` has matching `request_*` stubs that raise.
  `DaemonSupervisorT = DaemonSupervisor | ExternalDaemonSupervisor` is the shared
  param type. HTTP `POST /v1/admin/daemon/{start,stop,restart}` now call
  `request_*` and return **202** with the transitional status; the busy-guard /
  external stub both surface as **409**. The control socket dispatcher and the
  `localmail daemon {start,stop,restart}` CLI likewise use `request_*`; the CLI
  **polls `status` until the op settles** (`running`/`stopped`) — `--no-wait`
  skips the poll. CLI poll constants live in `daemon_cli.py`
  (`_LIFECYCLE_POLL_INTERVAL_S`, `_START_SETTLE_TIMEOUT_S`, reuses
  `_LIFECYCLE_TIMEOUT_BUFFER_S` + `_STATUS_TIMEOUT_S`). The GET-route fusion is
  extracted into `daemon_router.build_daemon_view(supervisor, conn, *,
  stale_seconds)` — the single source shared by the JSON route and the HTML
  panel. **Admin panel** at `/admin/daemon`
  ([src/localmail/serve/admin/daemon_panel_router.py](src/localmail/serve/admin/daemon_panel_router.py),
  mounted at `/admin`): a full page + a self-polling HTMX partial at
  `/admin/_partials/daemon-status` (the `#daemon-status` div re-carries its
  `hx-get`/`hx-trigger="every {{DAEMON_PANEL_POLL_SECONDS}}s"` after each
  `outerHTML` swap). Status table is red past `heartbeat_stale_seconds` (server
  `stale` flag, no client clock); lifecycle buttons are **disabled when
  `supervise_daemon_externally`**; Plane-A reload + per-account restart-sync
  buttons stay enabled. Each mutating control carries its own **method-bound**
  CSRF token via the reusable
  [serve/admin/csrf.py](src/localmail/serve/admin/csrf.py)`::csrf_token_context`
  helper (returns `csrf_token_for` legacy single-arg + `csrf_token_for_method`
  — the latter is the shared #125 mint, consumed by the account screens in 2A.3).
  Restart-sync buttons are deduped per account (idle+poll workers share one).
  The `/v1/admin/*` endpoints stay pure machine-JSON (no HTMX content
  negotiation); the panel polls the dedicated HTML partial. **No new migration.**
  **2B.5 follow-ups resolved (#148, #149):** the panel's mutating buttons use
  `hx-swap="none"`, so a rejected control (busy-guard **409**, CSRF **400**)
  used to look inert; the served static
  [admin/static/daemon-panel.js](src/localmail/serve/admin/static/daemon-panel.js)
  now binds an `htmx:afterRequest` listener (filtered to `verb === "post"` so
  the 2s status poll doesn't toast) that surfaces a transient toast in the
  `#daemon-toast` region. That region lives in `daemon/panel.html` **outside**
  the self-swapping `#daemon-status` fragment so the poll's `outerHTML` swap
  can't wipe an in-flight message. The JS is a served file (not inline / not an
  htmx `hx-on::`) because the `/admin` CSP is `script-src 'self'` with no
  `unsafe-inline`/`unsafe-eval`. **#149:** `DaemonSupervisor.close()` sets a
  `_closing` flag under `_lock` before its blocking `stop()`, and `start()`
  checks it under `_lock` as the single spawn chokepoint — so an async
  `request_restart` caught between its `stop()` and `start()` halves at serve
  shutdown can no longer re-spawn an orphaned child. The flag-set and the spawn
  are serialised by `_lock`: start() either sees the flag and skips, or spawned
  first and close's stop() reaps it.
- **Supervisor lifecycle robustness (#221 A–E, shipped):** five defects sharing
  the supervisor/shutdown area.
  - **A — the two shutdown budgets were the same number meaning different
    things.** `run_forever`'s teardown joined every thread with its *own*
    `shutdown_grace_seconds` timeout — idle then poll per account, sequentially —
    so the real worst case was `2 × accounts × grace`, while
    `DaemonSupervisor.stop()` waited exactly one `grace` before SIGKILL. With two
    or more accounts an ordinary stop or restart **SIGKILLed a healthy child**.
    The new [src/localmail/shutdown_budget.py](src/localmail/shutdown_budget.py)
    owns both halves so they cannot drift apart again: `wind_down_threads` sets
    **every stop event before the first join** (the load-bearing part — signalled
    up front the workers wind down concurrently, so the budget bounds the
    *slowest* rather than their sum) and spends the budget as one wall-clock
    deadline via the pure `remaining_seconds`; `supervisor_kill_after` derives the
    supervisor's kill deadline as the child's budget + `SUPERVISOR_KILL_MARGIN_S`,
    covering the fixed work *after* the last join (pool close, final log line,
    interpreter teardown). `remaining_seconds` clamps at 0 because
    `Thread.join(timeout=<negative>)` returns immediately rather than raising —
    a negative remainder would silently skip every remaining join while looking
    like a wait. **`Daemon._teardown_account` deliberately keeps its own
    per-account timeout**: that path removes *one* account from a daemon that
    keeps running, so it has no global deadline to share. `wind_down_threads`
    lives beside the arithmetic rather than in `daemon.py` because it is the sole
    consumer of `remaining_seconds` and the counterpart to
    `supervisor_kill_after` — splitting them is how the budgets drifted apart in
    the first place.
  - **B — `supervisor.close()` blocked the asyncio event loop** for up to the
    grace period on serve shutdown; now `await anyio.to_thread.run_sync(...)`.
    Nothing was scheduled during that wait, and a process supervisor that
    SIGKILLed the parent mid-wait orphaned the already-SIGTERMed child. Pinned by
    a source assertion in `test_serve_shutdown_not_blocking.py` (building a real
    app needs a DB) plus a demonstration of the property on the same primitive.
  - **C — `request_*` after `close()` stuck the state machine at `starting`
    forever.** `request_start` set STARTING, then the background `start()` saw
    `_closing` and returned *without* touching the state, so the admin panel
    showed a daemon that was never coming. All three `request_*` now refuse via
    the shared `_admit_lifecycle_request` guard **before any state is written**.
    The blocking `start()` **keeps its silent no-op** — #149's guard is what an
    in-flight async restart lands on during teardown and it must not raise there.
  - **D — `send_control_request` wrapped only `connect()`.** A peer that accepted
    and then stalled raised a bare `socket.timeout`; one that hung up mid-write a
    `BrokenPipeError`. Both are `OSError`, both escaped `daemon_cli.py`'s
    `except ControlSocketError`, and both showed the operator a traceback. The
    **whole exchange** is wrapped now.
  - **E — control-socket bind/chmod TOCTOU.** `bind()` ran before
    `os.chmod(path, 0o600)`, so the socket briefly existed at whatever the process
    umask allowed — and anything that connects through it can stop the daemon.
    Bind now runs under a private umask, restored in `finally` (including on bind
    failure — the umask is process-global, and leaking `0o177` would silently make
    every later file the serve process writes owner-only). The chmod stays as belt
    to that braces.
- The page cache namespaces cursors by `user_id` so a search cursor minted
  by user A and replayed by user B is treated as a cache miss — preventing
  cross-user pool leakage.
- TLS is on by default; `--no-tls` is only accepted with `--bind 127.0.0.1`.
- The HTTP server and the sync daemon never call each other — they share
  Postgres and can run independently.
- **Attachment download policy (#32 phase 1)**: `/v1/attachments/{sha256}`
  always emits `Content-Disposition: attachment` with both the legacy
  ASCII `filename=` and the RFC 5987 `filename*=UTF-8''…` form, so the
  browser is forced into a download (never inline render — the XSS sink
  in stored HTML/SVG blobs). MIME types in `_INLINE_RISKY_MIMES`
  (`text/html`, `application/xhtml+xml`, `image/svg+xml`, `text/xml`,
  `application/xml`) are clamped to `application/octet-stream` on the
  wire as defense in depth (the DB row is untouched). These invariants
  apply to **every** response — full GET, 206 Partial Content, *and*
  416 — so a proxy or client can never be tricked into rendering a
  ranged slice inline.
- **Range support (#54, phase 2 of #32)**: `/v1/attachments/{sha256}`
  advertises `Accept-Ranges: bytes` and honours `Range: bytes=…` per
  RFC 9110 §14.1. Parsing lives in
  [`src/localmail/api/range_requests.py`](src/localmail/api/range_requests.py)
  as a pure module (no IO, no FastAPI) so it's reusable by future
  transports (MCP, etc.). Contract:
    - Single closed range (`bytes=0-9`), open-ended (`bytes=10-`), and
      suffix (`bytes=-10`) → 206 with `Content-Range: bytes start-end/total`.
    - End past EOF is **clamped** to `size - 1` (RFC 9110 §14.1.2).
    - Start past EOF or suffix-of-0 → 416 with `Content-Range: bytes */N`.
    - Unparseable Range headers fall through to 200 full-response (RFC
      permissive branch — servers MAY ignore unsupported syntax).
    - Multi-range (`bytes=0-9,20-29`) also falls through to 200 — we
      don't emit `multipart/byteranges`; single-range covers PDF/video
      seek and connection-resume, which is all the GUI needs.
  Streaming uses `.seek(start)` + bounded chunked `read()` (never slurps
  the whole blob into memory) and still goes through `open_attachment_bytes`,
  so the per-user ACL applies to ranged requests too.
- **Short-read detection (#58)**: both `_stream_full` and `_stream_range`
  in [`src/localmail/serve/routes/attachments.py`](src/localmail/serve/routes/attachments.py)
  count bytes actually yielded and call `_log_truncation()` (WARNING on
  the `localmail.serve` logger, message
  `attachment stream truncated: sha256=… expected=… sent=…`) when the
  on-disk blob runs out before the DB-recorded `attachment_blobs.size_bytes`
  (or, on the 206 path, before the requested slice length). Headers are
  already flushed at that point, so the response is short and the client
  sees a stalled / prematurely-closed connection — the log is the only
  ops signal. Don't try to "patch up" the wire here. If a downstream
  consumer ever needs a pre-stream sanity check, add a `stat()` gate
  before the headers go out; the issue body for #58 explicitly scoped
  that out as not necessary.
- **Conditional GET — ETag / If-None-Match / If-Range (#59)**: the
  attachment route advertises a **strong** ETag of `"<sha256-hex>"` on
  every 200 / 206 / 416 response — content-addressable URLs make the
  ETag canonically strong and immutable, so it can be cached
  indefinitely. Parsing lives in
  [`src/localmail/api/conditional.py`](src/localmail/api/conditional.py)
  as a pure module (no IO, no FastAPI) for the same reason
  `range_requests.py` is — future transports (MCP, etc.) reuse it.
  Comparison rules per RFC 9110:
    - `If-None-Match` (§13.1.2) uses **weak** compare. `*`, exact
      strong, and weak (`W/"…"`) variants of the current SHA all match
      → 304 Not Modified with **no body**, carrying only the `ETag`
      header (no Content-Disposition / Accept-Ranges / Content-Length
      — §15.4.5 representation-metadata rules). Evaluated **before**
      Range, so a 304 never degrades to 206 even when both headers
      are present.
    - `If-Range` (§13.1.5) uses **strong** compare. On match, the
      Range proceeds and a 206 is served as today. On mismatch (weak
      tag, HTTP-date, garbage, or simply the wrong SHA) the Range is
      **ignored** and a full 200 is served — never stitch a resumed
      download onto a stale prefix.
    - `If-Range` without `Range` is a no-op (RFC 9110 forbids it; we
      tolerate it gracefully).
  Note that the ETag is `"<sha>"` quoted — `etag_for_sha256` returns
  exactly that; don't double-quote. The pure helpers are
  intentionally generic over `etag` so non-SHA streaming endpoints
  could reuse them.
- **304 short-circuit skips file-open + filename lookup (#62)**: the
  `stream_blob` route uses a cheap two-step probe before deciding to
  serve a body. First `get_attachment_blob_info` (DB-only: ACL +
  `attachment_blobs` row → `(mime, size, path)`, no `Path.exists()`,
  no JSONB filename scan). Then `if_none_match_satisfies` → if it
  fires, return 304 and never call `open_attachment_bytes` /
  `get_attachment_filename`. Only the body-carrying path pays for the
  file open and the JSONB scan that picks the per-message original
  filename. **The probe runs the same ACL check as
  `open_attachment_bytes`**, so a caller without a grant still sees
  404 — never 304 — even when their `If-None-Match` would otherwise
  satisfy. Tested by
  [`test_serve_attachments_conditional.py::test_304_does_not_call_open_attachment_bytes_or_filename`](tests/test_serve_attachments_conditional.py)
  (spy-on-imports asserts zero invocations) and
  [`test_304_acl_denied_returns_404_not_304`](tests/test_serve_attachments_conditional.py)
  (no grant → 404 even with matching If-None-Match). When adding any
  new conditional-GET endpoint, follow the same probe → conditional
  → expensive-IO ordering; never put the expensive call before the
  precondition check.
- **200/206 body path reuses the probe's row (#64, #67)**: the route
  uses the ACL-cleared `(mime, size, path)` tuple from
  `get_attachment_blob_info` directly. The file open goes through the
  module-private `_open_blob_file_at(path, sha256_hex)` helper in
  `api/attachments.py`, which does only `Path.exists()` + `Path.open('rb')`
  and has no `conn` parameter at all — so the ACL check cannot be
  forgotten "by accident". End-to-end on a 200 there is exactly one
  `_caller_can_read_blob` call and one `attachment_blobs` SELECT
  (the probe's), enforced by
  [`test_200_runs_exactly_one_acl_check`](tests/test_serve_attachments_conditional.py).
  `_open_blob_file_at` raises `NotFound` if the file is missing so a
  blob deleted between probe and open surfaces cleanly rather than
  as a mid-stream `FileNotFoundError`. `get_attachment_filename`
  remains a separate JSONB scan — same predicate shape, different
  query — and is out of scope for the #64 ACL-collapse acceptance.
  All three blob-row accessors (`get_attachment_metadata`,
  `get_attachment_blob_info`, `open_attachment_bytes`) share a single
  private `_lookup_blob_row` helper (#65). `open_attachment_bytes`
  itself is safe-by-default — it always runs the ACL EXISTS predicate
  and has no `prefetched=` kwarg (#67 removed the prior footgun).
- **ID typing (#33)**: every entity ID is a **string on the wire** in
  both directions — response bodies emit `str(id)` and path/query
  parameters accept digit-strings only. `localmail.api.ids.parse_int_id`
  is the single boundary cast: route handlers call it on `account_id` /
  `message_id` / `since` cursor and surface a uniform `400
  /problems/validation-failed` on non-digit input (including `+`/`-`,
  whitespace, decimals, hex prefixes, Unicode digits). The api/ layer
  still takes `int`, so the cast happens exactly once per request. When
  adding a new ID-bearing endpoint or MCP tool, declare the parameter
  as `str` and call `parse_int_id(...)`; never accept `int` directly
  from the wire, and never bypass the helper.
- **Browse & search pagination (PR #70)**:
  - `GET /v1/messages` is the canonical keyset browse endpoint, ordered
    `COALESCE(internal_date, date_sent) DESC NULLS LAST, id DESC` with
    an opaque `next_cursor` (URL-safe base64; `localmail.api.browse_cursor`).
    The GUI's initial mail-list load goes here, not `/v1/changes`.
  - `GET /v1/search` returns one of **two cursor flavours**, distinguished
    by prefix on the wire:
      - `"<token>:<page>"` — pool cursor for the hybrid retrieval pool,
        i.e. `sort=rank` with non-blank free text. Driven by
        `Searcher.continue_page` / `Searcher.grow_pool`. The route doubles
        `candidates_per_arm` up to `search.candidates_per_arm_max` (default
        800) when the page would advance past the cached pool; once the
        ceiling is hit `next_cursor` flips to `null`. **Correction:** this
        used to also read "and for `sort=date` with an empty query" — that
        was already stale when written. An empty query takes the keyset
        branch below regardless of `sort` and mints no pool cursor; see the
        `_date_sort_key` bullet under **`sort_order`** below for why that
        branch structurally cannot build one.
      - `"K|<base64>"` descending, `"KA|<base64>"` ascending — **and, since
        #326, `"KT|"` / `"KAT|"` for the same two directions when the walk
        carries free text**; the four are the `(direction, walk)` product
        and `api/search_cursor.py::_KEYSET_PREFIXES` is the table (annotated
        here in place, per the house convention, because this is the entry a
        reader hits first). Keyset
        cursor served by `Searcher._date_keyset_search` (one method now;
        formerly two near-identical ones, `_lexical_date_search` and the
        unpaginated `_list_recent_messages` — see **`sort_order`** below).
        It handles `sort=date` with any query and, regardless of `sort`,
        any blank query. With free text it matches the same FTS column as
        retrieval Arm 1 (`fts_v2 @@ plainto_tsquery('simple', q)`), so
        recall is identical to the lexical case; with none it walks the
        whole (filtered) archive date-ordered. No pool cap; unbounded
        scroll. Route dispatches on the prefix, which also carries the
        walk's direction.
  - **The cursor decides the continuation mode; a stated `sort` may not
    contradict it (#308).** `sort` defaulted to `"rank"` on the wire and was
    resolved to that default before the cursor could bear on the ordering —
    the cursor *was* read (`is_keyset_cursor` dispatched on it and the keyset
    was decoded and passed), it just lost. The Searcher
    picks its retrieval branch from `(sort, free_text)`, reading
    `keyset_cursor` in the lexical-date branch and nowhere else. So paging a
    `sort=date` search the documented way (`docs/mcp-usage.md`: "call the tool
    again with that value in `cursor`") dropped the cursor in silence and
    returned **page 1 of a rank-ordered hybrid search**, which looks like a
    continuation until the results repeat. Reported by an agent paging over
    MCP; the round trip is pinned end-to-end by
    `test_api_search_cursor_mode.py::test_paging_a_date_sorted_search_with_the_cursor_alone_advances`
    (it fails `['1','2','3'] == ['4','5','6']` against the pre-fix source).
    - The rule is the pure
      [src/localmail/api/search_cursor.py](src/localmail/api/search_cursor.py)`::resolve_cursor_plan`
      (renamed from `resolve_cursor_mode` when `sort_order` landed — see
      below — the rule it names is unchanged)
      — kept in the codec module so minting, matching, and *interpreting* stay
      together, the `blob_temps.py` / `sweep_pacing.py` call.
    - **`sort` is null-by-default on every transport**, so "omitted" is
      distinguishable from "asked for". An unstated sort must not out-vote the
      cursor; a *stated* one the cursor cannot serve is a **400**, because both
      other answers are silent — coercing ignores the caller, honouring drops
      the cursor. `DEFAULT_SORT` applies only when there is no cursor to
      inherit an ordering from.
    - **The MCP tool's own `sort="rank"` default was the half that mattered**
      and is easy to miss: `mcp/server.py` restates every parameter for the
      agent-facing schema, so fixing `run_search` alone would have left agents
      sending `"rank"` on their own behalf — turning the silent restart into a
      400 for the exact call the docs prescribe. Pinned by
      `test_mcp_server_build.py::test_search_declares_no_sort_default_of_its_own`,
      which reads the default off the published `inputSchema`.
    - **A keyset cursor with no `query` is rejected too.** That walk rebuilds
      its FTS predicate from the re-sent query; with none the Searcher answers
      from its empty-query recent-mail branch instead — the same restart, one
      branch over.
      - **"No query" is measured on `parse_query(free_text).free_text`, not on
        the request field** (review follow-up). `parse_query` lifts every
        filter operator out of the free text, so `subject:invoice` is
        non-blank as a request field and blank by the time the lexical branch
        tests it — the api gate admitted it as a keyset continuation, the
        branch declined the cursor, and the Searcher's guard fired **as a
        500**: a caller error rendered as an operator traceback, on the exact
        input class the gate exists to catch, under the adjacent invariant
        that a cursor problem is "409, never a 500". Two predicates for one
        rule is what allowed it; they ask the same question now. Composing
        the filters in first would change nothing — the tokens it adds are
        operators, which parse straight back out — and that equivalence is
        pinned, so the cheaper bare-text parse is what runs.
      - **The guard's exception is `KeysetCursorUnusable`, a named
        `ValueError` subclass**, and the keyset branch maps it to a 400 as a
        backstop. A subclass rather than the bare class because catching
        `ValueError` at that boundary would also catch what psycopg,
        `datetime` and the embedding backends raise — relabelling a real
        outage as a cursor problem and sending the caller to re-send a
        blameless query. With both predicates unified the catch is
        unreachable; it is there so a future divergence costs a 400 rather
        than a 500.
    - **Validation precedes the empty-ACL short-circuit.** That branch answers
      with an empty page, byte-identical to "you have reached the end of your
      results" — so a grant-nothing caller was told a contradictory request had
      succeeded and was complete. `resolve_cursor_plan` is pure and touches no
      cache, so it runs first; `_check_pool_sort`'s cache probe stays inside
      the pool branch, after the ACL check.
    - **The pool kind is checked against the pool, not against an invariant.**
      Pool cursors are only minted on the hybrid branch, which is unreachable
      with `sort="date"` — but encoding "pool ⟹ rank" in the route makes a
      future dispatch change silently wrong, so `PoolMetadata` carries the
      `sort` its pool was built with (no default: an unstated one would read as
      `rank` for a pool that is not) and `_check_pool_sort` compares. The probe
      is skipped when the caller stated nothing — nothing to contradict, no
      cache lookup spent.
      - **The *read* is `entry["sort"]`, not `entry.get("sort", "rank")`**
        (review follow-up). The defaultless field was undone one line below the
        comment forbidding it: the fallback made a date-built pool read as
        rank, which is what `_check_pool_sort` then makes a 400/200 call on —
        telling a caller who correctly asks for the pool's own sort that it is
        not the one they will get, and waving through the mismatch that is.
        A missing key is a bug in whichever `_cache.put` forgot it and belongs
        as a loud `KeyError` at the boundary that can still see it. All three
        readers (`get_pool_metadata`, `continue_page`, `grow_pool`) were
        brought over together, so the reporter cannot disagree with the two
        that serve the pages. Pinned by
        `test_pool_metadata_reports_the_sort_the_pool_was_actually_built_with`,
        which reaches past retrieval and puts a date pool in the cache — every
        other test of this field mocks it, so nothing exercised the real read.
    - **`Searcher.search` raises on a `keyset_cursor` it will not read**,
      independently of the route: the drop is a property of the Searcher's own
      dispatch, and the CLI and library callers reach it without passing
      through `api/`. A named `ValueError`, not an `assert` (asserts vanish
      under `python -O` — `upsert_message`'s reasoning). The guard fires before
      any connection is opened, which its test asserts by handing the Searcher
      a pool that raises when touched.
    - **`sort=None` is resolved once, in `Searcher.search` (#312).**
      (**Corrected by #324**: "to `DEFAULT_SORT`, at the top" — it resolves
      through `sort_axes.resolve_sort`, which reads the query, so it happens
      immediately *after* `parse_query` rather than before it. Once, and in
      one place, which is the claim that matters here and is unchanged.)
      It used to be neither accepted nor rejected:
      it fell through the `== "date"` test into the hybrid branch, which is the
      right *ordering* by accident and the wrong *record* — the raw argument is
      what the pool is cached with, and `_check_pool_sort` reads that field
      back to decide a 400. A pool built by a `sort=None` caller reported its
      sort as `None`, so the very next paging request stating the sort it would
      actually be served was told the cursor continues a `None`-sorted search
      and rejected. Every read inside the function goes through
      `effective_sort`; a surviving raw read is the defect.
      - **`DEFAULT_SORT` lives beside the `SortMode` it ranges over**, and
        `api/search_cursor.py` imports it from there
        (**corrected**: this said `api/search.py`, which imports neither
        default — the module that resolves an unstated axis is the one that
        needs the constant, and that is `search_cursor.py`) —
        (**address corrected again by #323**: both moved out of
        `search/searcher.py` into
        [src/localmail/search/sort_axes.py](src/localmail/search/sort_axes.py),
        because `date_keyset.py` needs `SortOrder` at runtime and defining
        it twice is this same drift one level down. Co-location is
        unchanged — the two axes and their two defaults sit together — and
        `searcher.py` imports them, so `from localmail.search.searcher
        import SortMode` still resolves.)
        `api/search_cursor.py` can no longer define it, because two layers
        resolving "unstated" from two literals is the drift itself. The api
        layer still resolves it explicitly at its own boundary (pinned by
        `test_an_omitted_sort_still_means_rank_when_there_is_no_cursor`, which
        mocks the Searcher and so cannot see its resolution); that is not
        duplication now that both read the same constant.
      - **The signature is `SortMode | None = None`, not a removed default.**
        `allowed_account_ids` is keyword-only-with-no-default because no safe
        value exists for it (#234); a sort has one, so the fix is to make
        "unstated" mean it explicitly rather than to make it unspellable.
  - **Page-cache miss surfaces as HTTP 409 `/problems/search-cursor-expired`,
    never a 500.** TTL eviction, LRU eviction, and cross-user replay all
    take this path. The GUI re-runs the query without a cursor on 409 and
    appends past rows it already holds — keep this transparent recovery
    working when touching `serve/routes/search.py` or `api/search.py`.
    - **409 and 400 are different kinds, and the client must not treat them
      alike (#311).** A 409 is recoverable — the request was well formed and
      only the pool is gone. A 400 is *permanent for that cursor*: re-issuing
      the identical pair cannot succeed, so a client must retire the cursor.
      The GUI recovered from 409 only, leaving `cursor`/`hasMore` untouched on
      a 400 — and its `IntersectionObserver` re-fires `loadMore` on every
      scroll event while `hasMore` is true, so the fix #308 shipped turned a
      silent restart into a request loop behind an error banner.
      - The two rules are the pure
        [gui/src/lib/search_paging.ts](gui/src/lib/search_paging.ts) —
        `statedSort` (a request carrying a cursor states **no** sort, which is
        what `docs/mcp-usage.md` tells every other client) and
        `isCursorRejected` (any 400 from a paging request stops the scroll).
        Minting the request and interpreting its refusal live together for the
        reason `api/search_cursor.py` gives on the server side.
      - **Omitting the sort is the load-bearing half**: it makes the
        contradicting-sort 400 unreachable from the GUI rather than merely
        recoverable, since the store's `sort` is user-mutable while a cursor
        is live. The 409 recovery re-runs with **no** cursor, so it must keep
        stating the sort — mutation-pinned, because omitting it there silently
        flips a `sort=date` search back to rank.
      - **`loadMore` guards on `loading` as well as `loadingMore`.** A fresh
        search in flight has already bumped `#submitSeq`, so neither response
        discards the other and the page's rows are appended to a *different*
        query's results. `isCursorRejected` keys on the status alone and reuses
        `admin_error.httpStatusOf`; the message the user sees is the server's
        own problem+json `title: detail`, which `formatError` already renders —
        the wording stays server-side, as with `rewrite_note_code`.
  - **`reranker_enabled` defaults to `False`.** The cross-encoder is
    O(pool size) and the cursor's `grow_pool` doubles the pool on each
    miss (50 → 100 → … → 800). On CPU that overruns request timeouts.
    Operators on GPU opt back in via `[search] reranker_enabled = true`
    in `config.toml`. Don't quietly flip this default; the rerank fanout
    cost compounds with the pagination work.
  - **Known follow-ups (filed)**: #72 (`EXPLAIN ANALYZE` under the
    per-user ACL filter on `messages_recent_idx`). `grow_pool` on the
    `sort=rank` path can still surface duplicates when the cache is
    exhausted past pool 100 — covered by `sort=date` for the "show me
    everything" intent.
  - **Searcher public boundaries (#71)**: the api/ layer (and any
    future MCP layer) uses `searcher.get_pool_metadata(token, *,
    user_id)` and `searcher.config` — never reach into
    `searcher._cache` or `searcher._cfg`. The accessor's `user_id`
    scoping mirrors `continue_page` / `grow_pool` exactly. Tests in
    `tests/test_searcher_pool_metadata.py` enforce.
  - **`sort_order` is a second axis, orthogonal to `sort`, not a new
    `sort` member.** Design:
    [docs/superpowers/specs/2026-08-24-search-sort-order-design.md](docs/superpowers/specs/2026-08-24-search-sort-order-design.md);
    plan:
    [docs/superpowers/plans/2026-08-24-search-sort-order.md](docs/superpowers/plans/2026-08-24-search-sort-order.md).
    **Both carry corrections in place** — the spec prescribed the OR-form
    keyset predicate and called it the more index-friendly of the two, and
    both restate the refuted longest-first prefix rationale. They were
    unlinked from here, which is how that survived review; linking them is
    the house convention every other feature in this file follows.
    `POST /v1/search` and the MCP `search` tool accept
    `sort_order: "asc"|"desc"`, null-by-default like `sort`, resolved to
    `DEFAULT_SORT_ORDER = "desc"` once at the top of `Searcher.search` —
    the same one-authority-per-axis rule #312 established for `sort`.
    `DEFAULT_SORT_ORDER` lives beside `DEFAULT_SORT` in
    `search/sort_axes.py` (moved there from `search/searcher.py` by #323 —
    see the corrected note above);
    `api/search_cursor.py` imports it rather than restating `"desc"`
    (**corrected** from `api/search.py`, which imports neither default). Adding
    `date_asc`/`date_descending`-style members to `sort` instead was
    rejected: a third ordering criterion (relevance-then-date, sender,
    size) would double the enum again, and either `"date"` becomes an
    alias to carry forever or every current client breaks.
    - **`sort="rank"` + `sort_order="asc"` is a 400 — refused, not
      honoured, not silently ignored.** The rank path serves a **bounded
      candidate pool** (the top-K fused across the four arms), so
      reversing it returns the least relevant *of the top hits*, not the
      least relevant mail in the archive — a result that looks meaningful
      and is an artifact of where the pool happened to stop.
      `sort_order="desc"` on `rank` is accepted (it's exactly what the
      rank path already serves); only `asc` is refused. Refusing rather
      than dropping it follows the rule this cluster keeps re-learning
      (#308, #312): a stated parameter the server will not honour is
      reported, never silently ignored. The guard fires **twice** —
      `api/search.py::run_search` raises `ValidationFailed` before any
      work starts (ahead of the empty-ACL short-circuit, for the same
      reason the cursor-mode guard is: that branch answers with an empty
      page, indistinguishable from "you've reached the end"), and
      `Searcher.search` raises its own named `SortOrderNotApplicable` (a
      `ValueError` subclass, not bare — so api/ can map exactly this to a
      400 without also catching what psycopg, `datetime`, and the
      embedding backends raise) before any connection is opened, since
      CLI and library callers reach the Searcher without passing through
      `api/`. Pinned by
      `tests/test_searcher_sort_order_guard.py::test_rank_with_ascending_order_is_refused_before_any_io`
      (asserts `pool.connection.assert_not_called()`) and
      `tests/test_api_search_sort_order.py::test_rank_with_ascending_is_refused_even_with_an_empty_acl`.
    - **The keyset cursor gained a second prefix, not a field on the
      payload.** `"K|<base64>"` keeps its existing meaning — descending —
      so no cursor already in flight changes meaning; `"KA|<base64>"` is
      ascending. Both share `api.browse_cursor`'s existing payload
      encoding, which `/v1/messages` also uses; encoding the direction
      inside that payload instead was rejected because it would have
      reached an endpoint this feature does not touch. **The two prefixes
      are disjoint by construction**: both end in the `|` terminator, so
      `"KA|…".startswith("K|")` is `False` and the converse is `False`
      too — no scan order can misclassify a cursor. (The implementation
      plan for this feature claimed the *opposite* — that a shortest-first
      scan would misclassify every ascending cursor as descending — which
      was simply wrong; do not propagate that reasoning here. The corrected
      version is the `#:` comment above `_KEYSET_PREFIXES` in
      `api/search_cursor.py` — **not** that module's docstring, which says
      nothing about disjointness, and **not**
      `tests/test_api_search_cursor_direction.py`, which asserts each
      prefix positively but pins no disjointness property. Both wrong
      pointers are corrected here; the refuted claim itself survives in
      the plan document, annotated in place. **Since #326 the property is
      asserted rather than only argued** —
      `test_api_search_cursor_walk.py::test_no_keyset_prefix_is_a_prefix_of_another`
      — so the comment is no longer the only record, though it remains the
      one that says *why*.) `resolve_cursor_mode` is
      renamed `resolve_cursor_plan`,
      returning `CursorPlan(mode, sort, sort_order)` — one function
      deciding both axes together rather than two functions each
      answering one, for the same reason the #308 follow-up defect
      happened in the first place: two predicates for one rule is what let
      the api gate and the retrieval branch disagree about what counted as
      a blank query.
    - **Ascending SQL is `ASC NULLS FIRST, id ASC` — never `NULLS LAST` —
      and this is measured, not a style choice.** It is the exact reverse
      of `messages_recent_idx`'s `DESC NULLS LAST, id DESC`, so it is
      served by a backward index scan. **No migration, no new index.**
      Measured on the live 128,289-message archive: `ASC NULLS FIRST`
      plans as `Index Scan Backward` at **44 buffers** (0.83 ms); the
      `NULLS LAST` spelling of the same ascending order **full-sorts** at
      **33,372 buffers** (42 ms); and restricting to `IS NOT NULL` does
      **not** rescue the `NULLS LAST` form — that variant was measured
      too, and still full-sorts at 33,372 buffers (30 ms). Only the
      `NULLS FIRST` spelling matches the index. **Do not "normalise" this
      spelling to `NULLS LAST`.** Both directions' ORDER BY are written
      exactly once, in `searcher._DATE_ORDER_BY_SQL`, whose key type is
      `SortOrder` so mypy refuses a wrong literal — the dict lookup stays
      as the loud failure for the value mypy cannot see, since
      `_keyset_clause` tests `== "desc"` and would hand `"ASC"` the
      *ascending* predicate.
      - **The plan is pinned, not just measured**, by
        `tests/test_searcher_sort_order_plan.py`: it EXPLAINs the ORDER BY
        composed from that constant (never a copy) and requires
        `Index Scan Backward using messages_recent_idx` with no full Sort,
        across the blank-query, FTS-restricted and mid-keyset shapes,
        keeping the `NULLS LAST` spelling as the **negative control** — the
        role `--predicate-form pre75` plays in `run_browse_explain.py`, and
        what stops the assertion being tautological on the only
        date-ordered index the table has. The *functional* half was already
        covered (a `NULLS LAST` slip breaks the undated-first and reversal
        assertions), but the performance half — the whole basis of "no
        migration and no new index" — was not, and a slip there returns
        correct rows while full-sorting the archive on every page. It
        asserts the planner's **choice**, not merely eligibility: unlike
        `test_api_browse_plan.py` the ascending spelling wins at 300 rows
        with nothing hidden and `enable_seqscan` left on.
    - **The ORDER BY is only half the plan: the ascending keyset predicate
      is a row comparison, `ROW(expr, m.id) > ROW(%s, %s)`, never the
      OR-form.** This shipped as the OR-form and was caught in review of
      #322. The two are semantically identical — same rows, same order,
      and the OR-form *keeps* `Index Scan Backward` and adds no Sort, so
      every assertion the plan test then carried passed for it. What it
      loses is the range bound: the predicate plans as a per-tuple
      `Filter`, so each continuation page restarts at the head of the index
      and discards every preceding row. This is #75 exactly, on the search
      path, in newly written code — the entry #75 left in this file says
      *"Do NOT rewrite the predicate as the OR-form even though it's
      semantically equivalent — the planner does not optimize it."*
      Measured mid-walk on the live 128,306-message archive, page ~1250:
      **62.1 ms / 53,789 buffers / 64,001 rows removed by filter**, against
      **0.57 ms / 46 buffers** with `Index Cond` for the row comparison.
      Cost is linear in scroll depth (offset 1,000 → 1.5 ms; 10,000 → 12.7
      ms; 40,000 → 49.6 ms), so it is invisible on page 1 — **which is the
      only page the "no new index" measurements above cover.** Pinned by
      `test_the_ascending_keyset_predicate_composes_an_index_range_bound`,
      which requires an `Index Cond` naming the date expression and forbids
      a `Filter` naming it, with the OR-form kept as a **second** negative
      control beside the `NULLS LAST` one. Structural, not a timing: at
      fixture scale both forms are fast, and being invisible until
      production depth is the whole defect.
      - **Descending has it too now (#323, resolved).** The asymmetry was
        never in the *predicate* — both directions are row comparisons —
        but in the undated block. Ascending needs no `OR expr IS NULL`
        disjunct: under `NULLS FIRST` that block is behind the cursor and
        `ROW(NULL, id) > ROW(…)` is NULL, so those rows drop out on their
        own. Descending must *admit* the undated tail ahead of the cursor,
        which is what its `OR expr IS NULL` was for and what denied it the
        range bound. Re-measured on the live 128,324-message archive at the
        same depth: **70.383 ms / 54,230 buffers / 64,001 rows removed by
        filter** for the OR-form, against **0.040 ms / 48 buffers** with an
        `Index Cond`. The disjunct is gone; those rows arrive from a second
        **top-up statement in the same response**, which is the shape
        `api.browse.list_messages` has used for #75 since before this walk
        existed. Do not "restore symmetry" by putting the OR-form back on
        either side, and do not "fix" a short page by restoring the
        disjunct — that is the one edit both directions' tests are built to
        catch.
        - **The `ts is None` branches keep their shapes**, in both
          directions — but **they are not the same shape and do not plan
          alike**, and an earlier wording here claimed one mechanism for
          both. Measured, both halves of that were wrong. **Descending**
          (`expr IS NULL AND id < %s`) puts *both* conjuncts in the `Index
          Cond`; the `id` comparison is not residual, it is the index's
          second column. **Ascending** (`(expr IS NULL AND id > %s) OR expr
          IS NOT NULL`) must admit every dated row and so gets **no index
          bound at all** — it plans as a `Filter` over a backward index
          scan. That is still fine, and for the reason the old wording gave
          as a conclusion: what it discards is the undated rows already
          behind the cursor, so the residual is bounded by the size of the
          undated block rather than by archive size, and is only paid while
          the walk is inside that block. Splitting it into two phases would
          buy nothing and add a second transition to get right. Neither
          branch has a plan test — `test_searcher_sort_order_plan.py` covers
          dated cursors only — so `keyset_clause`'s docstring is their only
          record, which is why it now states the mechanism per direction.
        - **The rules moved to
          [src/localmail/search/date_keyset.py](src/localmail/search/date_keyset.py)**
          — ordering per direction, both keyset predicates,
          `UNDATED_TAIL_ONLY_SQL`, `needs_undated_top_up`, and the one
          `compose_date_keyset_sql` emitter both phases go through. Pure,
          no IO, unit-tested without a database in
          `tests/test_date_keyset.py`. That is the #77 convention one module
          over: a hand-written second statement is exactly the duplicate
          `compose_browse_sql` exists to prevent. `searcher.py` shrank
          from 1360 lines to 1315 — 147 lines of SQL rules moved out, and
          #326's walk field and guards added some back. (An earlier wording
          here said "lost 146 lines", which was the raw deletion count read
          as a size change.) `SortMode`/`SortOrder` and their defaults moved with them
          to [src/localmail/search/sort_axes.py](src/localmail/search/sort_axes.py),
          because `date_keyset` needs `SortOrder` at runtime for its ORDER
          BY completeness check and defining it twice is #312's drift one
          level down; `searcher.py` imports both modules, so every existing
          import path still resolves.
        - **The top-up's own plan is pinned** (review of #333). Plan quality
          is the whole subject of #323's descending half, and the statement
          it *added* was the one place unchecked.
          `test_the_undated_top_up_statement_is_index_bounded` requires an
          `Index Cond` naming the date expression, with
          `COALESCE(date_sent, internal_date) IS NULL` — the same rows,
          arguments swapped, so no index match — as the negative control.
          That is verbatim the slip `DATE_EXPR_SQL`'s comment warns about.
          Note `IS NOT DISTINCT FROM NULL` does **not** work as a control:
          Postgres rewrites it to `IS NULL` and keeps the bound.
        - **`keyset_clause` and `needs_undated_top_up` take no `order`
          parameter** (review of #333). Both read `keyset.order`. Passing it
          beside the cursor re-admitted the exact pairing that putting
          `order` *on* the cursor was meant to make unrepresentable — a
          descending position walked with the ascending predicate — and
          production was correct only because `Searcher.search` sets
          `effective_order = keyset_cursor.order` whenever a cursor exists,
          i.e. by one caller's discipline, which is the standard
          `encode_keyset_cursor` had already been raised above. The
          redundancy was total: neither function is reachable without a
          cursor in hand. Removing it immediately exposed two plan tests
          that had been building `order="desc"` cursors and passing `"asc"`
          alongside them.
        - **The top-up must land in the same response, not the next page.**
          Deferring it leaves every row count correct while costing one
          wasted round trip per walk, at exactly the boundary the cursor was
          minted for — so
          `test_a_descending_page_straddling_the_undated_tail_is_topped_up`
          asserts the straddling page is **full**, which is the only
          assertion that separates the two failure modes.
    - **Undated rows sort first ascending, not last.** Ascending is the
      exact reverse of descending — the undated tail becomes the undated
      head, same rows, reversed — which is what makes `asc ==
      reversed(desc)` a testable invariant. Undated-last in both
      directions was rejected: that isn't a reversal, and would need the
      two-phase dated-then-top-up query `browse.py` carries for #75, plus
      its own cursor flavour. The live archive has 0 undated rows of
      128,289, so correctness matters more here than the cosmetics — both
      date columns are nullable and archive imports can produce such rows.
      Pinned by
      `tests/test_searcher_sort_order_walk.py::test_ascending_is_exactly_reversed_descending`
      and `::test_undated_rows_sort_first_ascending`.
    - **A blank-query search now paginates, in both directions.** It used
      to return exactly one page and `next_cursor: null`, always, which
      made "my oldest mail about nothing in particular" close to useless —
      there was no way past the first 50 rows. `_list_recent_messages`
      (unpaginated) and `_lexical_date_search` (the `sort=date` lexical
      walk) turned out to be the same SELECT list, ORDER BY, and filter
      composition minus one FTS predicate, so they collapsed into the one
      `Searcher._date_keyset_search`, and the blank-query branch now mints
      and honours a keyset cursor exactly like the lexical one. The
      rank+asc 400 still applies uniformly to a blank query — a caller
      wanting oldest-first blank browse states `sort="date"`,
      `sort_order="asc"` explicitly, rather than the blank case being
      special-cased out of the rule, which would be invisible from the
      wire.
      - **Consequence: the two "keyset needs a query" guards added for
        #308 had to relax.** `resolve_cursor_plan`'s (then still named
        `resolve_cursor_mode`) rejection of a keyset cursor presented with
        a blank query, and `Searcher.search`'s
        `KeysetCursorUnusable` for the same shape, both existed because
        the blank-query branch used to *drop* the cursor and answer with
        its own page 1 — a restart wearing a continuation's clothes. Once
        that branch honours the cursor, the premise is gone: the cursor is
        continued, at the right position, and the old guards would forbid
        exactly the paging this change adds. They now fire only for the
        hybrid pool branch (`sort="rank"` with non-blank text), the one
        branch that still does not read `keyset_cursor`. **This does not
        weaken the #308 property** — the keyset cursor has never
        identified a query, only a `(ts, id)` position; changing
        `folder_ids` or the free text between pages was already undefined
        and unvalidated, and blanking the query is one instance of that,
        not a new class. What #308 forbids is the server silently
        answering a *differently ordered* question, and ordering is
        exactly what the cursor still carries — now on both axes. Rewritten:
        `test_api_search_cursor_mode.py::test_a_keyset_cursor_with_a_blank_query_continues_the_recent_mail_walk`,
        `::test_a_query_of_only_filter_operators_continues_the_walk_too`,
        and
        `test_searcher_keyset_guard.py::test_an_empty_query_now_reads_the_keyset_cursor`.
        - **That relaxation was wider than the feature needed, and #326
          narrowed it back.** The paragraph above is still right about the
          *general* case — a cursor is a position, and a changed query or
          `folder_ids` between pages was undefined before #322 and is
          undefined now. What it did not weigh is that one instance of it
          had been caught by construction, and is the **single most likely
          client mistake**: `docs/mcp-usage.md` tells agents to "re-send
          the same `query` and filters", so paging a text search with the
          query left out was being served as the next `limit` messages of
          the whole archive, dressed as a continuation. Silently.
          - The distinction now rides on the cursor. `KeysetCursor.walk`
            (`"text"` | `"archive"`, **no default**, for the reason `order`
            has none) records which walk minted the position, and the rule
            is the pure
            [src/localmail/search/keyset_walk.py](src/localmail/search/keyset_walk.py)
            — `walk_for_text` and `keyset_walk_error`, shaped like
            `account_names.py::account_name_error`: a message, or `None`,
            with the caller deciding what an error *is*
            (`ValidationFailed` at the api boundary, the named
            `KeysetCursorUnusable` inside the Searcher, which is what
            covers CLI and library callers).
          - **`_date_keyset_search` derives the branch and the stamp from
            one `walk_for_text` call**, so a cursor cannot claim a walk its
            query did not take. That derivation was the *unpinned* part —
            replacing `walk=walk` with a constant left every mocked test
            green — which is why the end-to-end tests against a seeded
            archive exist. Mutation-proven in both flavours.
          - **The Searcher-side guard is pinned too, and was not.** Deleting
            `Searcher.search`'s `keyset_walk_error` block left **127 tests
            green**: every HTTP and MCP test exercises the *api* gate, and
            `test_searcher_keyset_guard.py`'s shared `_CURSOR` is
            deliberately `walk="archive"`, so nothing anywhere handed a
            text-walk cursor to the Searcher with a blank query. That guard
            is the whole of the protection for CLI and library callers —
            the callers `run_search` by definition does not serve — and its
            two siblings in that file each had a test.
            `test_a_text_walk_cursor_with_a_blank_query_is_refused_before_any_io`
            closes it, with a positive control beside it so a guard that
            fires too broadly also fails.
          - **Both readings measure `parse_query(...).free_text`**, never
            the raw request field. Two predicates for one rule is what
            produced #308's own follow-up defect. **They do not, however,
            measure the same string**, and an earlier wording here said
            they "ask the same question", which is wrong in a way worth
            keeping written down: the api gate parses the raw `free_text`,
            while `Searcher.search` parses
            `build_query_string(free_text, scoped_filters)` — the composed
            query, which `_scope_filters_by_acl` has already appended
            `account_id:` ACL tokens to. They agree because
            `build_query_string` is **free-text-neutral**, which is a
            property of the composer and of neither guard, and is therefore
            pinned on the composer by
            `test_api_search.py::test_build_query_string_is_free_text_neutral`
            (70 combinations of query shape × filter shape). CLAUDE.md
            claimed that equivalence for #308 and nothing pinned it.
            **The branch guard is the authority** — it sees the string the
            FTS predicate is actually built from; the gate exists to answer
            before any work is done, and before the empty-ACL branch can
            report a contradictory request as a completed one. The
            divergence is reachable with an unbalanced quote (`from:"`),
            which is why `run_search`'s catch of `KeysetCursorUnusable` is
            **not** the dead backstop its comment used to claim.
          - **Only the text-cursor-plus-blank-query pair is refused.** An
            archive cursor continues under any query, because it has no FTS
            predicate to rebuild — so #322's blank-query pagination is
            untouched, pinned by its own positive control rather than left
            to argument. A rule broadened to every keyset cursor fails 8
            tests, including #322's.
          - **Wire:** two prefixes join the table, spelled `K` + `A` when
            ascending + `T` when the walk carries text (`K|`, `KA|`, `KT|`,
            `KAT|`). Disjointness still comes from the `|` terminator and
            is now **asserted**, not argued. `K|` and `KA|` keep their
            meanings and read as `archive` — the lenient half: a legacy
            cursor could have come from either walk, and the strict reading
            would manufacture a 400 for a caller correctly paging a
            blank-query walk. `encode_keyset_cursor` **raises** on an
            unmapped `(order, walk)` pair rather than falling through to a
            default; that is what surfaced two long-standing test fakes
            whose auto-`MagicMock` `next_keyset` was being minted into a
            garbage cursor.
            - **That raise's premise is now checked at import.** It calls
              itself unreachable "while the table covers the product of both
              Literals", and nothing verified the table did: widening either
              `Literal` without widening `_KEYSET_PREFIXES` type-checked,
              imported, and then failed on the **response** path — the
              cursor is minted last, and only `APIError` reaches the
              problem+json handler, so it surfaced as a 500 on a search that
              had already succeeded. `search_cursor.py` now carries the same
              `get_args` completeness check `date_keyset.DATE_ORDER_BY_SQL`
              carries for its one-axis table, plus a duplicate-pair check;
              the raise stays as the backstop for a value in *neither*
              `Literal`, which construction cannot catch, and is pinned by
              `test_an_unmappable_pair_is_refused_rather_than_mislabelled`.
          - `resolve_cursor_plan` takes `free_text` again and now decodes
            the whole cursor rather than scanning its prefix, which also
            moves a malformed **payload** ahead of the empty-ACL
            short-circuit — where this module already said such a request
            belongs. `run_search` decodes a second time for the position it
            forwards; deliberate, since hoisting the cursor onto
            `CursorPlan` would add a third field its pool-mode consumer
            must ignore (#327).
          - **That gate's `parse_query` was a 500 on two paths that used to
            answer 200** (review of #333). `QueryParseError` — raised for a
            malformed `after:`/`before:` date and an empty `lang:` — is a
            bare `ValueError`, caught nowhere in `src/`, and `serve.app`
            registers a handler for `APIError` only. Moving the parse above
            the empty-ACL short-circuit and onto the pool-cursor branch
            widened a pre-existing fresh-path defect to two branches that
            never parsed `free_text` at all, and it reached MCP as an
            exception no `ToolError` mapping covers.
            `query="invoice after:last-week"` is exactly what an LLM agent
            emits. The rule is `api.search._gate_free_text`, which
            translates it to `ValidationFailed`; **one call, unconditional,
            at the top of `run_search`**, so it covers the fresh path too
            rather than needing a second catch. Pinned by
            `tests/test_api_search_malformed_query.py` across all four
            branches, with a positive control.
      - **Both sort axes are membership-checked at runtime** (review of
        #333). `date_keyset` reasons explicitly that CI runs no mypy step so
        a wrong literal must be caught at runtime, and applied that to
        `sort_order` twice plus an import-time check — but both its checks
        are on the date branch only, so the rank branch validated neither
        axis, and `sort` was never validated anywhere. Both were silent, and
        `sort` twice over: `sort="Date"` fell through the `== "date"` test
        into the hybrid branch, serving **rank ordering** *and*
        `next_keyset=None`, so the walk ended after one page; and
        `sort_order="ASC"` missed the exact-match rank+asc refusal, so the
        rank path neither honoured, validated, nor reported it —
        contradicting that guard's own docstring. Resolved once, at the top
        of `Searcher.search`, right after both axes are resolved and before
        either is read. A plain `ValueError` rather than a named subclass:
        HTTP and MCP both declare these as `Literal`s, so it cannot arrive
        from the wire and there is no api/ mapping to be caught by; worded
        like `date_keyset`'s sibling checks so the two cannot drift.
        Reachable from CLI and library callers only. Pinned by
        `tests/test_searcher_sort_axis_validation.py`.
      - **A query with no free text cannot be ranked, and saying so is a
        400 on page 1 (#324, resolved).** Such a query — blank, or made only
        of filter operators, the branch predicate being evaluated *after*
        `parse_query` lifts them out, so `subject:invoice` is one — has
        always been served by the date walk, because the lexical arms
        early-return with no terms and the vector arms would rank by
        distance to the embedding of the empty string. The stated `sort`
        was therefore dropped, silently. #322 gave that walk a cursor
        recording `KEYSET_SORT = "date"`, which turned the silent drop into
        a contradiction the caller met one page later: `{"query": "",
        "sort": "rank"}` was **accepted on page 1** and its own cursor
        **refused on page 2**. Option (2) of the issue: report it where the
        caller can still act on it. Do **not** "fix" it instead by having
        the cursor record the sort the caller *stated* rather than the one
        that ran — a cursor claiming an ordering it did not walk is #308
        itself.
        - **The rule is the pure
          [src/localmail/search/sort_axes.py](src/localmail/search/sort_axes.py)**,
          two halves co-located for the reason `keyset_walk.py` gives:
          `resolve_sort` says what will run, `sort_applicability_error`
          says whether the caller was told, and split they are two
          predicates for one question. The error half is shaped like
          `account_names.account_name_error` — a message, or `None`, with
          the caller deciding what an error *is* (`ValidationFailed` at the
          api boundary, the named `SortNotApplicable` inside the Searcher),
          so the wording cannot drift between the two ends.
        - **`DEFAULT_SORT` had to move with it.** Resolution is no longer a
          default but a function of the query, so a layer resolving from
          `DEFAULT_SORT` alone now disagrees with the branch that serves the
          request. That is #312's rule one level up, which is what makes the
          co-location load-bearing rather than tidy.
        - **The classification is `keyset_walk.walk_for_text`**, and it
          *replaces* the branch's own call rather than joining it: the
          branch predicate lost its `or walk_for_text(...)` arm, because
          `effective_sort` was already resolved from that same string. So
          the count is unchanged — `sort_axes` and the cursor stamp — and
          the property is stronger than "all three agree": the branch can no
          longer disagree with the resolution that predicts it, because it
          tests that resolution. One reading, not two. (An earlier wording
          here said "gaining a third caller beside the branch and the cursor
          stamp… all three ask the same question", which counted the call
          this same change removed; `grep -rn "walk_for_text(" src/` finds
          four sites — `sort_axes` twice, `keyset_walk_error`, and the
          stamp — and the branch is not among them.)
        - **`Searcher.search` resolves `sort` *after* `parse_query`, not
          before.** It reads the query now, so it cannot be resolved earlier;
          the rank+asc refusal moved with it. Safe because `apply_rewrite`
          leaves `free_text` untouched (it adds `rewritten_text` /
          `expansion_terms` so lexical exact-recall survives) and
          `_clamp_account_ids_to_acl` touches only `filters` — so the string
          resolved from *is* the one the branch sees. Both guards still
          precede every connection, which is what their tests assert.
        - **The membership check reads `sort` as *stated*, not as resolved.**
          A textless query resolves to `TEXTLESS_SORT` whatever arrived, so
          checking the resolved value would swallow `sort="Date"` on exactly
          the branch #333 found swallowing it. An unstated sort is a module
          constant and cannot be wrong, so the `is not None` guard costs no
          coverage. Pinned by
          `test_an_unknown_sort_is_refused_on_a_textless_query_too` — every
          pre-existing case in that file uses a query with free text and so
          reaches none of this.
        - **The inverse face was fixed with it, and had to be.** The rank+asc
          refusal was keyed on the stated-or-defaulted `sort`, so `{"query":
          "", "sort_order": "asc"}` was refused for naming a `rank` path the
          request would never take. It reads the *resolved* sort now, so that
          request is **honoured** — oldest-first over the whole archive or any
          filter, with an ascending cursor. Whatever page 1 decides a stated
          sort means for a textless query, this guard reasons from the same
          thing.
        - **`run_search` forwards the caller's *raw* axes, never `plan`'s
          resolution of them** (review follow-up). It shipped passing
          `plan.sort`/`plan.sort_order` on the fresh branch, and `plan.sort`
          is never `None` — so an *unstated* sort arrived at the Searcher
          looking stated, and on the divergent-parse class below a caller
          who omitted `sort` was refused with "pass sort='date' **or omit
          sort**", a remedy they had already followed. #324's own defect — a
          sort the caller never chose, reported as their statement —
          reintroduced by #324's fix, and it breaks both
          `sort_applicability_error`'s stated contract ("only a stated sort
          is judged") and this file's rule that the branch guard is the
          authority. The gate keeps its own resolution for its own early
          refusals; it just no longer speaks for the caller.
          - **Widening the fresh catch to `SortOrderNotApplicable` is part of
            that change, not tidying.** The divergence runs both ways: `'"'`
            is textless to the gate (whose rank+asc refusal therefore does
            not fire) and text once the ACL token composes in, so the
            Searcher resolves `rank`, meets a stated `asc`, and raises.
            Unreachable while the gate forwarded its own resolution.
          - **Known residual, filed not fixed**: the gate's rank+asc refusal
            still reads `plan.sort`, so `sort_order="asc"` with no sort on
            that same divergent class is still a 400 naming a `rank` path the
            request would not take. Pre-existing — `main` behaves identically
            — and fixing it means gating on the composed query, which needs
            `run_search`'s ordering restructured around the empty-ACL
            short-circuit.
        - **`KEYSET_SORT` is `TEXTLESS_SORT`, aliased rather than respelled.**
          Two `"date"` literals held up two non-local properties with nothing
          checking either: page 1 accepts `sort=TEXTLESS_SORT` and mints a
          keyset cursor that `_reject_sort_mismatch` compares against
          `KEYSET_SORT` (a divergence is #324's own accepted-then-refused
          shape), and `run_search`'s keyset branch *used to* omit
          `SortNotApplicable` from its catch, which was safe only because
          `sort_applicability_error` returns `None` for `TEXTLESS_SORT` (a
          divergence was a 500 on every keyset continuation of a blank-query
          walk). **The second property no longer rests on the alias** —
          since #344 both branches catch the `SearchArgumentRefused` family
          rather than naming members, so that omission is not expressible;
          the first still does, which is why the alias stays. They are one
          fact seen from two ends — the walk a textless
          query resolves to *is* the walk that mints those cursors — so the
          alias makes drift impossible and `test_sort_axes.py` asserts the
          property so that un-aliasing fails there rather than silently later.
        - **`run_search`'s catch of `SortNotApplicable` is live, not a
          backstop.** The api gate parses the raw request field and the
          Searcher parses the ACL-composed query, and `parse_query` is not
          compositional across an unbalanced quote: `from:"` leaves
          `'from:'` as free text alone and nothing once a trailing
          `account_id:` token joins it. Verified, not argued. Without the
          catch the caller's error escapes as a 500 — `serve.app` handles
          `APIError` only — on a query the boundary had already cleared.
        - **The GUI never states `rank` at all** (review follow-up).
          `search_paging.statedSort` returns `undefined` for it and `date`
          otherwise, reading only the cursor — **not** the query. It shipped
          reading the query (`sort === "rank" && query.trim() === ""`), and
          that was a regression: the server decides "textless" only *after*
          lifting operators out, so `from:alice` and `has:attachment` — the
          two shapes `SearchBar`'s own placeholder advertises — are textless
          there while non-blank here. Those searches stated `rank`, earned
          the new 400, and `submit()`'s catch cleared the results and showed
          an error banner, where before they returned date-ordered rows.
          The imprecision was documented as costing "one loud, actionable
          400"; what it actually cost was the placeholder's own examples.
        - **Dropping `rank` unconditionally is exactly equivalent, which is
          why it is the fix rather than a client-side `parse_query`.** An
          unstated sort resolves server-side to the branch that will serve
          the request — `rank` whenever ranking is possible at all — so
          omitting it matches stating it wherever stating it would have been
          honoured, and avoids the 400 wherever it would not. Three
          consequences: the query argument disappears (with it the
          transposition hole — `statedSort(cursor, sort, query)` had two
          adjacent `string` parameters and `cursor` is narrowed to `string`
          at the `loadMore` call site, so a 1↔3 swap type-checked); README's
          claim that both 400s are *unreachable* from the GUI becomes true
          rather than aspirational; and there is no second parser to keep in
          step with the first. `date` is still stated, or the sort selector
          goes inert.
        - **The store tests pin what reaches the wire, not the wiring.** The
          three call sites forwarding a query are gone, so the pins are
          behavioural: a filter-only search, an operator-only box (the
          regression above), an ordinary text query, `date` surviving, and
          both halves of the 409 recovery — which is a *fresh* request and
          so the second place a sort reaches the wire.
    - **Every argument the Searcher refuses is one family, caught as one
      (#344).** `Searcher.search` raises four sibling exceptions whose whole
      purpose is "map me to a 400", and they derived straight from
      `ValueError` with no shared base — so `api/search.py` enumerated them
      by name, in **two different tuples on two branches**, each carrying a
      per-member argument about which were unreachable there. A fifth guard
      added without widening a tuple is an operator-facing **500**
      (`serve.app` handles `APIError` only), and that is not hypothetical:
      #342 shipped with `SortNotApplicable` absent from the keyset tuple,
      safe only by the `KEYSET_SORT is TEXTLESS_SORT` aliasing decided in
      another module. The rule is
      [src/localmail/search/argument_errors.py](src/localmail/search/argument_errors.py)`::SearchArgumentRefused`;
      both boundaries catch **it**, and the per-member reachability
      arguments are retired with the enumeration.
      - **The named subclasses stay.** The point is not to collapse the
        diagnoses — each still tells the caller a different thing — but to
        let api/ catch precisely this family without also catching the
        `ValueError` psycopg, `datetime` and the embedding backends raise,
        which would relabel a real outage as a caller error. Pinned by a
        positive control asserting a bare `ValueError` still escapes as
        itself; a boundary widened to `ValueError` passes every other test
        in the file.
      - **The pins are two kinds, because either alone has a hole.**
        Structural: every exception class in `argument_errors` inherits the
        base, so a fifth guard *written in the right place* joins by
        construction. Behavioural: the family is enumerated from the
        **type** (`__subclasses__`, transitively) and every member is driven
        through *both* `run_search` branches — so a member added later is in
        scope without this test being edited, which a hand-written list
        cannot be. A `_family()` that silently returned `[]` would make the
        parametrised half vacuous, so that is its own negative control.
      - **They moved out of `searcher.py` rather than gaining a base in
        place**, the `sort_axes.py`/`keyset_walk.py` call: the family is the
        contract *between* the Searcher and every boundary that maps it, and
        stating the rule a new guard must join needs somewhere to state it.
        `searcher.py` re-exports the four, so
        `from localmail.search.searcher import KeysetCursorUnusable` keeps
        resolving; the base is new and has no legacy path, so boundaries
        import it from `argument_errors`. `searcher.py` went **1439 → 1391**
        (the four classes were 73 lines; the re-export and the hoisted
        guard's rationale account for the difference — measured, because the
        first draft of this line asserted 73 and was wrong).
      - **The membership checks on `sort`/`sort_order` deliberately stay
        outside the family.** They raise a plain `ValueError` because HTTP
        and MCP both declare those as `Literal`s, so a bad value cannot
        arrive from the wire and there is no api/ mapping to be caught by.
        Admitting them would claim a wire audience they do not have.
      - **A cursor problem outranks the textless rule, now at both layers.**
        `test_api_search_rank_without_text.py` states that as a *rule* ("the
        more specific diagnosis … must not be displaced by the textless
        one") and it held only at the api boundary, where
        `resolve_cursor_plan` never consults the textless rule once a cursor
        is present. Inside the Searcher the order was inverted, so
        `search("", keyset_cursor=<text-walk>, sort="rank")` was answered
        differently over HTTP than from a library call — the
        two-layers-wording-one-rule-differently shape, untested in either
        direction there, which is how it survived the review that created
        it. The walk guard moved ahead of the two sort guards; both
        directions are pinned, plus positive controls for each guard so a
        hoist that *swallowed* the textless rule fails too.
        - **No wire behaviour changed** — the api boundary already reported
          the cursor, and the widened catches are unreachable on the branch
          that gained them (the fresh branch passes no cursor; the keyset
          branch passes `sort="date"`, for which
          `sort_applicability_error` is `None`). Only a direct
          `Searcher.search` caller sees a different message, and both
          messages recommend the same remedy.
        - **It buys no IO, and the first draft of this entry claimed it
          did.** The rewriter runs only under `parsed.free_text.strip()`
          and this guard fires only when that string is blank, so no smart
          rewrite was ever paid for on the path. Measured, not reasoned —
          the claim was written down and then refuted before it shipped.
      - **#331's points 1, 3 and 4 landed with it; #331 is closed and its
        point 2 is folded into #305.** Point 4 *is* #344 — the issue asked
        for a `SearchRequestError` base in the same words — so it was
        already being done. The other two live
        in the very docstrings and handler this change rewrites, and leaving
        a known-false claim in a file one is authoring is not a smaller
        change, it is a worse one.
        - **Point 1 — `SortOrderNotApplicable`'s audience.** Its docstring
          said the api/ catch "is a backstop for a future dispatch change
          rather than a live path". That was true when written and **#324
          falsified it**: the gate and the Searcher judge different strings,
          and `'"'` is textless to the gate and text once the ACL token is
          composed in, so the gate can clear a `sort_order="asc"` against a
          resolved `date` that the Searcher resolves to `rank`. Corrected in
          place.
        - **Point 3 — `cursor:` was the branch's word, not the cause's.**
          The keyset branch wrote the prefix into its own f-string, so it
          labelled everything it caught: a `sort_order` refusal on a request
          whose cursor was fine would have read `cursor: sort_order='asc' is
          not applicable…`. Unreachable, and **widening the catch to the
          whole family makes it more so** — the category error arriving via
          its own fix. `SearchArgumentRefused.wire_prefix` now carries it and
          both boundaries interpolate, so it follows the cause: the same
          derive-don't-restate call as `version_report`'s severity word.
          - **The default is empty, not mandatory** — the opposite of
            `VersionSource`'s forced remedy, and deliberately so. A member
            that forgets a prefix loses a word of context; one that inherits
            a *wrong* prefix makes a false claim. The default fails in the
            harmless direction, and the mapping is pinned anyway so a new
            member has to decide.
          - **No reachable message changed**, which is the point: the only
            member reachable on the keyset branch is `KeysetCursorUnusable`,
            which sets `cursor: `. Pinned by a test asserting the shipped
            wording verbatim, beside the one asserting a sort refusal caught
            on that same branch is *not* labelled a cursor problem.
        - **Point 2 — `cli.py`'s search catches `RuntimeError` only**, so a
          `--sort-order` flag added there would traceback. Latent (no such
          flag exists), and **folded into #305** with the rest of the
          `cli.py` work rather than left as its own issue. Widen that catch
          to **`SearchArgumentRefused`**, never to bare `ValueError` — the
          family is the point, so a fifth guard must not need the catch
          edited. Adding either sort flag makes all four members reachable
          from the CLI at once.
    - **The keyset cursor carries its own direction, and the Searcher reads
      it (review of #322).** `KeysetCursor` was `(ts, id)` and nothing
      else, so `Searcher.search` paired a directionless cursor with a
      `sort_order` that defaults to `"desc"`: an ascending walk paged the
      documented way — state the order once, then send only the cursor
      back — **silently reversed**. Page 2 re-emitted a row the caller
      already held and then ran off the end (`[7, 8]` then `[7]`, 6 of 9
      rows lost), with no exception and no log line, so it reads as a data
      problem rather than a call-site one. HTTP and MCP were safe only
      because `run_search` happens to pass `plan.sort_order` on every hop
      — a property of one call site, not of the signature — and the repo's
      own paging idiom (`tests/test_searcher.py`) is exactly the losing
      shape.
      - The field is `KeysetCursor.order`, **no default**, stamped in
        `_date_keyset_search` from the walk that produced the rows. So
        `encode_keyset_cursor(ks)` lost its second argument and
        `_next_cursor` its `order=` parameter: the api layer can no longer
        supply a direction the walk did not use, rather than merely being
        careful not to. Minting beside matching, the `blob_temps.py` call.
      - `decode_keyset_cursor` returns the direction the prefix encodes, so
        the round trip carries position *and* sense. Nothing on the wire
        changed — the prefixes and payload encoding are untouched.
      - A **stated** `sort_order` contradicting the cursor raises
        `KeysetOrderMismatch`, mirroring the wire layer's
        `_reject_order_mismatch`. Both other answers are silent: honouring
        the argument walks the position in a direction it was not minted
        for, honouring the cursor ignores a parameter the caller wrote.
      - Only the *direction* is inherited from the cursor, **not `sort`**.
        Inferring the sort would silently retire `KeysetCursorUnusable`,
        which is the guard for a keyset cursor reaching the hybrid branch.
        Consequence: `search(q, keyset_cursor=asc_ks)` with no `sort` now
        raises `SortOrderNotApplicable` (rank+asc) where it used to
        reverse — loud, with the right remedy, and only reachable by
        library callers.
    - **Timestamp ties are pinned now, in both directions (review of
      #322).** The `id` tiebreaker in `ROW(expr, m.id) > ROW(%s, %s)` and
      in the descending `expr = ts AND m.id < %s` disjunct was exercised by
      **no fixture in the suite**: every one gave its dated rows distinct
      timestamps, so the tiebreaker-less `expr > %s` selected the same rows
      and dropping either left 150 and 174 focused tests green. The shipped
      SQL was correct; the *pin* was missing, and a tie group straddling a
      page boundary silently loses its remainder. Ties are ordinary here —
      bulk sends share `date_sent` to the second, and archive imports
      derive `internal_date` from an mbox `From_` line or a maildir mtime.
      `_seed(tied=)` in `tests/test_searcher_sort_order_walk.py` closes
      both, mutation-proven each way.
      - `tests/test_searcher_sort_order_plan.py::_seed` gained
        `_SEED_TIED_AT_CURSOR` rows **at the cursor's own timestamp**,
        because its fair-control assertion claimed in a comment to catch "a
        dropped tiebreaker" and could not: with every date distinct the row
        comparison and `expr > ts` select identical rows. The claim is true
        now (mutation-proven) rather than merely written down.
    - **`_date_sort_key` (and the `sort="date"` branch of `_build_results`
      it serves) is unreachable, and stays that way on purpose.** The
      hybrid pool branch — the only caller of `_build_results` with a
      `sort` other than the module default, and the only writer of a
      cached pool's own `sort` — is reached only as `rank` + non-blank
      text, because the date-keyset branch now claims `sort="date"` *and*
      every blank query. Kept rather than deleted, and pinned rather than
      assumed:
      `tests/test_searcher_pool_sort_unreachable.py::test_a_cached_pool_always_records_sort_rank`
      asserts every cached pool reports `sort="rank"`. The point of
      pinning dead code instead of deleting it is to stop a future reader
      adding `sort_order` handling to `_date_sort_key` "for symmetry" —
      code that would be tested against a branch that never runs (#278 is
      this codebase's precedent for a declared-but-unserved surface that
      four test files made look covered).
- **Hard ACL clamp inside the Searcher**: the ACL is enforced in **two**
  places, and both are load-bearing. `api/search.py::_scope_filters_by_acl`
  intersects the caller's *structured* `account_ids` filter and
  short-circuits an empty intersection to an empty page; but the ACL then
  travels to the Searcher as `account_id:` **DSL tokens in the query
  string**, and `parse_query` unions every `account_id:` token regardless of
  origin — so a token smuggled through the untrusted free text OR-widened
  `m.account_id = ANY(...)` past the grant. `Searcher.search` therefore takes
  `allowed_account_ids: list[int] | None` and pipes `parsed` through
  `_clamp_account_ids_to_acl` **after any smart rewrite and before every
  retrieval branch** (hybrid pool, `sort=date` lexical keyset, empty-query
  fallback), so the cached pool inherits the clamped filter and
  `continue_page` / `grow_pool` stay scoped without re-clamping. `None` means
  "no ACL" (CLI / local callers keep full DSL power); an **empty list** is a
  real grant-nothing ACL. The parameter is **keyword-only with no default**
  (#234) — `None` has to be written at the call site. It shipped defaulted in
  #229 and that was the residual hole: forgetting the kwarg produced a silent
  full-archive search, no `TypeError`, no failing test — the same footgun shape
  #67 removed from `open_attachment_bytes`. Pinned by
  `test_search_acl_clamp.py::test_search_requires_an_explicit_allowed_account_ids`.
  Two traps to preserve:
  - **An empty id set collapses to `_NO_ACCOUNT_SENTINEL = -1`, never `[]`.**
    `_filter_sql` treats an empty list as falsy and drops the clause
    entirely — i.e. *all accounts*, the exact inverse of the intent. The same
    trap bit `_resolve_account_names` (all `account:NAME` values unknown →
    `accounts=[]` → matched everything while logging "matching no rows"); it
    uses the same sentinel now.
  - **Only `account_ids` is clamped, deliberately.** `account:NAME` resolves
    into the separate `filters.accounts` field, which `_filter_sql` emits as
    its own `AND` clause — it can only intersect, never widen. Same for
    `folder_id:`, whose union is bounded by the account clause. Tests in
    `tests/test_search_acl_clamp.py`.
- **Server-side subscription cursors on `/v1/changes`**: migration
  `0032_channel_subscriptions.sql` adds `channel_subscriptions`
  (one row per `(user_id, name)`, `cursor BIGINT`, FK to
  `api_users` `ON DELETE CASCADE`). `GET /v1/changes?subscription=<name>`
  reads the stored cursor instead of a client-supplied `since` (the two
  are **mutually exclusive**, 400 if both are given);
  `POST /v1/changes/ack {"subscription","cursor"}` → 204 advances it.
  Lets a polling client be stateless — poll, process, ack — instead of
  re-reading the 200-message tail after every restart. Invariants, each
  with a test in `tests/test_serve_changes_route.py`:
  - **A fresh subscription primes at the current tip, not the backlog**,
    so a first-time subscriber never replays old mail as new work.
    The tip is `_current_tip` — `MAX(id)` **with the same
    `changes_safe_horizon_s` filter the `since` branch applies**. Using a
    raw `MAX(id)` here would be a silent permanent-loss bug: a tx that
    allocated a lower id can commit after one that allocated a higher id,
    so the cursor could start past a not-yet-visible message that no later
    poll would ever return. Note the horizon **bounds** this window rather
    than closing it — `date_received` defaults to the *transaction*
    timestamp and `sync_mailbox` commits per 50-message batch, so a batch
    slower than the horizon still races (pre-existing, applies equally to
    `since`).
  - **Acks are monotonic** (`GREATEST` in the upsert), so a stale or
    replayed ack cannot resurface processed messages.
  - **Creation is atomic.** `_claim_subscription` uses `ON CONFLICT
    (user_id, name) DO NOTHING RETURNING cursor` and the loser of a race
    re-reads the winner's cursor. A bare INSERT here raised
    `UniqueViolation` on two simultaneous first polls, which escaped as a
    **500** (only `APIError` subclasses reach the problem+json handler).
  - **An ack past the archive's highest `messages.id` is rejected (400).**
    Because acks are monotonic there is no API path back, so an
    out-of-range value (a timestamp, an overflowing BIGINT → a raw
    `NumericValueOutOfRange` 500) would silence the subscription for good.
    The bound comes from `_max_message_id`, which is deliberately
    **global — no ACL, no horizon** — because Postgres rewrites `MAX(id)`
    on the PK into a one-row `Index Only Scan Backward`. Do not "tighten"
    it to reuse `_current_tip`: that plan is an index scan over all of the
    caller's rows, acceptable once per subscription but not on every ack.
  - **Row growth is capped** at `serve.max_subscriptions_per_user`
    (default 32) on both the GET and the ack create paths, since a client
    deriving the name from a UUID would otherwise grow the table without
    bound. Advisory only — concurrent creates at the cap can overshoot by
    one; it is a resource guard, not a security boundary.

  Known gaps, filed not fixed: the SQL lives in `serve/routes/changes.py`
  rather than `api/`, so **MCP tools cannot use subscriptions** (#224); there
  is no reset/delete endpoint and the first `GET` has a write side effect
  (#225); and the safe-horizon precondition above is undocumented on the
  `since` path too (#227).

## MCP server (search Phase 3)

Remote, multi-user MCP server exposing the archive's read surface to AI
agents. Mounted into the existing `serve` FastAPI app at `/mcp` over
**Streamable HTTP** — no new listener; TLS and the `--no-tls`/`--bind
127.0.0.1` rules inherit from `serve` unchanged. Endpoint URL:
`https://<host>:<port>/mcp`. Operator/agent guide:
[docs/mcp-usage.md](docs/mcp-usage.md). No new migration (reuses
`api_users` / `api_tokens` / `user_accounts`).

- **Auth = opaque bearer reusing `api_tokens`.** `LocalmailTokenVerifier`
  ([src/localmail/mcp/auth.py](src/localmail/mcp/auth.py)) wraps the existing
  `api.auth.verify_token` and carries the user id in `AccessToken.subject`. The
  sync DB lookup is offloaded via `anyio.to_thread.run_sync` so the verifier
  never blocks the event loop. Agents get a token from `POST /v1/auth/login`
  (refresh: `/v1/auth/refresh`) and pass `Authorization: Bearer <token>` to
  `/mcp` — there is **no** OAuth authorization-server flow; clients configure the
  token directly.
- **Five ACL-scoped read tools** in
  [src/localmail/mcp/{server,tools,auth}.py](src/localmail/mcp/server.py), each
  calling `localmail.api` accessors directly (no HTTP hop, **no `wire.py`** — the
  api/ layer already returns the wire-shaped dicts, so HTTP routes and MCP tools
  share that serialization). Per-user ACL applies to every tool (results scoped
  to the token user's granted accounts).
  - `search(query, sort="rank"|"date", limit, cursor, account_ids, folder_ids,
    date_from, date_to, from_addr, to, subject, has_attachment, lang, smart)` —
    hybrid search; `smart=true` runs the Phase-4 LLM rewrite (page 1) and the
    response `rewrite_skipped` reflects whether it happened; page by re-calling
    with `next_cursor`; a cursor-expired error means re-run without a cursor.
  - `get_message(message_id, full_headers=False)`.
  - `get_attachment(sha256, mode="text"|"metadata")` — extracted text or
    metadata, **never raw bytes** (raw download stays the HTTP
    `/v1/attachments/{sha256}` route).
  - `list_messages(account_ids, folder_ids, limit, cursor)` — keyset
    date-ordered browse, newest first.
  - `list_accounts()`.
- **Wiring**: `FastMCP(token_verifier=…, auth=AuthSettings(issuer_url,
  resource_server_url, required_scopes=[]), stateless_http=True,
  json_response=True, streamable_http_path="/")`, mounted at `/mcp` in
  `create_app` (gated by `enable_mcp` **and** the importable `[mcp]` extra; if
  the extra is absent, `serve` runs and logs an INFO skip line). The session
  manager is started in the app lifespan (`async with
  mcp_server.session_manager.run()`).
- **Config** `McpConfig` (`localmail.config`, `[mcp]`): `enabled` (default
  false), `issuer_url` / `resource_server_url` (default
  `http://localhost:8443`; advertised in the SDK's OAuth resource-metadata —
  opaque-bearer clients ignore them). `serve` CLI forwards `cfg.mcp`.
- **Three design reconciliations vs the spec**: (1) no `wire.py` (shaping
  already lives in api/); (2) ONE `search` tool, not three — `run_search` takes a
  single optional `cursor` and auto-grows the pool, paging = re-call with
  `next_cursor`; (3) `get_message(full_headers=…)`, not
  `include_body`/`include_attachments`.
- Tools return structured content; `SearchCursorExpired` / `NotFound` /
  `ValidationFailed` map to clean `ToolError`s. Raw attachment bytes are
  intentionally NOT exposed over MCP (HTTP `/v1/attachments` only). **Deferred
  follow-ups**: full OAuth 2.1 **authorization server** (`/authorize`, `/token`,
  dynamic client registration) — the *discovery surface* half of "Approach B"
  is now shipped (see next bullet); richer per-tool docstrings.
- **RFC 9728 protected-resource discovery (shipped — "Approach B" discovery half):**
  a spec-strict MCP client can discover `/mcp` as a protected resource without
  localmail becoming an OAuth authorization server (it stays opaque-bearer;
  tokens come from `/v1/auth/login` out-of-band). The pure module
  [src/localmail/mcp/discovery.py](src/localmail/mcp/discovery.py) holds
  `MCP_MOUNT_PATH`/`RESOURCE_NAME`, `mcp_resource_url(base)` (origin + `/mcp`,
  trailing-slash-safe), `resolve_authorization_servers(configured, issuer)`
  (`configured or [issuer_url]`), and the one SDK-touching wrapper
  `build_protected_resource_routes(config)` (function-level SDK import so the
  module stays import-safe). Two halves make the surface reachable: (1)
  `build_mcp_server` passes `AnyHttpUrl(mcp_resource_url(...))` as
  `AuthSettings.resource_server_url`, so the SDK's 401 `WWW-Authenticate`
  challenge advertises the canonical root URL
  `/.well-known/oauth-protected-resource/mcp`; (2) `create_app` registers the
  SDK's `create_protected_resource_routes` on the **top-level** app (public,
  via `_try_build_mcp` → `app.router.routes.extend(...)`) at that exact path —
  the SDK's own sub-mounted copy lands at the non-canonical
  `/mcp/.well-known/oauth-protected-resource/mcp` and is left alone (harmless).
  New config `McpConfig.authorization_servers: list[AnyHttpUrl] | None = None`
  (operator-configurable; defaults to `[issuer_url]`). `resource_server_url`
  stays the bare public origin (no `/mcp`; appended internally). No migration,
  no new dependency. Design:
  [docs/superpowers/specs/2026-06-10-mcp-protected-resource-discovery-design.md](docs/superpowers/specs/2026-06-10-mcp-protected-resource-discovery-design.md).
- **OAuth 2.1 authorization server (opt-in, shipped):** localmail can act as an
  OAuth AS so spec-strict MCP clients self-onboard via browser login + consent —
  no hand-pasted bearer token. Enabled with `[mcp] authorization_server_enabled =
  true`; requires `[serve] state_signing_key` (>= 32 chars — `create_app` fails
  loud at startup without it). The AS issuer is **auto-derived** as
  `<resource_server_url>/mcp` in `_try_build_mcp` (zero-config for the operator;
  an explicit `[mcp] authorization_servers` override is still honoured for
  pointing at an external IdP). Code sub-packages:
  `src/localmail/mcp/oauth/` — `consent_state.py` (HMAC-signed state token),
  `consent_forms.py` (pure login/consent form logic), `clients.py` (DCR
  registration + unused-client cleanup), `codes.py` (authorization code issue +
  exchange), `refresh.py` (sliding refresh token rotation), `access.py`
  (access token issue), `provider.py` (`load_access_token` wraps the existing
  `verify_token` so the ACL is unchanged), `registration.py` (per-IP rate-limit
  guard); `src/localmail/serve/oauth/` — `consent_router.py`
  (`/oauth/consent` login + allow/deny screens), `registration_guard.py`
  (per-IP middleware). Access tokens are stored in the existing `api_tokens`
  table (`provider.load_access_token` wraps `verify_token`) — the per-user ACL
  and `grant-account` grants are unchanged. Refresh tokens are sliding-rotated:
  each refresh resets the 30-day clock (`oauth_refresh_token_ttl_s`); a browser
  re-login is required only after ~30 days of inactivity, on revocation, or if
  the api_user is disabled. The consent login reuses the `/v1/auth/login`
  rate-limit + `DUMMY_PASSWORD_HASH` timing-parity protections — **including the
  `X-Forwarded-For` peeling (#220, resolved)**, which was the one half of that
  reuse never wired up: `post_consent` read `request.client.host` raw, so behind
  a configured reverse proxy every user collapsed into one per-IP bucket, and
  guessing spread across usernames went unthrottled while one noisy client
  locked everyone else out. It now calls the same
  `api.client_ip.resolve_client_ip` as `/v1/auth/login`, `/admin/login`, and the
  DCR guard (empty `trusted_proxies` = socket peer, unchanged). Open DCR (`POST
  /register`) is bounded by a per-IP rate-limit middleware
  (`oauth_registration_max` per `oauth_registration_window_s`, default 20/hour)
  and unused-client cleanup (`oauth_client_unused_retention_s`, default 24h).
  **Known limitations:** AS metadata is served at the OIDC-style path-suffix
  form `<origin>/mcp/.well-known/oauth-authorization-server`; the strict RFC 8414
  §3.1 insertion form `<origin>/.well-known/oauth-authorization-server/mcp` is
  NOT served (real MCP clients use the path-suffix form). Migration
  `0028_oauth_server.sql` adds `oauth_clients`, `oauth_authorization_codes`,
  `oauth_refresh_tokens`, `oauth_registration_attempts`, and nullable
  `api_tokens.oauth_client_id`. No new uv dependency (`mcp` extra already
  provides the AS machinery). Design:
  [docs/superpowers/specs/2026-06-15-mcp-oauth-authorization-server-design.md](docs/superpowers/specs/2026-06-15-mcp-oauth-authorization-server-design.md).
- **AS hardening tidy-ups (#182 review follow-ups M1/M2/M3, shipped):**
  - **M1 — disabled-user refresh containment:** `refresh.load_refresh` JOINs
    `api_users` and filters `disabled_at IS NULL` (mirroring `api.auth.verify_token`),
    so a disabled user's refresh token is treated as non-existent — both the SDK's
    `load_refresh_token` and `rotate_refresh` reject it. RFC 9700 §4.13. If the
    user is disabled in the window *between* the SDK's load and exchange,
    `provider._exchange_refresh_sync` fails closed with a `TokenError`
    (`invalid_grant`) raised after the connection context exits — it no longer
    asserts on the `None` rotation (which would have been an HTTP 500).
  - **M2 — broadened unused-client cleanup:** `clients.cleanup_unused` now reaps a
    client when it has **no unexpired refresh token** *and* its last activity
    (`COALESCE(last_used_at, created_at)`) is older than the retention window —
    covering once-used-then-idle clients, not just never-used ones. The
    `NOT EXISTS` live-refresh-token guard means an actively-refreshing client is
    never reaped (reaping its row would break the next `get_client`).
  - **M3 — DCR rate-limit proxy peeling:** `RegistrationRateLimit` takes an
    `auth_config` and resolves the client IP via the new pure
    `registration_guard.resolve_scope_client_ip` → shared `api.client_ip.resolve_client_ip`,
    so the per-IP `/register` cap peels `X-Forwarded-For` against
    `auth.trusted_proxies` exactly like the login limiter (empty config = socket
    peer, unchanged). Wired in `create_app` (`auth_config=auth_cfg`).
  - No new migration for M1/M2/M3.
- **Refresh-token family revocation on reuse (#183, #185, shipped):** rotation no
  longer hard-deletes the presented refresh token. Migration
  `0029_oauth_refresh_token_family.sql` adds `oauth_refresh_tokens.family_id`
  (`UUID NOT NULL DEFAULT gen_random_uuid()` — existing rows become singleton
  families) + `consumed_at TIMESTAMPTZ` (NULL = live; set = rotated tombstone),
  plus indexes on `family_id` and `client_id` (the latter is **#185**, serving
  `cleanup_unused`'s correlated `NOT EXISTS`). `refresh.rotate_refresh` now
  returns a `RotateResult(outcome, new_token)` enum: it **tombstones** the
  presented token (UPDATE `consumed_at`) and mints a successor in the **same
  family** (`outcome="rotated"`); replaying an already-consumed token is reuse
  (a stolen-copy signal, RFC 9700 §4.14.2) → it `DELETE`s the **whole family**
  and returns `outcome="reuse"`; an absent/expired/disabled-user token is
  `outcome="unknown"` (natural, never nukes the family — the M1 disabled-user
  containment now lands here). `refresh.load_refresh` filters
  `consumed_at IS NULL` so tombstones never load as live;
  `refresh.sweep_consumed` GCs consumed tombstones past their own `expires_at`
  (opportunistic, called on the rotation path — reuse stays detectable for the
  token's full lifetime). `clients.cleanup_unused`'s live-token guard gained
  `AND r.consumed_at IS NULL` so a not-yet-expired tombstone can't keep an
  abandoned client alive (the M2 interaction). `provider._exchange_refresh_sync`
  switches on the outcome: `reuse` commits the family DELETE, logs a WARNING
  (`refresh-token reuse detected; revoked family for client_id=…`, no token
  leakage), and raises `TokenError("invalid_grant")`; `unknown` rolls back and
  raises. **Concurrency:** the tombstone UPDATE carries an
  `AND consumed_at IS NULL` guard + `rowcount == 1` claim check, so two
  concurrent rotations of the same live token are serialised by the row lock —
  exactly one claims it and mints a successor; the loser's guarded UPDATE
  matches 0 rows (the token was consumed out from under it = a reuse signal) and
  revokes the family. No double-successor, no `SELECT FOR UPDATE` needed.
  Design:
  [docs/superpowers/specs/2026-06-16-oauth-refresh-token-family-revocation-design.md](docs/superpowers/specs/2026-06-16-oauth-refresh-token-family-revocation-design.md).
- **Access-token family containment on reuse (closes the prior accepted
  limitation):** the family DELETE used to revoke refresh tokens only — access
  tokens already minted along the chain lived in `api_tokens` with no family
  correlation and stayed valid at `/mcp` until their ≤1h TTL. Migration
  `0030_api_tokens_refresh_family.sql` adds nullable
  `api_tokens.oauth_refresh_family_id` (UUID, partial index `WHERE … IS NOT
  NULL`). OAuth-minted access tokens are tagged with their refresh family
  (`access.mint_access(family_id=…)` — the code-exchange path reads the family
  via `load_refresh` after minting the refresh token; the rotation path reuses
  the `row.family_id` it already loads). On reuse detection
  (`RotateResult.family_id`, populated on the `reuse` outcome) the provider's
  reuse branch calls `access.revoke_access_family(family_id)` **inside the same
  transaction** as the refresh-family DELETE and **before** the commit, so both
  purges are atomic; the reuse WARNING gains `(access tokens purged=%d)`.
  Reuse-only — normal rotation predecessors still expire by their ≤1h TTL (eager
  revocation would break in-flight requests). Login tokens (`/v1/auth/login`,
  `oauth_refresh_family_id IS NULL`) are structurally immune to the family purge.
  `refresh.py` still touches only `oauth_refresh_tokens` (it reports `family_id`
  as data); `access.py` owns `api_tokens`; the provider orchestrates both. Design:
  [docs/superpowers/specs/2026-06-16-access-token-family-containment-design.md](docs/superpowers/specs/2026-06-16-access-token-family-containment-design.md).
- **Authorization-code single-use survives a failed exchange (#219):**
  `provider._exchange_code_sync` **commits the `consume_code` DELETE on its own**
  before minting anything. The burn and the mint used to share one transaction,
  so every failure path after the DELETE — the disabled-user branch's explicit
  `conn.rollback()`, or psycopg's rollback-on-exception from
  `mint_refresh`/`mint_access`/`touch_last_used` — took the DELETE with it and
  **resurrected the code** for the rest of its TTL, violating RFC 6749 §4.1.2. A
  client auto-retry (or a replay by anyone holding a copy) could then still
  exchange it; PKCE bounded the blast radius, which is why this was Medium not
  High. The trade is deliberate and in the right direction: a post-burn failure
  now costs the user a fresh consent round trip rather than leaving a replayable
  code. Contrast `refresh.rotate_refresh`, which needs no such split because its
  failure branches have no pending writes to lose. The concurrency guarantee is
  unchanged — a second exchange's DELETE blocks on the row lock, then matches 0
  rows and raises `invalid_grant`.
- **RFC 8707 resource indicators (shipped):** `/authorize` validates the
  client's `resource` against a configurable accepted set
  (`McpConfig.resource_indicators`, default
  `[mcp_resource_url(resource_server_url)]`) via the pure
  `mcp/oauth/resource_indicator.py`
  (`canonicalize_resource`/`resolve_accepted_resources`/`decide_resource`); the
  bound resource is carried through the consent blob →
  `oauth_authorization_codes.resource` → onto the minted access
  (`api_tokens.oauth_resource`) + refresh (`oauth_refresh_tokens.resource`)
  tokens, and enforced at `/mcp` in `access.load_access` (NULL = unrestricted;
  `/v1` REST unchanged). A missing `resource` is accepted (and bound to the
  first accepted resource) unless `oauth_require_resource_indicator = true`, in
  which case it's rejected with `invalid_request`. Migration
  `0031_oauth_resource_indicator.sql` adds the three `resource`/`oauth_resource`
  columns. **Accepted SDK limitations:** the SDK swallows the token-endpoint
  `resource` (validated at authorize time only) and lacks an `invalid_target`
  error code (a bad resource → `invalid_request`).
- **Integration test** [tests/test_mcp_integration.py](tests/test_mcp_integration.py):
  runs uvicorn in a thread + a real `mcp` client over Streamable HTTP, asserting
  the 5-tool list + ACL scoping (marked `integration`, skipped if the `mcp`
  client isn't installed).

## Desktop GUI admin mode (`gui/`, phases 2+3+4 shipped)

The Tauri 2 + Svelte 5 client gained an operator/admin mode gated on
`is_admin`. Design:
[docs/superpowers/specs/2026-07-23-admin-mode-tauri-gui-design.md](docs/superpowers/specs/2026-07-23-admin-mode-tauri-gui-design.md);
phase 1 (backend bearer auth) shipped in PR #203, phases 2+3 (frontend shell
+ Accounts panel) in the plan
[docs/superpowers/plans/2026-07-24-admin-mode-gui-phase2-3.md](docs/superpowers/plans/2026-07-24-admin-mode-gui-phase2-3.md),
phase 4 (Daemon panel) as a follow-up slice. **No Python changed** for phases
2+3+4 — the whole surface rides the existing `/v1/admin/accounts*` and
`/v1/admin/daemon*` JSON APIs (all bearer-capable via `require_admin()`; CSRF
is skipped for bearer, see `serve/admin/csrf.py::check_csrf`).

- **`is_admin` on the wire.** Rust `WhoamiResponse` carries it with
  `#[serde(default)]`, so a `serve` predating #203 still logs in (falls back
  to `false`) instead of failing to decode. The auth store's `logged_in`
  snapshot exposes `isAdmin`; MainView renders the Admin button from it.
  `screens/AdminView.svelte` is the tabbed overlay (Accounts / Daemon /
  Users / Imports); Accounts and Daemon are implemented, Users and Imports
  are placeholders.
- **Daemon panel (phase 4).** `components/admin/DaemonPanel.svelte` fetches the
  fused `GET /v1/admin/daemon` view (process state + `daemon_heartbeats` +
  recent log) and self-refreshes every `POLL_INTERVAL_MS = 2000` (mirrors the
  web panel's `DAEMON_PANEL_POLL_SECONDS = 2`); the interval is cleared in
  `onDestroy`. Rust proxies live in
  [gui/src-tauri/src/commands/admin/daemon.rs](gui/src-tauri/src/commands/admin/daemon.rs)
  (`daemon_tests.rs` split out) — `get_admin_daemon` (GET), `lifecycle_admin_daemon`
  (POST `/daemon/{start,stop,restart}`, decodes the **202** transitional status),
  `reload_admin_daemon` + `restart_account_sync` (POST, decode `{command_id}`).
  TS wrapper [gui/src/lib/api/admin_daemon.ts](gui/src/lib/api/admin_daemon.ts).
  **Staleness is the server's per-heartbeat `stale` flag alone — never a client
  clock** (matches the web panel + #148); stale rows render red. **Lifecycle
  (start/stop/restart) buttons are disabled when
  `supervise_daemon_externally`** (the launchd deployment), while reload +
  per-account restart-sync stay enabled — those are DB-mediated (Plane A) and
  work regardless of who owns the process. A rejected control (busy-guard /
  external-stub **409**, mapped by `isConflict`) surfaces as a visible
  `daemon-action-message`, never an inert button. The per-account restart-sync
  dedup (idle+poll workers → one button) is the pure, unit-tested
  [gui/src/lib/daemon_view.ts](gui/src/lib/daemon_view.ts)`::restartSyncAccountIds`.
  **CI-trap note:** any admin panel that fetches on mount MUST be stubbed in
  `AdminView.test.ts` **and** `MainView.test.ts` (both mount the overlay) or an
  unhandled promise rejection leaks while vitest still reports "passed" (the
  bug PR #205 caught post-push).
- **Rust proxies** live in
  [gui/src-tauri/src/commands/admin/accounts.rs](gui/src-tauri/src/commands/admin/accounts.rs)
  (tests split into `accounts_tests.rs` via `#[cfg(test)] #[path = …]` to keep
  the module under the size guideline). Each endpoint has a mockito-testable
  `fetch_*`/`post_*` helper + a keyring wrapper + a thin `#[tauri::command]`,
  mirroring `commands::auth_change_password`. `http/client.rs` gained
  `http_patch_json` + `http_delete`.
- **The PATCH body MUST omit unset fields.** `AdminAccountPatch` marks every
  field `#[serde(skip_serializing_if = "Option::is_none")]`. This is
  load-bearing, not style: `api.admin.accounts.update_account` writes *every
  key present* in `fields`, so a serialized `"imap_host": null` **blanks the
  column**. Pinned by
  `patch_update_omits_unset_fields_entirely`. `AccountForm` mirrors this on
  the TS side — it diffs against the loaded row and sends only changed keys,
  which is why a cleared IMAP port cannot be sent. For the same reason
  **`auth_method` is locked on edit** (the selector is `disabled`): every
  transition dead-ends under omit-unset — `→ oauth2` needs an `oauth_provider`
  the web consent flow supplies, and `→ archive` needs `imap_host`/`imap_port`
  nulled, which omit-unset can't express. Changing an account's auth method
  means recreating it. A non-numeric port is rejected inline (not silently
  dropped). Folder-filter editing is not yet in the form (issue #206).
- **Pure modules** (project convention — logic out of components):
  `lib/admin_error.ts` (`httpStatusOf`/`isConflict`/`isForbidden`, a
  depth-bounded walk of the nested `{kind, detail}` Rust error shape, so the
  UI can *act* on a status instead of string-matching `formatError`) and
  `lib/admin_auth_method.ts` (`hasImapEndpoint`/`usesStoredPassword`).
  Routing the auth-method comparisons through functions also stops TS from
  narrowing a local `$state` to its initialiser's literal type, which made
  `authMethod !== "archive"` look unreachable to `svelte-check`.
  `lib/search_paging.ts` (`statedSort`/`isCursorRejected`) is the third, and
  `admin_error.httpStatusOf` gained its first non-admin consumer through it —
  see the #311 bullets under **Browse & search pagination**.
- **Deliberately absent — do not "finish" without backend work first:**
  Gmail **Connect**. `POST /v1/admin/accounts/{id}/oauth/start` lives in
  `oauth_router.py`, which #203 did *not* swap to `require_admin()`, so it is
  still cookie-only and a bearer client cannot start the flow. The design's
  completion check ("poll secret status until the refresh token appears")
  also has no backing field — `_account_dict` exposes no secret status and no
  `/v1/admin` endpoint reports one. Both are backend gaps. `clear_secret`
  likewise has a service function but no JSON route.
- **`--all-targets` clippy is clean now, and CI still doesn't run it.** The
  long-standing `approx_constant` failure in
  `gui/src-tauri/src/commands/search.rs` (a `3.14` dummy `took_ms`) is fixed.
  Note the underlying gap remains: CI gates clippy (`gui-ci.yml` runs `cargo
  clippy --locked -- -D warnings`) **without `--all-targets`**, so
  `#[cfg(test)]` modules are never linted and a lint regression inside a test
  module will not turn `main` red. Run `cargo clippy --all-targets -- -D
  warnings` locally when touching Rust tests.

## Conventions

- **Branch before the first commit; land through a PR. Never push a substantive
  change straight to `main`.** Branch names follow the change:
  `fix/…`, `docs/…`, `gui-client-N`. Every substantive change has gone this way
  (#250, #247, #244, #243, #242, #240, #238, #233).

  CI is *not* the reason — `python-ci` triggers on `push: branches: [main]` as
  well as `pull_request`, so a direct push still gets the full Linux test signal.
  The PR is the **review gate**: the chance to read a multi-part behavioural
  change as one diff before it is on `main`. That is also why it cannot be
  recovered afterwards — a PR opened once the commits have landed has an empty
  diff, and the only way back is reverting and re-landing.

  **"Push it" is not approval of the shape.** Approval to push is approval to
  push whatever was already set up, so get the branch right first. (Session 14
  pushed four commits directly to `main`; CI passed and the work stood, but the
  review gate was gone and could not be restored.)

  **Base every branch on `main`, never on another in-flight branch — and put a
  session's code and its handoff in ONE PR.** Session 25 based its handoff PR
  (#298) on the *fix* branch (`fix/295-296-version-diagnostic-reach`) rather
  than on `main`. The operator merged the fix PR (#297) to `main` at 10:18:30
  and #298 merged into the already-merged fix branch 13 seconds later, so
  everything in #298 — **the entire review round: four closed gaps, 14 tests,
  the README and CLAUDE.md updates, the handoff** — landed on a branch nothing
  would ever merge again. `main` kept the pre-review fix and lost the rest, with
  no failing check and no open PR to notice. Session 26 recovered it by
  cherry-picking onto `main` (see the `version_report` §296 notes, all of which
  arrived that way).

  The failure is silent by construction, so the rule has to be structural: a
  second PR stacked on an in-flight branch is stranded the instant the first one
  merges, and *nothing* reports it — `gh pr list` is empty, CI is green, the
  branch is "merged". **`git log --oneline main..origin/<branch>` after a merge
  is the check**; a non-empty result on a branch whose PR is already merged is
  exactly this bug. One PR per session removes the window entirely, which is why
  that is the convention rather than merely the advice.
- **No comments unless the WHY is non-obvious.** Don't restate the SQL or the
  Python.
- **Don't write `.eml` fixtures to disk** — `tests/_eml.py` builds messages
  programmatically with `email.message.EmailMessage`. Same goes for any future
  test fixture: generate, don't check in.
- **DB tests** TRUNCATE before each test (see the `db_conn` / `pool` fixtures).
  Tests must work against the live test DB; never `DROP TABLE`.
- **No `cur.fetchone()[0]` without `assert row is not None` first** — mypy is
  enabled (`[tool.mypy]` in `pyproject.toml`) and will flag it. Note that mypy
  only catches this when the `conn` parameter is annotated
  (`conn: psycopg.Connection`); on an unannotated `conn` the cursor is `Any`
  and the violation passes silently, so annotate every new DB helper.
- New SQL goes in a new numbered migration file. **Never edit a migration
  that has been applied anywhere** — add the next-numbered file instead.
  Latest is `0036_api_keys.sql`; next free slot `0037_*.sql`.
  (2B.4 and 2B.5 added no migration — the supervisor, routes, CLI, and admin
  panel are stateless and reuse `0023_daemon_heartbeats.sql` +
  `0024_daemon_commands.sql`.)

## Testing notes

- `LOCALMAIL_TEST_DSN` defaults to the **`localmail_test`** database, not the
  live `localmail` one. This is intentional and important — running pytest
  must not touch live archives.
- **One pytest session at a time per test database, enforced (#335, #329).**
  `db_conn` opens every test with `TRUNCATE … RESTART IDENTITY CASCADE` over
  every data table, so two pytest processes on one database delete each
  other's seeded rows and seed rows into each other's queries. Nothing errors
  — the truncate *succeeds* — so it surfaces as impossible archive states
  ("48 rows where 9 were seeded"), mid-insert reads, and tests that pass alone
  and fail in company, all of which read as product bugs. The `db_session_lock`
  fixture holds a **session-level Postgres advisory lock** keyed on the
  database name; `db_dsn` requests it, so `apply_migrations` cannot run before
  the lock is held and two sessions cannot race the migration runner either.
  That ordering is a **fixture-graph dependency, not a statement order**, and
  is pinned as such — a line-order pin inside one function reads as though it
  covers this and is undone by any refactor that splits them. Rules:
  [tests/_db_session_lock.py](tests/_db_session_lock.py).
  - **#335 named the wrong mechanism, and the correction is the point.** It
    attributed this to `TRUNCATE` *blocking* on a connection left open by a
    previous test's `open_pool`. Measured against that: with a `lock_timeout`
    armed on the truncate, three full-suite runs and seven targeted runs
    recorded **zero** blocked truncates, while one concurrent pytest process
    reproduced the exact tests the issue names on the first attempt. A second
    session explains every symptom the issue lists; a lingering pool explains
    none of them. Note the issue *does* carry a real `DeadlockDetected`
    traceback, so contention is not impossible — it simply did not reproduce
    across those ten instrumented runs, and it is not what #329 was. Do not
    reach for a truncate-side fix without first reproducing one; the
    instrumentation was temporary and is not in the tree.
  - **A second session waits, then fails by name** — as a single
    `pytest.exit` line, not a raise: a session fixture that raises reports one
    ERROR block per dependent test, ~850 lines for a single file against a
    suite of ~1000 DB tests, which buries the one sentence saying what to do.
    `DEFAULT_LOCK_TIMEOUT_S` is the literal 600 s (a full suite is ~3 min
    here); `resolve_lock_timeout_s` applies the
    `LOCALMAIL_TEST_DB_LOCK_TIMEOUT_S` override **per call, not at import** —
    read at import, a typo was a bare `ValueError` that failed collection of
    the whole suite, including the ~2000 tests that never touch a database.
    The two are kept apart so the test pinning "long enough for a full suite"
    asserts the constant; asserting the resolved value turns the documented
    override into a red suite. The wait is announced once through
    pytest's terminal reporter — fixture-setup output is captured, so a plain
    `print` would be invisible for exactly as long as the wait lasts, which is
    the window where silence reads as a hung run.
  - **An advisory lock, not a row in a table**, because it dies with its
    backend: a run killed with SIGKILL releases it instead of wedging every
    later run. **Keyed per database**, so a session pointed at its own DSN
    never blocks on the shared one — the escape hatch for anyone who genuinely
    wants two suites at once.
  - **That same property is the guard's own failure mode, so it is re-checked
    rather than trusted.** The lock rides the most idle connection in the
    suite — open for the whole run with no traffic — which is the first thing
    a Postgres restart, an `idle_session_timeout`, a failover or a reaped TCP
    flow takes out. The lock then dies with that backend, a second session
    acquires freely, and **nothing notices**: psycopg does not see a dead
    backend until the connection is used, so `conn.closed` still reads `False`
    and even `close()` returns clean. `db_conn` therefore calls
    `verify_still_held` immediately **before** its `TRUNCATE` — guarding the
    destructive act itself, which bounds the damage to one test — and that
    ordering is pinned structurally. The check asks `pg_locks`, **not** whether
    the connection is alive: a pooler issuing `DISCARD ALL` releases every
    advisory lock while the socket survives, and a liveness ping would call
    that healthy.
  - **Postgres already scopes advisory locks per database** (`pg_locks` keys
    them by database OID), so the per-database key is defence in depth and is
    what keeps `busy_message` honest about *which* database is contended — it
    is **not** what provides the isolation. Three comments claimed otherwise
    and were wrong; the refutation is one query (`pg_try_advisory_lock(K)`
    succeeds concurrently in two databases). It matters because a shared probe
    database *does* collide: the exclusion tests below create their own.
  - **`database_name` defers to libpq** (`psycopg.conninfo.conninfo_to_dict`),
    never to `urlsplit`. `LOCALMAIL_TEST_DSN` may legitimately carry the
    keyword/value form (`host=… dbname=…`), which `urlsplit` returns *whole*
    as the "database name" with no error — so two sessions spelling one
    database differently derived two keys, both acquired, and the guard
    excluded nothing. That is the same silent failure the blake2b rule below
    exists to prevent, one function earlier; it also put `password=…` into
    `busy_message`, which is printed to the terminal and into CI logs.
  - **The key is a blake2b digest, never `hash()`.** `hash()` is salted per
    process, so two sessions would derive different keys, both acquire, and
    the guard would exclude nothing while every unit test still passed. That
    is the one way this fails silently, so it is pinned **across processes**
    (`test_the_key_is_stable_ACROSS_processes` spawns a subprocess) — the
    same-process assertion beside it is satisfied by the broken version.
  - **The exclusion tests create their own scratch database**, because the
    live session holds `localmail_test`'s lock for the whole run — that is the
    fix — so a test cannot acquire that key to prove anything. It must be
    unique per session (`localmail_locktest_<pid>`, dropped at teardown): a
    fixed probe database means every concurrent suite contends on one key, so
    the test file for the concurrency guard would itself break the escape
    hatch the guard documents. Measured against the shared `postgres` it used
    to use: two suites, **6 failures apiece**. `CREATE DATABASE` needs only
    `CREATEDB`, which the test role has — and notably *not* the superuser that
    `CREATE EXTENSION vector` would, since nothing migrates that database.
    A missing maintenance database is caught as `psycopg.OperationalError`
    alone, not `Exception`: the broad form also swallowed a bug in the
    fixture's own DSN rewrite and reported it as an environment fact, turning
    the entire exclusion proof into six silent skips on a green suite.
  - **CI reports `1 skipped`, and that is pre-existing.** The `0 skipped`
    reading is macOS-only. The control run on `main` at `815e74b` reads
    `2950 passed, 1 skipped` against 2951 collected, so the skip predates this
    work; do **not** read it as a missing uv extra (this repo's usual meaning
    for a non-zero skip count) without checking a `main` run first.
  - **Adding `pytest-xdist` needs `_db_session_lock.py` changed first.** It is
    not a dependency today. Each worker is its own process, so under one shared
    DSN exactly one acquires and the rest block for `DEFAULT_LOCK_TIMEOUT_S`
    and then fail — which reads as the guard being broken rather than as the
    workers sharing a database they must not share. The fix then is per-worker
    DSNs (`PYTEST_XDIST_WORKER` suffixing), which the per-database key already
    supports. Migrating such a database needs `CREATE EXTENSION vector`
    (migration **`0004`**, not `0001`), and `vector` is **not** a trusted
    extension, so that needs superuser — which the reference cluster's role
    does not have. This does not generalise: CI passes `POSTGRES_USER:
    localmail` to the `pgvector` image, making that role the bootstrap
    superuser, so the constraint does not bind there. Measure before relying
    on it either way.
  - **The guard covers the database now, not just pytest (#337, fixed).** The
    **five** standalone harnesses under `tests/acceptance/` truncate the same
    tables against the same `LOCALMAIL_TEST_DSN` and took no lock, so running
    one beside a suite reproduced exactly this corruption, in both directions
    and with the same silence. (This entry and the #337 issue both said
    **six**; `browse_explain_lib.py` touches the database but is imported by
    `run_browse_explain.py`, never started, so its work already runs inside
    that harness's lock. The Layout section's own list has always named five.)
    Each entry point now wraps its database work — from `apply_migrations`
    onward — in
    [tests/acceptance/_harness_lock.py](tests/acceptance/_harness_lock.py)`::harness_db_lock`.
    - **The helper and the rule that requires it live in one module**, the
      `blob_temps.py` minting-beside-matching call, and here the reason is
      sharper than usual: **nothing collects these files.** They match no
      `python_files` pattern, so no conftest fixture can arm them and the
      call has to be written into each `main()` by hand — which is precisely
      the kind of obligation that is forgotten. `harness_lock_error` walks
      each entry point's **AST** and reports any `DB_ENTRY_CALLS` member
      (`apply_migrations`, `open_pool`, `connect`) sitting outside a
      `with harness_db_lock(...)`. Entry points are enumerated from the
      filesystem, so a sixth harness is in scope the day it lands.
    - **"Somewhere in main" is not the rule; "before the first touch" is.**
      A harness that migrates and *then* locks has taken no lock at all — the
      truncate has already run — so the rule is about position, not presence.
    - **The walk follows `main` into its helpers, and that was a review fix,
      not the original shape.** Reading `main` alone left the rule blind to
      the most natural refactor there is — extract the database phase — and
      `run_chunk_insert_bench.py` **already has that shape**: `_run_mode`
      holds both the `psycopg.connect` and the `TRUNCATE`, so the only thing
      the rule checked in that file was the `apply_migrations` in `main`.
      Hoisting `_run_mode`'s call out of the `with` left every truncate
      unlocked and the rule silent — #337 admitted by the guard written to
      end it. A helper reached from *inside* the lock is not followed (it is
      covered whatever it does); one reached from outside is followed with
      its own lock-covered set computed, so a helper that takes the lock
      itself passes. Mutual recursion is bounded by a `seen` set.
    - **Module-level database work is reported separately**, because no
      `with` inside `main` can cover something that already ran at import.
    - **`_local_functions` keeps the *last* `def main`**, which is what
      Python binds; the rule used to read the first, i.e. audit a `main`
      that never runs.
    - **The lock check matches a name, so the module must import the real
      helper.** A local `harness_db_lock` that yields `None` satisfies the
      position rule while locking nothing. The import check runs *after* the
      position rule — it is the backstop for shadowing, which only matters
      once the position rule passes.
    - **Two imprecisions, both deliberate and both written down.** A callee
      *rename* (`from psycopg import connect as pg_connect`) dodges the rule,
      because following it means resolving imports rather than reading call
      names — the earlier claim that "an aliased import cannot dodge it" was
      true only of `import psycopg as pg`. And the combined
      `with harness_db_lock(dsn), psycopg.connect(dsn):` is *reported* though
      it is genuinely locked, since only the `with` body is covered — it
      fails closed, so it costs a spurious report and never a missed one.
      **#340 is the third and is open**: the rule compares call positions,
      never arguments, so locking one database while working against another
      passes. Latent (all five harnesses use one `dsn`), and a correct check
      needs parameter-flow analysis across the helper walk above.
    - **The AST, not the text**, for the reason `_mentions_version_option`
      gives: every harness names the helper in prose while explaining why it
      calls it, and a substring scan reads that as compliance.
    - **A contended database announces one line and exits `BUSY_EXIT_CODE`
      (3).** `SystemExit("some message")` prints the string and exits **1** —
      which is also what an eval returns when it fails its own acceptance
      gates, so a shell loop could not tell the two apart. It shipped that way
      first and the end-to-end test is what caught it; the unit test's
      `code != 0` had passed *vacuously*, because `code` was the message
      string. Message and status are separate channels on purpose.
      - **The replacement was vacuous too, and the constant is pinned against
        literals now.** Both assertions compared the observed status against
        `BUSY_EXIT_CODE` itself, so both sides moved together: `BUSY_EXIT_CODE
        = 0` left the whole file green while the real harness subprocess
        exited **0** on a contended database — the constant's own stated
        purpose, unenforced. A self-referential comparison is the same trap as
        the `!= 0` it replaced, so the value is asserted directly, the
        `DEFAULT_LOCK_TIMEOUT_S` arrangement one module over. `2` is excluded
        as well: that is argparse.
    - **The lock is re-checked, not trusted for the length of the run.**
      `_db_session_lock` states the obligation in the imperative — the lock
      rides the most idle connection in the run and dies silently with its
      backend — and `db_conn` discharges it before every `TRUNCATE`. The
      harnesses did not, while holding it across the longest work in the tree
      (a ~250 MB model download, a ~100 s docling init, a 100k-row seed) with
      destructive statements at the far end. Two halves now: `harness_db_lock`
      verifies **on the way out**, raising `SessionLockLost`, which cannot
      undo a truncate that already raced but turns "this run's numbers are
      quietly wrong" into a failed run; and `checkpoint(lock)` is called
      before the two truncates that are *far* from acquisition
      (`run_browse_explain`'s final clean-up, `run_chunk_insert_bench`'s
      per-mode truncate, which on the default `--mode both` fires after a
      complete benchmark). The exit check runs **only when the body completed**
      — rewriting every harness crash into `SessionLockLost` would bury the
      real cause.
    - **`acceptance_coverage_error` is the reverse cross-check, and it was
      missing.** `harness_entry_points` globs `run_*.py`, which is a naming
      habit rather than a rule, so a harness called `bench_*.py` or dropped in
      a subdirectory took no lock and was never asked to — coverage shrinking
      with every test still green. Every module under `tests/acceptance/` that
      names a `DB_ENTRY_CALLS` member must be an entry point or be listed in
      `COVERED_LIBRARIES`, and an allowlisted library may do no database work
      at **import** time, since that runs before the importing harness locks.
      This is `_pool_leaks.py`'s `pool_constructor_calls` arrangement, which
      exists because `missing_seam_error` asks only whether a name is present.
      It is also what finally pins the standing claim that
      `browse_explain_lib.py` is safe by virtue of being imported.
    - **The end-to-end pin runs a real harness subprocess against the database
      this very pytest session is holding**, requesting `db_session_lock` so
      the precondition is real rather than assumed. Every other test injects a
      fake `acquire`, so this is the only one proving the wiring — that the
      import resolves on the harness's own `sys.path`, that `--dsn` reaches
      the helper, and that the refusal survives as a process exit code. It
      drives `run_browse_explain.py` because that harness has **no required
      argparse arguments**: argparse runs *before* the lock, so a harness
      needing `--queries` would exit 2 first and pass the test for the wrong
      reason.
    - **`busy_message`/`waiting_message` say "test run", not "pytest
      session".** The holder may now be a harness, and naming pytest sends an
      operator hunting a process that need not exist — a confident, wrong
      diagnosis, which is the one thing those strings exist to avoid.
    - **Two mutations survived the first battery and both were test defects,
      not code defects.** `_is_lock_call` forced to `True` left every test
      green, because the only non-compliant fixture had no `with` at all and
      never reached it; and the assertion that caught the replacement,
      `"apply_migrations" in problem`, was satisfied by the *remedy* sentence
      ("from apply_migrations onward") rather than by the uncovered-call list
      — the `__version__ in output` trap, one module over. The assertions
      anchor on `"session lock: apply_migrations"` now, and there are
      fixtures for a wrong context manager of **both** AST shapes
      (`psycopg.connect(...)` is an `Attribute`, `ExitStack()` a `Name`;
      each branch survived until its own fixture existed).
    - **The `DB_ENTRY_CALLS` comment cited a pin that could not hold.** It
      named `test_the_db_entry_calls_are_the_ones_the_harnesses_actually_use`
      for the dropped-name property, but that test asserts a non-empty
      *intersection*, which `apply_migrations` satisfies for all five — so
      dropping `open_pool` or `connect` was a surviving mutation, and the rule
      then stopped seeing a pool opened before the lock. The directional
      `test_every_db_entry_call_name_is_reached_by_some_harness` is the pin;
      the per-file one keeps its narrower claim (each entry point is a DB
      harness) and now says so.
    - **The end-to-end pin bounds its own counterfactual.** It spawns a fully
      destructive harness against the database the suite is using, and its
      safety rested entirely on the lock still being held — the one thing
      that must be re-checked rather than assumed. It calls
      `verify_still_held` immediately before the spawn and passes
      `--total-rows 1`, so a wrong precondition costs one row instead of
      100,000. It does **not** pin that `--dsn` is read, which its docstring
      used to claim: the subprocess inherits `LOCALMAIL_TEST_DSN`, so the
      harness resolves the same database from its own default either way.
      The argparse rationale beside it was backwards too — a required-argument
      harness exits **2** and *fails* the assertion (which demands 3), rather
      than passing it for the wrong reason; that wording was left over from
      the draft asserting `!= 0`.
  - **The leaked-pool warning that ran alongside this is #321, now fixed** —
    see the next entry. It was never the cause of the corruption above; the
    instrumented runs showed zero contention.
- **Leaked test pools are closed by one autouse fixture, not by 38 files
  (#321).** Two seams, both real. `create_app` opens its pool eagerly
  (`open=True`) and closes it only in the FastAPI lifespan's `finally`, so a
  bare `create_app(...)` or a `TestClient(app)` used without `with` leaks it;
  and **`Daemon.stop()`/`join()` never close `self.pool`**, so 13 daemon tests
  across four files plus one `create_searcher` test leak one each through
  `db.open_pool`. The pool then holds its
  connections until the GC reaches it, and `ConnectionPool.__del__` joins the
  pool's own worker thread *from inside that thread* → `RuntimeError: cannot
  join current thread`, reported as a `PytestUnraisableExceptionWarning`
  against whichever unrelated test was running when the collection fired.
  **That is why the warning names a different set of files on every run and
  never names the leak site** — the handoff's five and this session's four
  overlap in one file. Rules:
  [tests/_pool_leaks.py](tests/_pool_leaks.py); fixture
  `conftest.close_leaked_pools`; 37 tests in
  [tests/test_pool_leaks.py](tests/test_pool_leaks.py).
  - **The `db.open_pool` half was invisible on macOS and reported on
    Linux/3.13.** The GC decides when `__del__` runs, so a platform can hide
    the whole thing: three green macOS full runs read 2 warnings while CI's
    3.13 leg read 3. **Do not conclude from a clean local run that this class
    of leak is gone** — instrument the seam instead. A temporary
    `pytest_sessionstart` plugin that wraps `localmail.db.ConnectionPool` and
    reports unclosed pools at `pytest_sessionfinish` named all 14 sites with
    their creation stacks in one run; reading the warning would never have,
    because it names the wrong file by construction.
  - **The per-file sweep #321 proposes was measured and rejected**: **41
    files, 162 call sites** — and as worded (wrap each in
    `with TestClient(...)`) it breaks the tests that exist to assert
    `create_app` alone is side-effect-free, since running the lifespan is
    exactly what binds the daemon control socket. The sweep also buys
    discipline where the seam buys construction: a new inline `create_app(...)`
    cannot reintroduce the leak.
    **Three counts, deliberately kept apart** — an earlier wording reported
    `34` for all of them, which is a number no measurement produces. `41`
    files call `create_app` (the sweep's scope, the set `162` also measures);
    `33` of those actually leak one (the other 8 close it, or stub
    `create_app` outright); `5` more leak a `db.open_pool` pool, disjoint from
    the 33 — hence `38` affected files in the heading.
  - **A seam is the `ConnectionPool` name in the module that builds the pool** —
    `localmail.serve.app` and `localmail.db`, listed in `POOL_SEAMS`. Each is
    resolved from that module's globals on every call, so patching it reaches
    every caller. Patching `create_app` itself would reach none of them: each
    test module binds it into its own namespace at import time.
  - **`missing_seam_error` reports rather than skips.** An aliased import
    (`... as Pool`) leaves the attribute absent, nothing patches, every pool
    leaks again — and no test fails, because closing a pool that was never
    recorded is a no-op. It deliberately does **not** check the seam's
    *identity*: swapping in a different pool class under the same name is a
    legitimate change the wrapper handles correctly.
  - **The fixture skips a seam whose module is not in `sys.modules`**,
    which keeps ~0.5 s of FastAPI import off every unit-only run (a bare
    `pytest tests/test_pgtext.py` is 0.27 s in-pytest, so the import would
    have doubled it). That inference — absent module ⟹ no collected test can
    call `create_app` — holds only because pytest imports every collected
    module before running any test, so it is sound for a **module-level**
    import and false for a function-local one. Seven such imports existed, in
    four files; they are hoisted, and the pure
    `function_local_serve_app_imports` scans every `.py` under `tests/` to
    keep it that way. It reads the **AST**, not the text, because
    `test_pool_leaks.py` quotes the forbidden import verbatim in the source
    strings it feeds the scanner as test cases — a substring scan flags those
    three lines. (The `_mentions_version_option` precedent is a *docstring*
    case; an earlier wording here claimed this rationale quoted the import
    too, and it does not. The reason is sound, the example was wrong.)
  - **The inference is verified at teardown, not trusted — and that is the
    load-bearing half.** A scanner cannot enumerate the ways a module arrives:
    the sibling `from localmail.serve import app`, an
    `importlib.import_module`, a `__import__`, or a lazy import inside `src/`
    — `cli.py`'s `serve_cmd` has one, so **production code** can load the
    module mid-test with no test-file import anywhere. Three such routes were
    live in the tree when this shipped, one of them inside the file that
    enforces the rule. `late_seam_error` therefore compares what is loaded at
    teardown against what was patched at setup and raises naming the test, so
    every route is caught by its outcome. The scanner stays as belt to that
    braces — it names the offending *line*, which the outcome check cannot.
  - **`POOL_SEAMS` is checked against `src/`, not just against itself.**
    `missing_seam_error` asks only whether the *name* is present, so a third
    module growing a pool, an existing one switching to a fully-qualified
    `psycopg_pool.ConnectionPool(...)` call, or a move to
    `AsyncConnectionPool` all leave the name intact, nothing patched, and no
    test failing. `pool_constructor_calls` walks every `src/localmail/**/*.py`
    for a call whose callee ends in `ConnectionPool` and the set of modules
    must equal `POOL_SEAMS` — mutation-proven in both directions (drop a seam;
    add a third constructor).
  - **The fixture owns a private `pytest.MonkeyPatch`.** Sharing the
    `monkeypatch` fixture meant a test calling `monkeypatch.undo()` reverted
    the seam patch too; five files call it, and one is two tests away from
    building a `Daemon` afterwards.
  - **A broken seam ends the run with one line** (`pytest.exit`), the call
    `db_session_lock` makes for the same reason — the condition is a property
    of the tree, identical for all ~3000 tests, and an ERROR block per test
    buries the sentence that says what to do. The *fixture's* use of
    `missing_seam_error` is pinned, not just the pure rule: replacing the
    report with a `continue` used to leave the whole suite green.
  - **`error::pytest.PytestUnraisableExceptionWarning` in `filterwarnings`
    is the regression gate.** Without it the only evidence this fixture still
    works was a human comparing warning counts between runs — and the leak is
    invisible on macOS while reported on Linux/3.13, so nobody would compare
    on the platform that shows it. The test it fails is arbitrary (the GC
    picks it); the message, `cannot join current thread`, is the diagnosis.
    It is one of **two** entries now — `PytestUnhandledThreadExceptionWarning`
    joined it for #299's worker-survival channel; the two are independent
    gates over different thread failure modes, so neither subsumes the other.
  - **`unclosed` filters before closing** so the count `close_pools` returns
    is the number of pools that genuinely leaked; `close()` is idempotent, so
    the filter is about the claim, not about safety.
  - **Measured**: `main` at `5dbaea0` runs 2988 passed with **6** warnings
    (2 pre-existing `websockets` deprecations, #25, + **4**
    `cannot join current thread`); the branch runs **2** warnings — the
    websockets pair alone — on macOS *and* on both CI legs. Only the warning
    count generalises across platforms: the pass count is `3025 passed,
    0 skipped` on macOS and `3024 passed, 1 skipped` on CI (both legs,
    verified) — the same 3025 tests, with the `1 skipped` that the #335 entry
    above records as pre-existing. macOS is the odd one out on skips, not CI.
    The instrumented run reports **0 of 131** `localmail.db` pools unclosed,
    where the first pass reported 14. Closing costs nothing measurable (a
    serve-heavy pair of files: 6.84 s with, 6.99 s without).
  - **"Safe by construction" is scoped to `create_app` and `open_pool`
    callers.** About a dozen test files build a `psycopg_pool.ConnectionPool`
    directly; those bypass both seams and are unrecorded. They all close what
    they build today, and the `filterwarnings` gate above is what now makes it
    a failure rather than a warning if one stops.
  - **`Daemon` does not close its own pool, and that is unchanged production
    behaviour** — `run_forever` owns the process, so the pool dies with it.
    The fixture is a *test* backstop; it is not a statement that `Daemon`
    should close it, and #321 is not the place to change that.
- **A busy-guard pin holds its window open; it never races a timer (#299).**
  The two tests #299 filed as flaky were flaky for a reason the issue did not
  name, and only one of them needed a change.
  - **The reported flake was a concurrent pytest session — #329/#335, closed
    by #336.** Measured rather than argued: an instrumented copy of the
    route-level pin used **6.7 ms of its 3000 ms** budget, a 450× margin, which
    cannot explain a test the issue reports failing in **3 of 3** runs. The
    mechanism was then reproduced directly, by running the pair beside a
    non-pytest process performing the per-test `TRUNCATE` a second session
    would: **8 of 8** runs failed, 2 with both tests failing — the issue's
    pattern exactly. And the failure is **not a timing one**: the interferer
    truncates `api_users`, the admin session's principal vanishes, the route
    303s to the login page, and `_poll_state` decodes HTML as JSON. Control on
    current `main`: **20 of 20** clean.
  - So **`test_route_driven_login_failures_persist_audit_rows` got no
    change.** It has no concurrency of its own and its exact-count assertion
    is correct; #336 is the fix. Do not "harden" it — there is nothing there
    to harden, and a retry or a tolerance would hide the next real #335.
  - The busy-guard pins were rewritten anyway, because **a pin that must win a
    wall-clock race is one a loaded runner eventually breaks and the next
    session then learns to ignore** — the failure mode this file already
    records for pins that "were weaker than they read".
    [tests/_gated_supervisor.py](tests/_gated_supervisor.py)`::GatedStopSupervisor`
    parks `stop()` on an event, so the second request is issued while the
    first is **provably** in flight. Only `stop()` is overridden;
    `request_stop` and the guard it consults are the production ones, and the
    guard reads *the thread*, not what the thread runs.
  - **The parked thread holds no lock**, which is load-bearing: it waits
    *before* delegating to `super().stop()`, the call that takes `_lock`.
    Parking under the lock would block the very `request_stop` whose refusal
    is being asserted, and the test would hang rather than fail.
  - **Both pins start the child synchronously**, not through the route: a
    routed start spawns a lifecycle thread of its own, which would still be in
    flight when the first stop lands and answer it with the very 409 the test
    attributes to the stop.
  - **`gate_timed_out` keeps the residual bound honest.** The wait cannot be
    unbounded (a test failing before its `finally` would hang the suite), but
    an expired park lets the lifecycle thread finish and the guard then
    *correctly* returns 202 — which reads as a broken guard. Asserting the flag
    reports the window instead of a verdict: the rule that a test whose subject
    is a refusal must pin *why* it was refused.
    - **The flag must be read BEFORE the verdict it explains**, which review
      caught the unit pin not doing: it sat after a `pytest.raises`, so an
      expired window aborted on `DID NOT RAISE` and the explanatory assertion
      was never reached — the misleading message the flag exists to replace,
      in the pin that introduced the flag. Both consumers now observe first
      (capture the state and the refusal), then read the flag, then judge.
  - **The settle assertion stays inside the `try`.** Both rewritten pins first
    moved it after the `finally`, where `sup.stop()` sets STOPPED from the main
    thread whatever the lifecycle thread is doing — so the poll passed with the
    accepted op wedged **forever** (measured), satisfied by the test's own
    teardown. On `main` it had been inside the `try` and was genuine. The pin
    releases the gate itself and watches the *accepted* stop settle, keeping the
    teardown as belt-and-braces.
  - **`_lifecycle_thread` is read off the supervisor, never `threading.enumerate()`.**
    A process-wide scan asserting "exactly one" thread by production name is a
    cross-test coupling — those threads are daemons and no `stop()` joins them —
    so the file that exists to remove wall-clock margins would have rested on
    one. `_spawn_lifecycle` assigns the attribute under `_lock` before
    `Thread.start()`, so it is set the moment `request_stop` returns.
  - **The double is single-use, and says so.** Both events latch and `release()`
    is permanent, so every signal means "at some point, ever". A second park on
    the same instance would return instantly from the previous cycle's signal
    and put the busy-guard assertion back on the clock with nothing failing.
  - **Two mutation results are recorded rather than smoothed over.** The
    *unit* busy-guard pin (`test_daemon_supervisor.py`) **survives** removing
    the gate — without it the window is milliseconds against a microsecond
    assertion path, so no mutation can demonstrate the gate there; it removes a
    small race, not an observable one. And
    `test_a_gated_stop_parks_instead_of_finishing` was first written asserting
    only that the state was still STOPPING, which survived that same mutation
    **by luck** — the identical lucky-win the gate exists to remove. It joins
    the thread now. Its timeout is one-sided by construction: it bounds only
    how long a *broken* gate is given to reveal itself.
  - **The three `time.sleep()` calls in `test_daemon_extract_thread.py` are
    deleted, not lengthened.** `start_workers()` calls `Thread.start()` for
    every worker synchronously, `Thread.start()` returns only once the thread
    is registered, and `threading.enumerate()` covers the active *and* limbo
    tables — so they waited for something that had already happened. Do not
    add one back; `_live_thread_names()` carries the reason.
    - **They were also proving survival, and that half had to be replaced.**
      Registration precedes the target running a line, so a name lookup passes
      for a worker that raises microseconds later: mutating `run_extract_worker`
      to raise on entry was caught **9 runs in 10** with the sleeps and **0 in
      10** without. The replacement is not a longer sleep but
      `filterwarnings = error::pytest.PytestUnhandledThreadExceptionWarning`
      (`pyproject.toml`), which fails **3 of the 4** tests on that mutation with
      no timer at all — deterministic where the sleep was probabilistic. An
      `is_alive()` at the same point would **not** work: it is True for a thread
      that has not yet been scheduled, i.e. a pin weaker than it reads.
- The `memory_keyring` fixture (autouse) intercepts every `keyring` call so
  real Keychain entries aren't written/read during tests.
- **If exactly the three `LISTEN`/`NOTIFY` tests fail** with
  `could not access status of transaction …` while everything else passes, it
  is a stale entry in Postgres' shared NOTIFY queue, **not** clog corruption
  and not a localmail bug. Cycling the sync daemon clears it — diagnosis and
  fix in
  [docs/operations/postgres-maintenance-runbook.md](docs/operations/postgres-maintenance-runbook.md).
- `tests/_fake_imap.py::FakeIMAPClient` is the only place to extend when sync
  or daemon code needs new IMAP verbs.

## Known gaps / non-goals (deliberate)

- No write path to IMAP (no sending, no flag changes, no deletion).
- No web UI / API — downstream agents read directly.
- No multi-host clustering — single-host daemon.
- Gmail "In production" OAuth verification is not pursued; the project stays in
  "Testing" mode with the user as an explicit Test User.
