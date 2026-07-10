# Telegram Morning Brief

Wake up to a **spoken news brief** waiting in your Telegram Saved Messages —
built overnight, on your own Mac, from the Telegram channels *you* choose.

Every morning you get two messages:

1. 🎧 an **audio brief** — a voice reading the overnight stories that matter
2. 🔎 a **Sources** message — every item with a tappable link to the original post

**Free and private.** The AI that picks the stories (Ollama) and the voice
(macOS `say`) both run locally on the Mac. Nothing is sent to any cloud
service except Telegram itself. No paid APIs, no subscriptions.

By default the AI runs in **extractive** mode: it only *selects and groups*
real messages — the brief is read from the actual message text, so it cannot
invent facts.

## The most important thing to understand

**Every install is personal.** The brief is built from *your* Telegram
account: your channel list, your login, delivered to *your* Saved Messages.
Nothing is shared — if three people want morning briefs, that's three Macs,
three Telegram accounts, three API keys, three separate setups of this code.

That means before installing, **you need your own Telegram API key**. It's
free and takes two minutes:

## Step 1 — Get your Telegram API key

1. Go to **https://my.telegram.org** in a browser.
2. Log in with the phone number of the Telegram account that will **receive
   the brief** (the confirmation code arrives in your Telegram app, not SMS).
3. Click **API development tools**.
4. Fill in the short form — *App title* and *Short name* can be anything
   (e.g. "Morning Brief"); platform "Desktop"; leave the URL blank.
5. You'll be shown an **api_id** (a number) and an **api_hash** (a long hex
   string). These are your "key" — you'll paste both into `config.toml`
   during setup.

> ⚠️ Treat the api_hash like a password: don't share it, don't commit it,
> don't reuse someone else's. It identifies *your* access to *your* account.

## Step 2 — Install (on the Mac that will run it)

Requirements: a Mac that stays **plugged in overnight**, Python 3, and
optionally [Ollama](https://ollama.com) for the AI story selection
(`brew install ollama && brew services start ollama && ollama pull gemma2:9b`
— skip it and you get a rule-based headline digest instead).

```bash
git clone https://github.com/leecairns337/telegram-morning-brief
cd telegram-morning-brief
./telegradam.sh
```

The script walks you through everything and ends with the brief **running**:

1. Creates the Python environment and your personal `config.toml`
2. Opens `config.toml` for you to paste your api_id/api_hash and list your
   channels (`@handle` for public channels, exact title for private groups)
3. One-time Telegram login — a code is sent to your Telegram app
4. Installs the daily schedule (asks for your Mac password once)
5. Runs a real live test and shows you exactly what it did

From then on it runs itself every morning. Health-check it any time with:

```bash
.venv/bin/python3 telegram_brief.py --check
```

## Configuration highlights (`config.toml`)

| Setting | What it does |
|---|---|
| `[channels] follow` | The channels/groups to read overnight |
| `[schedule] scrape_time` | When the brief is built (e.g. `"03:40"`) |
| `[summary] focus` | Topic areas that count as news — everything else is filtered out |
| `[summary] mode` | `"extractive"` (verbatim, can't fabricate) or `"synthesis"` (prose, can err) |
| `[voice] say_voice` | Any installed macOS voice — Enhanced/Premium ones sound best |
| `[alerts]` | A username to ping if a morning ever fails, a per-machine name, and an optional daily success heartbeat (`daily_heartbeat`) |

Full walkthrough, voice tips and troubleshooting: **[SETUP.md](SETUP.md)**.

## Privacy & safety notes

- `config.toml` (your key) and `tg_brief.session` (your login) stay on your
  Mac — both are gitignored and must **never** be shared or copied to
  another machine. The session file *is* a full Telegram login.
- Keep the account's phone number active: if the SIM lapses, Telegram revokes
  the login server-side and the briefs stop until you log in again.
- The script reads only the channels you list. (An explicit `["*"]` reads
  every chat on the account — it never does this by default.)

## License

[MIT](LICENSE)
