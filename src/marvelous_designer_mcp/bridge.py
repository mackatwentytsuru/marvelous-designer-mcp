import json
import os
import socket
import uuid
from typing import Any

from .config import MD_HOST, MD_PORT, MD_TIMEOUT, token_path


class BridgeError(RuntimeError):
    pass


def _recv_line(sock: socket.socket) -> bytes:
    buf = bytearray()
    while b"\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            raise BridgeError("connection closed before response")
        buf.extend(chunk)
    return bytes(buf).split(b"\n", 1)[0]


def _read_token() -> str:
    path = token_path()
    try:
        with open(path, encoding="utf-8") as f:
            token = f.read().strip()
    except OSError as e:
        raise BridgeError(f"cannot read listener token file {path} ({e}); is the listener running?") from e
    if not token:
        raise BridgeError(f"listener token file {path} is empty")
    return token


def call(method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
    """Send a JSON request to the MD listener and return the result.

    Wire format: one JSON object per line.
    Request:  {"id": str, "token": str, "method": str, "params": dict}
    Response: {"id": str, "result": any} or {"id": str, "error": str}
    """
    payload = {
        "id": uuid.uuid4().hex,
        "token": _read_token(),
        "method": method,
        "params": params or {},
    }
    data = (json.dumps(payload) + "\n").encode("utf-8")

    try:
        with socket.create_connection((MD_HOST, MD_PORT), timeout=timeout or MD_TIMEOUT) as sock:
            sock.sendall(data)
            line = _recv_line(sock)
    except OSError as e:  # includes timeouts and connection refused
        detail = ""
        if isinstance(e, TimeoutError):
            try:
                path = os.path.join(os.path.dirname(token_path()), "busy.json")
                with open(path, encoding="utf-8") as f:
                    busy = json.load(f)
                detail = f"; listener busy with {busy['method']} since {busy['since']}"
            except (OSError, ValueError, KeyError, TypeError):
                pass
        raise BridgeError(f"cannot talk to listener at {MD_HOST}:{MD_PORT} ({e}){detail}") from e

    try:
        resp = json.loads(line.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise BridgeError(f"invalid JSON from listener: {e}") from e

    if "error" in resp:
        raise BridgeError(str(resp["error"]))
    return resp.get("result")
