#!/bin/bash
# ============================================================================
# One-command deploy for a new Mac. Run this IN TERMINAL and it walks you
# from a fresh copy of the code to a RUNNING daily brief:
#
#     ./telegradam.sh
#
# Have ready:  • the account owner reachable (Telegram texts them a code)
#              • your Mac password (for the sleep/wake setup)
# Safe to re-run at any time — every step is idempotent.
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python3
step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

step "Step 1 of 5 — bootstrap (Python venv, dependencies, config template)"
./setup.sh

step "Step 2 of 5 — fill in config.toml"
config_ready() {
  $PY - <<'EOF'
import sys
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
with open("config.toml", "rb") as f:
    cfg = tomllib.load(f)
cred = cfg.get("credentials", {})
ok = (bool(cred.get("telegram_api_id")) and bool(cred.get("telegram_api_hash"))
      and bool(cfg.get("channels", {}).get("follow")))
sys.exit(0 if ok else 1)
EOF
}
while ! config_ready; do
  echo "config.toml still needs: [channels] follow, [credentials] api_id/api_hash"
  echo "(from https://my.telegram.org, logged in with the OWNER's number),"
  echo "and ideally your [alerts] username + a machine name."
  echo "Opening it now — edit, SAVE, then come back to this window."
  open -e config.toml 2>/dev/null || "${EDITOR:-nano}" config.toml
  read -rp "Press Enter when you've saved config.toml... "
done
echo "config.toml looks filled in."

step "Step 3 of 5 — one-time Telegram login (owner reads you the code)"
$PY telegram_brief.py --login

step "Step 4 of 5 — daily schedule + sleep/wake (macOS asks for your password)"
$PY install_schedule.py

step "Step 5 of 5 — full health check, then a real live brief"
$PY telegram_brief.py --check
./run_now.sh

scrape=$(grep -E '^scrape_time' config.toml | head -1 | cut -d'"' -f2)
printf '\n\033[1m🎉 Deployed and RUNNING.\033[0m\n'
echo "The brief builds itself every morning at ${scrape:-the configured time}."
echo "Leave the laptop plugged in (and mind the lid_closed setting in config.toml)."
