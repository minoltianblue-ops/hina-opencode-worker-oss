#!/usr/bin/env python3
"""T-A3-1 live entry guards — offline only (the real binary and any
model call are never exercised here; the binary probe sits behind every
guard)."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE_ENTRY = ROOT / "experiments" / "m4" / "a3" / "live_entry.py"


def _run(args, env_extra=None, timeout=30):
    env = dict(os.environ)
    env.pop("M4_A3_LIVE", None)
    env.update(env_extra or {})
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + \
        env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, str(LIVE_ENTRY)] + args,
                          capture_output=True, env=env, timeout=timeout)


class LiveEntryGuards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_refused_without_manual_env(self):
        p = _run(["--root", self.tmp.name,
                  "--server-url", "http://127.0.0.1:4096"])
        self.assertEqual(p.returncode, 2, p.stderr.decode())
        self.assertIn(b"M4_A3_LIVE", p.stderr)

    def test_refused_non_loopback_server(self):
        p = _run(["--root", self.tmp.name,
                  "--server-url", "http://10.1.2.3:4096"],
                 {"M4_A3_LIVE": "1"})
        self.assertEqual(p.returncode, 3, p.stderr.decode())
        self.assertIn(b"loopback", p.stderr)

    def test_refused_https_or_tunnel_style_url(self):
        # an https/remote URL is not the documented local backend hop
        for url in ("https://127.0.0.1:4096", "http://evil.example:443",
                    "http://127.0.0.1.evil.example:80"):
            p = _run(["--root", self.tmp.name, "--server-url", url],
                     {"M4_A3_LIVE": "1"})
            self.assertEqual(p.returncode, 3, url)
            self.assertIn(b"loopback", p.stderr)

    def test_refused_bad_model_pattern(self):
        p = _run(["--root", self.tmp.name,
                  "--server-url", "http://127.0.0.1:4096",
                  "--model", "glm free without slash"],
                 {"M4_A3_LIVE": "1"})
        self.assertEqual(p.returncode, 3, p.stderr.decode())
        self.assertIn(b"pattern", p.stderr)


if __name__ == "__main__":
    unittest.main()
