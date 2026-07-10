# Morning Audio Brief — setup guide (for the maintainer)

**You do this once per MacBook. After that the owner does nothing but leave the
laptop plugged in.** Their daily experience: wake up, open Telegram, tap play.

Each morning they get **two messages** in Saved Messages:
1. 🎧 the **audio brief** — a spoken rundown of what mattered overnight
2. 🔎 a **Sources** text message — each item with its channel, time, and a tappable
   link straight to the original Telegram message (for verifying or following up).

**Fully free — no paid accounts.** The voice is the built-in macOS `say`. A local AI
model (Ollama, on the laptop) picks which overnight messages matter — filtered to the
areas in `config.toml` `[summary] focus`. In the default **extractive** mode the AI
only *selects* messages; the brief is read from the *actual message text*, so it can't
invent facts. If Ollama is off or unreachable, a rule-based headline digest goes out
instead — a brief always arrives. Nothing is sent to any cloud service except Telegram
itself.

You only ever edit ONE file per machine: **config.toml**. The two `.py` files are
"don't touch" and identical on every machine.

Because the sources can mix public and private chats, it logs in as *the owner's*
Telegram account — that needs one 30-second step where they read you a login code
during setup (step 5), then never again.

---

## ⚠️ Setting up a second machine? Two things you must never copy

- **`tg_brief.session`** — this file IS a Telegram login. Copy it and the new machine
  posts as the *wrong person's account*. Each machine creates its own at step 5.
- **`config.toml`** — it holds that person's API keys and channel list. Each machine
  gets its own from the template (`setup.sh` does this).

Copy (or `git clone`) only the code: the `.py` files, `config.example.toml`,
`requirements.txt`, the `.sh` scripts, and this guide.

## The short version

```
./telegradam.sh
```

walks you through everything below in one sitting — bootstrap, config, login,
schedule, live test — and ends with the brief **running**. Have the owner
reachable (Telegram texts them a login code at step 3) and your Mac password
ready (step 4). The numbered sections below are the same steps done manually,
kept for reference and troubleshooting.

## 1. Put the files in place
```
mkdir -p ~/tgbrief
cp telegram_brief.py install_schedule.py config.example.toml requirements.txt setup.sh SETUP.md ~/tgbrief/
cd ~/tgbrief
```

## 2. Run the setup script
```
./setup.sh
```
It creates the Python environment, installs the dependencies, and creates
`config.toml` from the template. Re-running it is always safe.

For the AI summary, also install Ollama (free, local):
```
brew install ollama && brew services start ollama && ollama pull gemma2:9b
```
(Skip this and set `enabled = false` under `[summary]` — you'll get the rule-based
headline digest instead.)

## 3. Fill in config.toml
Open `config.toml` in any text editor and set:
- **[channels]** their news sources (@handles for public, exact titles for private)
- **[credentials]** Telegram api_id/api_hash from https://my.telegram.org — log in
  there with *the owner's* phone number. Get a fresh pair per person.
  (Click-by-click walkthrough: README, "Step 1 — Get your Telegram API key".)
- **[schedule]** the `scrape_time`, and `lid_closed = true` only if they shut the lid
- **[voice]** optionally a nicer `say_voice` (see below) and speaking `rate`
- **[summary]** the `focus` list — what counts as news worth reading out
- **[alerts]** your @username, plus a `name` for this machine (e.g. `"Sam"`) so you
  can tell whose brief is talking when several Macs report in

> **SIM warning (learned the hard way):** the Telegram account must live on a phone
> number that stays active. A lapsed PAYG SIM kills the login server-side and the
> briefs silently stop until someone re-logs-in.

**Better voice (optional, free):** the default macOS voice is a bit robotic. For a
natural one, open **System Settings → Accessibility → Spoken Content → System Voice →
Manage Voices**, download an **(Enhanced)** or **(Premium)** voice, then put its
exact name in `say_voice` (list names with `say -v '?'` in Terminal). Note: the "Siri"
voices can't be used by `say` — pick an Enhanced/Premium one.

## 4. Check everything before the login
```
.venv/bin/python3 telegram_brief.py --check
```
This verifies the config, the voice, Ollama and (once logged in) every channel handle
— in seconds, without sending anything. Fix any FAIL lines it prints.

## 5. First run — the one-time login
```
.venv/bin/python3 telegram_brief.py
```
It asks for the owner's phone number and a code Telegram sends to their app. **This is
the moment they read you the code.** A `tg_brief.session` file is created and every
future run is silent. Check Saved Messages for the two test messages (audio + 🔎
Sources), and tap a source link to confirm it opens the original message.

## 6. Turn on the daily schedule
```
.venv/bin/python3 install_schedule.py
```
This reads `config.toml`, sets the daily run time, and configures sleep/wake so it runs
while they're asleep. macOS will ask for your password. Then test it immediately:
```
./run_now.sh
```
It fires the real scheduled job, waits, and prints the logs plus what the voice
said — then check their Saved Messages for the audio.

---

## What to tell the owner
> "Leave the laptop plugged in and don't switch it off. Each morning a voice note
> appears in your Telegram Saved Messages — just tap play."

## Changing things later (you, in config.toml)
- **Channels, voice, focus areas, or keys** — edit `config.toml`, nothing else to do.
- **Scrape time or lid setting** — edit `config.toml`, then re-run
  `python3 install_schedule.py`.
- After any change, `python3 telegram_brief.py --check` confirms it's still healthy.

## Maintenance (occasional — only when you're at the machine)
Nothing auto-updates, deliberately: an unattended machine wants stability, and
even a broken Ollama only degrades the brief to the rule-based digest, never
kills it. Every month or three, while you're there anyway:
```
git pull                                       # update this tool
brew upgrade ollama && brew services restart ollama
ollama pull gemma2:9b                          # refresh the model if upstream changed
.venv/bin/python3 telegram_brief.py --check    # confirm everything is still green
```
Never update anything remotely the evening before you can't be reached —
do it when you could fix a surprise.

## Notes
- **Cost:** free. No API keys to fund — the voice and the summary both run on the laptop.
- **If it breaks:** you'll get the ⚠️ alert (prefixed with that machine's `name`). With
  `daily_heartbeat = true` you also get a short success ping every morning, so silence means trouble.
  Usually it's an expired login — re-run step 5. Run `--check` first to see what's wrong.
- **Quiet nights:** the owner gets a short "🌙 Quiet night" text instead of audio, so
  silence never looks like a breakage.
- **Faithfulness trade-off:** `mode = "extractive"` (default) reads real message text —
  the AI can't fabricate. `mode = "synthesis"` is smoother prose but can invent details;
  `enabled = false` skips the AI entirely for a plain headline digest.
- **Want it truly bulletproof?** An always-on machine removes the lid/power/sleep worries.
  (A cloud Linux box can't run macOS `say`, so the voice step would need swapping there —
  ask me and I'll sort it.)
