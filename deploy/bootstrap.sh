#!/usr/bin/env bash
#
# One-time host setup for a fresh Ubuntu 24.04 EC2 instance.
# Run once as a sudo-capable user, then use deploy.sh for every deployment after.
#
#   sudo bash deploy/bootstrap.sh search.example.com you@example.com
#
# Installs Docker, nginx, certbot, swap, a firewall, and unattended security
# updates. Obtains the TLS certificate. Does NOT start the service — deploy.sh does
# that, because it needs .env to exist first.
#
# Amazon Linux 2023 instead of Ubuntu: swap apt for dnf, `docker-compose-plugin`
# for `docker compose` (or install the plugin manually), ufw for firewalld, and
# unattended-upgrades for dnf-automatic. Everything else transfers.

set -euo pipefail

SERVICE_HOST="${1:-}"
LETSENCRYPT_EMAIL="${2:-}"

if [[ -z "$SERVICE_HOST" || -z "$LETSENCRYPT_EMAIL" ]]; then
  echo "usage: sudo bash deploy/bootstrap.sh <hostname> <email>" >&2
  echo "   eg: sudo bash deploy/bootstrap.sh search.example.com ops@example.com" >&2
  exit 64
fi

if [[ $EUID -ne 0 ]]; then
  echo "run with sudo" >&2
  exit 1
fi

# The user who invoked sudo — they need to be in the docker group, not root.
TARGET_USER="${SUDO_USER:-ubuntu}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }

# --------------------------------------------------------------- 1. base system
log "Updating base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq

log "Installing prerequisites"
apt-get install -y -qq \
  ca-certificates curl gnupg lsb-release ufw chrony \
  nginx certbot python3-certbot-nginx unattended-upgrades

# Accurate time is not optional: TLS handshakes reject skewed clocks, and the token
# store compares expiry timestamps against wall time.
log "Enabling time sync"
systemctl enable --now chrony

# --------------------------------------------------------------------- 2. swap
#
# A t3.medium has 4GB and no swap. The compose limits are sized to fit, but a burst
# of large pages plus Redis near its maxmemory can still squeeze the host. 2GB of
# swap turns an OOM kill into a slow minute, which is the better failure.
if [[ ! -f /swapfile ]]; then
  log "Creating 2GB swap"
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
  # Prefer reclaiming page cache over swapping the app out.
  sysctl -w vm.swappiness=10 >/dev/null
  echo 'vm.swappiness=10' > /etc/sysctl.d/99-swappiness.conf
else
  log "Swap already present, skipping"
fi

# -------------------------------------------------------------------- 3. docker
if ! command -v docker >/dev/null 2>&1; then
  log "Installing Docker"
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
else
  log "Docker already installed, skipping"
fi

log "Adding $TARGET_USER to the docker group"
usermod -aG docker "$TARGET_USER"

# ------------------------------------------------------------------ 4. firewall
#
# Belt and braces alongside the EC2 security group. The security group is the real
# control; this catches the case where someone widens it by accident.
#
# Note what is NOT opened: 8000 (the app, loopback only) and 6379 (Redis, which has
# no password and must never be reachable).
log "Configuring ufw"
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp   comment 'ssh' >/dev/null
ufw allow 80/tcp   comment 'acme + redirect' >/dev/null
ufw allow 443/tcp  comment 'api' >/dev/null
ufw --force enable >/dev/null
ufw status verbose

# --------------------------------------------------------- 5. security updates
log "Enabling unattended security upgrades"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
systemctl enable --now unattended-upgrades

# ---------------------------------------------------------------------- 6. TLS
log "Preparing nginx for the ACME challenge"
mkdir -p /var/www/certbot

# A minimal HTTP-only site, just enough for certbot's webroot challenge. The real
# site config goes in afterwards — obtaining the certificate first means the TLS
# config never references files that do not exist yet, which would fail nginx -t.
cat > /etc/nginx/sites-available/search-bootstrap <<EOF
server {
    listen 80 default_server;
    server_name $SERVICE_HOST;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 404; }
}
EOF

rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/search-bootstrap /etc/nginx/sites-enabled/search-bootstrap
nginx -t
systemctl reload nginx

log "Requesting a certificate for $SERVICE_HOST"
echo "    DNS must already point at this instance, or this step fails."
certbot certonly --webroot -w /var/www/certbot \
  -d "$SERVICE_HOST" \
  --email "$LETSENCRYPT_EMAIL" \
  --agree-tos --no-eff-email --non-interactive

# certbot installs its own systemd timer; verify rather than assume.
log "Checking renewal is scheduled"
systemctl list-timers --all | grep -i certbot || \
  echo "    WARNING: no certbot timer found. Add a cron entry for 'certbot renew'."

# --------------------------------------------------------------- 7. site config
log "Installing the production nginx site"
install -m 0644 "$REPO_DIR/deploy/nginx-limits.conf" /etc/nginx/conf.d/search-limits.conf
sed "s/SERVICE_HOST/$SERVICE_HOST/g" "$REPO_DIR/deploy/nginx-site.conf" \
  > /etc/nginx/sites-available/search

rm -f /etc/nginx/sites-enabled/search-bootstrap
ln -sf /etc/nginx/sites-available/search /etc/nginx/sites-enabled/search
nginx -t
systemctl reload nginx

log "Bootstrap complete"
cat <<EOF

  Host is ready. Next:

  1. Log out and back in so docker group membership applies:
         exit && ssh back in

  2. Create the production environment file:
         cp .env.prod.example .env
         nano .env            # fill in SERVICE_API_KEYS and the provider keys
         chmod 600 .env

  3. Deploy:
         bash deploy/deploy.sh

  nginx is serving $SERVICE_HOST over TLS but the API is not running yet, so
  https://$SERVICE_HOST/livez will 502 until step 3 completes. That is expected.
EOF
