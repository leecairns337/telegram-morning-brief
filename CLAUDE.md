# Morning Brief — project notes for Claude Code

## What this is
A small automation that gives a news-follower (e.g. a journalist — **not technically minded**)
a spoken news brief each morning. It reads their Telegram news chats overnight, picks what
matters with a **local Ollama model** (`[summary]` in config — "extractive" mode only
SELECTS real messages so it can't fabricate; "synthesis" writes prose but can), converts
it to speech with the built-in macOS `say` voice, and posts the audio (plus a 🔎 Sources
message with links) to their Telegram **Saved Messages** so it's waiting on their phone
when they wake.

**Fully free / offline-ish:** the only external services are Telegram and nothing else —
Ollama runs on the laptop. If Ollama is off/unreachable, a rule-based headline digest
(`[digest]` section: first lines, de-duplicated, capped) goes out instead, so a brief
always arrives.

The person operating you is the **maintainer**, who sets this up on one or more MacBooks
(one per person, each with their own Telegram account and channel list) and maintains
them. The listeners never touch anything except leaving the laptop plugged in.

## Who runs what
- **You (Claude Code)** are the mechanic: install, configure, test, and debug.
- The **daily run** is NOT you — it's a macOS `launchd` job that triggers `telegram_brief.py`.
  You only set that job up; it runs on its own every morning.

## Files
- `config.toml` — the ONLY file the maintainer edits: channels, scrape time, lid setting,
  voice, summary/digest rules, Telegram credentials, alert username + machine `name`.
  Everything is read from here. Per-machine; never copied between Macs.
- `config.example.toml` — the safe template `config.toml` is created from on a new machine.
- `telegram_brief.py` — does the scrape → summarise/digest → speak (`say`) → send. Reads
  `config.toml`. Also has a read-only `--check` mode that validates an install in seconds
  (config, voice, Ollama, login, every channel handle) without sending anything.
- `install_schedule.py` — reads `config.toml` and applies the daily schedule + sleep/wake
  behaviour to macOS. Run once at setup, and again whenever scrape_time or lid_closed change.
- `telegradam.sh` — the maintainer's one-command deploy: setup → config → login → schedule →
  live test, ending with the brief RUNNING. Contains BOTH human-only steps (login code,
  sudo password) — the maintainer runs it in Terminal; you must never run it yourself.
- `setup.sh` — new-machine bootstrap: venv + `requirements.txt` + config from template.
  (Called by telegradam.sh; safe standalone.)
- `run_now.sh` — fire the real launchd job immediately and print the outcome (logs +
  brief.txt). SENDS a real brief to Saved Messages — it is a live test, not a dry run.
- `requirements.txt` — pinned deps (`telethon`, plus `tomli` only on Python < 3.11).

## Setup order (per machine)
The maintainer just runs `./telegradam.sh`, which walks through all of this. Individually:
1. `./setup.sh` — venv, dependencies, `config.toml` from the template.
2. Fill in `config.toml` (Telegram api_id/api_hash from my.telegram.org using the
   OWNER's number; channels; optional voice/summary tuning; maintainer's @username and a
   machine `name` for alerts). No paid keys. Optional: `brew install ollama` +
   `ollama pull gemma2:9b` for the AI summary.
3. `.venv/bin/python3 telegram_brief.py --check` — fix any FAIL lines it prints.
4. `python3 telegram_brief.py --login` — the one-time interactive Telegram login
   (login only, scrapes and sends nothing; a plain run also logs in first if needed).
5. `python3 install_schedule.py` — sets the schedule; macOS asks for the password once.
6. Test: `./run_now.sh` (fires the real job, prints logs + brief.txt), then check Saved Messages.

## Deploying to another MacBook
- Bring only the code: `.py` files, `config.example.toml`, `requirements.txt`, `setup.sh`,
  docs. **NEVER copy `config.toml` or `tg_brief.session`** — the session file IS a
  Telegram login; copying it makes the new Mac post as the wrong person's account.
- Each machine: own api_id/api_hash (from my.telegram.org with that person's number),
  own channel list, own login at step 4.
- Set a distinct `[alerts] name` per machine so the maintainer's heartbeats say whose Mac
  is talking.
- The account's phone number must be a SIM that stays active — a lapsed PAYG SIM
  revokes the Telegram session server-side (this has happened; see memory).

## Steps a HUMAN must do — never attempt these yourself
- **Telegram login code (step 4):** Telegram texts a code to the account owner's app.
  Only they/the maintainer can read and enter it. Pause and ask them to type it.
- **The sudo password (step 5):** `pmset` needs it. Ask the maintainer to enter it themselves.
- Do not create accounts or read secrets (the Telegram api_hash) aloud.

## Key constraints / gotchas
- Mix of public AND private chats → it logs in as the owner's own account (Telethon user
  session), not a bot. The `tg_brief.session` file holds that login; don't delete it.
- A sleeping Mac can't run the job. `install_schedule.py` handles this from config:
  `lid_closed = false` → disables sleep on power (lid must stay OPEN, plugged in);
  `lid_closed = true` → schedules a wake 5 min before the scrape (works lid-closed, plugged in).
- The scrape TIME lives in `config.toml` but only takes effect after `install_schedule.py`
  rewrites the launchd job. Editing the time alone does nothing until that's re-run.
- On failure the script messages the maintainer's @username (from `[alerts]`). Silent success,
  loud failure — that's intended.

## Troubleshooting playbook
- **First move for anything:** `.venv/bin/python3 telegram_brief.py --check` — it
  pinpoints dead logins, bad channel handles, missing voices and Ollama problems.
- **No brief arrived:** check `run.err` first. Common causes below.
- **"What did it say this morning?"** — `brief.txt` holds the exact spoken words of the
  last run, and `run.log` says which path produced them (`Spoken: AI extractive, 5 item(s).`).
- **Login/auth error or "unauthorized":** the session expired — re-run `python3 telegram_brief.py`
  to log in again (needs a fresh code from the account owner).
- **No audio / `say` error:** a bad `say_voice` name in config — list valid names with
  `say -v '?'`, or set `say_voice = ""` for the system default.
- **Job never fired:** Mac was off, asleep with the wrong lid setting, or unplugged. Confirm
  `launchctl list | grep tgbrief` shows the job, and that the `lid_closed`/power setup matches
  their habits. Re-run `install_schedule.py` after any time/lid change.
- **"No overnight messages found":** legitimate on a quiet night — the listener gets a 🌙
  "quiet night" text so it doesn't look broken; only worry if it's every day
  (then a channel handle in `config.toml` is likely wrong — `--check` verifies them all).

## Don't
- Don't move secrets out of `config.toml` into a repo or anywhere shared.
- Don't replace launchd with a long-running daemon (it won't survive the Mac sleeping).
- Don't edit the `.py` files for routine changes — change `config.toml` instead.

## If asked to make it bulletproof
An always-on machine removes the lid/power/sleep fragility. NOTE: the voice step uses
macOS `say`, which does NOT exist on Linux — a cloud Linux box would need the TTS swapped
(e.g. `espeak-ng`/`piper`) plus `cron` instead of `launchd` and no `pmset`. Offer this only
if the laptop proves unreliable, and flag the `say` replacement as the main change.
