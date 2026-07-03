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

# The AI summary needs Ollama (optional — set [summary] enabled=false to skip).
if ! command -v ollama >/dev/null 2>&1; then
  echo ""
  echo "NOTE: Ollama not found. For the AI summary run:"
  echo "        brew install ollama && brew services start ollama && ollama pull gemma2:9b"
  echo "      or set 'enabled = false' under [summary] in config.toml."
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
