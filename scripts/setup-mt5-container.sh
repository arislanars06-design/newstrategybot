#!/usr/bin/env bash
# =============================================================================
# One-shot MT5 container bootstrap.
#
# Run this AFTER you have logged into your Exness account via the web
# VNC at http://localhost:3000 . That login is persisted in the
# `mt5_data` Docker volume so you only do it once.
#
# What this script does, end-to-end:
#
#   1. Ensures the MT5 container is up (idempotent).
#   2. Waits for Wine + MetaTrader 5 to finish their boot dance.
#   3. Pins mt5linux to a version that still ships the `-w` flag the
#      gmag11 image's start.sh depends on. Without this pin the image
#      auto-upgrades to mt5linux 1.0.3 which removed `-w`, breaks the
#      built-in start.sh, and leaves port 8001 unbound.
#   4. Restarts the container so the patched start.sh succeeds at
#      [7/7] and actually publishes the RPC server.
#   5. Verifies port 8001 is listening — the bot's only requirement.
#
# Idempotent: re-running is safe. If port 8001 is already serving
# the script just confirms and exits.
#
# Usage:
#     bash scripts/setup-mt5-container.sh
#
# Exit codes:
#     0 — port 8001 is listening, you can proceed to enable-real-mt5.sh
#     1 — something went wrong; the script prints the diagnostic logs
# =============================================================================

set -euo pipefail

# --- Configuration ---------------------------------------------------------
CONTAINER_NAME="${CONTAINER_NAME:-futures-mt5}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.mt5.yml}"
# 0.1.9 is the last mt5linux release that supports the `-w wine` CLI
# flag the gmag11 image's start.sh still passes. 1.0.x dropped the
# flag and the image hasn't been updated to match yet.
MT5LINUX_PIN="${MT5LINUX_PIN:-0.1.9}"
# Time to wait for Wine + MT5 to settle. ~60 s is enough on most
# VPSes; bump it via env if you're on a slow host.
BOOT_WAIT_SECONDS="${BOOT_WAIT_SECONDS:-60}"
# Time to wait for mt5linux to listen after the restart.
LISTEN_WAIT_SECONDS="${LISTEN_WAIT_SECONDS:-90}"

# --- Pretty printing -------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

say()  { printf "${BLUE}==>${NC} %s\n" "$*"; }
ok()   { printf "${GREEN}[OK]${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}[!!]${NC} %s\n" "$*"; }
die()  { printf "${RED}[ERR]${NC} %s\n" "$*" >&2; exit 1; }

# --- Pre-flight ------------------------------------------------------------
say "Pre-flight checks"

# Verify we're in the repo root so relative paths to compose work.
if [[ ! -f "${COMPOSE_FILE}" ]]; then
    die "${COMPOSE_FILE} not found. Run this script from the repo root."
fi

# docker present?
command -v docker >/dev/null 2>&1 || die "docker is not installed"

# --- Step 1: ensure container is up ----------------------------------------
say "Step 1/5: starting MT5 container (if not already running)"
docker compose -f "${COMPOSE_FILE}" up -d
ok "Container is up"

# --- Step 2: wait for boot -------------------------------------------------
say "Step 2/5: waiting ${BOOT_WAIT_SECONDS}s for Wine + MT5 to settle"
sleep "${BOOT_WAIT_SECONDS}"

if ! docker ps --format '{{.Names}}' | grep -qx "${CONTAINER_NAME}"; then
    docker logs "${CONTAINER_NAME}" --tail 40 || true
    die "Container ${CONTAINER_NAME} is not running"
fi
ok "Container is running"

# --- Step 3: pin mt5linux 0.1.9 -------------------------------------------
say "Step 3/5: pinning mt5linux to ${MT5LINUX_PIN} (this may take a minute)"

# Linux-side install. The image is Debian Bookworm (Python 3.11),
# but mt5linux 0.1.9 pins numpy==1.21.4 which doesn't build for
# Python >= 3.11. The image's own start.sh dodges this by using
# --no-deps and installing the runtime deps (rpyc, plumbum, numpy)
# separately — we do the same here.
say "  - installing on the Linux side..."
docker exec -u abc "${CONTAINER_NAME}" \
    pip install --user --no-cache-dir --force-reinstall --break-system-packages --no-deps \
    "mt5linux==${MT5LINUX_PIN}" > /tmp/mt5linux-linux.log 2>&1 || {
        cat /tmp/mt5linux-linux.log
        die "Linux-side mt5linux install failed (see log above)"
    }
# Runtime deps that mt5linux actually needs at import / serve time.
# Versions left unpinned so pip picks the latest wheel that matches
# the container's Python; the rigid pins in mt5linux 0.1.9's setup.py
# don't reflect real compatibility.
docker exec -u abc "${CONTAINER_NAME}" \
    pip install --user --no-cache-dir --break-system-packages \
    rpyc plumbum numpy >> /tmp/mt5linux-linux.log 2>&1 || {
        cat /tmp/mt5linux-linux.log
        die "Linux-side mt5linux runtime deps install failed (see log above)"
    }
ok "  Linux-side mt5linux ${MT5LINUX_PIN} installed"

# Wine-side install. Wine python is what mt5linux talks to under the
# hood; both sides need to be on the same protocol.
say "  - installing on the Wine python side (slower, ~30-60 s)..."
docker exec -u abc \
    -e WINEPREFIX=/config/.wine \
    -e WINEDEBUG=-all \
    "${CONTAINER_NAME}" \
    wine python -m pip install --no-cache-dir --force-reinstall \
    "mt5linux==${MT5LINUX_PIN}" > /tmp/mt5linux-wine.log 2>&1 || {
        cat /tmp/mt5linux-wine.log
        die "Wine-side mt5linux install failed (see log above)"
    }
ok "  Wine-side mt5linux ${MT5LINUX_PIN} installed"

# --- Step 4: restart so start.sh picks up the pinned version ---------------
say "Step 4/5: restarting container so start.sh's [7/7] succeeds"
docker restart "${CONTAINER_NAME}" >/dev/null
ok "Restart issued"

# --- Step 5: wait for port 8001 to listen ----------------------------------
say "Step 5/5: waiting up to ${LISTEN_WAIT_SECONDS}s for mt5linux RPC on :8001"

waited=0
listening=0
while (( waited < LISTEN_WAIT_SECONDS )); do
    # ss -tln on the host shows the docker-proxy binding once the
    # container side starts listening.
    if ss -tln 2>/dev/null | grep -q '127.0.0.1:8001'; then
        listening=1
        break
    fi
    sleep 3
    waited=$((waited + 3))
    # Mid-wait nudge: show progress every 15s so the user knows
    # we're alive.
    if (( waited % 15 == 0 )); then
        printf "    ... %ds elapsed\n" "${waited}"
    fi
done

if (( listening == 1 )); then
    ok "mt5linux RPC server is listening on 127.0.0.1:8001"
    echo
    say "Recent container logs (last 15 lines):"
    docker logs "${CONTAINER_NAME}" --tail 15
    echo
    ok "Setup complete. Next step:  bash scripts/enable-real-mt5.sh"
    exit 0
fi

# --- Failure diagnostics ---------------------------------------------------
warn "Port 8001 never came up. Dumping diagnostics:"
echo
echo "----- docker ps -----"
docker ps --filter "name=${CONTAINER_NAME}" --format \
    "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
echo
echo "----- last 40 log lines -----"
docker logs "${CONTAINER_NAME}" --tail 40 2>&1 || true
echo
echo "----- ports listening on host -----"
ss -tln 2>/dev/null | grep -E '3000|8001' || echo "  (none)"
echo
die "mt5linux did not start. Most common cause: you have not yet logged into your broker via http://localhost:3000 . Log in, then re-run this script."
