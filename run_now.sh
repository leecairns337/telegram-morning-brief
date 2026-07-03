#!/bin/bash
# Trigger the morning brief RIGHT NOW — through launchd, so it runs in exactly
# the same environment as the 3:40am schedule — then show what happened.
#
# NOTE: this sends a REAL brief (audio + sources) to Saved Messages.
set -euo pipefail
cd "$(dirname "$0")"

LABEL="com.user.tgbrief"

if ! launchctl list | grep -q "$LABEL"; then
  echo "The schedule isn't installed on this Mac — run:  python3 install_schedule.py"
  exit 1
fi

echo "Starting the brief via launchd (sends a real brief to Saved Messages)..."
launchctl start "$LABEL"
sleep 2

# Wait for the job to finish: launchctl list shows a PID while it runs, "-" after.
printf "Running"
for _ in $(seq 1 120); do            # up to ~10 minutes (Ollama can be slow to load)
  pid=$(launchctl list | awk -v l="$LABEL" '$3==l {print $1}')
  [ "$pid" = "-" ] && break
  printf "."
  sleep 5
done
echo

# Show this run's section of each log (everything after the last dated banner).
section() { awk '/——— run started/{buf=""} {buf=buf $0 ORS} END{printf "%s", buf}' "$1" 2>/dev/null; }

echo "=== run.log (this run) ==="
section run.log
err=$(section run.err)
if [ -n "$err" ]; then
  echo "=== run.err (this run) ==="
  echo "$err"
fi

if [ -f brief.txt ]; then
  echo "=== what the voice said (brief.txt) ==="
  cat brief.txt
fi

if section run.log | grep -q "Sent brief\|quiet night\|No overnight messages"; then
  echo "✅ Done — check Saved Messages."
else
  echo "⚠️  Didn't see a completion line — read run.err above."
  exit 1
fi
