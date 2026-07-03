#!/usr/bin/env python3
"""
Overnight Telegram -> audio brief.  (Fully local, no paid APIs.)

All settings live in config.toml — you should not need to edit this file.
Pulls recent messages from the chats listed in config.toml, summarises them
with a LOCAL AI model (Ollama, running on this Mac — free, no account), turns
the summary into speech with the built-in macOS `say` voice, and posts the
audio to Telegram Saved Messages — plus a text "Sources" message with a
tappable link to each original.

No paid APIs, no cloud: the only external service is Telegram. If the local
Ollama server isn't reachable, it falls back to a rule-based headline digest so
a brief still goes out. See the [summary] and [digest] sections of config.toml.

Default summary mode is "extractive": the local model only SELECTS and groups
the real messages worth airing (filtered to the [summary] focus areas); the
brief is then read from the actual message text. The model writes no prose, so
it cannot fabricate names or facts — important for a journalist. A "synthesis"
mode (the model writes its own prose) is available but can invent details.
"""

import asyncio
import datetime as dt
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import tomllib                # Python 3.11+
except ModuleNotFoundError:
    import tomli as tomllib       # older Python: pip install tomli

from telethon import TelegramClient
from telethon.tl.types import Channel

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.toml"
SESSION = str(HERE / "tg_brief")     # login session file (created on first run)
OUT_AUDIO = HERE / "brief.m4a"       # macOS `say` writes m4a; Telegram plays it fine
OUT_TEXT = HERE / "brief.txt"        # written record of what the voice said
MAX_MSGS_PER_CHAT = 300              # safety cap per chat
PROMPT_CHAR_BUDGET = 24000           # max chars of messages fed to the local model
TELEGRAM_MSG_LIMIT = 4000            # stay safely under Telegram's 4096-char cap


def load_config():
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


def message_link(entity, msg):
    """A tappable t.me link to one message, or None if we can't build one."""
    uname = getattr(entity, "username", None)
    if uname:
        return f"https://t.me/{uname}/{msg.id}"            # public channel
    if isinstance(entity, Channel):
        return f"https://t.me/c/{entity.id}/{msg.id}"      # private channel/supergroup (members only)
    return None                                            # legacy groups/users: t.me/c/ links don't work


async def collect_messages(client, channels, cutoff):
    """Return (items, failed) — message dicts since cutoff, and a list of any
    channels that couldn't be opened (so the brief can flag a broken source)."""
    items = []
    failed = []
    if channels == ["*"]:
        # Explicit opt-in only: reads EVERY chat on the account, personal ones too.
        targets = [d.entity async for d in client.iter_dialogs()]
    else:
        targets = channels
    for target in targets:
        try:
            entity = await client.get_entity(target) if isinstance(target, str) else target
        except Exception as e:
            print(f"[could not open {target}: {e}]")
            failed.append(str(target))
            continue
        name = getattr(entity, "title", None) or getattr(entity, "username", str(target))
        # Walk NEWEST-first and stop at the cutoff, so on a very busy night the
        # per-chat cap drops the oldest messages — never the freshest news.
        n_seen = 0
        capped = True                      # assume we ran out of budget…
        async for msg in client.iter_messages(entity, limit=MAX_MSGS_PER_CHAT):
            if msg.date and msg.date < cutoff:
                capped = False             # …unless we reached the cutoff first
                break
            n_seen += 1
            if msg.text:
                when = msg.date.astimezone() if msg.date else None
                items.append({
                    "channel": name,
                    "dt": when,
                    "time": when.strftime("%H:%M") if when else "--:--",
                    "text": msg.text.strip(),
                    "link": message_link(entity, msg),
                })
        if capped and n_seen == MAX_MSGS_PER_CHAT:
            print(f"[{name}: hit the {MAX_MSGS_PER_CHAT}-message cap — older overnight messages skipped]")
    return items, failed


def _first_line(text):
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return text.strip()


# Emoji / pictographs / dingbats / arrows — `say` reads many of these out by
# name ("rocket", "police car light"), which sounds absurd in a news brief.
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF"      # emoji, flags, symbols & pictographs
    "\u2600-\u27BF"               # misc symbols + dingbats (suns, ticks, planes)
    "\u2B00-\u2BFF"               # arrows, stars
    "\u2190-\u21FF"               # more arrows
    "\uFE0F\u20E3\u2122]"        # variation selector, keycap, trademark sign
)


def _for_speech(s):
    """Tidy a snippet for the TTS voice: strip markdown links, URLs, hashtags,
    stray markdown punctuation and emoji, then collapse whitespace."""
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)   # [text](url) -> text
    s = re.sub(r"\[([^\]]+)\]\(", r"\1", s)          # half-open [text]( -> text
    s = re.sub(r"https?://\S+", "", s)               # bare URLs
    s = re.sub(r"#\w+", "", s)                       # hashtags, word and all
    s = _EMOJI_RE.sub("", s)                         # emoji (see above)
    s = re.sub(r"[*_`#>]+", "", s)                   # markdown emphasis/heading marks
    s = re.sub(r"[\[\]()]", "", s)                   # leftover brackets
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _truncate(s, n):
    if len(s) <= n:
        return s
    return s[:n].rsplit(" ", 1)[0].rstrip() + "…"


def _split_message(text, limit=TELEGRAM_MSG_LIMIT):
    """Split a long text into Telegram-sized chunks on line boundaries.
    Telegram rejects messages over ~4096 chars, and a busy night's Sources
    list can exceed that."""
    chunks, cur = [], ""
    for ln in text.split("\n"):
        if cur and len(cur) + 1 + len(ln) > limit:
            chunks.append(cur)
            cur = ln
        else:
            cur = f"{cur}\n{ln}" if cur else ln
    if cur:
        chunks.append(cur)
    return chunks


def _keep_newest(lines, budget):
    """Keep as many of the NEWEST lines (list is oldest→newest) as fit in a
    character budget. Returns (kept_lines, dropped_count) — for a morning news
    brief, recency wins, so overflow always drops the oldest."""
    total = 0
    start = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        total += len(lines[i]) + 1
        if total > budget:
            break
        start = i
    return lines[start:], start


def _trim_log(path, keep=400):
    """Keep run.log / run.err from growing forever on machines nobody looks at.
    Safe while launchd holds the file open: it appends (O_APPEND), so writes
    land at the new end after truncation."""
    try:
        lines = path.read_text(errors="replace").splitlines(keepends=True)
        if len(lines) > keep:
            path.write_text("".join(lines[-keep:]))
    except OSError:
        pass


def build_digest(items, digest_cfg):
    """Assemble the spoken brief + sources list locally (no AI). Returns
    (spoken_text, sources_text, dropped_count)."""
    style = digest_cfg.get("style", "headlines")            # "headlines" or "full"
    max_items = int(digest_cfg.get("max_items", 25))         # hard cap on items read
    headline_chars = int(digest_cfg.get("headline_chars", 220))

    # Oldest -> newest across all channels.
    floor = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    items = sorted(items, key=lambda it: it["dt"] or floor)

    # De-duplicate near-identical posts (same story reposted across chats).
    # Unicode-aware key so non-Latin channels (Cyrillic, Arabic, Hebrew…) dedupe
    # too instead of every headline collapsing to "" and being dropped.
    seen, unique = set(), []
    for it in items:
        norm = "".join(ch for ch in _first_line(it["text"]).lower() if ch.isalnum())[:120]
        if norm in seen:
            continue
        if norm:                     # emoji/punctuation-only lines can't be keyed —
            seen.add(norm)           # keep them rather than silently dropping them
        unique.append(it)

    # Cap: keep the most recent max_items.
    dropped = max(0, len(unique) - max_items)
    if dropped:
        unique = unique[-max_items:]

    spoken_lines = ["Good morning. Here's what came in overnight."]
    source_lines = []
    for it in unique:
        body = _strip_noise_lines(it["text"])
        snippet = body if style == "full" else _first_line(body)
        spoken = _for_speech(snippet)
        if style == "headlines":
            spoken = _truncate(spoken, headline_chars)
        if spoken:
            spoken_lines.append(spoken.rstrip(".") + ".")
        head = _truncate(_for_speech(_first_line(body)), 90)
        link = it["link"] or ""
        source_lines.append(f"- {head} — {it['channel']}, {it['time']} {link}".rstrip())

    spoken_lines.append(f"That's {len(unique)} item{'s' if len(unique) != 1 else ''}. End of brief.")
    return "\n".join(spoken_lines), "\n".join(source_lines), dropped


SUMMARY_SYSTEM = (
    "You are a neutral wire-service editor writing a spoken morning brief for a journalist. "
    "You read many raw wire messages, work out the handful of real stories they describe, "
    "and for each one report what happened and its concrete, factual implications for the "
    "matter concerned. You are strictly factual and neutral: no spin, no opinion, no "
    "value judgements, no emotive or editorial language, no claims about how big or "
    "significant something is. You never invent facts, names, numbers or places, and you "
    "copy names and figures EXACTLY as written; if a name is garbled you describe the role "
    "instead of guessing. You write plain spoken prose."
)

def _focus_block(focus):
    """Build the relevance-filter instruction from the [summary] focus list."""
    if not focus:
        return ""
    bullets = "\n".join(f"  - {f}" for f in focus)
    return (
        "RELEVANCE FILTER — apply this FIRST, before anything else:\n"
        "Only include stories that genuinely bear on one of these areas:\n"
        f"{bullets}\n"
        "Leave out everything else entirely — trivia, memes, stunts, celebrity, sport, "
        "weather, local crime, and any item that does not clearly relate to the areas "
        "above. A story counts only if its core subject is one of those areas; do not "
        "stretch a tangential mention to qualify. If nothing qualifies, say only: "
        "\"Good morning. Nothing of note came in overnight.\"\n\n"
    )


SUMMARY_INSTRUCTIONS = """Write the spoken morning brief now. The key rule: SYNTHESISE, do
not relay. Do NOT walk through messages one by one. Instead:

1. Work out the MAIN stories the messages collectively describe that pass the relevance
   filter above.
2. For each story, write a short factual paragraph: pull every related message into one
   account of what happened, then state the concrete implications — the direct, factual
   consequences for the parties or situation involved (what it changes, what follows from
   it). Many messages about one event become a single paragraph, not many sentences.
3. If a story is genuinely just one small fact, state it in one plain sentence.
4. Ignore opinion posts, pundit quotes, promos, memes, greetings and duplicates entirely.

Strict neutrality and accuracy rules:
- Report only. NO spin, NO opinion, NO value judgements, NO emotive words, NO loaded
  adjectives. Attribute claims to who made them ("the army said", "the ministry reported").
- Do NOT characterise scale or importance. Never say things like "significant", "major",
  "heaviest in weeks", "escalation", "dozens" — give the actual stated numbers instead.
- "Implications" means factual consequences stated or directly entailed by the messages —
  NOT your assessment of how important it is. If the implication isn't grounded, omit it.
- Every fact must come from the messages. Copy names, places and numbers EXACTLY. Never
  round or generalise a number. Never add outside background or speculation.
- Plain spoken sentences for a text-to-speech voice. NO markdown, asterisks, headings,
  bullets, URLs or emoji. No preamble, no AI sign-off, no "this summary is not exhaustive".
Start with exactly: "Good morning. Here's what came in overnight."
Then the brief, grouped into those few stories. Then stop."""


def _clean_spoken(text):
    """Strip anything that would sound wrong read aloud, and trim model preamble."""
    # Remove markdown emphasis/heading/bullet characters.
    text = re.sub(r"[*_#`]+", "", text)
    text = re.sub(r"(?m)^\s*[-•]\s+", "", text)
    text = re.sub(r"https?://\S+", "", text)
    # Start at the intended opening line, dropping any model preamble before it.
    m = re.search(r"good morning\b", text, re.IGNORECASE)
    if m:
        text = text[m.start():]
    # Drop AI meta / self-talk lines anywhere — small models sometimes emit a
    # sign-off about the brief itself ("The brief is now free of...", "Let me
    # check...", "I hope this helps"). These must never reach the voice.
    meta = re.compile(
        r"^\s*(please note|note that|disclaimer|i hope|let me|here('?s| is)|"
        r"the (brief|summary)\b|this (summary|brief)\b|in summary|to summari[sz]e|"
        r"that('?s| is) (the|all)|end of brief|as an ai|i (have|will|can))",
        re.IGNORECASE,
    )
    lines = [ln.rstrip() for ln in text.splitlines() if not meta.match(ln.strip())]
    # Re-add the intended opening if the filter above removed it.
    if not lines or not lines[0].lower().startswith("good morning"):
        lines.insert(0, "Good morning. Here's what came in overnight.")
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _ollama_ready(host, timeout_s):
    """Wait until the local Ollama server answers, up to timeout_s. True if up."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"{host}/api/version", timeout=3)
            return True
        except Exception:
            time.sleep(2)
    return False


def _ollama_generate(summary_cfg, system, prompt, options, want_json=False):
    """One request to the local Ollama server. Returns the response text, or
    None on any failure so the caller falls back to the rule-based digest."""
    host = summary_cfg.get("ollama_host", "http://127.0.0.1:11434").rstrip("/")
    model = summary_cfg.get("model", "gemma2:9b")
    wait_s = int(summary_cfg.get("startup_wait_s", 60))

    if not _ollama_ready(host, wait_s):
        print("Ollama not reachable — falling back to rule-based digest.")
        return None

    # num_ctx matters: without it Ollama uses a small default context window and
    # SILENTLY drops the front of a long prompt — most of the night's messages.
    payload = {
        "model": model,
        "system": system,
        "prompt": prompt,
        "stream": False,
        "options": {"num_ctx": 8192, **options},
    }
    if want_json:
        payload["format"] = "json"          # ask Ollama to constrain output to JSON
    try:
        req = urllib.request.Request(
            f"{host}/api/generate", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            out = json.loads(resp.read())
        return (out.get("response") or "").strip()
    except Exception as e:
        print(f"Ollama request failed ({e}) — falling back to rule-based digest.")
        return None


def summarize_local(items, summary_cfg):
    """Summarise the overnight messages with a local Ollama model.

    Returns the spoken brief text, or None if Ollama is unreachable/failed so
    the caller can fall back to the rule-based digest."""
    # Feed the model the channel + time + text of each message (oldest first).
    floor = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    ordered = sorted(items, key=lambda it: it["dt"] or floor)
    lines = [
        f"[{it['channel']} {it['time']}] {_for_speech(it['text'])}" for it in ordered
    ]
    lines, cut = _keep_newest(lines, PROMPT_CHAR_BUDGET)
    if cut:
        print(f"[busy night: the oldest {cut} messages didn't fit the model prompt]")
    raw = "\n".join(lines)

    # Instructions go AFTER the messages too: small models attend most to the end.
    # The relevance filter is stated both before the messages and in the trailing
    # instructions, so the model applies it when deciding what to keep.
    focus_block = _focus_block(summary_cfg.get("focus", []))
    prompt = (
        focus_block
        + "Here are the raw overnight messages:\n\n"
        + raw
        + "\n\n" + focus_block + SUMMARY_INSTRUCTIONS
    )
    response = _ollama_generate(
        summary_cfg, SUMMARY_SYSTEM, prompt,
        {"temperature": 0.2, "num_predict": 900},
    )
    if response is None:
        return None
    return _clean_spoken(response) or None


SELECT_SYSTEM = (
    "You are a wire-desk sub-editor selecting which raw overnight messages belong in a "
    "morning news brief. You do NOT write or rephrase anything — you only choose message "
    "numbers and group the ones that report the same story. You output JSON only."
)


_PROMO_RE = re.compile(
    r"youtube|rumble|odysee|rutube|tik[\s-]?tok|instagram|facebook|patreon|"
    r"subscribe|premiere|follow us|join (us|our)|boost\b|donat",
    re.IGNORECASE,
)


def _looks_promo(text):
    """Detect roundup/promo posts (platform plugs, premiere schedules, emoji
    walls). They're often the LONGEST message in a story group, but the last
    thing a spoken brief should read out."""
    if len(_PROMO_RE.findall(text)) >= 2:
        return True
    return len(_EMOJI_RE.findall(text)) > 8


# Lines WITHIN a message that are channel furniture, not news: signature
# footers ("@Channel | Socials | Donate"), plugs, and the label lines of
# geolocation map cards ("Coordinates: 50.93284,34.81981" read digit by digit).
_NOISE_LINE_RE = re.compile(
    r"^\s*(place|date|time|coordinates|geolocation|squad|source|map)\s*:|"
    r"boost the channel|subscribe|follow us|watch here|"
    r"\|\s*(socials|donate|advertising|boost)|@\w+\s*\|",
    re.IGNORECASE,
)


def _strip_noise_lines(text):
    """Drop footer/plug/metadata lines from a message before it's read aloud.
    If everything matched (it was ALL furniture), keep the original rather
    than return nothing."""
    kept = [ln for ln in text.splitlines() if not _NOISE_LINE_RE.search(ln)]
    out = "\n".join(kept).strip()
    return out if out else text


def select_relevant(items, summary_cfg):
    """Quote-only mode: the model SELECTS and GROUPS real messages (returns their
    numbers); we then read the actual message text. The model writes no prose, so
    it cannot invent facts. Returns spoken text, or None to fall back."""
    max_groups = int(summary_cfg.get("max_groups", 12))
    focus = summary_cfg.get("focus", [])

    # Number every message so the model can refer to them without retyping text.
    floor = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    ordered = sorted(items, key=lambda it: it["dt"] or floor)
    lines = [
        f"[{i}] ({it['channel']} {it['time']}) {_for_speech(it['text'])}"
        for i, it in enumerate(ordered)
    ]
    # Numbers index into `ordered`, so dropping the oldest lines keeps the rest valid.
    lines, cut = _keep_newest(lines, PROMPT_CHAR_BUDGET)
    if cut:
        print(f"[busy night: the oldest {cut} messages didn't fit the model prompt]")
    numbered = "\n".join(lines)

    focus_txt = ""
    if focus:
        focus_txt = (
            "RELEVANCE — include a message ONLY if it REPORTS A CONCRETE EVENT or official "
            "action/decision whose core subject is one of:\n"
            + "\n".join(f"  - {f}" for f in focus)
            + "\nA message qualifies only if it describes something that HAPPENED (a strike, "
            "an attack, a death toll, a sanction, a government/military decision, a troop or "
            "weapons movement, a diplomatic action). EXCLUDE, even if they mention one of the "
            "areas above:\n"
            "  - opinion, analysis, commentary, predictions, or pundit/analyst/expert quotes\n"
            "  - sport, football, entertainment, celebrity, protests at games\n"
            "  - reactions, condemnations and 'X said' takes that report no new event\n"
            "  - memes, stunts, trivia, weather, local crime, promos\n"
            "If you are unsure whether a message reports a real event, EXCLUDE it.\n\n"
        )

    instr = (
        focus_txt
        + "From the numbered messages above, choose only the ones that pass the relevance "
        "test and group those reporting the SAME event together (to collapse duplicates).\n"
        "Within each group, put FIRST the number of the message that most factually and "
        "completely states the event (never a promo, schedule or link roundup).\n"
        "Output JSON ONLY, no prose: a list of groups, each group a list of message "
        "numbers, ordered most important first. Example: [[4,9],[12],[2,7,15]]\n"
        f"Include at most {max_groups} groups. If nothing qualifies, output []."
    )
    prompt = numbered + "\n\n" + instr

    response = _ollama_generate(
        summary_cfg, SELECT_SYSTEM, prompt,
        {"temperature": 0.0, "num_predict": 500}, want_json=True,
    )
    if response is None:
        return None
    groups = _parse_groups(response, len(ordered))
    if groups is None:                    # unusable model output — digest fallback
        return None
    if not groups:
        # A cleanly parsed "[]" is an ANSWER — nothing passed the focus filter —
        # not a failure. Falling back here would read out the unfiltered digest
        # of exactly the trivia the model just rejected.
        return "Good morning. Nothing of note came in overnight. End of brief."

    # Build the spoken brief from the ACTUAL message text. Nothing here is
    # model-generated. Per group: the longest NON-PROMO message (most complete
    # account); if every candidate looks like promo, trust the model's first
    # pick rather than rewarding a platform-plug roundup for being long.
    spoken = ["Good morning. Here's what came in overnight."]
    for grp in groups[:max_groups]:
        cands = [ordered[i] for i in grp]
        pool = [it for it in cands if not _looks_promo(it["text"])] or cands[:1]
        rep = max(pool, key=lambda it: len(it["text"]))
        line = _for_speech(_strip_noise_lines(rep["text"]))
        if line:
            spoken.append(line.rstrip(".") + ".")
    return "\n".join(spoken) if len(spoken) > 1 else None


def _parse_groups(raw, n):
    """Pull a list-of-lists of valid message indices out of the model's JSON.

    Returns None when the output is unusable (caller falls back to the digest),
    or a list — possibly EMPTY, which means the model decided nothing qualifies.
    The caller treats those two cases very differently."""
    try:
        data = json.loads(raw)
    except Exception:
        m = re.search(r"\[.*\]", raw, re.DOTALL)      # salvage JSON from any wrapper
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except Exception:
            return None
    if isinstance(data, dict):                        # tolerate {"groups": [...]}
        data = next((v for v in data.values() if isinstance(v, list)), None)
    if not isinstance(data, list):
        return None
    groups, seen = [], set()
    for grp in data:
        ids = grp if isinstance(grp, list) else [grp]
        clean = []
        for x in ids:
            try:
                i = int(x)
            except (TypeError, ValueError):
                continue
            if 0 <= i < n and i not in seen:          # valid + not already used
                seen.add(i)
                clean.append(i)
        if clean:
            groups.append(clean)
    if data and not groups:
        return None      # it named messages but none were valid — don't trust it
    return groups


def speak(text, path, say_voice="", rate=None):
    """Render text to an audio file with the built-in macOS `say` command."""
    # `say` writes to an AIFF temp first. We do NOT let `say` pick the .m4a codec:
    # it sometimes emits raw LPCM in an .m4a container, which shows a duration but
    # plays SILENT on Telegram mobile. We then transcode to real AAC with afconvert.
    aiff = Path(str(path) + ".aiff")
    # Absolute paths: the launchd (scheduled) environment has a minimal PATH, so
    # bare "say"/"afconvert" could fail to resolve at 3:40am. These live in /usr/bin.
    say_bin = "/usr/bin/say" if Path("/usr/bin/say").exists() else "say"
    afconvert_bin = "/usr/bin/afconvert" if Path("/usr/bin/afconvert").exists() else "afconvert"
    cmd = [say_bin, "-o", str(aiff), "--file-format=AIFF"]
    if say_voice:
        cmd += ["-v", say_voice]
    if rate:
        cmd += ["-r", str(rate)]
    cmd += ["-f", "-"]                    # read the text from stdin (handles long input)
    try:
        # Encode to UTF-8 ourselves (input as bytes) so it works even when the
        # terminal locale is ASCII — otherwise curly quotes etc. crash the pipe.
        subprocess.run(cmd, input=text.encode("utf-8"), check=True)
        # Transcode to AAC in an m4a container — the format Telegram plays everywhere.
        subprocess.run(
            [afconvert_bin, "-f", "m4af", "-d", "aac", "-b", "64000",
             str(aiff), str(path)],
            check=True,
        )
    finally:
        aiff.unlink(missing_ok=True)


async def run_check(cfg):
    """`python3 telegram_brief.py --check` — verify a (new) install in seconds
    instead of waiting for the first 3:40am run. Read-only: connects to
    Telegram but sends nothing. Returns a shell exit code."""
    failures = 0

    def report(good, label, fix=""):
        nonlocal failures
        failures += 0 if good else 1
        mark = " ok " if good else "FAIL"
        print(f"[{mark}] {label}" + (f"  →  {fix}" if (fix and not good) else ""))

    # --- config values ---
    cred = cfg.get("credentials", {})
    have_creds = bool(cred.get("telegram_api_id")) and bool(cred.get("telegram_api_hash"))
    report(have_creds, "Telegram credentials filled in",
           "get api_id/api_hash at https://my.telegram.org and put them in config.toml")
    channels = cfg.get("channels", {}).get("follow", [])
    report(bool(channels), "[channels] follow has entries", "add at least one channel")
    scrape = str(cfg.get("schedule", {}).get("scrape_time", ""))
    report(bool(re.fullmatch(r"\d{1,2}:\d{2}", scrape)),
           f'scrape_time "{scrape}" is 24-hour HH:MM', 'use e.g. "03:40"')

    # --- voice ---
    say_voice = cfg.get("voice", {}).get("say_voice", "")
    if say_voice:
        say_bin = "/usr/bin/say" if Path("/usr/bin/say").exists() else "say"
        listing = subprocess.run([say_bin, "-v", "?"], capture_output=True, text=True).stdout
        report(any(ln.startswith(say_voice + " ") for ln in listing.splitlines()),
               f'say voice "{say_voice}" is installed', "list valid names with:  say -v '?'")
    else:
        report(True, "say voice: system default")

    # --- Ollama (only if the AI summary is on) ---
    summary_cfg = cfg.get("summary", {})
    if summary_cfg.get("enabled", True):
        host = summary_cfg.get("ollama_host", "http://127.0.0.1:11434").rstrip("/")
        model = summary_cfg.get("model", "gemma2:9b")
        up = _ollama_ready(host, 5)
        report(up, f"Ollama server at {host}",
               "start Ollama (brew services start ollama), or set [summary] enabled = false")
        if up:
            try:
                with urllib.request.urlopen(f"{host}/api/tags", timeout=5) as resp:
                    tags = json.loads(resp.read())
                names = [m.get("name", "") for m in tags.get("models", [])]
                report(any(nm == model or nm.split(":")[0] == model for nm in names),
                       f'Ollama model "{model}" is pulled', f"run:  ollama pull {model}")
            except Exception as e:
                report(False, "could not list Ollama models", str(e))
    else:
        report(True, "AI summary disabled — rule-based digest will be used")

    # --- Telegram login + every channel handle ---
    if have_creds:
        client = TelegramClient(SESSION, cred["telegram_api_id"], cred["telegram_api_hash"])
        await client.connect()
        try:
            authed = await client.is_user_authorized()
            report(authed, "Telegram login is live",
                   "run `python3 telegram_brief.py` once in Terminal to log in")
            if authed:
                for ch in channels:
                    if ch == "*":
                        report(True, 'follow = ["*"] — will read EVERY chat on this account')
                        continue
                    try:
                        entity = await client.get_entity(ch)
                        name = getattr(entity, "title", None) or getattr(entity, "username", ch)
                        report(True, f"channel {ch}  ({name})")
                    except Exception as e:
                        report(False, f"channel {ch}", f"{e}")
                alert_to = cfg.get("alerts", {}).get("parent_alert_to", "")
                if alert_to:
                    try:
                        await client.get_entity(alert_to)
                        report(True, f"alert recipient {alert_to} resolves")
                    except Exception as e:
                        report(False, f"alert recipient {alert_to}",
                               f"{e} — check the @username in [alerts]")
                else:
                    report(True, "alerts off (parent_alert_to is empty)")
        finally:
            await client.disconnect()
    else:
        report(False, "Telegram login", "cannot check without credentials")

    # --- schedule installed? ---
    plist = Path.home() / "Library" / "LaunchAgents" / "com.user.tgbrief.plist"
    report(plist.exists(), "daily schedule installed", "run `python3 install_schedule.py`")

    print()
    if failures:
        print(f"{failures} problem(s) found — fix the FAIL lines above and re-run --check.")
    else:
        print("All checks passed. Test a real run now with:  launchctl start com.user.tgbrief")
    return 1 if failures else 0


async def main():
    cfg = load_config()
    for logfile in (HERE / "run.log", HERE / "run.err"):
        _trim_log(logfile)
    # Date every run in BOTH logs. Tracebacks and Telethon's connection
    # warnings land in run.err with no timestamps of their own — without a
    # banner there's no telling which morning they belong to.
    banner = f"——— run started {dt.datetime.now():%a %d %b %Y %H:%M:%S} ———"
    print(banner, flush=True)
    print(banner, file=sys.stderr, flush=True)
    cred = cfg["credentials"]
    sched = cfg["schedule"]
    voice = cfg.get("voice", {})
    digest_cfg = cfg.get("digest", {})
    summary_cfg = cfg.get("summary", {})
    channels = cfg["channels"]["follow"]
    alerts = cfg.get("alerts", {})
    alert_to = alerts.get("parent_alert_to", "")
    heartbeat = alerts.get("daily_heartbeat", True)
    # With several Macs running this, the alert says WHOSE morning it was.
    tag = f"[{alerts.get('name')}] " if alerts.get("name") else ""

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=sched["lookback_hours"])

    client = TelegramClient(SESSION, cred["telegram_api_id"], cred["telegram_api_hash"])
    # Only prompt for a phone/login code when run by hand in a real terminal
    # (the one-time setup login). The scheduled launchd run has no terminal, so
    # we connect silently and fail with a clear message if the session is dead —
    # rather than trying to read a phone number from nowhere and dumping a
    # traceback into run.err.
    interactive = sys.stdin.isatty()
    try:
        if interactive:
            await client.start()
        else:
            await client.connect()
            if not await client.is_user_authorized():
                raise RuntimeError(
                    "Telegram session is not authorized (expired or never set up). "
                    "Re-run `python3 telegram_brief.py` by hand to log in again."
                )
        try:
            if not channels:
                # An empty list must NOT mean "read everything" — that would pull
                # the account's personal chats into a spoken brief. Explicit only.
                raise RuntimeError(
                    "[channels] follow in config.toml is empty — add at least one "
                    'channel (or ["*"] to deliberately read every chat).'
                )
            items, failed = await collect_messages(client, channels, cutoff)
            if not items:
                print("No overnight messages found.")
                OUT_TEXT.write_text("(quiet night — no overnight messages, no audio sent)\n")
                # Tell the listener too — otherwise a quiet night looks broken.
                await client.send_message(
                    "me", "🌙 Quiet night — no new messages in your channels overnight."
                )
                if alert_to and heartbeat:
                    note = f" (couldn't read: {', '.join(failed)})" if failed else ""
                    await client.send_message(
                        alert_to,
                        f"✅ {tag}Brief ran {dt.datetime.now():%H:%M} — quiet night, no overnight messages.{note}",
                    )
                return
            # Sources list is always built from the real messages (factual).
            # The spoken part is the AI summary when available, else the digest.
            digest_spoken, sources, dropped = build_digest(items, digest_cfg)
            spoken, spoken_by = digest_spoken, "rule-based digest"
            if summary_cfg.get("enabled", True):
                mode = summary_cfg.get("mode", "extractive")
                ai = (select_relevant(items, summary_cfg) if mode == "extractive"
                      else summarize_local(items, summary_cfg))
                if ai:
                    spoken, spoken_by = ai, f"AI {mode}"
            # A written record of what the voice said: run.log gets which path
            # produced it, brief.txt (next to brief.m4a) holds the exact words.
            n_lines = max(0, len([ln for ln in spoken.splitlines() if ln.strip()]) - 1)
            print(f"Spoken: {spoken_by}, {n_lines} item(s).")
            OUT_TEXT.write_text(spoken + "\n")
            speak(spoken, OUT_AUDIO, voice.get("say_voice", ""), voice.get("rate"))
            stamp = dt.datetime.now().strftime("%a %d %b, %H:%M")
            await client.send_file("me", OUT_AUDIO, caption=f"Overnight brief — {stamp}")
            if sources:
                if dropped:
                    sources += f"\n(+{dropped} more not read — raise max_items in config.toml)"
                if failed:
                    sources += f"\n⚠️ Couldn't read: {', '.join(failed)} — check the handle in config.toml"
                # A busy night's sources can exceed Telegram's message size cap.
                for chunk in _split_message("🔎 Sources\n" + sources):
                    await client.send_message("me", chunk, link_preview=False)
            print("Sent brief to Saved Messages.")
            # Heartbeat: a daily "all good" ping to the maintainer. If this stops
            # arriving, something is wrong — even an expired login that can't
            # send a failure alert will show up as a *missing* heartbeat.
            if alert_to and heartbeat:
                await client.send_message(
                    alert_to,
                    f"✅ {tag}Morning brief sent {dt.datetime.now():%H:%M}.",
                )
        except Exception as e:
            # Something broke — quietly alert whoever maintains this, not the listener.
            # Note: if the failure IS the dead session, this Telegram alert can't
            # send either (we're not authorized) — that's what the daily heartbeat
            # is for: its ABSENCE is the signal. This alert covers all other faults.
            print(f"ERROR: {e}")
            if alert_to:
                try:
                    await client.send_message(
                        alert_to,
                        f"⚠️ {tag}Morning brief failed at {dt.datetime.now():%H:%M} — {e}",
                    )
                except Exception:
                    pass
            raise
    finally:
        await client.disconnect()


async def run_login(cfg):
    """`--login` — do (only) the one-time interactive Telegram login. Prompts
    for the owner's phone number and the code Telegram texts them; sends and
    scrapes nothing. A no-op if the session is already live."""
    cred = cfg["credentials"]
    client = TelegramClient(SESSION, cred["telegram_api_id"], cred["telegram_api_hash"])
    await client.start()
    me = await client.get_me()
    who = f"@{me.username}" if getattr(me, "username", None) else (me.first_name or "unknown")
    print(f"Telegram login OK — this Mac posts as {who}.")
    await client.disconnect()


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(asyncio.run(run_check(load_config())))
    if "--login" in sys.argv:
        asyncio.run(run_login(load_config()))
        sys.exit(0)
    asyncio.run(main())
