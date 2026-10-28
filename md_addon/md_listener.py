"""Marvelous Designer side MCP listener (blocking, runs on MD's main thread).

MD's embedded Python (3.11) does not give CPU to background daemon threads, so
the listener is a plain blocking accept loop on whatever thread the Python
Editor uses (the GUI thread). While it runs, MD's GUI is unresponsive; it stops
when a client sends {"method": "shutdown"} (the MCP server exposes a
`shutdown_listener` tool for that). Without a client, close Marvelous Designer.

SECURITY: this listener executes arbitrary Python inside MD by design. It binds
127.0.0.1 only, takes the port exclusively on Windows, and requires a random
per-session token (written to a per-user file, see default_token_path) on every
request, including shutdown.

How to start it (paste into MD's Python Editor, or use scripts/md_start_listener.py):

    import sys
    sys.path.insert(0, r"<path to this repo>\\md_addon")
    import importlib, md_listener
    importlib.reload(md_listener)
    md_listener.serve_forever()

Wire protocol: one JSON object per line over TCP 127.0.0.1:7421, one request per
connection.
    Request:  {"id": str, "token": str, "method": str, "params": dict}
    Response: {"id": str, "result": any}  or  {"id": str, "error": str}

MD's API is exposed as importable modules (import_api, export_api, fabric_api,
pattern_api, utility_api, ...), NOT as globals. Code sent via execute_python
must `import` what it needs and bind its return value to a name called `result`.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import hmac
import io
import json
import os
import secrets
import socket
import subprocess
import sys
import traceback

HOST = "127.0.0.1"
PORT = 7421
CONN_TIMEOUT = 5.0                 # seconds to receive one request line
MAX_REQUEST_BYTES = 16 * 1024 * 1024


def default_token_path() -> str:
    """Token file location. Override with env MD_MCP_TOKEN_FILE."""
    override = os.environ.get("MD_MCP_TOKEN_FILE")
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "marvelous-designer-mcp", "token")


# Persists across execute_python calls within one listener session.
_persistent_globals: dict = {"__name__": "__md_mcp__"}


def _handle_ping(_params: dict) -> dict:
    return {"pong": True}


def _handle_execute_python(params: dict) -> dict:
    code = params.get("code", "")
    out, err = io.StringIO(), io.StringIO()
    result = None
    error = None
    _persistent_globals["result"] = None  # never leak the previous call's result
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            exec(code, _persistent_globals)
        result = _persistent_globals.get("result")
    except BaseException:  # includes SystemExit from user code: must not kill the listener
        error = traceback.format_exc()
    payload = {"stdout": out.getvalue(), "stderr": err.getvalue(), "result": result, "error": error}
    try:
        json.dumps(payload)
    except TypeError:
        payload["result"] = repr(result)
    return payload


HANDLERS = {
    "ping": _handle_ping,
    "execute_python": _handle_execute_python,
}


class RequestTooLarge(Exception):
    pass


def _read_line(conn: socket.socket) -> bytes | None:
    """Read up to the first newline. Never waits for EOF. Raises socket.timeout / RequestTooLarge."""
    buf = bytearray()
    while b"\n" not in buf:
        chunk = conn.recv(65536)
        if not chunk:
            return None
        buf.extend(chunk)
        if len(buf) > MAX_REQUEST_BYTES:
            raise RequestTooLarge()
    return bytes(buf).split(b"\n", 1)[0]


def _send(conn: socket.socket, obj: dict) -> None:
    conn.sendall((json.dumps(obj) + "\n").encode("utf-8"))


def _token_ok(supplied, token: str) -> bool:
    if not isinstance(supplied, str):
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8"))


def _serve_conn(conn: socket.socket, token: str, busy_path: str) -> bool:
    """Handle one request on one connection. Returns False iff an authorized shutdown was requested."""
    with conn:
        conn.settimeout(CONN_TIMEOUT)
        try:
            line = _read_line(conn)
        except socket.timeout:
            return True
        except RequestTooLarge:
            with contextlib.suppress(OSError):
                _send(conn, {"id": None, "error": "request too large"})
            return True
        except OSError:
            return True
        if line is None:
            return True  # client connected then closed without sending anything
        try:
            req = json.loads(line.decode("utf-8"))
            if not isinstance(req, dict):
                raise ValueError("request must be a JSON object")
        except (ValueError, UnicodeDecodeError) as e:  # JSONDecodeError is a ValueError
            with contextlib.suppress(OSError):
                _send(conn, {"id": None, "error": f"bad json: {e}"})
            return True

        req_id = req.get("id")
        if not _token_ok(req.get("token"), token):
            with contextlib.suppress(OSError):
                _send(conn, {"id": req_id, "error": "unauthorized"})
            return True

        method = req.get("method")
        if method == "shutdown":
            with contextlib.suppress(OSError):
                _send(conn, {"id": req_id, "result": {"bye": True}})
            return False

        # busy marker: lets a client that timed out see what the listener is doing.
        # Removed BEFORE replying, so a client never sees a stale marker after its answer.
        with contextlib.suppress(OSError), open(busy_path, "w", encoding="utf-8") as f:
            json.dump({"method": method, "id": req_id,
                       "since": datetime.now(timezone.utc).isoformat()}, f)
        try:
            handler = HANDLERS.get(method)
            if handler is None:
                resp = {"id": req_id, "error": f"unknown method: {method}"}
            else:
                try:
                    params = req.get("params")
                    resp = {"id": req_id, "result": handler(params if isinstance(params, dict) else {})}
                except BaseException:
                    resp = {"id": req_id, "error": traceback.format_exc()}
        finally:
            with contextlib.suppress(OSError):
                os.remove(busy_path)
        with contextlib.suppress(OSError):
            _send(conn, resp)
        return True


def _restrict_to_current_user(path: str) -> None:
    """Best effort: on Windows drop inherited ACLs and grant only the current user."""
    if sys.platform != "win32":
        return
    user = os.environ.get("USERNAME")
    if not user:
        return
    with contextlib.suppress(Exception):
        subprocess.run(["icacls", path, "/inheritance:r", "/grant:r", f"{user}:F"],
                       capture_output=True, timeout=10, check=False)


def write_token_file(path: str, token: str) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token)
    _restrict_to_current_user(path)


def _bind(host: str, port: int) -> socket.socket:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            # Nobody else may bind this port, even with SO_REUSEADDR.
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port))
        srv.listen(4)
    except OSError as e:
        srv.close()
        raise RuntimeError(f"[md-mcp] cannot bind {host}:{port} ({e}) -- a listener is probably already running") from e
    return srv


def serve_forever(host: str = HOST, port: int = PORT, token_path: str | None = None, on_ready=None) -> None:
    """Blocking listener loop. Call this from MD's Python Editor.

    Blocks the MD GUI until an authorized client sends {"method": "shutdown"}.
    `on_ready(port)` (optional) is called once listening; used by tests.
    """
    token_path = token_path or default_token_path()
    srv = _bind(host, port)  # bind first: if the port is taken, do not overwrite the running listener's token
    token = secrets.token_urlsafe(32)
    try:
        write_token_file(token_path, token)
    except BaseException:
        srv.close()
        raise
    busy_path = os.path.join(os.path.dirname(token_path), "busy.json")
    with contextlib.suppress(OSError):
        os.remove(busy_path)
    actual_port = srv.getsockname()[1]
    print(f"[md-mcp] listening (blocking) on {host}:{actual_port}; token file: {token_path}")
    print("[md-mcp] MD GUI is frozen until a client sends 'shutdown'")
    if on_ready is not None:
        on_ready(actual_port)
    try:
        with srv:
            while True:
                conn, _addr = srv.accept()
                try:
                    keep_going = _serve_conn(conn, token, busy_path)
                except BaseException:
                    traceback.print_exc()
                    keep_going = True
                if not keep_going:
                    break
    finally:
        with contextlib.suppress(OSError):
            os.remove(token_path)
        print("[md-mcp] stopped")


if __name__ == "__main__":
    serve_forever()
