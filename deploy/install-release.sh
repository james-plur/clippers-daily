#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 /srv/clippers/releases/TIMESTAMP" >&2
  exit 2
fi

release=${1%/}
root=/srv/clippers
case "$release" in
  /srv/clippers/releases/*) ;;
  *) echo "release must be under /srv/clippers/releases" >&2; exit 2 ;;
esac
[[ -f "$release/pyproject.toml" ]] || { echo "invalid release" >&2; exit 2; }

install -d -m 0755 "$root/releases" "$root/backups"
# The web and daily services run as ubuntu and must be able to update these
# persistent directories.  Reapply ownership on upgrades as well as on the
# first install so deployments created by root do not make API secret writes
# fail with HTTP 500.
install -d -o ubuntu -g ubuntu -m 0755 "$root/data" "$root/config"
install -d -o ubuntu -g ubuntu -m 0700 "$root/secrets"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup="$root/backups/$stamp"
install -d -m 0700 "$backup"
[[ -f "$root/data/clippers.db" ]] && cp -a "$root/data/clippers.db" "$backup/clippers.db"
[[ -d "$root/config" ]] && cp -a "$root/config" "$backup/config"
[[ -d "$root/data/reports" ]] && cp -a "$root/data/reports" "$backup/reports"
[[ -f /srv/paperlab-publish/topics.yaml ]] && cp -a /srv/paperlab-publish/topics.yaml "$backup/paperlab-topics.yaml"

for source in "$release"/config/*.yaml; do
  target="$root/config/$(basename "$source")"
  [[ -f "$target" ]] || cp -a "$source" "$target"
done

python3 -m venv "$release/.venv"
"$release/.venv/bin/pip" install --disable-pip-version-check "$release"
CLIPPERS_CONFIG_DIR="$root/config" CLIPPERS_DATA_DIR="$root/data" "$release/.venv/bin/clippers" migrate

ln -sfn "$release" "$root/current.new"
mv -Tf "$root/current.new" "$root/current"
echo "release=$release backup=$backup"
