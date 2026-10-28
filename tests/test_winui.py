import base64
import json
from pathlib import Path
import struct
import subprocess
import sys
import time
from types import SimpleNamespace
import zlib

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from marvelous_designer_mcp import bridge, server, winui


def window(hwnd, title, width, height, pid=10):
    return {"hwnd": hwnd, "pid": pid, "title": title, "rect": [0, 0, width, height]}


@pytest.mark.parametrize("editor", ["Python Editor", "Pythonエディタ", "Python エディター", "Python 편집기"])
def test_classification_per_process_excludes_editor(editor):
    main = window(1, "Main", 1000, 800)
    dialog = window(2, "情報", 400, 200)
    other_main = window(5, "Other main", 800, 600, 20)
    other_dialog = window(6, "Error", 200, 100, 20)
    mains, dialogs = winui.classify_windows([
        dialog, window(3, editor, 2000, 1500), main,
        window(4, "Large palette", 1000, 400), other_dialog, other_main,
        window(7, "Empty", 0, 0),
    ])
    assert mains == [main, other_main]
    assert dialogs == [dialog, other_dialog]


def test_classification_prefers_owned_and_disabled_main_then_area_fallback():
    main = {**window(1, "Tiny main", 300, 200), "enabled": False}
    owned = {**window(2, "Large owned dialog", 800, 700), "owner": 1}
    floating = window(3, "Palette", 100, 100)
    mains, dialogs = winui.classify_windows([main, owned, floating])
    assert mains == [main]
    # Disabled main and area fallback include other windows while a modal is up.
    assert dialogs == [owned, floating]


def test_classification_owned_window_is_candidate_even_when_large():
    main = window(1, "Main", 800, 600)
    owned = {**window(2, "Large dialog", 700, 550), "owner": 1}
    mains, dialogs = winui.classify_windows([main, owned])
    assert mains == [main]
    assert dialogs == [owned]


@pytest.mark.parametrize("output", [
    '\ufeff[{"hwnd":1,"texts":["境界線"],"buttons":["OK"]}]',
    '{"hwnd":1,"texts":"境界線","buttons":"OK"}',
])
def test_parse_uia_unicode_and_single_values(output):
    assert winui._parse_uia(output) == [{"hwnd": 1, "texts": ["境界線"], "buttons": ["OK"]}]


@pytest.mark.parametrize("output", ["", "no json", "null", '["bad"]', '[{}]', '{"hwnd":1,"texts":[1]}'])
def test_parse_uia_rejects_bad_response(output):
    with pytest.raises(winui.UIError, match="Invalid UI Automation response"):
        winui._parse_uia(output)


def test_uia_untrusted_button_is_only_env_data(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout='[{"hwnd":1}]', stderr="")

    monkeypatch.setattr(winui.subprocess, "run", run)
    hostile = "OK'; Write-Output hacked; #\n\"`$(exit) 境界線"
    winui._uia([window(1, "Info", 100, 100)], button="OK", timeout=0.5)
    winui._uia([window(1, "Info", 100, 100)], button=hostile, timeout=0.5)
    assert calls[0][0] == calls[1][0]
    args, kwargs = calls[1]
    assert hostile not in " ".join(args)
    assert json.loads(kwargs["env"]["MD_MCP_UI_REQUEST"])["button"] == hostile
    assert base64.b64decode(args[-1]).decode("utf-16-le") == winui._UIA_SCRIPT
    assert kwargs["timeout"] == 0.5


@pytest.mark.parametrize("failure", ["timeout", "exit"])
def test_uia_failure_is_bounded(monkeypatch, failure):
    def run(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("powershell", kwargs["timeout"])
        return SimpleNamespace(returncode=1, stdout="", stderr="test error")

    monkeypatch.setattr(winui.subprocess, "run", run)
    with pytest.raises(winui.UIError) as caught:
        winui._uia([], timeout=0.1)
    if failure == "timeout":
        assert "timed out after 0.1s" in str(caught.value)
        assert "EncodedCommand" not in str(caught.value)


def test_dialogs_merge_text_and_report_uia_failure(monkeypatch):
    monkeypatch.setattr(winui, "list_windows", lambda pid: [window(1, "Main", 1000, 800), window(2, "情報", 300, 100)])
    monkeypatch.setattr(winui, "_uia", lambda *a, **k: [{"hwnd": 2, "texts": ["境界線", "境界線"], "buttons": ["OK"]}])
    found = winui.dialogs()
    assert found[0]["index"] == 0
    assert found[0]["text"] == "情報\n境界線\nOK"

    def fail(*a, **k):
        raise winui.UIError("timed out")

    monkeypatch.setattr(winui, "_uia", fail)
    assert winui.dialogs()[0]["uia_error"] == "timed out"
    assert winui.dialogs()[0]["text"] == "情報"


def test_generic_title_python_editor_is_excluded_and_indexes_rebuilt(monkeypatch):
    monkeypatch.setattr(winui, "list_windows", lambda pid: [
        window(1, "Main", 2000, 1000), window(2, "Product", 800, 800), window(3, "Info", 300, 100)])
    monkeypatch.setattr(winui, "_uia", lambda *a, **k: [
        {"hwnd": 2, "texts": ["Pythonエディタ", "Source:"]},
        {"hwnd": 3, "texts": ["Meshing error"], "buttons": ["OK"]},
    ])
    found = winui.dialogs()
    assert [(w["hwnd"], w["index"]) for w in found] == [(3, 0)]


@pytest.mark.parametrize("index", [-1, 1, 10])
def test_dismiss_rejects_invalid_index_without_invoking(monkeypatch, index):
    monkeypatch.setattr(winui, "list_windows", lambda pid: [window(1, "Main", 1000, 800), window(2, "Info", 300, 100)])
    def read(*args, **kwargs):
        assert "button" not in kwargs, "must not invoke"
        return [{"hwnd": 2, "texts": [], "buttons": ["OK"]}]

    monkeypatch.setattr(winui, "_uia", read)
    assert server.dismiss_dialog(index=index)["ok"] is False


def test_dismiss_forwards_exact_button_and_one_window(monkeypatch):
    dialog = window(2, "Info", 300, 100)
    monkeypatch.setattr(winui, "list_windows", lambda pid: [window(1, "Main", 1000, 800), dialog])
    calls = []

    def invoke(windows, **kwargs):
        calls.append((windows, kwargs))
        if "button" in kwargs:
            return [{"hwnd": 2}]
        return [{"hwnd": 2, "buttons": ["承認"], "texts": []}]

    monkeypatch.setattr(winui, "_uia", invoke)
    assert server.dismiss_dialog("承認", hwnd=2)["invoked"] is True
    assert calls[0] == ([dialog], {"timeout": None})
    assert calls[1][0][0]["hwnd"] == 2
    assert calls[1][1] == {"button": "承認"}
    assert len(calls) == 2


def test_dismiss_matches_requested_hwnd_and_rejects_empty_button(monkeypatch):
    dialogs = [window(2, "First", 300, 100), window(3, "Second", 300, 100)]
    monkeypatch.setattr(winui, "dialogs", lambda **kwargs: [
        {**dialogs[0], "buttons": ["OK"]}, {**dialogs[1], "buttons": ["OK"]}
    ])
    invoked = []
    monkeypatch.setattr(winui, "_uia", lambda windows, **kwargs: invoked.append(windows) or [{"hwnd": windows[0]["hwnd"]}])
    assert winui.dismiss("OK", index=0, hwnd=3)["hwnd"] == 3
    assert invoked[-1][0]["hwnd"] == 3
    with pytest.raises(winui.UIError, match="must not be empty"):
        winui.dismiss("")
    assert len(invoked) == 1


def test_png_encoder_dimensions_pixels_and_checksums():
    png = winui._png(2, 1, bytes([0, 0, 255, 0, 0, 255, 0, 0]))
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    offset = 8
    chunks = {}
    while offset < len(png):
        size = struct.unpack_from("!I", png, offset)[0]
        kind = png[offset + 4:offset + 8]
        payload = png[offset + 8:offset + 8 + size]
        assert struct.unpack_from("!I", png, offset + 8 + size)[0] == zlib.crc32(kind + payload)
        chunks[kind] = payload
        offset += size + 12
    assert struct.unpack("!2I5B", chunks[b"IHDR"]) == (2, 1, 8, 2, 0, 0, 0)
    assert zlib.decompress(chunks[b"IDAT"]) == b"\0\xff\0\0\0\xff\0"


def test_capture_timeout_uses_only_our_helper_then_screen(monkeypatch, tmp_path):
    calls = []

    def run(args, **kwargs):
        calls.append(json.loads(kwargs["input"]))
        assert args == [sys.executable, "-I", str(Path(winui.__file__).resolve())]
        if len(calls) == 1:
            assert kwargs["timeout"] == pytest.approx(0.05)
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        assert kwargs["timeout"] > 0
        return SimpleNamespace(returncode=0, stdout='{"method":"screen","occlusion_possible":true}')

    monkeypatch.setattr(winui.subprocess, "run", run)
    result = winui._capture_bounded(window(1, "Main", 100, 100), tmp_path / "out.png", timeout=0.1)
    assert result["occlusion_possible"]
    assert [c["screen"] for c in calls] == [False, True]


@pytest.mark.parametrize("which,expected", [("main", [1]), ("dialog", [2]), ("all", [1, 2])])
def test_screenshot_selection_and_names(monkeypatch, tmp_path, which, expected):
    monkeypatch.setattr(winui, "_supported", lambda: None)
    monkeypatch.setattr(winui, "list_windows", lambda pid: [window(1, "Main", 1000, 800), window(2, "Info", 300, 100)])
    monkeypatch.setattr(winui, "_uia", lambda *a, **k: [{"hwnd": 2}])
    monkeypatch.setattr(winui, "_capture_bounded", lambda w, p, **kwargs: {"hwnd": w["hwnd"], "path": str(p)})
    result = server.screenshot(str(tmp_path / "capture.png"), which)
    assert result["ok"]
    assert [w["hwnd"] for w in result["screenshots"]] == expected
    assert len({w["path"] for w in result["screenshots"]}) == len(expected)


def test_screenshot_rejects_backslash_and_overwrite_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(winui, "_supported", lambda: None)
    monkeypatch.setattr(winui, "list_windows", lambda pid: [window(1, "Main", 1000, 800)])
    with pytest.raises(winui.UIError, match="backslash"):
        winui.screenshot("\\\\server\\share\\capture.png")
    existing = tmp_path / "exists.png"
    existing.write_bytes(b"keep")
    with pytest.raises(winui.UIError, match="already exists"):
        winui.screenshot(str(existing))
    assert existing.read_bytes() == b"keep"


def test_screenshot_rejects_minimized_window(monkeypatch, tmp_path):
    monkeypatch.setattr(winui, "_supported", lambda: None)
    monkeypatch.setattr(winui, "list_windows", lambda pid: [{**window(1, "Main", 1000, 800), "minimized": True}])
    with pytest.raises(winui.UIError, match="minimized"):
        winui.screenshot(str(tmp_path / "minimized.png"))


def test_capture_bounded_uses_one_overall_budget(monkeypatch, tmp_path):
    values = iter([10.0, 10.0, 10.08, 10.15])
    monkeypatch.setattr("time.monotonic", lambda: next(values))
    calls = []
    def run(args, **kwargs):
        calls.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])
    monkeypatch.setattr(winui.subprocess, "run", run)
    with pytest.raises(winui.UIError, match="overall time budget"):
        winui._capture_bounded(window(1, "Main", 10, 10), tmp_path / "x.png", timeout=0.1)
    assert calls == [pytest.approx(0.05), pytest.approx(0.02)]


@pytest.mark.parametrize("tool,args", [("md_dialogs", ()), ("dismiss_dialog", ()), ("screenshot", ("capture.png",))])
def test_non_windows_tools_have_clear_result(monkeypatch, tool, args):
    monkeypatch.setattr(winui, "_supported", lambda: (_ for _ in ()).throw(winui.UIError("Windows UI tools are not supported on this OS")))
    response = getattr(server, tool)(*args)
    assert not response["ok"] and "not supported" in response["error"]


@pytest.mark.parametrize("pid", ["0", "-1", "bad"])
def test_invalid_pid(monkeypatch, pid):
    monkeypatch.setattr(winui, "_supported", lambda: None)
    monkeypatch.setenv("MD_MCP_PID", pid)
    with pytest.raises(winui.UIError, match="positive integer"):
        winui.process_ids()


def test_explicit_pid_overrides_environment(monkeypatch):
    monkeypatch.setattr(winui, "_supported", lambda: None)
    monkeypatch.setenv("MD_MCP_PID", "10")
    # No matching MD executable is present in this test environment.
    assert winui.process_ids() == set()
    assert winui.process_ids(20) == set()


def test_explicit_pid_still_requires_process_pattern_match():
    assert winui._process_matches("MarvelousDesigner-13.exe", "MarvelousDesigner*.exe")
    assert not winui._process_matches("OtherApp.exe", "MarvelousDesigner*.exe")


@pytest.mark.parametrize("inspection", ["dialog", "failure", "empty"])
def test_bridge_timeout_dialog_diagnostic_is_best_effort(monkeypatch, inspection):
    monkeypatch.setattr(bridge, "_read_token", lambda: "test-token")

    def timeout(*a, **k):
        raise TimeoutError("test timeout")

    def inspect(**kwargs):
        assert kwargs == {"timeout": 2.0}
        if inspection == "failure":
            raise RuntimeError("UIA unavailable")
        return [{"text": "境界線\nOK", "buttons": ["OK"]}] if inspection == "dialog" else []

    monkeypatch.setattr(bridge.socket, "create_connection", timeout)
    monkeypatch.setattr(winui, "dialogs", inspect)
    with pytest.raises(bridge.BridgeError) as caught:
        bridge.call("ping")
    assert "test timeout" in str(caught.value)
    assert ("possible dialog: '境界線\nOK' (check with md_dialogs, then dismiss_dialog)" in str(caught.value)) == (inspection == "dialog")


@pytest.mark.parametrize("candidate", [
    {"text": "No buttons", "buttons": []},
    {"text": "UIA failed", "buttons": ["OK"], "uia_error": "failed"},
])
def test_bridge_timeout_omits_non_actionable_dialogs(monkeypatch, candidate):
    monkeypatch.setattr(bridge, "_read_token", lambda: "test-token")
    monkeypatch.setattr(bridge.socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("timeout")))
    monkeypatch.setattr(winui, "dialogs", lambda **kwargs: [candidate])
    with pytest.raises(bridge.BridgeError) as caught:
        bridge.call("ping")
    assert "possible dialog:" not in str(caught.value)


_FAKE_DIALOG = r"""
Add-Type -AssemblyName PresentationFramework
$main = New-Object System.Windows.Window
$main.Title = 'MCP test main'
$main.Width = 900; $main.Height = 700
$main.Show()
$dialog = New-Object System.Windows.Window
$dialog.Title = 'MCP test information'
$dialog.Width = 360; $dialog.Height = 200
$dialog.Owner = $main
$panel = New-Object System.Windows.Controls.StackPanel
$label = New-Object System.Windows.Controls.TextBlock
$label.Text = 'Test boundary message: 境界線'
$button = New-Object System.Windows.Controls.Button
$button.Content = 'OK'
$button.Add_Click({ $dialog.Close() })
[void]$panel.Children.Add($label)
[void]$panel.Children.Add($button)
$dialog.Content = $panel
[void]$dialog.ShowDialog()
$main.Close()
"""


@pytest.mark.skipif(not winui._has_interactive_desktop(), reason="interactive Windows desktop required")
def test_own_fake_dialog_list_screenshot_and_invoke(tmp_path, monkeypatch):
    # Never target an existing app. Only this Popen PID is selected or terminated.
    command = base64.b64encode(_FAKE_DIALOG.encode("utf-16-le")).decode("ascii")
    process = subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-EncodedCommand", command],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        monkeypatch.delenv("MD_MCP_PID", raising=False)
        monkeypatch.setenv("MD_MCP_PROCESS", "PoWeRsHeLl*.ExE")
        assert process.pid in winui.process_ids()
        monkeypatch.setenv("MD_MCP_PID", str(process.pid))
        deadline = time.monotonic() + 20
        found = []
        while time.monotonic() < deadline and process.poll() is None:
            _, found = winui.classify_windows(winui.list_windows())
            if found:
                break
            time.sleep(0.1)
        assert found, "Own fake dialog did not appear"
        response = server.md_dialogs()
        assert response["ok"] and len(response["dialogs"]) == 1
        dialog = response["dialogs"][0]
        assert dialog["pid"] == process.pid
        assert "uia_error" not in dialog, dialog
        assert "Test boundary message: 境界線" in dialog["text"]
        assert "OK" in dialog["buttons"]
        response = server.screenshot(str(tmp_path / "fake.png"), "all")
        assert response["ok"], response
        assert len(response["screenshots"]) == 2
        for shot in response["screenshots"]:
            data = Path(shot["path"]).read_bytes()
            assert len(data) > 100 and data[:8] == b"\x89PNG\r\n\x1a\n"
            assert struct.unpack_from("!II", data, 16) == (shot["width"], shot["height"])
            assert shot["pid"] == process.pid
        response = server.dismiss_dialog(hwnd=dialog["hwnd"])
        assert response["ok"], response
        assert response["pid"] == process.pid
        process.wait(timeout=10)
        assert process.returncode == 0
        assert winui.list_windows(pid=process.pid) == []
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=10)
