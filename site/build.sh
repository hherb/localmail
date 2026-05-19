#!/usr/bin/env sh
# Cloudflare Pages build for mylocalmail.org.
#
# The Cloudflare project is configured with:
#   Build command          : sh site/build.sh
#   Build output directory : site
#
# All this does is copy the user manual into site/manual/ so the published
# directory tree looks like:
#   /index.html, /style.css, /assets/    -> landing page
#   /manual/...                          -> user manual (copied from docs/manual/users/)
#
# The copy is reproduced on every deploy, so editing files under
# docs/manual/users/ on main automatically updates mylocalmail.org.

set -eu

src="docs/manual/users"
dst="site/manual"

if [ ! -d "$src" ]; then
  echo "build.sh: source $src not found" >&2
  exit 1
fi

rm -rf "$dst"
mkdir -p "$dst"
cp -R "$src/." "$dst/"

echo "build.sh: copied $src -> $dst"
