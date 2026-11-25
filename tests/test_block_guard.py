"""Tests for the private public-feed content guard (no network, no API calls).

No listed term is typed in this file: the "dirty" sample is built at run time
from the private block list. If that list cannot be read, the test FAILS.
"""

import contextlib
import io
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import podcast  # noqa: E402  (may read API key files at import; makes no API call)


def _private_terms():
    """Alternatives for this show from the private list, or None if unreadable."""
    try:
        text = podcast.BLOCK_FILE_DEFAULT.read_text(encoding="utf-8")
    except OSError:
        return None
    out, section = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
        elif section in ("all", podcast.SHOW_KEY):
            out.append(line)
    return out


def _plain_term():
    """First alternative that is plain letters/digits/spaces (usable as text)."""
    terms = _private_terms()
    if not terms:
        return None
    for t in terms:
        if re.fullmatch(r"[A-Za-z0-9 ]{4,}", t):
            return t
    return None


def _script(extra=""):
    filler = "calm clear steady plan today before the next meeting we write it down first"
    lines = []
    for i in range(45):
        who = podcast.HOST if i % 2 == 0 else podcast.EXPERT
        lines.append(f"{who}: {filler} number {i}. {extra if i == 20 else ''}".strip())
    return "\n".join(lines)


def _item():
    return {"day": 1, "week": 1, "track": "Test Track", "kind": "playbook",
            "title": "A steady plan", "brief": "Write the plan down.",
            "homework": "Write one line.", "takeaway": "Write one line."}


class BlockGuardTests(unittest.TestCase):
    def test_a_listed_term_is_refused_before_tts(self):
        term = _plain_term()
        if term is None:
            self.fail("private block list unreadable; cannot build the dirty sample")
        dirty = _script(f"We also talked about {term} for a moment.")
        self.assertIsNotNone(podcast.check_block(dirty))
        with mock.patch.object(podcast, "call_text", return_value=dirty) as ct, \
                mock.patch.object(podcast, "gen_audio") as tts, \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError) as cm:
                podcast.gen_script(_item())
            self.assertIn("content-guard-blocked", str(cm.exception))
            self.assertEqual(ct.call_count, 2)  # first try + one scrub retry
            with self.assertRaises(RuntimeError):
                podcast.build_episode(_item(), {})
            tts.assert_not_called()

    def test_b_clean_script_passes(self):
        clean = _script()
        self.assertIsNone(podcast.check_block(clean))
        with mock.patch.object(podcast, "call_text", return_value=clean), \
                contextlib.redirect_stdout(io.StringIO()):
            out = podcast.gen_script(_item())
        self.assertEqual(out.splitlines()[0], clean.splitlines()[0])

    def test_c_loader_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            missing = Path(d) / "nope.txt"
            empty = Path(d) / "empty.txt"
            empty.write_text("", encoding="utf-8")
            short = Path(d) / "short.txt"
            short.write_text("[all]\n" + "\n".join(f"zz{i}x" for i in range(19)) + "\n",
                             encoding="utf-8")
            enough = Path(d) / "enough.txt"
            enough.write_text("[all]\n" + "\n".join(f"zz{i}x" for i in range(20)) + "\n",
                              encoding="utf-8")
            for bad in (missing, empty, short):
                with self.subTest(file=bad.name):
                    with mock.patch.dict(os.environ, {"PODCAST_BLOCK_FILE": str(bad)}):
                        with self.assertRaises(podcast.BlockListError):
                            podcast.load_block()
                        # the whole-program stop: one FAIL line, non-zero exit
                        log = Path(d) / f"log-{bad.name}.md"
                        with mock.patch.object(podcast, "RUN_LOG", log), \
                                contextlib.redirect_stdout(io.StringIO()):
                            with self.assertRaises(SystemExit) as cm:
                                try:
                                    podcast.load_block()
                                except podcast.BlockListError as e:
                                    podcast.block_fail_closed(str(e))
                        self.assertNotEqual(cm.exception.code, 0)
                        lines = log.read_text(encoding="utf-8").splitlines()
                        self.assertEqual(len(lines), 1)
                        self.assertRegex(lines[0], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2} FAIL ")
            with mock.patch.dict(os.environ, {"PODCAST_BLOCK_FILE": str(enough)}):
                self.assertTrue(podcast.load_block().search("ZZ3X"))


if __name__ == "__main__":
    unittest.main()
