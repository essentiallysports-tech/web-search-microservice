#!/usr/bin/env bash
#
# Build and (re)start the service. Safe to re-run — this is the update path too.
#
#   bash deploy/deploy.sh                             # 4GB box (t3.medium)
#   bash deploy/deploy.sh docker-compose.t3small.yml  # 2GB box (t3.small)
#
# Refuses to start on a configuration that is wrong in a way we can detect here, then
# gates on a real health check rather than assuming the container came up.
# Run bootstrap.sh once before the first deploy.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

log()  { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
fail() { printf '\n\033[1;31mFAIL:\033[0m %s\n' "$*" >&2; exit 1; }
warn() { printf '\033[1;33m  !\033[0m %s\n' "$*"; }

# Always base + prod; extra overlays come from the arguments.
#
# Arguments rather than the COMPOSE_FILE environment variable on purpose: that
# variable's separator is platform-specific (':' on Linux, ';' on Windows), which is
# exactly the kind of thing that works when you test it and fails elsewhere.
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)

for overlay in "$@"; do
  [[ -f "$overlay" ]] || fail "overlay '$overlay' not found in $REPO_DIR"
  COMPOSE+=(-f "$overlay")
done

# ------------------------------------------------------------ 1. preflight
log "Checking configuration"

[[ -f .env ]] || fail ".env not found. Copy .env.prod.example to .env and fill it in."

# World-readable secrets on a shared box are worth stopping for.
PERMS="$(stat -c '%a' .env 2>/dev/null || echo unknown)"
[[ "$PERMS" == "600" ]] || warn ".env is mode $PERMS; run: chmod 600 .env"

# Read values without sourcing the file — sourcing would execute anything in it.
get() { grep -E "^$1=" .env | head -1 | cut -d= -f2- | tr -d '"'"'"' ' || true; }

ENVIRONMENT="$(get ENVIRONMENT)"
AUTH_ENABLED="$(get AUTH_ENABLED)"
SERVICE_API_KEYS="$(get SERVICE_API_KEYS)"
SERPER_API_KEY="$(get SERPER_API_KEY)"

[[ "$ENVIRONMENT" == "prod" ]] || fail "ENVIRONMENT is '$ENVIRONMENT', expected 'prod'."

# The app's own _validate_startup only WARNS about this combination, so catch it here
# where refusing is cheap. Auth off means every caller shares one identity, and anyone
# who can reach the host can spend your provider credits.
if [[ "$AUTH_ENABLED" != "true" ]]; then
  fail "AUTH_ENABLED is '$AUTH_ENABLED'. The service would accept unauthenticated
       requests from anyone who can reach it. Set AUTH_ENABLED=true."
fi

[[ -n "$SERVICE_API_KEYS" ]] || fail "SERVICE_API_KEYS is empty but AUTH_ENABLED=true.
       The app will refuse to boot. Generate one with:
         python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"

# A trailing comment parsed as the value is a documented trap: python-dotenv reads
# 'KEY=  # note' as the literal string '# note'. This once enabled a paid tier.
for var in SERVICE_API_KEYS SERPER_API_KEY FIRECRAWL_API_KEY ANTHROPIC_API_KEY; do
  value="$(get "$var")"
  if [[ "$value" == \#* ]]; then
    fail "$var looks like a trailing comment, not a value. Put comments on their own line."
  fi
done

[[ -n "$SERPER_API_KEY" ]] || warn "SERPER_API_KEY is empty; every search will use the ~5x dearer fallback."

# The dev overlay publishes Redis on the host, and Redis has no password.
if grep -q "docker-compose.dev.yml" <<<"${COMPOSE[*]}"; then
  fail "the dev overlay publishes Redis on the host with no password. Never use it here."
fi

# Whatever overlay combination was chosen, the limits have to fit the box. Catching it
# here beats discovering it when the kernel OOM-kills Redis under load.
TOTAL_MB="$("${COMPOSE[@]}" config --format json 2>/dev/null | python3 -c "
import json, sys
try:
    cfg = json.load(sys.stdin)
except Exception:
    print(0); sys.exit()
total = 0
for svc in cfg.get('services', {}).values():
    lim = (svc.get('deploy') or {}).get('resources', {}).get('limits', {}).get('memory')
    if lim:
        total += int(lim) / 2**20
print(int(total))
" 2>/dev/null || echo 0)"

HOST_MB="$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 0) / 1024 ))"

if [[ "${TOTAL_MB:-0}" -gt 0 && "${HOST_MB:-0}" -gt 0 ]]; then
  # Leave at least 400MB for the kernel, dockerd, sshd and host nginx.
  if (( TOTAL_MB + 400 > HOST_MB )); then
    fail "container memory limits total ${TOTAL_MB}MB but this host has ${HOST_MB}MB.
       Nothing would ever reach its own limit — the HOST would OOM instead, and the
       kernel would kill whichever process it liked. Very possibly Redis, losing your
       cache AND every issued token, because of something the API did.
       On a 2GB box, add the overlay:
         bash deploy/deploy.sh docker-compose.t3small.yml"
  fi
  echo "  memory limits ${TOTAL_MB}MB of ${HOST_MB}MB host RAM"
fi

echo "  ENVIRONMENT=prod, AUTH_ENABLED=true, $(tr ',' '\n' <<<"$SERVICE_API_KEYS" | grep -c .) admin key(s)"

# ------------------------------------------------------------ 2. build
log "Building the image"
"${COMPOSE[@]}" build --pull

# ------------------------------------------------------------ 3. start
log "Starting the stack"
"${COMPOSE[@]}" up -d --remove-orphans

# ------------------------------------------------------------ 4. health gate
log "Waiting for health"

deadline=$((SECONDS + 90))
healthy=false
while (( SECONDS < deadline )); do
  if curl -fsS --max-time 5 http://127.0.0.1:8000/livez >/dev/null 2>&1; then
    healthy=true
    break
  fi
  sleep 3
done

if [[ "$healthy" != true ]]; then
  echo
  warn "Service did not come up within 90s. Last 40 log lines:"
  "${COMPOSE[@]}" logs --tail=40 api || true
  fail "deploy failed — the previous containers have already been replaced, so fix the
       cause and re-run. 'docker compose logs -f api' has the detail."
fi

# /livez only proves the process is alive. /health proves the providers resolved.
log "Provider health"
curl -fsS --max-time 10 http://127.0.0.1:8000/health | python3 -m json.tool 2>/dev/null \
  || curl -fsS --max-time 10 http://127.0.0.1:8000/health || true

# ------------------------------------------------------------ 5. verify auth
log "Verifying auth is actually enforced"

code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
  -X POST http://127.0.0.1:8000/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"deploy smoke test"}' || echo 000)"

if [[ "$code" == "401" ]]; then
  echo "  unauthenticated request -> 401. Correct."
else
  fail "unauthenticated request returned $code, expected 401. Auth is NOT being
       enforced — stop and check AUTH_ENABLED before exposing this."
fi

# ------------------------------------------------------------ 6. done
log "Deployed"
"${COMPOSE[@]}" ps

cat <<'EOF'

  Next:
    - Confirm from outside:  curl https://YOUR-HOST/livez
    - Issue app tokens through the admin UI, or directly:
        curl -X POST https://YOUR-HOST/admin/tokens \
          -H "X-API-Key: $ADMIN_KEY" -H 'Content-Type: application/json' \
          -d '{"name":"blog-app","expires_in_days":90}'
    - Watch the cost meters for the first day:
        curl -s http://127.0.0.1:8000/metrics | grep -E 'wss_(search_credits|external_calls)'

  Rollback: containers are replaced in place, so redeploying the previous commit is
  the rollback. Leave Redis running and neither the cache nor issued tokens are lost.
EOF
