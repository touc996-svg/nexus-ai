#!/usr/bin/env bash
# Create the throwaway secret files for the LOCAL production-like stack (deploy/compose.yaml) in deploy/secrets/.
# Random values from `openssl rand`; existing files are kept unless --force is given. Nothing is printed. The directory is
# git-ignored. These are rehearsal secrets: a real deployment injects its own through its platform's secret store.
set -euo pipefail
cd "$(dirname "$0")/../deploy"
dir=secrets
force=0
[ "${1:-}" = "--force" ] && force=1
umask 077
mkdir -p "$dir"
chmod 700 "$dir"

make() { # name, value
  if [ -e "$dir/$1" ] && [ "$force" -eq 0 ]; then return; fi
  printf '%s' "$2" > "$dir/$1"
  chmod 644 "$dir/$1"  # readable by the container users; the 700 directory keeps other host users out
}

pg_password=$(openssl rand -hex 24)
api_key=$(openssl rand -hex 24)
qdrant_key=$(openssl rand -hex 24)

if [ ! -e "$dir/postgres_password" ] || [ "$force" -eq 1 ]; then
  make postgres_password "$pg_password"
  make database_url "postgresql://nexus:${pg_password}@db:5432/nexus"
fi
make nexus_api_key "$api_key"
if [ ! -e "$dir/qdrant_api_key" ] || [ "$force" -eq 1 ]; then
  make qdrant_api_key "$qdrant_key"
  make qdrant_config.yaml "service:
  api_key: ${qdrant_key}
"
fi
echo "secrets ready in deploy/secrets (not printed)"
