#!/bin/bash
# One-command setup for a new machine. Safe to re-run at any time.
set -euo pipefail
cd "$(dirname "$0")"

echo "== Morning Brief setup =="

# Python venv + dependencies
if [ ! -d .venv ]; then
  python3 -m venv .venv
  echo "Created Python venv."
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
echo "Dependencies installed."

# Per-machine config — created from the template, NEVER copied from another
# Mac (config.toml holds that person's Telegram keys).
if [ ! -f config.toml ]; then
  cp config.example.toml config.toml
  echo "Created config.toml from the template — you must edit it next."
fi

# The AI summary uses Apple's on-device model through a tiny Swift helper
# (optional — set [summary] enabled=false to skip). Needs the Xcode command
# line tools to build, and Apple Intelligence switched on to run.
if command -v swiftc >/dev/null 2>&1; then
  if [ ! -x apple_llm ] || [ apple_llm.swift -nt apple_llm ]; then
    swiftc -O apple_llm.swift -o apple_llm 2>/dev/null
    echo "Built the Apple model helper (apple_llm)."
  fi
  if ! ./apple_llm --check >/dev/null 2>&1; then
    echo ""
    echo "NOTE: Apple's on-device model isn't available yet. For the AI summary,"
    echo "      turn on Apple Intelligence (System Settings > Apple Intelligence & Siri)"
    echo "      and let it finish downloading, or set 'enabled = false' under [summary]."
  fi
else
  echo ""
  echo "NOTE: Swift not found. For the AI summary run:  xcode-select --install"
  echo "      then re-run ./setup.sh, or set 'enabled = false' under [summary]."
fi

cat <<'EOF'

Next steps, in this order:
  1. Edit config.toml                                 (channels, credentials, alerts)
  2. .venv/bin/python3 telegram_brief.py --check      (verify everything)
  3. .venv/bin/python3 telegram_brief.py              (one-time Telegram login —
                                                       needs the code texted to THEIR phone)
  4. .venv/bin/python3 install_schedule.py            (turn on the daily schedule —
                                                       asks for your Mac password)
  5. launchctl start com.user.tgbrief                 (test a real run right now)
EOF
