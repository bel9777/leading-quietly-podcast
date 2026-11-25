"""Weekly chaptered MP3 compilations for Leading Quietly.

Stitches every completed 7-day week (Days 1-7, 8-14, ...) into a single
chaptered MP3 under docs/weekly/ and records metadata in data/weekly.json
so podcast.py adds it as a bonus "Full Week N" item in feed.xml.
"""
import hashlib
import json
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent
HOME = Path.home()
FFMPEG = (HOME / r"AppData\Local\Microsoft\WinGet\Packages"
          r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
          r"\ffmpeg-8.1.2-full_build\bin\ffmpeg.exe")
SITE = "https://bel9777.github.io/leading-quietly-podcast"
WEEKLY_DIR = REPO / "docs" / "weekly"
WEEKLY_JSON = REPO / "data" / "weekly.json"


def _meta_escape(s):
    return s.replace("\\", "\\\\").replace("=", "\\=").replace(";", "\\;").replace("#", "\\#").replace("\n", " ")


def update(eps):
    existing = json.loads(WEEKLY_JSON.read_text(encoding="utf-8")) if WEEKLY_JSON.exists() else []
    by_week = {w["week"]: w for w in existing}
    weeks = {}
    for e in eps.values():
        w = (e["day"] - 1) // 7 + 1
        weeks.setdefault(w, []).append(e)

    built = 0
    for w, items in sorted(weeks.items()):
        if len(items) < 7:
            continue
        items = sorted(items, key=lambda x: x["day"])
        key = hashlib.sha256("|".join(f"{e['day']}:{e['audioBytes']}" for e in items).encode()).hexdigest()[:10]
        if w in by_week and by_week[w]["key"] == key and (WEEKLY_DIR / Path(by_week[w]["url"]).name).exists():
            continue

        WEEKLY_DIR.mkdir(parents=True, exist_ok=True)
        concat_txt = WEEKLY_DIR / f"_concat_w{w:02d}.txt"
        meta_txt = WEEKLY_DIR / f"_meta_w{w:02d}.txt"
        tmp_mp3 = WEEKLY_DIR / f"week-{w:02d}.new.mp3"

        concat_lines, meta_lines, chapters = [], [";FFMETADATA1", f"title=Leading Quietly — Week {w}"], []
        cursor = 0
        for e in items:
            p = REPO / "docs" / e["audioPath"].lstrip("/")
            concat_lines.append(f"file '{p.as_posix()}'")
            dur = int(e["durationSeconds"])
            title = f"Day {e['day']}: {e['title']}"
            chapters.append({"start": cursor, "title": title})
            meta_lines += [
                "[CHAPTER]", "TIMEBASE=1/1",
                f"START={cursor}", f"END={cursor + dur}",
                f"title={_meta_escape(title)}",
            ]
            cursor += dur

        concat_txt.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")
        meta_txt.write_text("\n".join(meta_lines) + "\n", encoding="utf-8")
        subprocess.run(
            [str(FFMPEG), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", str(concat_txt), "-i", str(meta_txt), "-map_metadata", "1",
             "-c:a", "copy", "-id3v2_version", "3", str(tmp_mp3)],
            check=True,
        )
        concat_txt.unlink(missing_ok=True)
        meta_txt.unlink(missing_ok=True)
        size = tmp_mp3.stat().st_size
        final = WEEKLY_DIR / f"week-{w:02d}-{key}-{size}.mp3"
        tmp_mp3.rename(final)

        by_week[w] = {
            "week": w,
            "key": key,
            "title": f"Week {w}: {items[0]['track']} (Days {items[0]['day']}-{items[-1]['day']})",
            "publishedAt": items[-1]["publishedAt"],
            "url": f"{SITE}/weekly/{final.name}",
            "bytes": size,
            "durationSeconds": cursor,
            "chapters": chapters,
        }
        built += 1

    if built:
        WEEKLY_JSON.parent.mkdir(parents=True, exist_ok=True)
        WEEKLY_JSON.write_text(
            json.dumps(sorted(by_week.values(), key=lambda x: -x["week"]), indent=1, ensure_ascii=False),
            encoding="utf-8",
        )
    return built
