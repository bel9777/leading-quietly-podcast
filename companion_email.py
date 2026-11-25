"""Daily pairing email companion for Leading Quietly.

Generates a crisp, scannable written study companion from the exact episode
transcript, checks it against the hard BLOCK content guard, attaches the
shared mailfmt.py HTML twin, and sends it to the owner's own mailbox via
the local ~/.gmail-mcp sign-in (no email address stored in this public repo).

State is kept in data/emailed.json (gitignored so personal message IDs never
enter git history).

Usage:
    py companion_email.py              # send pairing email for newest unsent episode
    py companion_email.py --day 1      # send for a specific day
    py companion_email.py --verify ID  # read back a sent Gmail message to verify
"""

import base64
import importlib.util
import json
import os
import re
import socket
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

# Force IPv4 process-wide (IPv6 is blackholed on this laptop).
_orig_getaddrinfo = socket.getaddrinfo
socket.getaddrinfo = lambda *a, **kw: [r for r in _orig_getaddrinfo(*a, **kw) if r[0] == socket.AF_INET]

HOME = Path.home()
REPO = Path(__file__).resolve().parent
GMAIL_DIR = HOME / ".gmail-mcp"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me/"
EMAILED_FILE = REPO / "data" / "emailed.json"


def _mailfmt():
    spec = importlib.util.spec_from_file_location("mailfmt", str(HOME / "fleet-watchdog" / "mailfmt.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def gmail_token():
    keys = json.loads((GMAIL_DIR / "gcp-oauth.keys.json").read_text(encoding="utf-8"))
    keys = keys.get("installed") or keys.get("web")
    creds = json.loads((GMAIL_DIR / "credentials.json").read_text(encoding="utf-8"))
    body = urllib.parse.urlencode({
        "client_id": keys["client_id"],
        "client_secret": keys["client_secret"],
        "refresh_token": creds["refresh_token"],
        "grant_type": "refresh_token",
    }).encode("utf-8")
    with urllib.request.urlopen(urllib.request.Request("https://oauth2.googleapis.com/token", data=body), timeout=30) as r:
        return json.loads(r.read())["access_token"]


def gmail_get(tok, path):
    req = urllib.request.Request(GMAIL_API + path, headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def load_emailed():
    if EMAILED_FILE.exists():
        return json.loads(EMAILED_FILE.read_text(encoding="utf-8"))
    return {}


def save_emailed(state):
    EMAILED_FILE.parent.mkdir(parents=True, exist_ok=True)
    EMAILED_FILE.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")


def draft_companion_body(ep, transcript, podcast_mod, scrub_warning=""):
    mins, secs = divmod(ep["durationSeconds"], 60)
    extra = (f"\nIMPORTANT: Previous draft used a restricted word ({scrub_warning}). "
             "Keep it 100% general — never mention any real employer, university, city, colleague, vendor, property, or raw personality score.\n") if scrub_warning else ""
    prompt = f"""Write a crisp, scannable daily companion email matching today's podcast episode of "{podcast_mod.SHOW_TITLE}".
The listener either just heard this episode or is about to listen on the drive. Match the exact insights, phrasing, and homework from the transcript below.
{extra}
Formatting rules (strictly follow this exact layout so the HTML formatter styles it cleanly):
- Line 1: # Day {ep['day']}: {ep['title']}
- Line 2: Leading Quietly | Week {ep['week']} | {ep['track']} | {mins}:{secs:02d} listen
- Blank line, then section heading in ALL CAPS: THE CORE IDEA (2-3 plain sentences)
- Blank line, then ALL CAPS: KEY TAKEAWAYS (3-4 bullet lines starting with "- Bold Lead-In: concise insight", no markdown asterisks needed)
- Blank line, then ALL CAPS: PHRASES TO USE IN THE ROOM (2 exact spoken lines from the episode starting with "- ")
- Blank line, then ALL CAPS: WATCH OUT FOR (1-2 crisp sentences on the trap covered today)
- Blank line, then a single line starting literally with: try this: {ep.get('homework', '')}
- Blank line, then ALL CAPS: LISTEN AND TRANSCRIPT
  - Episode player: {podcast_mod.SITE}/
  - Full transcript: {podcast_mod.SITE}{ep['transcriptPath']}

Rules:
- Keep total length concise (~260-360 words). Zero fluff, zero filler praise, zero corporate buzzwords.
- Plain ASCII/UTF-8 text only, no code fences, no HTML tags, no markdown bold stars around headings.
- Strictly public-safe: no employer, colleague, property, vendor, city, or raw test scores.

TRANSCRIPT:
{transcript}"""
    try:
        resp = podcast_mod.gapi("models/gemini-3.8-flash:generateContent", {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.45, "thinkingConfig": {"thinkingBudget": 0}},
        }, timeout=45)
        body = resp["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        body = podcast_mod.call_text(prompt, temperature=0.45)
    body = re.sub(r"^```.*$", "", body, flags=re.M).strip()
    return body


def _guard_text(text, podcast_mod):
    """Run BLOCK check on everything EXCEPT the show's own public SITE URL."""
    return podcast_mod.check_block(text.replace(podcast_mod.SITE, ""))


def build_email_for_episode(ep, podcast_mod):
    tpath = REPO / "docs" / ep["transcriptPath"].lstrip("/")
    transcript = tpath.read_text(encoding="utf-8") if tpath.exists() else ep["title"]
    subject = f"Leading Quietly — Day {ep['day']}: {ep['title']}"
    scrub = ""
    for _ in range(2):
        body = draft_companion_body(ep, transcript, podcast_mod, scrub_warning=scrub)
        hit = _guard_text(subject + "\n" + body, podcast_mod)
        if not hit:
            return subject, body
        scrub = hit
    raise RuntimeError(f"email content guard blocked on '{scrub}'")


def send_email(subject, text, podcast_mod):
    hit = _guard_text(subject + "\n" + text, podcast_mod)
    if hit:
        raise RuntimeError(f"email content guard blocked on '{hit}'")
    tok = gmail_token()
    me = gmail_get(tok, "profile")["emailAddress"]
    msg = EmailMessage()
    msg["To"], msg["From"], msg["Subject"] = me, me, subject
    msg.set_content(text)
    try:
        _mailfmt().attach(msg, subject, text)
    except Exception:
        pass
    req = urllib.request.Request(
        GMAIL_API + "messages/send",
        data=json.dumps({"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}).encode("utf-8"),
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["id"]


def verify(msg_id):
    tok = gmail_token()
    m = gmail_get(tok, f"messages/{msg_id}?format=full")
    heads = {h["name"].lower(): h["value"] for h in m["payload"]["headers"]}
    own = gmail_get(tok, "profile")["emailAddress"].lower()

    def walk(p):
        yield p
        for c in p.get("parts", []) or []:
            yield from walk(c)

    kinds = [p["mimeType"] for p in walk(m["payload"])]
    html_len = 0
    for p in walk(m["payload"]):
        if p["mimeType"] == "text/html" and p.get("body", {}).get("data"):
            html_len = len(base64.urlsafe_b64decode(p["body"]["data"]).decode("utf-8", "replace"))
    to = [a.strip().lower() for a in heads.get("to", "").split(",") if a.strip()]
    print("subject      :", heads.get("subject"))
    print("only_own     :", len(to) == 1 and own in to[0])
    print("labels       :", ", ".join(m.get("labelIds", [])))
    print("parts        :", ", ".join(kinds))
    print("html_length  :", html_len)


def send_missing(eps, podcast_mod, only_day=None):
    emailed = load_emailed()
    targets = [only_day] if only_day else sorted(d for d in eps if str(d) not in emailed)
    sent_days = []
    for day in targets:
        if day not in eps:
            continue
        subject, body = build_email_for_episode(eps[day], podcast_mod)
        msg_id = send_email(subject, body, podcast_mod)
        emailed[str(day)] = {"day": day, "sentAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "id": msg_id}
        save_emailed(emailed)
        sent_days.append((day, msg_id))
        print(f"  emailed day {day}: id={msg_id}")
    return sent_days


def main():
    if "--verify" in sys.argv:
        verify(sys.argv[sys.argv.index("--verify") + 1])
        return
    import podcast
    eps = podcast.load_state()
    only = int(sys.argv[sys.argv.index("--day") + 1]) if "--day" in sys.argv else None
    send_missing(eps, podcast, only_day=only)


if __name__ == "__main__":
    main()
