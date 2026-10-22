"""stdio MCP proxy — separate process from the supervisor (MCP.md §1).

Bridges the Secure MCP Tunnel's stdio MCP connection to the local
supervisor Unix domain socket. One UDS round-trip per tool call; the
proxy is short-lived and holds no state. stdin EOF or a dropped MCP
connection is NEVER a job cancel (MCP.md §1, T15).

stdio purity (T16): stdout carries newline-delimited JSON-RPC frames
only — every log line goes to stderr (and optionally a 0600 --log-file).
Secret values (approval key material, OPENCODE_PASSWORD, challenges)
are never logged and never copied into the proxy: OpenCode credentials
belong to OpenCode; the approval key never leaves the supervisor.

The 8 tool definitions are served verbatim from the frozen contract
file (docs/contracts/mcp-tools.json) — no invented schemas (T13).

JSON-RPC protocol errors follow MCP.md §4 (-32700/-32600/-32601/-32602);
tool-level INVALID_ARGUMENT travels as a tool result with isError.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import sys

try:  # package import
    from .artifacts import redact_text
except ImportError:  # pragma: no cover - direct script execution
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
    from hina_opencode_worker.artifacts import redact_text

MAX_LINE_BYTES = 256 * 1024            # §5 input envelope bound
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]
SERVER_INFO = {"name": "hina-opencode-worker-proxy", "version": "0.3.0"}
TOOL_NAMES = ("worker_catalog", "worker_start", "worker_status",
              "worker_wait", "worker_result", "worker_followup",
              "worker_cancel", "worker_approve")

# JSON-RPC standard error codes
E_PARSE = -32700
E_INVALID_REQUEST = -32600
E_METHOD_NOT_FOUND = -32601
E_INVALID_PARAMS = -32602


class Proxy:
    def __init__(self, *, sock_path: str, tools_doc: dict, log=None):
        self.sock_path = sock_path
        self.tools = [t for t in tools_doc["tools"]
                      if t.get("name") in TOOL_NAMES]
        if [t["name"] for t in self.tools] != sorted(
                t["name"] for t in self.tools):
            self.tools.sort(key=lambda t: t["name"])
        assert len(self.tools) == 8, "contract must expose exactly 8 tools"
        self._by_name = {t["name"]: t for t in self.tools}
        self.log = log or (lambda *_: None)
        self.negotiated_version = LATEST_PROTOCOL_VERSION

    # ----------------------------------------------------------- supervisor

    def _call_supervisor(self, req: dict) -> dict:
        try:
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.settimeout(30.0)
            conn.connect(self.sock_path)
        except OSError:
            return {"contract_version": "0.3", "tool": req.get("tool"),
                    "observed_at": _now_iso(),
                    "receipt": {"request_id": None, "replayed": False},
                    "error": {"code": "INTERNAL",
                              "message": "supervisor unreachable",
                              "retryable": True, "details": {}}}
        try:
            payload = json.dumps(req, ensure_ascii=False).encode("utf-8")
            conn.sendall(struct.pack(">I", len(payload)) + payload)
            hdr = _recv_exact(conn, 4)
            if hdr is None:
                raise OSError("no response header")
            (length,) = struct.unpack(">I", hdr)
            body = _recv_exact(conn, length)
            if body is None:
                raise OSError("truncated response")
            return json.loads(body.decode("utf-8"))
        finally:
            conn.close()

    # -------------------------------------------------------------- MCP ops

    def tools_list(self) -> dict:
        return {"tools": [
            {"name": t["name"], "description": t["description"],
             "inputSchema": t["inputSchema"],
             "outputSchema": t["outputSchema"],
             "annotations": t.get("annotations", {})}
            for t in self.tools]}

    def tools_call(self, params) -> tuple[dict | None, dict | None]:
        """Returns (result, error). Tool-level failures come back as a
        result with isError=true; protocol-level problems as error."""
        if not isinstance(params, dict) or \
                not isinstance(params.get("name"), str):
            return None, _rpc_error(None, E_INVALID_PARAMS,
                                    "tools/call requires {name, arguments}")
        name = params["name"]
        args = params.get("arguments", {})
        if name not in self._by_name:
            return None, _rpc_error(None, E_INVALID_PARAMS,
                                    f"unknown tool {name!r}")
        if not isinstance(args, dict):
            return None, _rpc_error(None, E_INVALID_PARAMS,
                                    "arguments must be an object")
        env = self._call_supervisor({"client": "mcp_proxy", "tool": name,
                                     "arguments": args})
        is_error = isinstance(env, dict) and "error" in env
        try:
            text = json.dumps(env, ensure_ascii=False)
        except (TypeError, ValueError):  # never happens for contract dicts
            env = {"contract_version": "0.3", "tool": name,
                   "observed_at": _now_iso(),
                   "receipt": {"request_id": None, "replayed": False},
                   "error": {"code": "INTERNAL", "message": "bad envelope",
                             "retryable": False, "details": {}}}
            text = json.dumps(env)
        result = {"content": [{"type": "text", "text": text}],
                  "structuredContent": env, "isError": bool(is_error)}
        return result, None

    def handle(self, msg: dict) -> dict | None:
        """One JSON-RPC message -> response (or None for notifications)."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" \
                or not isinstance(msg.get("method"), str):
            return _rpc_error(msg.get("id") if isinstance(msg, dict) else None,
                              E_INVALID_REQUEST, "malformed JSON-RPC request")
        mid = msg.get("id")
        method = msg["method"]
        params = msg.get("params") or {}
        is_notification = "id" not in msg
        try:
            if method == "initialize":
                requested = params.get("protocolVersion")
                # Never echo an unsupported version (MCP.md §1).
                self.negotiated_version = (
                    requested if requested in SUPPORTED_PROTOCOL_VERSIONS
                    else LATEST_PROTOCOL_VERSION)
                result = {"protocolVersion": self.negotiated_version,
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": SERVER_INFO}
            elif method.startswith("notifications/"):
                return None          # notifications get no response
            elif method == "ping":
                if is_notification:
                    return None
                return _rpc_result(mid, {})
            elif method == "tools/list":
                result = self.tools_list()
            elif method == "tools/call":
                res, err = self.tools_call(params)
                if err is not None:
                    err["id"] = mid
                    return err
                return _rpc_result(mid, res)
            else:
                if is_notification:
                    return None
                return _rpc_error(mid, E_METHOD_NOT_FOUND,
                                  f"method {method!r} not found")
        except Exception:  # safety net — keep stdio pure, answer as JSON-RPC
            self.log("proxy", "handler failure")
            if is_notification:
                return None
            return _rpc_error(mid, E_INVALID_REQUEST, "handler failure")
        if is_notification:
            return None
        return _rpc_result(mid, result)


def _rpc_result(mid, result) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _rpc_error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": code, "message": message}}


def _recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_tools_doc(path: str) -> dict:
    doc = json.loads(open(path, encoding="utf-8").read())
    if doc.get("kind") != "implementation_spec_not_live_tools" \
            or doc.get("contract_version") != "0.3":
        raise SystemExit("tools file is not the contract v0.3 spec")
    return doc


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--socket", required=True,
                    help="supervisor UDS path "
                         "(~/Library/Application Support/"
                         "HinaOpenCodeWorker/run/supervisor.sock)")
    ap.add_argument("--tools", required=True,
                    help="path to docs/contracts/mcp-tools.json")
    ap.add_argument("--log-file", default=None)
    ap.add_argument("--verbose", action="store_true")
    ns = ap.parse_args(argv)

    log_fh = None
    if ns.log_file:
        fd = os.open(ns.log_file,
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        log_fh = os.fdopen(fd, "a", encoding="utf-8")

    def log(where: str, text: str) -> None:
        # Redact, then send to STDERR only — stdout stays JSON-RPC pure.
        safe, _ = redact_text(f"{where}: {text}")
        line = safe[:2000] + "\n"
        sys.stderr.write(line)
        sys.stderr.flush()
        if log_fh:
            log_fh.write(line)
            log_fh.flush()

    proxy = Proxy(sock_path=os.path.expanduser(ns.socket),
                  tools_doc=load_tools_doc(ns.tools),
                  log=log if ns.verbose else (lambda w, t: None))
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    # stdin EOF / tunnel disconnect => proxy exits; the supervisor and the
    # current job are untouched (T15).
    while True:
        line = stdin.readline(MAX_LINE_BYTES + 1)
        if not line:
            log("proxy", "stdin EOF — closing (job unaffected)")
            return 0
        if len(line) > MAX_LINE_BYTES:
            resp = _rpc_error(None, E_INVALID_REQUEST,
                              "request exceeds 256KiB")
            stdout.write(json.dumps(resp).encode() + b"\n")
            stdout.flush()
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            resp = _rpc_error(None, E_PARSE, "parse error")
            stdout.write(json.dumps(resp).encode() + b"\n")
            stdout.flush()
            continue
        resp = proxy.handle(msg)
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False).encode()
                         + b"\n")
            stdout.flush()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
