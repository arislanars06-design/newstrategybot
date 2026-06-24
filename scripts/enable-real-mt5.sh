#!/usr/bin/env bash
# =============================================================================
# Flip the futures bot from mock-mode to real MT5.
#
# Run this AFTER scripts/setup-mt5-container.sh has reported success.
#
# What it does:
#
#   1. Prompts for your broker credentials (login / password / server)
#      and writes them into `.env` next to the bot. Nothing is echoed
#      back to the terminal; the password is read silently.
#   2. Flips FB_USE_MOCK_ADAPTER from 1 to 0 in the same .env.
#   3. Restarts the systemd `futures-bot` service.
#   4. Tails the bot's log for ~20 s and looks for "MT5 connected".
#      Reports success / failure based on what it sees.
#
# Idempotent: re-running it is safe. If your .env already has the
# values, the script confirms them rather than re-prompting (unless
# you pass `--reset` to force a fresh prompt).
#
# Usage:
#     bash scripts/enable-real-mt5.sh
#     bash scripts/enable-real-mt5.sh --reset      # re-enter creds
# =============================================================================

set -euo pipefail

ENV_FILE="${ENV_FILE:-.env}"
SERVICE_NAME="${SERVICE_NAME:-futures-bot}"
RESET=0

if [[ "${1:-}" == "--reset" ]]; then
    RESET=1
fi

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
[[ -f "${ENV_FILE}" ]] || die "${ENV_FILE} not found. Run this from the repo root."
command -v systemctl >/dev/null 2>&1 || die "systemctl not available"

# --- Host-side mt5linux client library -------------------------------------
#
# The bot runs on the host under systemd, so it needs the mt5linux
# Python package available to its interpreter (the RPC client side).
# We pin to the same 0.1.9 as the container's server side and dodge
# the broken numpy pin the same way: --no-deps then install the
# real runtime deps unpinned.
ensure_host_mt5linux() {
    # Resolve which Python the service uses by parsing the unit's
    # ExecStart. Falls back to /usr/bin/python3 if anything is
    # unusual; that matches a from-source install with `pip install -e .`.
    local exec_line
    exec_line=$(systemctl cat "${SERVICE_NAME}" 2>/dev/null \
        | grep -E '^ExecStart=' | head -n1 | sed -E 's/^ExecStart=//')
    local svc_python
    svc_python=$(printf '%s' "${exec_line}" | awk '{print $1}')
    if [[ -z "${svc_python}" || ! -x "${svc_python}" ]]; then
        svc_python="/usr/bin/python3"
    fi

    # Check that mt5linux imports AND that rpyc is the protocol-
    # matching 5.0.1 — anything else handshakes the server and gets
    # 'ValueError: invalid message type: 18' on first call.
    if "${svc_python}" -c 'import mt5linux' >/dev/null 2>&1; then
        local rpyc_ver
        rpyc_ver=$("${svc_python}" -c 'import rpyc; print(rpyc.__version__)' 2>/dev/null || echo "?")
        if [[ "${rpyc_ver}" == "5.0.1" ]]; then
            ok "host already has mt5linux + rpyc 5.0.1 (${svc_python})"
            return 0
        fi
        warn "  rpyc on host is ${rpyc_ver}, need 5.0.1 — reinstalling"
    fi

    say "  - installing mt5linux 0.1.9 for ${svc_python}..."
    "${svc_python}" -m pip install --break-system-packages --no-cache-dir \
        --no-deps --force-reinstall "mt5linux==0.1.9" \
        > /tmp/mt5linux-host.log 2>&1 || {
            cat /tmp/mt5linux-host.log
            die "host-side mt5linux install failed (see log above)"
        }
    # Pin rpyc to 5.0.1 to match the container's server side.
    # mt5linux 0.1.9 declares this exact version; the protocol
    # differs between rpyc 5.x and 6.x so any drift between client
    # and server produces 'invalid message type: 18' at handshake.
    "${svc_python}" -m pip install --break-system-packages --no-cache-dir \
        --force-reinstall "rpyc==5.0.1" plumbum numpy >> /tmp/mt5linux-host.log 2>&1 || {
            cat /tmp/mt5linux-host.log
            die "host-side mt5linux runtime deps install failed (see log above)"
        }
    if ! "${svc_python}" -c 'import mt5linux' >/dev/null 2>&1; then
        die "post-install import of mt5linux still fails — inspect /tmp/mt5linux-host.log"
    fi
    ok "host-side mt5linux installed (${svc_python})"
}

say "Step 0/3: ensuring host has the mt5linux client library"
ensure_host_mt5linux

# --- Helper: read/write/upsert a KEY=VALUE line in .env -------------------
#
# We intentionally do this with plain awk-free shell so the file order
# is preserved and comments survive. The only invariant we care about
# is that the key appears exactly once with the new value.
env_get() {
    grep -E "^${1}=" "${ENV_FILE}" 2>/dev/null | head -n1 | cut -d= -f2- || true
}
env_set() {
    local key="$1"
    local value="$2"
    # Escape backslashes and ampersands so sed doesn't interpret them.
    local escaped
    escaped=$(printf '%s' "$value" | sed -e 's/[\/&]/\\&/g')
    if grep -qE "^${key}=" "${ENV_FILE}"; then
        sed -i.bak -E "s/^${key}=.*/${key}=${escaped}/" "${ENV_FILE}"
        rm -f "${ENV_FILE}.bak"
    else
        printf '\n%s=%s\n' "${key}" "${value}" >> "${ENV_FILE}"
    fi
}

# --- Step 1: collect broker creds ------------------------------------------
say "Step 1/3: broker credentials"

existing_login=$(env_get FB_MT5_LOGIN)
existing_server=$(env_get FB_MT5_SERVER)
existing_password=$(env_get FB_MT5_PASSWORD)

if (( RESET )) || [[ -z "${existing_login}" || -z "${existing_server}" || -z "${existing_password}" ]]; then
    echo
    echo "Enter your Exness DEMO account details."
    echo "(The password is read silently — nothing will appear as you type.)"
    echo
    read -r -p "  MT5 login (account number): " input_login
    [[ -n "${input_login}" ]] || die "login cannot be empty"

    read -r -s -p "  MT5 password: " input_password
    echo
    [[ -n "${input_password}" ]] || die "password cannot be empty"

    read -r -p "  MT5 server (e.g. Exness-MT5Trial16): " input_server
    [[ -n "${input_server}" ]] || die "server cannot be empty"

    env_set FB_MT5_HOST "127.0.0.1"
    env_set FB_MT5_PORT "8001"
    env_set FB_MT5_LOGIN "${input_login}"
    env_set FB_MT5_PASSWORD "${input_password}"
    env_set FB_MT5_SERVER "${input_server}"
    ok "broker credentials written to ${ENV_FILE}"
else
    ok "broker credentials already present (login=${existing_login}, server=${existing_server})"
    say "  Use ./scripts/enable-real-mt5.sh --reset to re-enter them."
fi

# --- Step 2: flip the mock flag -------------------------------------------
say "Step 2/3: switching from mock to real MT5 adapter"
env_set FB_USE_MOCK_ADAPTER "0"
ok "FB_USE_MOCK_ADAPTER=0"

# --- Step 3: restart the bot and watch the logs ---------------------------
say "Step 3/3: restarting ${SERVICE_NAME} and watching for 'MT5 connected'"
systemctl restart "${SERVICE_NAME}"

# Give systemd a moment to spawn the new process.
sleep 3

# Tail logs since the restart. ``--since 5s`` would be slightly
# tighter but '-n 60 -f' with a wall-clock cutoff is more portable.
deadline=$(( SECONDS + 20 ))
connected=0
echo
echo "----- live log (20 s) -----"
while (( SECONDS < deadline )); do
    if journalctl -u "${SERVICE_NAME}" -n 60 --no-pager 2>/dev/null \
            | tail -n 40 \
            | grep -q "MT5 connected"; then
        connected=1
        break
    fi
    sleep 2
done

journalctl -u "${SERVICE_NAME}" -n 30 --no-pager 2>/dev/null | tail -n 25 || true
echo "----- end log -----"
echo

if (( connected == 1 )); then
    ok "Bot connected to MT5. You're live!"
    echo
    echo "Next: open Telegram and send the bot /balance — you should see"
    echo "your Exness demo account balance, not the mock 10000.00."
    exit 0
fi

warn "Did not see 'MT5 connected' within 20 s."
echo
echo "Most common causes:"
echo "  * MT5 not yet logged in via web VNC → http://localhost:3000"
echo "  * Wrong broker credentials in .env (login / password / server)"
echo "  * mt5linux RPC not running → re-run scripts/setup-mt5-container.sh"
echo
echo "Inspect the full log with:"
echo "  journalctl -u ${SERVICE_NAME} -n 100 --no-pager"
exit 1
