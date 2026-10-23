#!/usr/bin/env python3
"""
Overnight Telegram -> audio brief.  (Fully local, no paid APIs.)

All settings live in config.toml — you should not need to edit this file.
Pulls recent messages from the chats listed in config.toml, summarises them
with Apple's on-device AI model (Apple Intelligence, on this Mac — free, no
account), turns the summary into speech with the built-in macOS `say` voice, and posts the
audio to Telegram Saved Messages — plus a text "Sources" message with a
tappable link to each original.

No paid APIs, no cloud: the only external service is Telegram. If Apple's
model isn't available, it falls back to a rule-based headline digest so a brief
still goes out. See the [summary] and [digest] sections of config.toml.

Default summary mode is "extractive": the on-device model only SELECTS and groups
the real messages worth airing (filtered to the [summary] focus areas); the
brief is then read from the actual message text. The model writes no prose, so
it cannot fabricate names or facts — important for a journalist. A "synthesis"
mode (the model writes its own prose) is available but can invent details.
"""

import asyncio
import datetime as dt
import json
import platform
import re
import subprocess
import sys
import unicodedata
import wave
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
APPLE_LLM = HERE / "apple_llm"       # built from apple_llm.swift by setup.sh
JUDGE_MSG_CHARS = 700                # long posts are cut to this for judging (not for reading)
SAME_STORY_HOURS = 3                 # reports further apart than this are never merged
SAME_STORY_NAME_SHARE = 0.05         # a name in more of the night's messages than this is too common to link two
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


# Straighten typographic punctuation and turn separator glyphs into commas:
# the voice pauses naturally at a comma, but stumbles or goes silent on
# em-dashes, pipes and bullets.
_PUNCT_MAP = str.maketrans({
    "“": '"', "”": '"', "„": '"', "«": '"', "»": '"',
    "‘": "'", "’": "'", "‚": "'",
    "—": ",", "–": ",", "…": ",",
    "|": ",", "•": ",", "·": ",", "~": "",
    " ": " ",
})


def _normalize_punct(s):
    """Make punctuation speakable (used on everything that reaches the voice)."""
    s = re.sub(r"(?<=\d)\s*[–—-]\s*(?=\d)", " to ", s)   # 700–800 -> "700 to 800"
    s = s.translate(_PUNCT_MAP)
    s = re.sub(r"[‼⁉⁈]", "!", s)                         # say names these aloud
    s = re.sub(r"\s+([,!])", r"\1", s)                  # "word ," -> "word,"
    s = re.sub(r",(?:\s*,)+", ",", s)                    # collapse comma runs
    return s


def _strip_symbols(s):
    """Remove emoji and EVERY other symbol-class character the voice would
    read out by name (\u25b6, \u25fe, \u3297 ...). Filtering by Unicode category
    rather than code-point ranges means new symbol blocks can't slip through.
    ASCII always passes; degree/plus-minus/times/divide and currency signs are
    kept (`say` reads them sensibly). Unicode files a few geometric shapes
    under Sm (math), hence its inclusion in the strip set."""
    s = _EMOJI_RE.sub("", s)
    return "".join(
        ch for ch in s
        if ord(ch) < 128
        or ch in "\u00b0\u00b1\u00d7\u00f7"          # degree, plus-minus, times, divide
        or (ch not in "\u200d\ufe0e"                   # ZWJ, text-style selector
            and unicodedata.category(ch) not in ("So", "Sk", "Sm", "Cs", "Co", "Cn"))
    )


def _strip_foreign_script(s):
    """Drop words in scripts the brief can't voice (Arabic, Hebrew, CJK ...),
    which an English voice reads as gibberish. Latin and Cyrillic stay:
    Russian is read out by a Russian voice (see speak()). Punctuation left
    dangling by a removed run ("Summary , .") is tidied up too."""
    out = "".join(
        ch for ch in s
        if not ch.isalpha() or any(k in unicodedata.name(ch, "LATIN")
                                   for k in ("LATIN", "CYRILLIC"))
    )
    if out == s:
        return s
    out = re.sub(r"[ \t]+([,.;:])", r"\1", out)       # space before punctuation
    out = re.sub(r"([,;:])(?:\s*[,;:.])+", r"\1", out)  # runs of leftover separators
    out = re.sub(r"(?m)^[\s,;:.]+$", "", out)          # lines left as bare punctuation
    out = re.sub(r"[,;:]\s*$", "", out)                # trailing separator
    return out


def _for_speech(s):
    """Tidy a snippet for the TTS voice: strip markdown links, URLs, hashtags,
    stray markdown punctuation and emoji, then collapse whitespace."""
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)   # [text](url) -> text
    s = re.sub(r"\[([^\]]+)\]\(", r"\1", s)          # half-open [text]( -> text
    s = re.sub(r"https?://\S+", "", s)               # bare URLs
    s = re.sub(r"#\w+", "", s)                       # hashtags, word and all
    s = _strip_symbols(s)                            # emoji + all symbol chars
    s = _strip_foreign_script(s)                     # scripts no voice here can read
    s = re.sub(r"[*_`#>]+", "", s)                   # markdown emphasis/heading marks
    # Wire-service urgency stamps are noise by the time the brief is read.
    # (After the markdown strip, so "**BREAKING**" is caught too.)
    s = re.sub(r"(?im)^\W*(?:breaking(?:\s+news)?|urgent|just in)\s*(?:[.:,|–—-]+\s*|$)", "", s)
    s = _normalize_punct(s)
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

SUMMARY_INSTRUCTIONS = """Write the spoken morning brief now. The key rule: SYNTHESISE, do
not relay. Do NOT walk through messages one by one. Instead:

1. Work out the MAIN stories the messages collectively describe.
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
    text = _strip_symbols(_normalize_punct(text))      # model output can carry these too
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class AppleModelUnavailable(Exception):
    """Apple Intelligence is off, still downloading, or the helper is missing."""


def _apple_generate(system, prompt, temperature=0.0, max_tokens=500):
    """One request to Apple's on-device model via the apple_llm helper.

    Returns (text, status): status is "ok", "too_long" (prompt
    over the context window), "blocked" (Apple's safety guardrails) or "error".
    Raises AppleModelUnavailable when the model can't be used at all, so the
    caller stops trying and falls back to the rule-based digest."""
    if not APPLE_LLM.exists():
        raise AppleModelUnavailable("apple_llm helper not built — run ./setup.sh")
    req = {"system": system, "prompt": prompt,
           "temperature": temperature, "max_tokens": max_tokens}
    try:
        proc = subprocess.run([str(APPLE_LLM)], input=json.dumps(req).encode("utf-8"),
                              capture_output=True, timeout=300)
    except subprocess.TimeoutExpired:
        return None, "error"
    err = proc.stderr.decode("utf-8", "replace").strip()
    if proc.returncode == 0:
        return proc.stdout.decode("utf-8", "replace").strip(), "ok"
    if proc.returncode == 2:
        raise AppleModelUnavailable(err)
    status = {3: "blocked", 4: "too_long"}.get(proc.returncode, "error")
    if status == "error":
        print(f"[Apple model error: {err}]")
    return None, status


SELECT_SYSTEM = (
    "You are a wire-desk sub-editor judging raw overnight messages for a spoken morning "
    "news brief. You judge each message on its own text only. You never add facts."
)

_TOPIC_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXY"
_NOT_EVENT = ("QUOTE", "OPINION", "PROMO", "POLITICS", "SPORT", "OTHER")
_EVENT = ("MILITARY", "DIPLOMATIC")


def _judge_question(focus):
    """The multiple-choice question asked about every message. A 3B model
    answers "yes" to almost any yes/no question, but picks sensibly from a
    short list of categories."""
    lines = [
        "Classify the message above. Answer in exactly three lines.",
        "TYPE: one word from this list:",
        "  QUOTE (mostly what someone said, an interview answer, a claim, a reaction)",
        "  OPINION (analysis, commentary, explainer, prediction)",
        "  PROMO (advert, schedule, links, a caption for a video or photo)",
        "  POLITICS (domestic politics, rallies, hecklers, elections, campaigning, protests)",
        "  SPORT",
        "  OTHER (celebrity, trivia, memes, technology, business, anything else)",
        "  MILITARY (a factual report of a strike, attack, casualties, or a troop or "
        "weapons movement)",
        "  DIPLOMATIC (a factual report of talks, a sanction, an evacuation, or an "
        "official order or decision by a government)",
    ]
    if focus:
        lines.append("TOPIC: one letter from this list:")
        lines += [f"  {_TOPIC_LETTERS[k]} ({f})" for k, f in enumerate(focus)]
        lines.append(f"  {_TOPIC_LETTERS[len(focus)]} (none of these)")
    lines.append("LABEL: who did what, where, in at most 10 words.")
    return "\n".join(lines)


def _parse_judgement(raw, n_focus):
    """(kind, on_topic) from the model's three-line answer: kind is "MILITARY"
    or "DIPLOMATIC" for a reported event, else None. Small models
    vary the layout ("TYPE: MILITARY", "Message classification: MILITARY, C, ...")
    so this reads it loosely, and any hint of a non-event category wins."""
    upper = raw.upper()
    words = set(re.findall(r"[A-Z]+", upper))
    kinds = words.intersection(_EVENT)
    kind = None if len(kinds) != 1 or words.intersection(_NOT_EVENT) else kinds.pop()
    if not n_focus:
        return kind, True
    valid = _TOPIC_LETTERS[:n_focus]
    m = re.search(r"TOPIC\W*\b([A-Z])\b", upper)
    if m:
        letter = m.group(1)
    else:    # "MILITARY, C, label..." — the first lone capital letter after the type
        types = "|".join(_EVENT + _NOT_EVENT)
        m = re.search(rf"\b(?:{types})\b\W+([A-Z])\b", upper)
        letter = m.group(1) if m else ""
    return kind, letter in valid


def _judge(text, question, n_focus):
    """Ask the model about one message. Returns (kind, on_topic), or a
    status string ("blocked", "too_long", "error") if it gave no answer."""
    prompt = "Message:\n" + text + "\n\n" + question
    raw, status = _apple_generate(SELECT_SYSTEM, prompt, max_tokens=60)
    if status != "ok":
        return status
    return _parse_judgement(raw, n_focus)


_STOPWORDS = set("""
about after again against also among another because been before being between
both could during each first from have having into just last like made make many
more most much near only other over said says some such than that their them then
there these they this those through under until very want were what when where
which while will with would your breaking urgent update just video watch reports
report according sources source told today tonight overnight morning evening
the and but for new big now not all its his her has had was are
""".split())


def _names(text):
    """Capitalised words (places, people, organisations) in a message's opening."""
    text = re.sub(r"@\w+", "", _for_speech(text)[:400])
    words = re.findall(r"\b[A-Z][A-Za-z'-]{2,}", text)
    return {re.sub(r"'s$", "", w.lower()) for w in words} - _STOPWORDS


def _same_event(a, b):
    """Ask the model whether two messages report the same event. A "SAME" is
    only believed if it holds with the messages swapped too: a 3B model says
    "SAME" now and then for unrelated stories, rarely twice."""
    def ask(first, second):
        prompt = (
            f"Message 1:\n{_truncate(_for_speech(_strip_noise_lines(first)), 400)}\n\n"
            f"Message 2:\n{_truncate(_for_speech(_strip_noise_lines(second)), 400)}\n\n"
            "Do these two messages report the very same incident? Answer SAME only if "
            "they describe one incident: the same action, by the same people, in the "
            "same place. A different town, different people or a different kind of "
            "action means DIFFERENT. Answer with one word: SAME or DIFFERENT."
        )
        raw, status = _apple_generate(SELECT_SYSTEM, prompt, max_tokens=5)
        return status == "ok" and "SAME" in raw.upper() and "DIFFERENT" not in raw.upper()
    return ask(a, b) and ask(b, a)


def _same_story_groups(indices, ordered, kinds):
    """Group messages that report the same event, so a story carried by three
    channels is read once. Losing a story is worse than hearing it twice, so
    this is cautious: two messages are only candidates when they are close in
    time and share a SPECIFIC name (a place, person or organisation that few of
    the night's messages mention: "Sirik" counts, "Israeli" does not, which
    keeps "shells the town of X" templates apart). The model then has to call
    them the same event both ways round, and both must be the same kind
    (a strike never merges with a diplomatic meeting). Each message is compared with the
    FIRST message of each story, never chained through other members."""
    names = {i: _names(ordered[i]["text"]) for i in range(len(ordered))}
    freq = {}
    for ns in names.values():
        for n in ns:
            freq[n] = freq.get(n, 0) + 1
    common = max(3, int(len(ordered) * SAME_STORY_NAME_SHARE))
    specific = {i: {n for n in names[i] if freq[n] <= common} for i in indices}
    window = dt.timedelta(hours=SAME_STORY_HOURS)

    groups = []                         # each: [first message, *others]
    for i in indices:
        for grp in groups:
            first = grp[0]
            ti, tf = ordered[i]["dt"], ordered[first]["dt"]
            if kinds[i] != kinds[first] or (ti and tf and abs(ti - tf) > window):
                continue
            if specific[i] & specific[first] and _same_event(
                    ordered[first]["text"], ordered[i]["text"]):
                grp.append(i)
                break
        else:
            groups.append([i])
    return groups


def _representative(cands):
    """The message to read out for a story group: the longest NON-PROMO one
    (most complete account); if every candidate looks like promo, the first
    rather than rewarding a platform-plug roundup for being long."""
    pool = [it for it in cands if not _looks_promo(it["text"])] or cands[:1]
    return max(pool, key=lambda it: len(it["text"]))


def _pick_groups(ordered, summary_cfg):
    """Select and group the messages worth airing, reading EVERY message.

    Apple's on-device model judges each message on its own (what kind of
    message is it, and is it on one of the focus topics); the keepers are then
    grouped so duplicates collapse. Stories carried by more channels come
    first, then the most recent. Returns groups of indices into `ordered`
    (empty = nothing qualifies), or None to fall back to the rule-based digest."""
    max_groups = int(summary_cfg.get("max_groups", 12))
    focus = summary_cfg.get("focus", [])
    question = _judge_question(focus)
    skipped = {}
    keep, kinds, answered = [], {}, 0
    try:
        for i, it in enumerate(ordered):
            if _looks_promo(it["text"]):
                continue
            text = _truncate(_for_speech(_strip_noise_lines(it["text"])), JUDGE_MSG_CHARS)
            verdict = _judge(text, question, len(focus))
            if isinstance(verdict, str):
                skipped[verdict] = skipped.get(verdict, 0) + 1
                continue
            answered += 1
            kind, on_topic = verdict
            if kind and on_topic:
                keep.append(i)
                kinds[i] = kind
    except AppleModelUnavailable as e:
        print(f"Apple model unavailable ({e}) — falling back to rule-based digest.")
        return None
    if skipped:
        print(f"[Apple model gave no answer for some messages: {skipped}]")
    print(f"[Apple model judged {answered} of {len(ordered)} messages, kept {len(keep)}]")
    if not answered:
        return None
    if not keep:
        return []

    groups = _same_story_groups(keep, ordered, kinds)
    floor = dt.datetime.min.replace(tzinfo=dt.timezone.utc)

    def weight(grp):
        channels = {ordered[i]["channel"] for i in grp}
        latest = max((ordered[i]["dt"] or floor) for i in grp)
        return (len(channels), latest)

    groups.sort(key=weight, reverse=True)
    return groups[:max_groups]


def summarize_local(items, summary_cfg):
    """Synthesis mode: the on-device model writes its own prose brief.

    Apple's model can't read a whole night at once, so the stories are first
    picked exactly as in extractive mode, then the model writes prose from one
    message per story. Returns the spoken brief text, or None to fall back to
    the rule-based digest."""
    floor = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    ordered = sorted(items, key=lambda it: it["dt"] or floor)
    groups = _pick_groups(ordered, summary_cfg)
    if groups is None:
        return None
    if not groups:
        return "Good morning. Nothing of note came in overnight."
    stories = [_representative([ordered[i] for i in grp]) for grp in groups]
    lines = [f"[{it['channel']} {it['time']}] {_story_text(it)}"
             for it in stories]
    # Most important first, so if it's too long for the model drop from the END.
    while lines:
        prompt = (
            "Here are the overnight messages, one per story:\n\n"
            + "\n".join(lines)
            + "\n\n" + SUMMARY_INSTRUCTIONS
        )
        try:
            response, status = _apple_generate(SUMMARY_SYSTEM, prompt,
                                               temperature=0.2, max_tokens=800)
        except AppleModelUnavailable as e:
            print(f"Apple model unavailable ({e}) — falling back to rule-based digest.")
            return None
        if status == "ok":
            return _clean_spoken(response) or None
        if status != "too_long":
            return None
        lines = lines[:-1]
    return None


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
    r"\boriginal msg\b|\bsupport us\b|"
    r"boost the channel|subscribe|follow us|watch here|"
    r"\|\s*(socials|donate|advertising|boost)|@\w+\s*\||"
    r"^\s*@\w+\s*[,:;|—–-]",       # "@channel — slogan" signature lines
    re.IGNORECASE,
)


def _strip_noise_lines(text):
    """Drop footer/plug/metadata lines from a message before it's read aloud.
    If everything matched (it was ALL furniture), keep the original rather
    than return nothing."""
    # Symbols and markdown marks first: a leading emoji or "__bold__" wrapper
    # would otherwise defeat the anchored patterns below (and `_` is a word
    # character, so \W doesn't skip it). Newlines survive both, so the
    # line-by-line rules still work.
    text = _strip_symbols(text)
    text = re.sub(r"[*_`]{2,}", "", text)
    # "Fwd from @channel" is a PREFIX, not always its own line — some channels
    # glue it to the story title, so cut the prefix instead of the whole line.
    text = re.sub(r"^\W*fwd from\s+@?\S*\s*", "", text, flags=re.IGNORECASE)
    kept = [ln for ln in text.splitlines() if not _NOISE_LINE_RE.search(ln)]
    out = "\n".join(kept).strip()
    return out if out else text


def _story_text(it):
    """A message tidied for reading aloud as a story: noise lines and symbols
    removed, and the channel's own name dropped when it signs off with it
    ("... Peskov said. Military Summary")."""
    text = _for_speech(_strip_noise_lines(it["text"]))
    channel = re.escape(str(it["channel"]))
    return re.sub(rf"[\s.,;:|-]*\b{channel}\W*$", "", text, flags=re.IGNORECASE).strip()


def select_relevant(items, summary_cfg):
    """Quote-only mode: the model SELECTS and GROUPS real messages; we then
    read the actual message text. The model writes no prose, so it cannot
    invent facts. Returns spoken text, or None to fall back."""
    floor = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    ordered = sorted(items, key=lambda it: it["dt"] or floor)
    groups = _pick_groups(ordered, summary_cfg)
    if groups is None:                    # model unusable — digest fallback
        return None
    if not groups:
        # An answer of "nothing qualifies" is not a failure. Falling back here
        # would read out the unfiltered digest of exactly the trivia the model
        # just rejected.
        return "Good morning. Nothing of note came in overnight. End of brief."

    # Build the spoken brief from the ACTUAL message text. Nothing here is
    # model-generated.
    spoken = ["Good morning. Here's what came in overnight."]
    for grp in groups:
        rep = _representative([ordered[i] for i in grp])
        line = _story_text(rep)
        if line:
            spoken.append(line.rstrip(".") + ".")
    return "\n".join(spoken) if len(spoken) > 1 else None


# A stretch of Russian: Cyrillic words plus the spaces and punctuation
# between them, so "Новости России, Военные Сводки" stays one phrase.
_CYRILLIC_RUN_RE = re.compile(r"[\u0400-\u04ff](?:[\u0400-\u04ff\s,.;:!?«»\"'()\-]*[\u0400-\u04ff.!?»])?")


def _russian_voice(configured=""):
    """The voice for Russian stretches: the configured one if it's installed,
    else the first installed ru_RU voice (Milena ships with macOS), else ""
    for none. Never a name `say` would reject, which would sink the brief."""
    say_bin = "/usr/bin/say" if Path("/usr/bin/say").exists() else "say"
    try:
        listing = subprocess.run([say_bin, "-v", "?"], capture_output=True, text=True).stdout
    except Exception:
        return ""
    russian = []
    for line in listing.splitlines():
        m = re.match(r"(.+?)\s+([a-z]{2}_[A-Z]{2})\s", line)
        if configured and line.startswith(configured + " "):
            return configured
        if m and m.group(2) == "ru_RU":
            russian.append(m.group(1).strip())
    return russian[0] if russian else ""


def _speech_parts(text, russian_voice):
    """Split the brief into (text, is_russian) parts. With no Russian voice
    installed, Russian is dropped rather than read as gibberish."""
    parts, pos = [], 0
    for m in _CYRILLIC_RUN_RE.finditer(text):
        if m.start() > pos:
            parts.append((text[pos:m.start()], False))
        if russian_voice:
            parts.append((m.group(0), True))
        pos = m.end()
    parts.append((text[pos:], False))
    return [(t, ru) for t, ru in parts if t.strip(" \t,;:")]


def speak(text, path, say_voice="", rate=None, russian_voice=""):
    """Render text to an audio file with the built-in macOS `say` command.
    Russian stretches are read by a Russian voice, the rest by `say_voice`."""
    # Absolute paths: the launchd (scheduled) environment has a minimal PATH, so
    # bare "say"/"afconvert" could fail to resolve at 3:40am. These live in /usr/bin.
    say_bin = "/usr/bin/say" if Path("/usr/bin/say").exists() else "say"
    afconvert_bin = "/usr/bin/afconvert" if Path("/usr/bin/afconvert").exists() else "afconvert"
    russian_voice = _russian_voice(russian_voice)
    parts = _speech_parts(text, russian_voice)

    # Each part is spoken to its own WAV in one fixed format, so they can be
    # joined sample for sample. We do NOT let `say` pick the .m4a codec: it
    # sometimes emits raw LPCM in an .m4a container, which shows a duration but
    # plays SILENT on Telegram mobile. The joined WAV is transcoded to real AAC.
    wavs = []
    joined = Path(str(path) + ".wav")
    try:
        for n, (chunk, is_russian) in enumerate(parts):
            wav = Path(f"{path}.{n}.wav")
            wavs.append(wav)
            voice = russian_voice if is_russian else say_voice
            cmd = [say_bin, "-o", str(wav), "--file-format=WAVE", "--data-format=LEI16@22050"]
            if voice:
                cmd += ["-v", voice]
            if rate:
                cmd += ["-r", str(rate)]
            cmd += ["-f", "-"]                # read the text from stdin (handles long input)
            # Encode to UTF-8 ourselves (input as bytes) so it works even when the
            # terminal locale is ASCII — otherwise curly quotes etc. crash the pipe.
            subprocess.run(cmd, input=chunk.encode("utf-8"), check=True)
        with wave.open(str(joined), "wb") as out:
            for k, wav in enumerate(wavs):
                with wave.open(str(wav), "rb") as w:
                    if k == 0:
                        out.setparams(w.getparams())
                    out.writeframes(w.readframes(w.getnframes()))
        # Transcode to AAC in an m4a container — the format Telegram plays everywhere.
        subprocess.run(
            [afconvert_bin, "-f", "m4af", "-d", "aac", "-b", "64000",
             str(joined), str(path)],
            check=True,
        )
    finally:
        for f in wavs + [joined]:
            f.unlink(missing_ok=True)


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
    ru_voice = _russian_voice(cfg.get("voice", {}).get("russian_voice", ""))
    report(bool(ru_voice), f'Russian voice for Russian text: {ru_voice or "none installed"}',
           "Russian text will be skipped; add one in System Settings > Accessibility > "
           "Spoken Content > System Voice > Manage Voices")

    # --- Apple's on-device model (only if the AI summary is on) ---
    summary_cfg = cfg.get("summary", {})
    if summary_cfg.get("enabled", True):
        mac_ver = platform.mac_ver()[0] or "0"
        report(platform.machine() == "arm64" and int(mac_ver.split(".")[0]) >= 27,
               f"Apple silicon Mac on macOS 27 or later (this one: {platform.machine()}, "
               f"macOS {mac_ver})",
               "the AI summary needs both; below that, set [summary] enabled = false")
        if not APPLE_LLM.exists():
            report(False, "Apple model helper (apple_llm) is built", "run:  ./setup.sh")
        else:
            proc = subprocess.run([str(APPLE_LLM), "--check"], capture_output=True)
            report(proc.returncode == 0, "Apple Intelligence model is available",
                   "turn on Apple Intelligence in System Settings and let it finish "
                   "downloading, or set [summary] enabled = false")
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
            speak(spoken, OUT_AUDIO, voice.get("say_voice", ""), voice.get("rate"),
                  voice.get("russian_voice", ""))
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
