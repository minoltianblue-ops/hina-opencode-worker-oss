#!/usr/bin/env python3
"""Read-only local A3 transport probe. No model call or worker_start.

Checks the *actual dialer* with persistent stdin, not just the TCP endpoint.
A local PASS is deliberately not proof of a ChatGPT/cloud round trip.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time
import urllib.error
import urllib.request

REQUIRED_TOOLS = {'worker_start', 'worker_status', 'worker_wait', 'worker_result',
                  'worker_catalog', 'worker_approve', 'worker_cancel', 'worker_followup'}


def check_catalog(response: dict) -> dict:
    result = response.get('result', {})
    if 'error' in response or result.get('isError'):
        raise ValueError('catalog RPC returned an error')
    envelope = result.get('structuredContent')
    if envelope is None:
        texts = [item.get('text', '') for item in result.get('content', [])
                 if item.get('type') == 'text']
        envelope = json.loads(texts[0]) if texts else {}
    if envelope.get('tool') != 'worker_catalog' or 'error' in envelope:
        raise ValueError('catalog envelope invalid')
    data = envelope.get('data', {})
    if data.get('gate') != 'PASS':
        raise ValueError('worker catalog gate is not PASS')
    return {'gate': data['gate'], 'engine': data.get('engine', {}),
            'models': data.get('models', []), 'profiles': data.get('profiles', [])}


def local_probe(python: str, dialer: Path, port: int = 8899, timeout=10.0) -> dict:
    process = subprocess.Popen([python, str(dialer), '--port', str(port)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    buffer = bytearray()
    def rpc(mid, method, params=None):
        message = {'jsonrpc':'2.0', 'method':method}
        if mid is not None:
            message['id'] = mid
        if params is not None:
            message['params'] = params
        process.stdin.write(json.dumps(message).encode() + b'\n')
        process.stdin.flush()
        if mid is None:
            return None
        deadline = time.monotonic() + timeout
        while b'\n' not in buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                raise TimeoutError('local bridge RPC timed out')
            data = os.read(process.stdout.fileno(), 65536)
            if not data:
                raise ValueError('local bridge closed stdout')
            buffer.extend(data)
            if len(buffer) > 1048576:
                raise ValueError('local bridge response exceeds limit')
        line, _, rest = buffer.partition(b'\n')
        buffer[:] = rest
        reply = json.loads(line)
        if reply.get('id') != mid or reply.get('jsonrpc') != '2.0' or 'error' in reply:
            raise ValueError('local RPC id/envelope mismatch')
        return reply
    try:
        init = rpc(1, 'initialize', {'protocolVersion':'2025-03-26',
                   'capabilities':{}, 'clientInfo':{'name':'hina-readonly-probe','version':'1'}})
        rpc(None, 'notifications/initialized')
        rpc(2, 'ping')
        listed = rpc(3, 'tools/list')
        names = {item['name'] for item in listed['result']['tools']}
        if names != REQUIRED_TOOLS:
            raise ValueError('unexpected MCP tool catalog')
        catalog = check_catalog(rpc(4, 'tools/call', {'name':'worker_catalog',
                                                   'arguments':{'refresh':False}}))
        process.stdin.close()
        process.stdin = None
        if process.wait(timeout=6) != 0:
            raise ValueError('bridge did not close cleanly')
        return {'status':'PASS', 'protocol':init['result']['protocolVersion'],
                'tool_count':len(names), 'catalog':catalog, 'worker_start_calls':0,
                'cloud_roundtrip':'NOT_TESTED'}
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill();process.wait(timeout=3)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def health(path: str) -> dict:
    # Ignore inherited proxies: these probes must stay on loopback.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open('http://127.0.0.1:8081/' + path, timeout=3) as response:
            return {'status': response.status, 'reachable':True}
    except urllib.error.HTTPError as error:
        return {'status':error.code, 'reachable':True}
    except (urllib.error.URLError, TimeoutError, OSError):
        return {'status':None, 'reachable':False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--dialer', type=Path, default=Path(__file__).with_name('stdio_dialer.py'))
    parser.add_argument('--python', default=sys.executable)
    ns = parser.parse_args(argv)
    try:
        local = local_probe(ns.python, ns.dialer)
    except (OSError, ValueError, KeyError, TimeoutError, subprocess.TimeoutExpired) as error:
        local = {'status':'FAIL', 'error_type':type(error).__name__, 'worker_start_calls':0}
    report = {'observed_at':time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'local_bridge':local, 'tunnel_health':health('healthz'),
              'tunnel_ready':health('readyz'), 'external_chatgpt':'NOT_TESTED'}
    print(json.dumps(report, indent=2))
    return 0 if local['status']=='PASS' and report['tunnel_ready']['status']==200 else 1


if __name__ == '__main__':
    sys.exit(main())
