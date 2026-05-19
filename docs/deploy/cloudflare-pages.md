# Deploying mylocalmail.org to Cloudflare Pages

This is the one-time setup for the public landing page + manual at
[https://mylocalmail.org](https://mylocalmail.org). Everything after this
runs automatically: every push to `main` produces a new production deploy
in ~30 s, and every PR gets a per-branch preview URL.

## Architecture

```
                 push to main / open PR
                          │
                          ▼
              ┌────────────────────────┐
              │   github.com/hherb/    │
              │     localmail          │
              └───────────┬────────────┘
                          │  webhook
                          ▼
              ┌────────────────────────┐   sh site/build.sh
              │  Cloudflare Pages      │  ─────────────────► copies
              │  (mylocalmail-org)     │   docs/manual/users → site/manual
              └───────────┬────────────┘
                          │  publishes ./site as static files
                          ▼
                  mylocalmail.org
                  *.localmail.pages.dev   (previews)
```

The Cloudflare project's **build output directory** is `site/`. The
landing page lives at `site/index.html`; the manual lives at
`docs/manual/users/` in the repository and is copied into
`site/manual/` by `site/build.sh` at build time. That keeps the manual
sources where they belong (under `docs/`) without duplicating them in
the repo.

## One-time setup

### 1. Connect Cloudflare Pages to the repo

1. Log in to [dash.cloudflare.com](https://dash.cloudflare.com).
2. Sidebar → **Workers & Pages** → **Create application** → **Pages**
   tab → **Connect to Git**.
3. Authorize Cloudflare to access the `hherb/localmail` repository (you
   only need to grant it that one repo).
4. Pick the repository, then click **Begin setup**.

### 2. Build settings

| Field                    | Value                              |
| ------------------------ | ---------------------------------- |
| Project name             | `mylocalmail-org` (becomes `mylocalmail-org.pages.dev`) |
| Production branch        | `main`                             |
| Framework preset         | **None**                           |
| Build command            | `sh site/build.sh`                 |
| Build output directory   | `site`                             |
| Root directory           | *(leave empty)*                    |
| Environment variables    | *(none required)*                  |

Click **Save and Deploy**. The first build takes ~30 s and lands at
`https://mylocalmail-org.pages.dev`. Open that URL to confirm the
landing page and `/manual/` both render before attaching the custom
domain.

### 3. Attach the custom domain

1. In the new Pages project → **Custom domains** → **Set up a custom
   domain**.
2. Enter `mylocalmail.org`. Because the DNS zone is already on
   Cloudflare, the dashboard offers to create the CNAME for you — click
   **Activate domain**.
3. Repeat for `www.mylocalmail.org` (Cloudflare will add an apex →
   www redirect or vice versa; either direction is fine).
4. After ~1 minute, `https://mylocalmail.org` resolves with a
   Cloudflare-issued TLS cert.

### 4. (Optional) Preview deployments for PRs

Preview deployments are on by default. Each PR gets a unique
`https://<short-hash>.mylocalmail-org.pages.dev` URL that Cloudflare
posts as a check on the PR — useful for reviewing doc changes before
merge.

If you want to keep previews private (so search engines don't index
half-finished pages), Pages project → **Settings** → **General** →
**Access policy** → restrict previews to your Cloudflare account.

## Day-to-day workflow

- **Editing the manual**: change files under `docs/manual/users/`,
  push to `main`. The next deploy picks the change up because
  `site/build.sh` copies the directory into `site/manual/`.
- **Editing the landing page**: change `site/index.html` or
  `site/style.css`, push to `main`.
- **Local preview**: from the repo root, run `sh site/build.sh &&
  python3 -m http.server -d site 8000` and open
  <http://localhost:8000>.
- **Rollback**: Pages project → **Deployments** → pick a previous
  deploy → **Rollback to this deployment**.

## Files in this repo that drive the deploy

| Path                  | Purpose                                                                  |
| --------------------- | ------------------------------------------------------------------------ |
| `site/index.html`     | Landing page.                                                            |
| `site/style.css`      | Landing-page stylesheet.                                                 |
| `site/assets/`        | Landing-page images (logo, banner).                                      |
| `site/_redirects`     | Cloudflare-Pages URL rewrites & redirects.                               |
| `site/_headers`       | Cloudflare-Pages response headers (security, caching).                   |
| `site/build.sh`       | Copies `docs/manual/users/` into `site/manual/` at deploy time.          |
| `docs/manual/users/`  | The user manual itself — edit here, not in `site/manual/`.               |
| `.gitignore`          | Excludes `site/manual/` (it is generated at deploy time, not committed). |

## Troubleshooting

- **Build fails with "source docs/manual/users not found"**: check the
  build command is exactly `sh site/build.sh` (not run from the wrong
  working directory) and that the `Root directory` field is empty.
- **`/manual/` returns 404**: the build copied the manual into
  `site/manual/`, but the production deploy's output directory isn't
  `site`. Fix in Pages → Settings → Builds & deployments → Build
  configuration.
- **Custom domain still pending after 10 minutes**: confirm the
  CNAME record exists at Cloudflare DNS and points at the Pages
  project's `*.pages.dev` host. Pages will tell you which.
- **TLS warning in browser**: the cert is issued by Cloudflare and
  takes up to a few minutes to be ready. Retry.
