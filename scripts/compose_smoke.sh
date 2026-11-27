#!/usr/bin/env bash
# End-to-end rehearsal of the local production-like stack (deploy/compose.yaml): builds the image, starts everything, checks
# TLS, health, readiness, authentication, migrations, metrics/alerts, non-root execution, exposure and secret hygiene, then
# tears the stack and its volumes down. Needs docker compose, openssl, curl and python3. No model server is required:
# readiness ignores the LLM (NEXUS_READINESS_REQUIRE_LLM=false) so the rest of the stack is what is tested.
set -uo pipefail
cd "$(dirname "$0")/.."
F=deploy/compose.yaml
export NEXUS_READINESS_REQUIRE_LLM=false
export NEXUS_HTTPS_PORT="${NEXUS_HTTPS_PORT:-8443}"
U="https://localhost:${NEXUS_HTTPS_PORT}"
fail=0
ok()  { echo "PASS  $1"; }
bad() { echo "FAIL  $1"; fail=1; }
check() { # description, command...
  local d=$1; shift
  if "$@" >/dev/null 2>&1; then ok "$d"; else bad "$d"; fi
}
cleanup() { docker compose -f "$F" down -v --remove-orphans >/dev/null 2>&1; rm -f "${CA:-/dev/null}"; }
trap cleanup EXIT

./scripts/deploy_local_secrets.sh >/dev/null
docker compose -f "$F" config -q && ok "compose config is valid" || { bad "compose config"; exit 1; }
docker compose -f "$F" up -d --build --wait --wait-timeout 240 >/dev/null 2>&1 && ok "stack started and healthy" || { bad "stack start"; docker compose -f "$F" ps -a; docker compose -f "$F" logs --no-color --tail 40; exit 1; }

CA=$(mktemp)
docker compose -f "$F" exec -T caddy cat /data/caddy/pki/authorities/local/root.crt > "$CA"
K=$(cat deploy/secrets/nexus_api_key)
code() { curl -s --cacert "$CA" -o /dev/null -w '%{http_code}' "$@"; }

[ "$(code "$U/health/live")" = 200 ] && ok "liveness 200 over HTTPS (certificate validated against the local CA)" || bad "liveness"
[ "$(code "$U/health/ready")" = 200 ] && ok "readiness 200 (database, schema revision and Qdrant)" || bad "readiness"
curl -s --cacert "$CA" "$U/health/ready" | grep -q '"schema":{"status":"current"}' && ok "database is at the Alembic head" || bad "schema revision"
[ "$(curl -s -o /dev/null -w '%{http_code}' -m 5 "$U/health/live")" != 200 ] && ok "the local CA is not trusted by default (validation is on)" || bad "TLS validation"
[ "$(code "$U/system/metrics")" = 401 ] && ok "metrics: 401 without a key" || bad "metrics unauthenticated"
[ "$(code -H "X-API-Key: $K" "$U/system/metrics")" = 200 ] && ok "metrics: 200 with the operator key" || bad "metrics authenticated"
[ "$(code -H "X-API-Key: $K" "$U/system/alerts")" = 200 ] && ok "alerts: 200 with the operator key" || bad "alerts"
curl -s --cacert "$CA" -H "X-API-Key: $K" "$U/system/metrics" | grep -q '"status":"ok"' && ok "metrics are persistent (database-backed)" || bad "persistent metrics"

[ "$(docker exec nexus-local-api-1 id -u)" != 0 ] && ok "API container runs as a non-root user" || bad "non-root"
for s in db qdrant api migrate; do
  [ -z "$(docker port "nexus-local-$s-1" 2>/dev/null)" ] && ok "$s publishes no host port" || bad "$s publishes a host port"
done
docker port nexus-local-caddy-1 | grep -q '^443/tcp -> 127.0.0.1:' && ok "Caddy is the only published port, bound to loopback" || bad "caddy port binding"
[ "$(docker network inspect nexus-local_backend --format '{{.Internal}}')" = true ] && ok "backend network is internal-only" || bad "backend network"

leaks=0
for f in postgres_password nexus_api_key qdrant_api_key; do
  v=$(cat "deploy/secrets/$f")
  n=$( (docker inspect $(docker compose -f "$F" ps -aq); docker compose -f "$F" logs --no-color) 2>&1 | grep -c -F "$v")
  leaks=$((leaks + n))
done
[ "$leaks" = 0 ] && ok "no secret value in docker inspect or container logs" || bad "secret values found in inspect/logs ($leaks)"

docker compose -f "$F" stop qdrant >/dev/null 2>&1
[ "$(code "$U/health/ready")" = 503 ] && [ "$(code "$U/health/live")" = 200 ] && ok "Qdrant down: readiness 503, liveness 200" || bad "qdrant outage behaviour"
docker compose -f "$F" start qdrant >/dev/null 2>&1
for _ in $(seq 1 30); do [ "$(code "$U/health/ready")" = 200 ] && break; sleep 2; done
[ "$(code "$U/health/ready")" = 200 ] && ok "Qdrant back: readiness recovers" || bad "qdrant recovery"

for _ in 1 2 3; do code -H "X-API-Key: $K" "$U/system/alerts" >/dev/null; done
before=$(curl -s --cacert "$CA" -H "X-API-Key: $K" "$U/system/metrics" | python3 -c "import sys,json;d=json.load(sys.stdin);print(sum(c['value'] for c in d['counters'] if c['name']=='http_requests_total' and c['labels']['route']=='/system/alerts'))")
t0=$(date +%s)
docker compose -f "$F" stop api >/dev/null 2>&1
[ "$(docker inspect nexus-local-api-1 --format '{{.State.ExitCode}}')" = 0 ] && ok "graceful shutdown: API exited 0 in $(( $(date +%s) - t0 ))s" || bad "graceful shutdown"
docker compose -f "$F" start api >/dev/null 2>&1
for _ in $(seq 1 40); do [ "$(code "$U/health/ready")" = 200 ] && break; sleep 2; done
after=$(curl -s --cacert "$CA" -H "X-API-Key: $K" "$U/system/metrics" | python3 -c "import sys,json;d=json.load(sys.stdin);print(sum(c['value'] for c in d['counters'] if c['name']=='http_requests_total' and c['labels']['route']=='/system/alerts'))")
[ "${after:-0}" -ge "${before:-1}" ] && ok "metrics survived the restart ($before -> $after)" || bad "metrics lost on restart ($before -> $after)"

echo; [ "$fail" = 0 ] && echo "ALL CHECKS PASSED" || echo "SOME CHECKS FAILED"
exit "$fail"
