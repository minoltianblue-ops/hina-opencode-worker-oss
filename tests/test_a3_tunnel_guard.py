"""Offline guard tests; fake binary/key helper, no provider or worker calls."""
import contextlib
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

FILE = Path(__file__).resolve().parents[1] / 'experiments/m4/a3/tunnel_guard.py'
spec = importlib.util.spec_from_file_location('a3_guard_test_module', FILE)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class TunnelGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.binary = self.root / 'fake-tunnel'
        self.helper = self.root / 'fake-keychain'
        self.profile = self.root / 'profile.json'
        self.profile.write_text('{}')
        self.executable(self.helper, 'print("sk-" + "X" * 50)\n')
        self.executable(self.binary, 'raise SystemExit(0)\n')

    def executable(self, path, source):
        path.write_text('#!' + sys.executable + '\n' + source)
        path.chmod(0o700)

    def manifest(self):
        return dict(root=str(self.root), binary=str(self.binary),
                    profile=str(self.profile), keychain_helper=str(self.helper),
                    keychain_service='test-only', pins={str(p): guard.sha256(p)
                    for p in (self.binary, self.helper, self.profile)})

    def test_unrelated_tunnel_environment_is_not_inherited(self):
        env = guard.clean_environment({'HOME':'/tmp/home', 'PATH':'/bin',
             'CONTROL_PLANE_TUNNEL_ID':'wrong', 'MCP_SERVER_URL':'wrong',
             'OPENAI_API_KEY':'secret', 'HEALTH_LISTEN_ADDR':'0.0.0.0:8080',
             'LOG_HTTP_RAW_UNSAFE':'true', 'HTTPS_PROXY':'wrong'})
        self.assertEqual(env, {'HOME':'/tmp/home','PATH':'/bin'})

    def test_changed_profile_is_refused(self):
        manifest = self.manifest()
        self.profile.write_text('{"changed":true}')
        with self.assertRaises(guard.Refused):guard.verify_spec(manifest)

    def test_changed_binary_is_refused(self):
        manifest = self.manifest()
        self.binary.write_text('changed')
        with self.assertRaises(guard.Refused):guard.verify_spec(manifest)

    def test_required_pins_cannot_be_omitted(self):
        manifest = self.manifest();manifest['pins'].pop(str(self.helper))
        with self.assertRaises(guard.Refused):guard.verify_spec(manifest)

    def test_keychain_errors_never_expose_helper_output(self):
        self.executable(self.helper, 'print("SENSITIVE_DO_NOT_LOG");raise SystemExit(1)')
        with self.assertRaises(guard.Refused) as caught:
            guard.child_environment(self.manifest())
        self.assertNotIn('SENSITIVE',str(caught.exception))

    def test_bounded_restart_even_after_zero_exit(self):
        stop = threading.Event()
        with contextlib.redirect_stderr(io.StringIO()):
            code = guard.supervise(self.manifest(), stop, preflight=False,
                                    initial_delay=.01, max_restarts=2)
        state=json.loads((self.root/'run/a3-tunnel-guard.json').read_text())
        self.assertEqual((code,state['state'],state['starts']),(70,'GAVE_UP',3))
        self.assertEqual((self.root/'logs/a3-tunnel-client.log').stat().st_mode & 0o777,0o600)

    def test_duplicate_guard_is_refused(self):
        (self.root/'run').mkdir()
        with (self.root/'run/a3-tunnel-guard.lock').open('w') as locked:
            fcntl.flock(locked,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(guard.Refused):
                guard.supervise(self.manifest(),threading.Event(),preflight=False)

    def test_operator_stop_does_not_restart_or_leave_child(self):
        self.executable(self.binary, 'import time\ntime.sleep(30)\n')
        stop=threading.Event();pids=[]
        def stop_when_running():
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                try:
                    state=json.loads((self.root/'run/a3-tunnel-guard.json').read_text())
                    pids.append(state['child_pid']);break
                except (OSError,KeyError,ValueError):time.sleep(.01)
            stop.set()
        thread=threading.Thread(target=stop_when_running);thread.start()
        with contextlib.redirect_stderr(io.StringIO()):
            code=guard.supervise(self.manifest(),stop,preflight=False)
        thread.join(4)
        state=json.loads((self.root/'run/a3-tunnel-guard.json').read_text())
        self.assertEqual((code,state['state'],state['starts']),(0,'STOPPED',1))
        self.assertEqual(len(pids),1)
        with self.assertRaises(ProcessLookupError):os.kill(pids[0],0)

    def test_preflight_refuses_occupied_health_port(self):
        with mock.patch.object(guard,'port_open',return_value=True):
            with self.assertRaises(guard.Refused):
                guard.supervise(self.manifest(),threading.Event())
        self.assertFalse((self.root/'logs/a3-tunnel-client.log').exists())

    def test_preflight_refuses_missing_endpoint(self):
        with mock.patch.object(guard,'port_open',return_value=False):
            with self.assertRaises(guard.Refused):
                guard.supervise(self.manifest(),threading.Event())


if __name__=='__main__':unittest.main()
