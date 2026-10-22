"""P2 test support: fixture loading, fake CLI binary, fake API runner.

Nothing here talks to a real OpenCode binary or server — every test is
offline. Recorded event shapes are represented by redacted, pseudonymized public
fixtures, with manifest checksums under tests/golden/. Original traces stay private.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

# `unittest discover -s tests` imports test modules as top-level names and
# does NOT execute tests/__init__.py, so the src bootstrap lives here too.
_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

GOLDEN = Path(__file__).resolve().parent / "golden"
RAW = GOLDEN / "raw"
SYNTH = GOLDEN / "synthetic"
NORM = GOLDEN / "normalized"

FAKE_CLI_SRC = '''#!/usr/bin/env python3
# Fake CLI for capture tests (SYNTHETIC). Emits scripted bytes and exits
# with a scripted code; ignores all argv. Env-controlled so the test can
# simulate chunked writes, delays, big lines and nonzero exits.
import os, sys, time
def _emit(stream, var, chunk_env, delay_env):
    path = os.environ.get(var)
    if not path:
        return
    data = open(path, "rb").read()
    n = int(os.environ.get(chunk_env, "0"))
    delay = float(os.environ.get(delay_env, "0"))
    delay_chunks = int(os.environ.get("HINA_FAKE_DELAY_CHUNKS", "-1"))
    if n and n < len(data):
        for i in range(0, len(data), n):
            stream.write(data[i:i + n])
            stream.flush()
            if delay and (delay_chunks < 0 or i // n < delay_chunks):
                time.sleep(delay)
    else:
        stream.write(data)
    stream.flush()
_emit(sys.stdout.buffer, "HINA_FAKE_STDOUT", "HINA_FAKE_CHUNK_OUT",
      "HINA_FAKE_DELAY_OUT")
_emit(sys.stderr.buffer, "HINA_FAKE_STDERR", "HINA_FAKE_CHUNK_ERR",
      "HINA_FAKE_DELAY_ERR")
if os.environ.get("HINA_FAKE_SLEEP"):
    time.sleep(float(os.environ["HINA_FAKE_SLEEP"]))
sys.exit(int(os.environ.get("HINA_FAKE_EXIT", "0")))
'''


def write_fake_cli(tmpdir: str) -> str:
    """Executable fake CLI (python3 shebang) in tmpdir; returns its path."""
    path = os.path.join(tmpdir, "fake_opencode")
    src = f"#!{sys.executable}\n" + FAKE_CLI_SRC
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return path


def read_raw(name: str) -> bytes:
    return (RAW / name).read_bytes()


def read_synth(name: str) -> bytes:
    return (SYNTH / name).read_bytes()


def synth_env(base: dict, tmpdir: str, stdout_bytes: bytes,
              exit_code: int = 0, **extra) -> dict:
    """Point the fake CLI at scripted stdout bytes via a temp file."""
    path = os.path.join(tmpdir, "scripted-stdout.jsonl")
    with open(path, "wb") as f:
        f.write(stdout_bytes)
    env = dict(base)
    env["HINA_FAKE_STDOUT"] = path
    env["HINA_FAKE_EXIT"] = str(exit_code)
    env.update({k: str(v) for k, v in extra.items()})
    return env


class FakeApiRunner:
    """Scripted SessionApiTransport process_runner.

    Responses keyed by (METHOD, path). Each response is either a dict
    (JSON body, exit 0) or {"exit_code": n, "body": ...}. Records every
    call (argv, env) so tests can assert on call counts and that no
    password ever appears in argv.
    """

    def __init__(self, responses: dict[tuple[str, str], dict]):
        self.responses = responses
        self.calls: list[dict] = []

    @staticmethod
    def _method_path(argv) -> tuple[str, str]:
        # argv = [binary, "api", "--server", url, METHOD, path, (-d json)?]
        i = argv.index("--server")
        return argv[i + 2], argv[i + 3]

    def __call__(self, argv, env) -> tuple[int, str, str]:
        self.calls.append({"argv": list(argv), "env": dict(env)})
        method, path = self._method_path(argv)
        resp = self.responses.get((method, path))
        if resp is None:
            return 1, "", f"no scripted response for {method} {path}"
        body = resp.get("body")
        out = json.dumps(body, ensure_ascii=False) if body is not None else ""
        return resp.get("exit_code", 0), out, resp.get("stderr", "")

    def calls_for(self, method: str, path: str) -> int:
        return sum(1 for c in self.calls
                   if self._method_path(c["argv"]) == (method, path))


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def golden_fixture_test(test: unittest.TestCase) -> None:
    """Guard: manifest hashes must match the files on disk (protects the
    provenance chain from silent edits)."""
    manifest = json.loads((GOLDEN / "golden_manifest.json").read_text())
    for entry in manifest["fixtures"]:
        p = GOLDEN / entry["path"]
        test.assertTrue(p.exists(), f"missing fixture {entry['path']}")
        test.assertEqual(sha256_bytes(p.read_bytes()), entry["sha256"],
                         f"fixture hash drift: {entry['path']}")
