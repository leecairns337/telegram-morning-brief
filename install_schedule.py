#!/usr/bin/env python3
"""
Apply the schedule from config.toml to macOS.

Run this once after setup, and again any time you change `scrape_time`
or `lid_closed` in config.toml. It writes the launchd job and sets the
Mac's sleep/wake behaviour so the brief runs even while he's asleep.

    python3 install_schedule.py
"""

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Re-launch under the project venv if we aren't already running in it. The Mac's
# stock python3 is 3.9 (no stdlib tomllib) and may not have `tomli` installed,
# so reading config.toml outside the venv would crash. The venv has tomli.
_venv_py = HERE / ".venv" / "bin" / "python3"
if _venv_py.exists() and Path(sys.executable).resolve() != _venv_py.resolve():
    os.execv(str(_venv_py), [str(_venv_py), *sys.argv])

import datetime as dt
import re
import subprocess

try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        sys.exit(
            "Could not read config.toml: no TOML parser.\n"
            "Activate the venv and install deps first:\n"
            "  source .venv/bin/activate && pip install telethon tomli"
        )

LABEL = "com.user.tgbrief"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def pmset(*args):
    """Run `sudo pmset ...` and stop LOUDLY if it fails (wrong password,
    cancelled prompt) — otherwise this script would print success while the
    Mac still sleeps through the 3:40am job."""
    if subprocess.run(["sudo", "pmset", *args]).returncode != 0:
        sys.exit(
            "pmset failed — sleep/wake behaviour was NOT configured.\n"
            "Re-run  python3 install_schedule.py  and enter your password when asked."
        )


def main():
    with open(HERE / "config.toml", "rb") as f:
        cfg = tomllib.load(f)

    scrape = str(cfg["schedule"]["scrape_time"]).strip()
    lid_closed = cfg["schedule"].get("lid_closed", False)
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", scrape)
    if not m or not (0 <= int(m[1]) <= 23 and 0 <= int(m[2]) <= 59):
        sys.exit(f'Bad scrape_time "{scrape}" in config.toml — use 24-hour HH:MM, e.g. "03:40".')
    hh, mm = int(m[1]), int(m[2])

    python_bin = HERE / ".venv" / "bin" / "python3"
    if not python_bin.exists():
        python_bin = Path(sys.executable)
    script = HERE / "telegram_brief.py"

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_bin}</string>
        <string>{script}</string>
    </array>
    <key>WorkingDirectory</key><string>{HERE}</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>{hh}</integer>
        <key>Minute</key><integer>{mm}</integer>
    </dict>
    <key>StandardOutPath</key><string>{HERE / 'run.log'}</string>
    <key>StandardErrorPath</key><string>{HERE / 'run.err'}</string>
</dict>
</plist>
"""
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_text(plist)
    subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True)
    subprocess.run(["launchctl", "load", str(PLIST)], check=True)
    print(f"Scheduled the brief daily at {scrape}.")

    # Make sure the Mac is awake at that time. Each branch also UNDOES the
    # other mode's setting, so flipping lid_closed doesn't leave stale state
    # (a leftover daily wake, or sleep still disabled forever).
    print("\nmacOS may now ask for your password to set sleep behaviour...")
    if lid_closed:
        wake_dt = dt.datetime(2000, 1, 1, hh, mm) - dt.timedelta(minutes=5)
        wake = wake_dt.strftime("%H:%M:00")
        pmset("repeat", "wakeorpoweron", "MTWRFSU", wake)
        pmset("-c", "sleep", "10")          # re-allow normal sleep (undo lid-open mode)
        print(f"Set a daily wake at {wake} (lid-closed mode). Keep it plugged in.")
    else:
        pmset("repeat", "cancel")           # remove any old daily wake (undo lid-closed mode)
        pmset("-c", "sleep", "0")
        print("Disabled sleep on power (lid-open mode). Leave the lid open and plugged in.")

    print("\nAll set. Test it now with:  launchctl start com.user.tgbrief")


if __name__ == "__main__":
    main()
