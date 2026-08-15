# Build provenance on the wire — design (#278, #300)

**Status:** accepted, 2026-08-15
**Issues:** [#278](https://github.com/hherb/localmail/issues/278) (the About tab
renders a `build_hash` the server never emits) ·
[#300](https://github.com/hherb/localmail/issues/300) (an unresolvable version
has no machine-readable channel)
**Predecessors:** [#291](https://github.com/hherb/localmail/issues/291),
[#295](https://github.com/hherb/localmail/issues/295),
[#296](https://github.com/hherb/localmail/issues/296),
[#302](https://github.com/hherb/localmail/issues/302)–
[#304](https://github.com/hherb/localmail/issues/304) — the version-diagnostic
cluster, whose rules this design inherits and, in one place, deliberately does
not.

## Problem

Two open issues are the same question asked from two ends: **what exactly is
this server running, and can a caller tell when we do not know?**

**#278.** `/v1/version` returns three keys — `api_major`, `api_minor`,
`server_version`. It has never emitted `build_hash`. But `build_hash` is plumbed
through the entire client as if it were real: declared in
`gui/src/lib/api/version.ts`, declared in
`gui/src-tauri/src/commands/version.rs`, rendered by
`gui/src/screens/settings/SettingsAbout.svelte`, and supplied by **five test
files** — one of which asserts a decoded `"abc123"`. The About tab's **"Server
build"** row therefore always displays `?`, while the mocks make the path look
covered.

**#300.** `__version__` degrades to the sentinel `0.0.0+unknown` when the
distribution metadata cannot be read (#291, #296). Since #304 that failure is
legible to a *human* on every entry point. It is legible to a *machine* on
none: `localmail --version` exits 0 with a well-formed line, and `/v1/version`
ships the sentinel unflagged, where the GUI renders it as though it were a
version. `__version_source__` is retained today with no production reader,
explicitly as the structured input this fix would need.

### Why they are one design

`build_hash` is worthless without a way to say why it is absent, and "why is
this value absent" is exactly what #300 asks about `server_version`. Shipping
them separately means changing an **irreversible** wire contract twice — and
#295 already declined to add a version field once, citing #278 as the
cautionary case of a key nothing renders.

## Constraints discovered before designing

Three facts about this repository shaped every decision below. All were
verified, not assumed.

1. **There is no build.** `.github/workflows/` contains `python-ci.yml` and
   `gui-ci.yml`, both test-only. Nothing builds a wheel or an sdist, nothing
   publishes, and `git tag` is empty.
2. **Neither deployment installs an artifact.** The Mac runs an editable
   install — `/Users/hherb/src/localmail/.venv/bin/localmail` resolves through
   `src/`, which is why CLAUDE.md warns that the launchd daemon executes
   whatever the tree is checked out to. The DGX resolves `localmail` to
   `/home/hherb/src/localmail/src/localmail/__init__.py`. Both run from a git
   checkout.
3. **`build_hash` is already `Option<String>`** in the Rust `VersionInfo`, so
   adding the key is backward-compatible and its absence already decodes to
   `None`. No client change is required for the row to start showing a value —
   the client work in §5 buys the *explanation* of an absent one, not the value
   itself.

Consequence: a hash stamped at **wheel-build time** would be absent on both
machines the row would ever be read on. Runtime resolution from the checkout is
the only mechanism that is correct where localmail actually runs. Build-time
stamping is retained as a **declared seam**, not as machinery — see *Out of
scope*.

## Decisions

| Question | Decision |
| --- | --- |
| What does "Server build" answer? | Both "which commit is this" and "which artifact is this", with the **source stated** alongside. |
| Scope | One design closing #278 and #300 together. |
| Dirty working tree | Short SHA plus a `-dirty` suffix, one readable token: `eec8e09-dirty`. |
| Dirtiness measured on | **Tracked files only.** |
| Build-time stamping | Declared seam; not implemented. |
| CLI half of #300 | Documented and pinned, not changed. |

## Design

### 1. `src/localmail/build_report.py`

A new module, top-level and a sibling of `version_report.py` for the same
reason that one is: `serve/routes/version.py` reads it and a subpackage would
invite an import cycle.

```python
class BuildSource(Enum):
    STAMPED          # a generated _build_info.py was found (the seam)
    GIT_CHECKOUT     # resolved from the working tree
    NOT_A_REPO       # an installed artifact, or a repo that is not ours
    GIT_UNAVAILABLE  # no git binary
    GIT_FAILED       # git ran and failed, or timed out

@dataclass(frozen=True)
class BuildInfo:
    build_hash: str | None   # "eec8e09" / "eec8e09-dirty", else None
    source: BuildSource
```

Four properties differ from `version_report`, each deliberately.

**It never logs, and no source carries a remedy.** `VersionSource` enforces a
remedy on every failure member at class creation, because an unresolvable
*version* is always a fault. An unresolvable *build hash* usually is not:
`NOT_A_REPO` is the normal and correct state of an installed artifact. Copying
that rule across would put an ERROR in front of an operator for a healthy
install — #291 inverted. `log_version_diagnostic` has no counterpart here.

**`__post_init__` enforces the one invariant that does hold:** `build_hash is
None` **iff** `source in {NOT_A_REPO, GIT_UNAVAILABLE, GIT_FAILED}`. This is the
`ResolvedVersion.__post_init__` shape — a pairing the field comments would
otherwise merely claim. A blank hash is rejected for the same reason a blank
`detail` is there.

**Resolution is lazy and cached, never at import.** `import localmail` runs for
all 38 CLI commands; a `git` subprocess on that path costs every invocation and
can *hang* — a stale network mount is the precise scenario #296 was about, and
that module's first rule is that import must not fail. `functools.cache` plus
`reset_build_info()` and an autouse conftest fixture (the
`reset_version_reports()` / `secrets.reset_to_default()` shape). Caching also
gives the semantics the row wants: a value pinned for the life of the process,
so it reports what the process is **running**, not what the tree says now.

**The repo it finds must be *this* repo.** `git rev-parse --show-toplevel` run
from inside a `site-packages` that happens to sit under an unrelated
repository — a virtualenv inside a dotfiles repo — would otherwise report that
project's SHA as localmail's build. The guard: `<toplevel>/src/localmail/__init__.py`
must resolve to the very file we imported. Anything else is `NOT_A_REPO`.

### 2. Resolution mechanics

`<dir>` is the directory containing the imported `localmail/__init__.py`
(`Path(localmail.__file__).resolve().parent`) — never the process's working
directory, which for a daemon is arbitrary and for the CLI is wherever the
operator happened to stand. Deriving it from the imported module is what makes
the identity guard below meaningful: the question is which tree *this code* came
from.

Two subprocess calls, both `argv` lists, **never** a shell:

- `git -C <dir> rev-parse --show-toplevel --short HEAD` — the toplevel for the
  identity guard and the SHA, in one call.
- `git -C <dir> diff --quiet HEAD` — exit status is the dirty bit. Tracked
  files only, which is the decision above for free.

A single `git describe --always --dirty` would produce `eec8e09-dirty` exactly
and halve the calls. It is **rejected**: the day someone tags a release it
silently begins returning `v0.4.0-3-geec8e09-dirty`, changing the field's format
under us. Two calls keeps the format ours.

`GIT_DIR` and `GIT_WORK_TREE` are stripped from the subprocess environment. A
stray one in the daemon's environment makes `-C` a no-op and would have us
report an unrelated repository's SHA — cheap to prevent and invisible if it ever
happened.

Each call is bounded by a named `_GIT_TIMEOUT_S = 2.0`, so the worst case is two
timeouts — paid once per process, on a path no request blocks behind twice. Two
seconds is generous for `rev-parse` and `diff --quiet` on a repository of this
size (both are milliseconds warm) and short enough that a wedged mount does not
hold the first `/v1/version` request open. `FileNotFoundError` →
`GIT_UNAVAILABLE`; `TimeoutExpired` or a non-zero exit → `GIT_FAILED`.
`resolve_build_info()` **never raises**: every failure maps to a source. The
broad `except Exception` is justified as `version_report`'s is — this feeds an
endpoint that must answer — with one honest difference: not being on the import
path, a raise here would not kill `import localmail`, only 500 an
unauthenticated endpoint.

### 3. Wire

`/v1/version` gains three keys:

```json
{ "api_major": 1, "api_minor": 0,
  "server_version": "0.3.0",
  "build_hash": "eec8e09-dirty",
  "build_source": "git_checkout",
  "version_source": "installed" }
```

- **`build_hash`** closes #278 from the server side alone — the Rust struct and
  the About tab already carry it.
- **`build_source`** is what makes a `null` hash readable. Without it,
  "installed from a wheel" (normal) and "git ran and failed" (notable) are the
  same `null`, which is the shape #291 spent four sessions removing from the
  version line.
- **`version_source`** closes #300's wire half: `installed`, `not_installed`,
  `metadata_incomplete`, `metadata_unreadable`. A monitoring client can alert on
  anything but the first.

Two rules constrain it:

**The diagnostic text never goes on the wire.** `/v1/version` is
unauthenticated, and `__version_diagnostic__` embeds rendered exception text —
which since #303 walks the `__cause__` chain and therefore carries errno values
and filesystem paths. Identifiers yes; paths and exception strings no. The human
line stays in the server's logs, where #295 put it.

**The wire strings are their own contract, not `source.name.lower()`.**
CLAUDE.md is explicit that `VersionSource`'s member payloads are debugging aids
and *not* a wire contract; deriving the wire from a member name would let a
rename silently break clients. Each member carries an explicit wire name, pinned
by a test asserting the literal strings — the call `rewrite_note_code` already
made. The full contract is five values plus four:

| `build_source` | `version_source` |
| --- | --- |
| `stamped` | `installed` |
| `git_checkout` | `not_installed` |
| `not_a_repo` | `metadata_incomplete` |
| `git_unavailable` | `metadata_unreadable` |
| `git_failed` | |

Both fields are **always present and never null** — only `build_hash` is
nullable. A client that cannot explain an absent hash is the state this design
exists to end.

### 4. The CLI half of #300 — documented, not changed

`localmail --version` keeps its six-way contract: reads no config, touches no
database, stdout is the single machine-readable line, the diagnostic goes to
stderr, exit stays 0, and it is not a `log_version_diagnostic` caller.

The machine-readable channel it needs **already exists**: stderr is non-empty
if and only if the version could not be resolved. That is currently an accident
of implementation rather than a stated contract, and nothing pins it. The fix is
to state it in README and pin it — a test asserting empty stderr on a healthy
resolution and non-empty on each unresolvable source. No behaviour changes, and
stdout is not touched.

### 5. GUI

The About tab **explains** an absent hash rather than showing a bare `?`:

- `build_hash` present → render it (already implemented; no change).
- `build_hash` null → render an em dash plus the reason derived from
  `build_source` (`not a repo`, `git unavailable`, `git failed`).
- `version_source` other than `installed` → mark the **Server** row, so a server
  shipping `0.0.0+unknown` is not displayed as though that were its version.

`VersionInfo` gains `build_source` and `version_source` as `Option<String>`
(serde ignores unknown fields, so an older server keeps decoding). The mapping
from source string to human phrase is a **pure TS module** with its own unit
tests, per project convention — components hold no logic.

The five existing mocks that supply `build_hash` stop being fiction.

## Testing

Written in this order:

1. **Pure.** The `sha` + dirty → token composition; `__post_init__`'s
   biconditional in both directions; the wire-name map — every member has one,
   none duplicated, strings asserted **literally**, since they are the contract.
2. **Real temp git repo** (`git init`, one commit): asserts `GIT_CHECKOUT` and
   the SHA, then modifies a tracked file and asserts the `-dirty` suffix.
   Skipped only when `git` is genuinely absent.
3. **The identity guard, its own test** — it is the one that fails silently. A
   temp repo containing a fake `site-packages/localmail`, asserting `NOT_A_REPO`
   rather than that repo's SHA.
4. **Injected failures** for each unidentified source: `FileNotFoundError`,
   `TimeoutExpired`, non-zero exit.
5. **Resolved once** — two reads, one subprocess call.
6. **Route.** The six keys; `build_hash is None` iff the source is unidentified;
   and a negative assertion that the diagnostic text never reaches the body,
   asserted against the module's own constant with a positive control beside it
   (`"cause:" not in body` cannot fail once the prefix is renamed).
7. **CLI.** Empty stderr on a healthy resolution; non-empty on each unresolvable
   source; stdout unchanged in both.
8. **GUI.** Unit tests for the pure phrase mapping; a component test that a null
   hash renders the reason and a non-`installed` version source marks the row.

## Out of scope

- **The hatchling build hook and a release pipeline.** `STAMPED` reads a
  generated `_build_info.py` that nothing writes today. The branch is a few
  lines reading a file that does not exist; the day a release pipeline appears,
  the hook is the only addition. Building it now would be machinery for a
  distribution path that does not exist, and unverifiable on either host.
- **#305** (`--version` dies on a missing third-party dependency): a `cli.py`
  import-structure problem, owed with that file's refactor.
- **Emitting the diagnostic text on the wire** — see the unauthenticated
  constraint above. If a machine-readable *reason* beyond the source enum is
  ever wanted, it belongs on an authenticated endpoint.

## Consequences

- `/v1/version` grows from three keys to six. Additive and backward-compatible;
  removing a shipped key is not, which is why the shape is settled here rather
  than grown twice.
- Both deployments will report `GIT_CHECKOUT` and a real SHA. An operator can
  answer "did the daemon get restarted after my pull?" — a live question on the
  Mac, where the editable install means a running process can be older than the
  tree.
- The About tab's "Server build" row stops being decoration.
