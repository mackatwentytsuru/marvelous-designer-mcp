import importlib.util
import json
import os
import socket
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "md_addon"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import md_listener  # noqa: E402
from marvelous_designer_mcp import bridge, config  # noqa: E402


@pytest.fixture
def listener(tmp_path, monkeypatch):
    token_file = tmp_path / "tok" / "token"
    ready = threading.Event()
    box = {}

    def on_ready(port):
        box["port"] = port
        ready.set()

    t = threading.Thread(
        target=md_listener.serve_forever,
        kwargs={"port": 0, "token_path": str(token_file), "on_ready": on_ready},
        daemon=True,
    )
    t.start()
    assert ready.wait(10)
    monkeypatch.setattr(bridge, "MD_PORT", box["port"])
    monkeypatch.setenv("MD_MCP_TOKEN_FILE", str(token_file))
    yield {"port": box["port"], "token_file": token_file, "thread": t}
    try:
        bridge.call("shutdown")
    except Exception:
        pass
    t.join(5)


def raw(port, data: bytes, read=True, timeout=10):
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(data)
        if not read:
            return None
        return s.makefile("rb").readline()


def test_ping_with_token(listener):
    assert bridge.call("ping") == {"pong": True}


def test_token_file_created_and_removed_on_shutdown(listener):
    assert listener["token_file"].read_text().strip()
    bridge.call("shutdown")
    listener["thread"].join(5)
    assert not listener["token_file"].exists()


def test_missing_and_wrong_token_rejected(listener):
    for req in ({"id": "1", "method": "ping"}, {"id": "2", "token": "nope", "method": "ping"},
                {"id": "3", "token": 123, "method": "ping"}):
        resp = json.loads(raw(listener["port"], (json.dumps(req) + "\n").encode()))
        assert resp["error"] == "unauthorized"


def test_shutdown_requires_token(listener):
    resp = json.loads(raw(listener["port"], b'{"id":"x","method":"shutdown"}\n'))
    assert resp["error"] == "unauthorized"
    assert listener["thread"].is_alive()
    assert bridge.call("ping") == {"pong": True}


def test_execute_and_result_reset(listener):
    r = bridge.call("execute_python", {"code": "result = 1+1"})
    assert r["result"] == 2 and r["error"] is None
    r2 = bridge.call("execute_python", {"code": "x = 5"})
    assert r2["result"] is None  # previous result must not leak


def test_sys_exit_does_not_kill_listener(listener):
    r = bridge.call("execute_python", {"code": "import sys; sys.exit(3)"})
    assert "SystemExit" in r["error"]
    assert bridge.call("ping") == {"pong": True}


def test_bad_json_and_non_object(listener):
    assert "bad json" in json.loads(raw(listener["port"], b"{oops\n"))["error"]
    assert "bad json" in json.loads(raw(listener["port"], b"[1]\n"))["error"]
    assert bridge.call("ping") == {"pong": True}


def test_receive_timeout_does_not_block_listener(listener, monkeypatch):
    monkeypatch.setattr(md_listener, "CONN_TIMEOUT", 0.3)
    stall = socket.create_connection(("127.0.0.1", listener["port"]))
    stall.sendall(b"{partial")  # never sends newline
    try:
        assert bridge.call("ping") == {"pong": True}  # served after the stalled conn times out
    finally:
        stall.close()


def test_request_size_cap(listener, monkeypatch):
    monkeypatch.setattr(md_listener, "MAX_REQUEST_BYTES", 1024)
    resp = json.loads(raw(listener["port"], b"a" * 5000))
    assert resp["error"] == "request too large"
    assert bridge.call("ping") == {"pong": True}


def test_second_listener_cannot_take_port(listener):
    with pytest.raises(RuntimeError, match="already running"):
        md_listener.serve_forever(port=listener["port"], token_path=str(listener["token_file"]) + "2")
    # the running listener's token was not overwritten
    assert bridge.call("ping") == {"pong": True}


@pytest.mark.skipif(sys.platform != "win32", reason="SO_EXCLUSIVEADDRUSE is Windows-only")
def test_port_cannot_be_stolen_with_reuseaddr(listener):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    with pytest.raises(OSError):
        s.bind(("127.0.0.1", listener["port"]))
    s.close()


def test_bridge_without_listener(tmp_path, monkeypatch):
    monkeypatch.setenv("MD_MCP_TOKEN_FILE", str(tmp_path / "missing"))
    with pytest.raises(bridge.BridgeError, match="token file"):
        bridge.call("ping")


def test_token_path_matches_listener(monkeypatch):
    monkeypatch.delenv("MD_MCP_TOKEN_FILE", raising=False)
    assert config.token_path() == md_listener.default_token_path()


def _load_installer():
    spec = importlib.util.spec_from_file_location("install_md_plugin", ROOT / "scripts" / "install_md_plugin.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_installer_merge_keeps_others_and_is_idempotent():
    inst = _load_installer()
    data = {"plugins": [{"m_SourcePath": "C:/other.py", "m_PlugInTitle": "other"}], "version": 2}
    new, action = inst.merge_entry(data, "C:/X/md_start_listener.py")
    assert action == "added" and len(new["plugins"]) == 2 and new["plugins"][0]["m_PlugInTitle"] == "other"
    assert len(data["plugins"]) == 1  # input not mutated
    _, again = inst.merge_entry(new, "c:/X/MD_START_LISTENER.py")
    assert again == "unchanged"


def test_installer_backup_and_atomic_write(tmp_path, monkeypatch, capsys):
    inst = _load_installer()
    plugins = tmp_path / "Plugins"
    plugins.mkdir()
    settings = plugins / "pluginSettings.json"
    original = json.dumps({"plugins": [{"m_SourcePath": "C:/other.py"}], "version": 2})
    settings.write_text(original, encoding="utf-8")
    monkeypatch.setattr(inst, "MD_PLUGINS", plugins)
    monkeypatch.setattr(inst, "SETTINGS", settings)
    assert inst.main(["--dry-run"]) == 0
    assert settings.read_text(encoding="utf-8") == original
    assert inst.main([]) == 0
    backups = list(plugins.glob("pluginSettings.json.bak-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == original
    assert not list(plugins.glob("*.tmp"))
    assert len(json.loads(settings.read_text(encoding="utf-8"))["plugins"]) == 2


def test_no_hardcoded_author_paths():
    for f in ROOT.rglob("*"):
        if f.is_file() and ".git" not in f.parts and ".venv" not in f.parts and f.suffix in {".py", ".md", ".toml"} \
                and f.name != "test_listener.py":
            assert "azoo" not in f.read_text(encoding="utf-8", errors="ignore").lower(), f


@pytest.mark.parametrize("code", ["result = 7", "raise RuntimeError('test failure')"])
def test_busy_marker_during_execute_and_cleanup(listener, code):
    import time

    marker = listener["token_file"].with_name("busy.json")
    replies = []
    worker = threading.Thread(target=lambda: replies.append(bridge.call(
        "execute_python", {"code": "import time; time.sleep(1)\n" + code})))
    worker.start()
    try:
        deadline = time.monotonic() + 3
        busy = None
        raw_text = ""
        while time.monotonic() < deadline:
            try:
                raw_text = marker.read_text()
                busy = json.loads(raw_text)
                break
            except (OSError, ValueError):  # not written yet, or caught mid-write (Windows)
                time.sleep(0.01)
        assert busy is not None
        assert set(busy) == {"method", "id", "since"}
        assert busy["method"] == "execute_python"
        assert busy["id"] and busy["since"]
        assert listener["token_file"].read_text().strip() not in raw_text
    finally:
        worker.join(5)
    assert not worker.is_alive()
    assert replies
    assert bridge.call("ping") == {"pong": True}
    assert not marker.exists()


def test_bridge_timeout_reports_busy(listener):
    with pytest.raises(bridge.BridgeError, match="listener busy with execute_python since "):
        bridge.call("execute_python", {"code": "import time; time.sleep(1)"}, timeout=0.2)
    assert bridge.call("ping") == {"pong": True}
    assert not listener["token_file"].with_name("busy.json").exists()


def test_busy_marker_removed_after_handler_error(listener, monkeypatch):
    def fail(_params):
        raise RuntimeError("handler failed")

    monkeypatch.setitem(md_listener.HANDLERS, "fail", fail)
    with pytest.raises(bridge.BridgeError, match="handler failed"):
        bridge.call("fail")
    assert bridge.call("ping") == {"pong": True}
    assert not listener["token_file"].with_name("busy.json").exists()
