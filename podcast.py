"""Leading Quietly — daily two-host leadership coaching podcast.

Self-contained pipeline:
  1. Resolves today's due episode(s) from syllabus.json (Days 1-42) and
     auto-extends Days 43+ into syllabus_extended.json so it runs forever.
  2. Generates a two-host coaching script (Sam=Puck, Claire=Sulafat) via
     Gemini 3.8 Flash.
  3. Runs a hard BLOCK content guard (employer, colleagues, properties,
     vendors, raw Big Five scores) BEFORE TTS and BEFORE push — retries
     once with an explicit scrub instruction, otherwise aborts loudly. The
     term list is private (outside the repo); if it is missing or too short
     the run fails closed.
  4. Renders audio via Gemini 3.8 Flash TTS (with preview fallbacks +
     chunked fallback), encodes 96k MP3 normalized to -16 LUFS, and gates
     on duration, silence ratio, words/sec, LRA, and loudness dips.
  5. Rebuilds docs/feed.xml (episodic, newest first, plus weekly bonus
     compilations) and docs/index.html, verifies all advertised enclosures
     exist on disk, checks local git author identity (GitHub no-reply address
     only), and pushes to GitHub Pages.

Usage:
    py podcast.py              # status only (safe)
    py podcast.py --run        # generate due episode(s), build feed, push
    py podcast.py --run --limit 1
    py podcast.py --run --no-push
"""

import base64
import io
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from html import escape
from pathlib import Path

# Force IPv4 process-wide (IPv6 is blackholed on this laptop).
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_getaddrinfo(*args, **kwargs):
    return [r for r in _orig_getaddrinfo(*args, **kwargs) if r[0] == socket.AF_INET]


socket.getaddrinfo = _ipv4_getaddrinfo
os.environ["GIT_TERMINAL_PROMPT"] = "0"

HOME = Path.home()
REPO = Path(__file__).resolve().parent
START_DATE = date(2026, 11, 25)  # Day 1 = Wed 2026-11-25

SHOW_TITLE = "Leading Quietly"
SHOW_SUBTITLE = "Daily Leadership, My Way"
SHOW_DESC = (
    "A daily two-host leadership coaching podcast for analytical, quiet, "
    "process-driven leaders: speaking early, running the room, leading "
    "specialists, holding your line calmly, and scaling beyond yourself."
)
SITE = "https://bel9777.github.io/leading-quietly-podcast"
AUDIO_BASE = SITE

HOST, EXPERT = "Sam", "Claire"
VOICE_HOST, VOICE_EXPERT = "Puck", "Sulafat"

# Public-feed content guard (BLOCK) is loaded further down from a private file.


def _load_key():
    for name, label in (("gemini-api-key.txt", "primary"),
                        ("gemini-applied-api-key.txt", "fallback")):
        f = HOME / ".ai-keys" / name
        if f.exists():
            return f.read_text(encoding="utf-8").strip(), label
    sys.exit("FATAL: no Gemini key file in ~/.ai-keys")


KEY, KEY_LABEL = _load_key()
GBASE = "https://generativelanguage.googleapis.com/v1beta"
TEXT_MODELS = ["gemini-3.8-flash", "gemini-flash-latest", "gemini-3.7-flash",
               "gemini-3.5-flash", "gemini-3.1-flash-lite"]
TTS_MODELS = ["gemini-3.8-flash-tts", "gemini-2.5-flash-preview-tts",
              "gemini-3.1-flash-tts-preview"]
PER_PART_SPEAKER_MODELS = {"gemini-3.8-flash-tts"}
FALLTHROUGH_CODES = (429, 500, 502, 503, 504)

AUDIO_FILTERS = "dynaudnorm=f=300:g=31:p=0.95,loudnorm=I=-16:TP=-1.5:LRA=7"
MAX_LRA, MIN_I, MAX_I = 8.5, -19.5, -13.5
DIP_LU, MAX_DIP_SECONDS = 6, 3.0
CHUNK_PARTS = 4
FFMPEG = (HOME / r"AppData\Local\Microsoft\WinGet\Packages"
          r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
          r"\ffmpeg-8.1.2-full_build\bin\ffmpeg.exe")

TEXT_TIMEOUT, TTS_TIMEOUT = 60, 600
RUN_CAP_SECONDS = 3 * 3600
MAX_SILENCE_RATIO, MIN_WORDS_PER_SEC = 0.10, 2.0
MIN_SCRIPT_WORDS = 600
MIN_SECONDS, MAX_SECONDS = 200, 900

SYLLABUS_FILE = REPO / "syllabus.json"
EXTENDED_FILE = REPO / "data" / "syllabus_extended.json"
STATE = REPO / "data" / "episodes.json"
AUDIO_DIR = REPO / "docs" / "audio"
TRANS_DIR = REPO / "docs" / "transcripts"
RUN_LOG = REPO / "_RUN-LOG.md"


# ---- Public-feed content guard ---------------------------------------------
# The list of terms that must never reach the public feed is PRIVATE: it lives
# outside this repo (default ~/.ai-keys/podcast-block.txt, override with the
# PODCAST_BLOCK_FILE environment variable). One regex alternative per line,
# '#' comments and blank lines ignored, sections [all] / [leading-quietly] /
# [directors-desk]; this show uses [all] plus its own section.
# Fail closed: a missing, unreadable or too-short list stops the whole program
# before any script, TTS, commit, push or email, with one FAIL line in RUN_LOG.
SHOW_KEY = "leading-quietly"
BLOCK_FILE_DEFAULT = HOME / ".ai-keys" / "podcast-block.txt"
BLOCK_MIN_ALTERNATIVES = 20


class BlockListError(RuntimeError):
    """The private block list cannot be trusted. Message is a short reason
    token only: never a path and never a listed term."""


def load_block(show=SHOW_KEY, path=None):
    """Return the compiled guard regex for `show`, or raise BlockListError."""
    path = Path(path or os.environ.get("PODCAST_BLOCK_FILE") or BLOCK_FILE_DEFAULT)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise BlockListError("file-missing") from None
    except (OSError, UnicodeError):
        raise BlockListError("file-unreadable") from None
    alts, section = [], None
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            if section not in ("all", "leading-quietly", "directors-desk"):
                raise BlockListError("unknown-section")
            continue
        if section is None:
            raise BlockListError("entry-before-section")
        if section in ("all", show) and line not in alts:
            alts.append(line)
    for alt in alts:
        try:
            re.compile(alt, re.I)
        except re.error:
            raise BlockListError("bad-pattern") from None
    if len(alts) < BLOCK_MIN_ALTERNATIVES:
        raise BlockListError(f"too-few-alternatives-{len(alts)}")
    pattern = re.compile("|".join(alts), re.I)
    if pattern.search(""):
        raise BlockListError("pattern-matches-empty")
    return pattern


def block_fail_closed(reason):
    """Append one FAIL line to RUN_LOG (same shape as main()'s line) and exit 1."""
    line = (f"{datetime.now():%Y-%m-%d %H:%M} FAIL made:0 ondisk:n/a ledger:n/a "
            f"missing:n/a key:{KEY_LABEL} NOT-RUN block-list-{reason} "
            f"failed:block-list")
    try:
        with open(RUN_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line)
    sys.exit(1)


try:
    BLOCK = load_block()
except BlockListError as _e:
    block_fail_closed(str(_e))

EXTENSION_TRACKS = [
    "Executive Presence Without Becoming Loud",
    "Leading Former Peers and High-Autonomy Specialists",
    "Calm Negotiation and Holding Boundaries Under Pressure",
    "Managing Up and Pairing With Fast-Moving Executives",
    "Building Scalable Processes and True Delegation",
    "Cross-Functional Influence and Change Management at the Owner's Pace",
    "Decision Quality, Written Briefs, and Running Tight Reviews",
    "Sustainable Energy, Coaching, and Building Team Standards",
]


def gapi(path, payload, timeout=300, attempts=3):
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(
            f"{GBASE}/{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"x-goog-api-key": KEY, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (500, 502, 503, 504) and attempt < attempts:
                wait = 20 * attempt
                print(f"    HTTP {e.code} - retry {attempt}/{attempts - 1} in {wait}s")
                time.sleep(wait)
                continue
            raise


def call_text(prompt, temperature=0.75):
    for i, model in enumerate(TEXT_MODELS):
        try:
            resp = gapi(f"models/{model}:generateContent", {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": temperature},
            }, timeout=TEXT_TIMEOUT)
            return resp["candidates"][0]["content"]["parts"][0]["text"].strip()
        except urllib.error.HTTPError as e:
            if e.code not in (404, 429, 500, 502, 503, 504) or i == len(TEXT_MODELS) - 1:
                raise
            print(f"    {model}: HTTP {e.code}, trying next text model")
        except (TimeoutError, socket.timeout, urllib.error.URLError) as e:
            if i == len(TEXT_MODELS) - 1:
                raise
            print(f"    {model}: {type(e).__name__}, trying next text model")


def load_syllabus():
    base = json.loads(SYLLABUS_FILE.read_text(encoding="utf-8"))
    ext = json.loads(EXTENDED_FILE.read_text(encoding="utf-8")) if EXTENDED_FILE.exists() else []
    by_day = {item["day"]: item for item in base}
    for item in ext:
        by_day[item["day"]] = item
    return by_day


def plan_extension_day(day, existing):
    ep_date = START_DATE + timedelta(days=day - 1)
    wd = ep_date.weekday()  # 0=Mon..6=Sun
    kind_map = {0: "playbook", 1: "playbook", 2: "script_lab",
                3: "research", 4: "failure_mode", 5: "case_study", 6: "drill"}
    kind = kind_map[wd]
    week = (day - 1) // 7 + 1
    track = EXTENSION_TRACKS[(week - 7) % len(EXTENSION_TRACKS)]
    recent_titles = [existing[d]["title"] for d in sorted(existing)[-21:]]
    prompt = f"""Plan Day {day} (Week {week}, {ep_date.strftime('%A')}) of the daily leadership podcast "{SHOW_TITLE}".
Listener profile: quiet, analytical, high-integrity finance/operations leader stepping into senior leadership.
Strengths: builds order into processes, straight-dealing, prepared, steady, listens deeply.
Growth edges: speaking in first 5 minutes, beating pre-room nerves via rehearsal, going first so quiet isn't read as distant, pre-committing limits so agreeableness doesn't give ground, delegating with named backups, pacing process changes with current owners.

Track: {track}
Episode kind: {kind}
Recent titles to NEVER repeat: {json.dumps(recent_titles)}

Return ONLY valid JSON with keys: "title", "brief" (120-180 words of specific, practical, research-grounded teaching content), "homework" (one concrete same-day action).
Never mention any real employer, person's real name, city, property, vendor, or raw personality test score."""
    raw = call_text(prompt, temperature=0.7)
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S).strip()
    obj = json.loads(raw)
    item = {
        "day": day, "week": week, "track": track, "kind": kind,
        "title": obj["title"].strip(),
        "brief": obj["brief"].strip(),
        "homework": obj["homework"].strip(),
    }
    ext = json.loads(EXTENDED_FILE.read_text(encoding="utf-8")) if EXTENDED_FILE.exists() else []
    ext.append(item)
    EXTENDED_FILE.parent.mkdir(parents=True, exist_ok=True)
    EXTENDED_FILE.write_text(json.dumps(ext, indent=2, ensure_ascii=False), encoding="utf-8")
    existing[day] = item
    return item


def episode_shape(item):
    kind = item["kind"]
    day = item["day"]
    intro = f'then "This is {SHOW_TITLE}, Day {day}."'
    if kind == "script_lab":
        return f"""Structure (Script Lab / Role-Play):
- {HOST} opens with the specific awkward or high-stakes moment this solves, {intro}
- {EXPERT} explains in two short turns why quiet, thoughtful leaders get tripped up in this exact moment.
- Run a brief TWO-VERSION live role-play right inside the dialogue: first the common stumble (15-20 seconds), then the calm, prepared version (25-35 seconds). Clearly signal in natural speech when stepping into and out of the role-play (no stage directions or brackets).
- Dissect the exact two phrases that made version two work.
- Give the listener one two-sentence line to say aloud today, then assign the homework cleanly."""
    if kind == "research":
        return f"""Structure (Research & Books Deep Dive):
- {HOST} opens with a common leadership assumption or myth, {intro}
- {EXPERT} walks through the real study or book principle clearly, naming the authors/book and explicitly noting its limits (honest evidence, never hype).
- Connect it to day-to-day leadership for a quiet, analytical manager leading experienced people.
- Quick two-question check: {EXPERT} asks, {HOST} answers in plain words, {EXPERT} sharpens.
- Close with the one concrete homework rep."""
    if kind == "failure_mode":
        return f"""Structure (Failure Mode & Guardrail):
- {HOST} opens by naming the specific blind spot or trap plainly, {intro}
- Unpack why it happens specifically to people who are quiet, candid, and inclined to keep the peace or build systems themselves.
- Walk through what it costs a team if left unchecked.
- Lay out the concrete, mechanical guardrail (calendar habit, written rule, or pre-committed phrase) that removes willpower from the equation.
- Close with the one homework rep to install this guardrail today."""
    if kind == "case_study":
        return f"""Structure (Case Study):
- {HOST} opens with the concrete story or leadership lesson, {intro}
- {EXPERT} walks through what actually happened and the operational choices that mattered (no fluff, no hero worship).
- Translate the lesson into two specific habits an internal leader stepping up into a bigger seat can use this week.
- Close with one sharp question to reflect on and today's homework rep."""
    if kind == "drill":
        return f"""Structure (Weekly Spoken Drill):
- {HOST} opens saying it is weekly review and aloud-rehearsal day, {intro}
- Rapidly recap the week's core thread in two crisp turns.
- Run four realistic rapid-fire leadership or executive questions: {EXPERT} asks the question, pauses naturally ("Take a few seconds to answer that out loud in your own words..."), {HOST} gives a tight 25-35 second grounded answer, and {EXPERT} calls out the one phrase that makes it land.
- Close with today's 60-second solo rehearsal prompt."""
    return f"""Structure (Core Playbook):
- {HOST} opens with a concrete everyday leadership moment this solves, {intro}
- {EXPERT} teaches the core principle clearly and practically — built for a quiet, prepared, fact-driven leader, never fake charisma.
- Walk through how it plays out in a real meeting or one-on-one, step by step.
- Address the subtle trap that makes people fumble it.
- Quick two-question check between {EXPERT} and {HOST}.
- Close with today's concrete homework rep and a crisp sign-off."""


def dialogue_prompt(item, scrub_warning=""):
    extra = f"\nIMPORTANT CORRECTION: Your previous draft accidentally used a restricted term ({scrub_warning}). Keep the entire script 100% general — no employer names, cities, real people's names, properties, vendors, or raw personality scores.\n" if scrub_warning else ""
    return f"""You write scripts for a daily two-host leadership coaching podcast called "{SHOW_TITLE}".
This podcast is crafted for an analytical, quiet, steady, high-integrity leader who builds order into processes, listens first, and is growing into broader executive leadership.

Hosts:
- {HOST}: sharp, grounded operations/finance leader. Asks practical, skeptical questions a real manager asks; translates theory into Monday-morning reality.
- {EXPERT}: direct, warm, research-backed leadership coach. Explains clearly, never preaches, never uses corporate buzzwords.

{episode_shape(item)}
{extra}
Rules:
- This podcast is PUBLIC: NEVER name any real employer, university, hospital, city, county, real colleague, vendor, software product, property, farm, or raw personality test label or score. Speak strictly in generalizable leadership principles and realistic fictional scenarios.
- Sound like two sharp real people talking: natural contractions, varied sentence lengths, crisp pacing. NO filler praise ("great question", "love that", "absolutely", "delve", "unpack", "game-changer").
- Plain spoken text ONLY: no headings, no bullet lists, no stage directions, nothing in brackets or asterisks, no URLs.
- Target 900 to 1,100 words total (about 6 to 7.5 minutes of audio).
- FORMAT: every single line starts with "{HOST}:" or "{EXPERT}:" followed by what they say. Nothing else.

Episode brief:
Day {item['day']} (Week {item['week']} — {item['track']}): {item['title']}
Teaching brief: {item['brief']}
Today's homework rep: {item['homework']}"""


def check_block(text):
    m = BLOCK.search(text)
    return m.group(0) if m else None


def gen_script(item):
    scrub = ""
    for attempt in range(2):
        raw = call_text(dialogue_prompt(item, scrub_warning=scrub), temperature=0.75)
        raw = re.sub(r"^```.*$", "", raw, flags=re.M).strip()
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        good = [ln for ln in lines if re.match(rf"^({HOST}|{EXPERT}):", ln)]
        script = "\n".join(good)
        words = sum(len(ln.split()) for ln in good)
        if len(good) < len(lines) - 2 or words < MIN_SCRIPT_WORDS:
            raise ValueError(f"script failed QA: {len(good)}/{len(lines)} lines, {words} words")
        hit = check_block(item["title"] + "\n" + script)
        if not hit:
            return script
        print(f"    content guard hit '{hit}' on attempt {attempt + 1}; retrying with scrub")
        scrub = hit
    raise RuntimeError(f"content-guard-blocked:{scrub}")


def _split_script(script, parts):
    lines = [ln for ln in script.splitlines() if ln.strip()]
    size = -(-len(lines) // parts)
    return ["\n".join(lines[i:i + size]) for i in range(0, len(lines), size)]


def _tts_parts(script, model):
    if model not in PER_PART_SPEAKER_MODELS:
        return [{"text":
            f"TTS the following podcast conversation between {HOST} and {EXPERT}. "
            f"{HOST} sounds thoughtful, calm, and practical; {EXPERT} sounds warm, "
            "direct, and steady. Natural conversational pacing.\n\n" + script}]
    parts = []
    for ln in script.splitlines():
        m = re.match(rf"^({HOST}|{EXPERT}):\s*(.+)", ln.strip())
        if m:
            parts.append({"text": m.group(2), "speechMetadata": {"speaker": m.group(1)}})
    return parts


def _gen_audio_with(script, tts_model):
    resp = gapi(f"models/{tts_model}:generateContent", {
        "contents": [{"role": "user", "parts": _tts_parts(script, tts_model)}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"multiSpeakerVoiceConfig": {"speakerVoiceConfigs": [
                {"speaker": HOST, "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": VOICE_HOST}}},
                {"speaker": EXPERT, "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": VOICE_EXPERT}}},
            ]}},
        },
    }, timeout=TTS_TIMEOUT)
    pcm, rate = b"", 24000
    for part in resp["candidates"][0]["content"]["parts"]:
        mime = part["inlineData"]["mimeType"]
        data = base64.b64decode(part["inlineData"]["data"])
        if "wav" in mime:
            with wave.open(io.BytesIO(data)) as w:
                rate, data = w.getframerate(), w.readframes(w.getnframes())
        elif "rate=" in mime:
            rate = int(re.search(r"rate=(\d+)", mime).group(1))
        pcm += data
    return pcm, rate


def gen_audio(script):
    last_429 = None
    for model in TTS_MODELS:
        try:
            return (*_gen_audio_with(script, model), model)
        except urllib.error.HTTPError as e:
            if e.code not in FALLTHROUGH_CODES:
                raise
            last_429 = e
            print(f"    {model}: HTTP {e.code}, trying next model")
    for model in TTS_MODELS:
        try:
            parts, rate = [], None
            chunks = _split_script(script, CHUNK_PARTS)
            for i, chunk in enumerate(chunks, 1):
                pcm, rate = _gen_audio_with(chunk, model)
                parts.append(pcm)
                print(f"    {model}: chunk {i}/{len(chunks)} rendered")
                time.sleep(8)
            return b"".join(parts), rate, f"{model}+chunked"
        except urllib.error.HTTPError as e:
            if e.code not in FALLTHROUGH_CODES:
                raise
            last_429 = e
            print(f"    {model}: chunked blocked (HTTP {e.code})")
    raise last_429


def encode_mp3(pcm, rate, out_path):
    tmp = out_path.with_suffix(".tmp.wav")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    subprocess.run(
        [str(FFMPEG), "-y", "-loglevel", "error", "-i", str(tmp),
         "-af", AUDIO_FILTERS, "-codec:a", "libmp3lame", "-b:a", "96k", str(out_path)],
        check=True,
    )
    tmp.unlink()
    return len(pcm) / (rate * 2)


def loudness(path):
    r = subprocess.run(
        [str(FFMPEG), "-hide_banner", "-v", "verbose", "-i", str(path),
         "-af", "ebur128=framelog=verbose", "-f", "null", "-"],
        capture_output=True, text=True, errors="replace",
    )
    tail = r.stderr[r.stderr.rfind("Summary:"):]
    shortterm = [float(v) for v in re.findall(r"\sS:\s*(-?[\d.]+)", r.stderr)][30:]
    speech = sorted(v for v in shortterm if v > -40)
    median = speech[len(speech) // 2] if speech else 0
    dips = sum(1 for v in speech if v < median - DIP_LU) / 10
    return (
        float(re.search(r"I:\s*(-?[\d.]+) LUFS", tail).group(1)),
        float(re.search(r"LRA:\s*([\d.]+) LU", tail).group(1)),
        dips,
    )


def silence_ratio(path):
    r = subprocess.run(
        [str(FFMPEG), "-i", str(path), "-af", "silencedetect=noise=-50dB:d=2", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    quiet = sum(float(m) for m in re.findall(r"silence_duration: ([\d.]+)", r.stderr))
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    if not m:
        return 0.0
    secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return quiet / secs if secs else 0.0


def slugify(title):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", title.lower())).strip("-")[:60]


def load_state():
    if STATE.exists():
        eps = json.loads(STATE.read_text(encoding="utf-8"))
        return {e["day"]: e for e in eps if (REPO / "docs" / e["audioPath"].lstrip("/")).exists()}
    return {}


def save_state(eps):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(
        json.dumps(sorted(eps.values(), key=lambda e: -e["day"]), indent=1, ensure_ascii=False),
        encoding="utf-8",
    )


def build_episode(item, eps):
    day = item["day"]
    try:
        script = gen_script(item)
    except urllib.error.HTTPError:
        raise
    except Exception as e:
        raise RuntimeError(f"script-{e}") from e

    slug = slugify(item["title"])
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    TRANS_DIR.mkdir(parents=True, exist_ok=True)
    tmp_mp3 = AUDIO_DIR / f"day-{day:03d}-{slug}.new.mp3"

    last_err = None
    for render_try in range(2):
        try:
            pcm, rate, tts_model = gen_audio(script)
        except urllib.error.HTTPError:
            raise
        except Exception as e:
            raise RuntimeError(f"tts-{type(e).__name__}") from e
        secs = encode_mp3(pcm, rate, tmp_mp3)
        sil = silence_ratio(tmp_mp3)
        wps = len(script.split()) / secs if secs else 0
        li, lra, dips = loudness(tmp_mp3)
        ok = (
            MIN_SECONDS <= secs <= MAX_SECONDS
            and sil <= MAX_SILENCE_RATIO
            and wps >= MIN_WORDS_PER_SEC
            and lra <= MAX_LRA
            and MIN_I <= li <= MAX_I
            and dips <= MAX_DIP_SECONDS
        )
        if ok:
            break
        tmp_mp3.unlink(missing_ok=True)
        last_err = (f"audio QA failed (try {render_try + 1}): {secs:.0f}s, sil={sil:.0%}, "
                    f"wps={wps:.2f}, I={li:+.1f}, LRA={lra:.1f}, dips={dips:.1f}s")
        print(f"    {last_err}")
        if render_try == 0:
            time.sleep(8)
    else:
        raise ValueError(last_err)

    size = tmp_mp3.stat().st_size
    final = AUDIO_DIR / f"day-{day:03d}-{slug}-{size}.mp3"
    tmp_mp3.rename(final)
    (TRANS_DIR / f"day-{day:03d}-{size}.txt").write_text(script, encoding="utf-8")
    pub_dt = datetime.combine(START_DATE + timedelta(days=day - 1), datetime.min.time()) + timedelta(hours=11, minutes=45)
    eps[day] = {
        "day": day,
        "week": item["week"],
        "track": item["track"],
        "kind": item["kind"],
        "title": item["title"],
        "homework": item["homework"],
        "format": "two-host",
        "guid": f"leading-quietly-day-{day:03d}",
        "publishedAt": pub_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "audioPath": f"/audio/{final.name}",
        "transcriptPath": f"/transcripts/day-{day:03d}-{size}.txt",
        "audioBytes": size,
        "durationSeconds": int(secs),
        "ttsModel": tts_model,
        "metrics": {"words": len(script.split()), "lra": lra, "lufs": li,
                    "dipSeconds": dips, "silenceRatio": round(sil, 4)},
    }
    return secs


def rfc822(iso):
    return format_datetime(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc))


def _weekly_items():
    f = REPO / "data" / "weekly.json"
    out = []
    for w in json.loads(f.read_text(encoding="utf-8")) if f.exists() else []:
        when = (datetime.fromisoformat(w["publishedAt"]) + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
        hrs, rem = divmod(w["durationSeconds"], 3600)
        chap = "".join(f"{c['start'] // 60}:{c['start'] % 60:02d} {escape(c['title'])}&lt;br/&gt;" for c in w["chapters"])
        out.append((when, f"""    <item>
      <title>Full {escape(w['title'])}</title>
      <description>The whole week as one continuous listen, one chapter per day:&lt;br/&gt;{chap}</description>
      <guid isPermaLink="false">leading-quietly-weekly-{w['week']}-{w['key']}</guid>
      <pubDate>{rfc822(when)}</pubDate>
      <itunes:episodeType>bonus</itunes:episodeType>
      <enclosure url="{w['url']}" length="{w['bytes']}" type="audio/mpeg"/>
      <itunes:duration>{hrs}:{rem // 60:02d}:{rem % 60:02d}</itunes:duration>
    </item>"""))
    return out


def build_feed(eps):
    items = []
    for e in sorted(eps.values(), key=lambda x: -x["day"]):
        mins, secs = divmod(e["durationSeconds"], 60)
        desc = f"{e['track']} — {e['title']}. Today's rep: {e.get('homework', '')}"
        items.append((e["publishedAt"], f"""    <item>
      <title>Day {e['day']}: {escape(e['title'])}</title>
      <description>{escape(desc)}</description>
      <guid isPermaLink="false">{escape(e['guid'])}</guid>
      <pubDate>{rfc822(e['publishedAt'])}</pubDate>
      <itunes:episode>{e['day']}</itunes:episode>
      <enclosure url="{AUDIO_BASE}{e['audioPath']}" length="{e['audioBytes']}" type="audio/mpeg"/>
      <itunes:duration>{mins}:{secs:02d}</itunes:duration>
    </item>"""))
    items = [xml for _, xml in sorted(items + _weekly_items(), key=lambda t: t[0], reverse=True)]
    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{escape(SHOW_TITLE)}</title>
    <link>{SITE}/</link>
    <language>en-us</language>
    <description>{escape(SHOW_DESC)}</description>
    <itunes:author>{escape(SHOW_TITLE)}</itunes:author>
    <itunes:type>episodic</itunes:type>
    <itunes:image href="{SITE}/cover.png"/>
    <atom:link href="{SITE}/feed.xml" rel="self" type="application/rss+xml"/>
{chr(10).join(items)}
  </channel>
</rss>
"""
    (REPO / "docs").mkdir(exist_ok=True)
    (REPO / "docs" / "feed.xml").write_text(feed, encoding="utf-8")


def build_index(eps):
    rows = []
    for e in sorted(eps.values(), key=lambda x: -x["day"]):
        mins, secs = divmod(e["durationSeconds"], 60)
        rows.append(
            f'<li><strong>Day {e["day"]}: {escape(e["title"])}</strong> '
            f'({mins}:{secs:02d} · {escape(e["track"])}) '
            f'<audio controls preload="none" src="{e["audioPath"].lstrip("/")}"></audio> '
            f'<a href="{e["transcriptPath"].lstrip("/")}">transcript</a></li>'
        )
    html = (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"/>'
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>'
        f'<title>{escape(SHOW_TITLE)}</title>'
        '<style>body{font-family:Georgia,serif;max-width:46rem;margin:2rem auto;'
        'padding:0 1rem;line-height:1.55;color:#1b2631}li{margin:1em 0}audio{width:100%;'
        'max-width:26rem;display:block;margin:.35em 0}</style></head><body>'
        f'<h1>{escape(SHOW_TITLE)}</h1>'
        f'<p>{escape(SHOW_DESC)}</p>'
        f'<p>Subscribe in Apple Podcasts by URL: <code>{SITE}/feed.xml</code></p>'
        '<ul>' + "".join(rows) + '</ul></body></html>'
    )
    (REPO / "docs" / "index.html").write_text(html, encoding="utf-8")


def git_sync():
    r = subprocess.run(["git", "-C", str(REPO), "pull", "--rebase", "--autostash"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"WARNING: pre-run git pull failed: {r.stderr.strip()[:200]}")
    return r.returncode == 0


def feed_enclosures_on_disk():
    missing = []
    f = REPO / "docs" / "feed.xml"
    if not f.exists():
        return missing
    try:
        root = ET.fromstring(f.read_text(encoding="utf-8"))
    except ET.ParseError as e:
        return [f"feed.xml: UNPARSEABLE ({e})"]
    for enc in root.findall(".//enclosure"):
        url = enc.get("url") or ""
        if not url.startswith(SITE):
            continue
        rel = url[len(SITE):].lstrip("/")
        if not (REPO / "docs" / rel).exists():
            missing.append(f"feed.xml->{rel}")
    return missing


def check_git_identity():
    email = subprocess.run(["git", "-C", str(REPO), "config", "user.email"],
                           capture_output=True, text=True).stdout.strip().lower()
    if not email.endswith("@users.noreply.github.com"):
        raise RuntimeError("refusing to commit/push: git user.email is not a GitHub no-reply address")


def publish(msg):
    check_git_identity()
    subprocess.run(["git", "-C", str(REPO), "add", "-A"], check=True)
    r = subprocess.run(["git", "-C", str(REPO), "diff", "--cached", "--quiet"])
    committed = r.returncode != 0
    if committed:
        subprocess.run(["git", "-C", str(REPO), "commit", "-m", msg, "--quiet"], check=True)
    ahead = subprocess.run(["git", "-C", str(REPO), "rev-list", "--count", "@{u}..HEAD"],
                           capture_output=True, text=True)
    if not committed and ahead.stdout.strip() == "0":
        return False
    subprocess.run(["git", "-C", str(REPO), "pull", "--rebase", "--quiet"], check=True)
    missing = feed_enclosures_on_disk()
    if missing:
        raise RuntimeError(f"refusing to push: {len(missing)} advertised enclosure(s) missing: {missing[:3]}")
    subprocess.run(["git", "-C", str(REPO), "push", "--quiet"], check=True, timeout=300)
    return True


def main():
    run = "--run" in sys.argv
    no_push = "--no-push" in sys.argv
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None
    started = datetime.now()

    if run and not no_push:
        git_sync()

    today = date.today()
    due_day = max(1, (today - START_DATE).days + 1)
    syllabus = load_syllabus()
    eps = load_state()
    missing = [d for d in range(1, due_day + 1) if d not in eps]
    print(f"show: {SHOW_TITLE} | due: 1-{due_day} | done: {len(eps)} | missing: {missing or 'none'} | key:{KEY_LABEL}")
    if not run:
        return

    queue = [missing[-1]] + missing[:-1] if missing else []
    made, fails, stopped = [], [], ""
    for day in (queue[:limit] if limit else queue):
        if (datetime.now() - started).total_seconds() > RUN_CAP_SECONDS:
            stopped = f"run-cap at day {day}"
            break
        try:
            item = syllabus.get(day) or plan_extension_day(day, syllabus)
            secs = build_episode(item, eps)
            save_state(eps)
            made.append(day)
            print(f"  day {day}: OK ({secs:.0f}s) - {item['title']}")
            time.sleep(10)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:250]
            if e.code == 429:
                stopped = f"quota-429 at day {day}"
                print(f"  day {day}: {stopped}")
                break
            print(f"  day {day}: HTTP {e.code} {body}")
            if e.code in (401, 403):
                stopped = f"HTTP-{e.code} at day {day}"
                break
            fails.append(f"{day}:HTTP{e.code}")
        except Exception as e:
            fails.append(f"{day}:{e.args[0] if isinstance(e, RuntimeError) and e.args else type(e).__name__}")
            print(f"  day {day}: FAILED {e!r}")

    build_feed(eps)
    build_index(eps)

    weekly_note = ""
    if eps:
        try:
            import weekly
            n = weekly.update(eps)
            weekly_note = f" weekly:+{n}" if n else ""
            if n:
                build_feed(eps)
        except Exception as e:
            weekly_note = f" weekly-FAILED:{type(e).__name__}"
            fails.append(f"weekly:{type(e).__name__}")
            print(f"weekly failed: {e!r}")

    if no_push:
        pushed = "no-push(flag)"
    else:
        label = f"day(s) {', '.join(map(str, made))}" if made else "feed refresh"
        try:
            pushed = "PUSHED" if publish(f"Episodes: {label}") else "no-push"
        except Exception as e:
            pushed = f"PUSH-FAILED:{type(e).__name__}"
            print(f"publish failed: {e!r}")

    # Pairing daily companion email (runs after push so web/transcript links exist).
    email_note = ""
    if eps and not no_push and not pushed.startswith("PUSH-FAILED"):
        try:
            import companion_email
            sent = companion_email.send_missing(eps, sys.modules[__name__])
            email_note = f" emailed:+{len(sent)}" if sent else ""
        except Exception as e:
            email_note = f" email-FAILED:{type(e).__name__}"
            fails.append(f"email:{type(e).__name__}")
            print(f"companion email failed: {e!r}")

    ondisk = len(load_state())
    if pushed.startswith("PUSH-FAILED") or ondisk != len(eps):
        status = "FAIL"
    elif fails or (made and pushed == "no-push"):
        status = "WARN"
    else:
        status = "OK"
    line = (
        f"{datetime.now():%Y-%m-%d %H:%M} {status} made:{len(made)} "
        f"ondisk:{ondisk} ledger:{len(eps)} missing:{len([d for d in range(1, due_day + 1) if d not in eps])} "
        f"key:{KEY_LABEL} {pushed}{weekly_note}{email_note}"
        + (f" failed:{','.join(fails)}" if fails else "")
        + (f" stopped:{stopped}" if stopped else "")
    )
    with open(RUN_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


if __name__ == "__main__":
    main()
