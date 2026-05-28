# Admin UI — manual smoke (Sub-plan 1)

After applying migrations and configuring `[serve] session_signing_key` and
`[serve] state_signing_key` in `config.toml`, verify the admin UI scaffolding
end-to-end.

## Prerequisites

```bash
# Generate two distinct signing keys
python -c "import secrets; print(secrets.token_urlsafe(32))"   # session_signing_key
python -c "import secrets; print(secrets.token_urlsafe(32))"   # state_signing_key
```

Add to `~/.config/localmail/config.toml`:

```toml
[serve]
session_signing_key = "<paste first key>"
state_signing_key   = "<paste second key>"
oauth_callback_url  = "https://localhost:8443/admin/oauth/callback"
```

## Bootstrap the first admin

```bash
uv run localmail init-db                                  # applies migrations through 0021
uv run localmail add-api-user --admin horst               # interactive password prompt
uv run localmail list-api-users --with-grants             # confirm the user exists
```

## Sign in

```bash
uv run localmail serve --bind 127.0.0.1 --port 8443
```

In your browser:

1. Visit `https://127.0.0.1:8443/admin/` — you should be redirected to `/admin/login`.
2. Sign in with `horst` / your password — you land on the dashboard at `/admin/`.
3. The dashboard shows "Signed in as **horst**" and three placeholder bullet points.
4. Click "Sign out" — you return to `/admin/login` and the session cookie is gone
   (`document.cookie` empty in DevTools).
5. Try to visit `/admin/` again — you are redirected to `/admin/login`.

If any of those fail, that's a Sub-plan 1 regression — investigate before
starting Sub-plan 2.

## Negative cases worth checking

- Tampered cookie: edit `localmail_admin_session` in DevTools → flip one
  character → reload `/admin/` → redirects to login.
- Non-admin user: `uv run localmail revoke-admin horst`, then try to sign
  in → form re-renders with "This account is not an admin."
- Missing signing key: remove `session_signing_key` from `config.toml`,
  restart `serve` → `/admin/login` returns 404 (admin routes not mounted).
