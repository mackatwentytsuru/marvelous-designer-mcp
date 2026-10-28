"""Windows UI access outside MD; no listener, focus changes or simulated keys.

Dialogs are a heuristic: owned windows and windows shown while the main window
is disabled are preferred; area is only a fallback. This can include palettes
and miss large dialogs. Indexes are snapshots, ordered by PID then HWND.
"""
from __future__ import annotations

import base64
import contextlib
import ctypes as C
from ctypes import wintypes as W
import fnmatch
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import zlib


class UIError(RuntimeError):
    pass


def _supported():
    if sys.platform != "win32":
        raise UIError("Windows UI tools are not supported on this OS")


def _has_interactive_desktop() -> bool:
    if sys.platform != "win32":
        return False
    user = C.WinDLL("user32", use_last_error=True)
    user.OpenInputDesktop.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    user.OpenInputDesktop.restype = W.HANDLE
    user.CloseDesktop.argtypes = [W.HANDLE]
    user.CloseDesktop.restype = W.BOOL
    desktop = user.OpenInputDesktop(0, False, 0x0100)
    if not desktop:
        return False
    user.CloseDesktop(desktop)
    return True


# Only this constant is executed. All caller data travels as JSON in an env var.
_UIA_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$request = $env:MD_MCP_UI_REQUEST | ConvertFrom-Json
$results = @()
foreach ($item in $request.windows) {
    try {
        $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr][long]$item.hwnd)
        if ($root.Current.ProcessId -ne [int]$item.pid) { throw 'Window process changed' }
        $title = $root.Current.Name
        $elements = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants,
            [System.Windows.Automation.Condition]::TrueCondition)
        $texts = @(); $buttons = @(); $target = $null
        foreach ($el in $elements) {
            $name = $el.Current.Name
            if ($el.Current.ControlType -eq [System.Windows.Automation.ControlType]::Text -and $name) {
                $texts += $name
            }
            if ($el.Current.ControlType -eq [System.Windows.Automation.ControlType]::Button) {
                $buttons += $name
                if ($name -ceq $request.button -and $el.Current.IsEnabled -and !$el.Current.IsOffscreen) {
                    $target = $el
                }
            }
        }
        if ($request.action -eq 'invoke') {
            if ($null -eq $target) { throw 'Named button not found or unavailable' }
            $pattern = $target.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
            $pattern.Invoke()
        }
        $results += @{hwnd=$item.hwnd; title=$title; texts=@($texts); buttons=@($buttons)}
    } catch {
        $results += @{hwnd=$item.hwnd; error=$_.Exception.Message}
    }
}
ConvertTo-Json -InputObject @($results) -Depth 6 -Compress
"""


def _parse_uia(output: str) -> list[dict]:
    try:
        data = json.loads(output.lstrip("\ufeff").strip())
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list) or any(not isinstance(x, dict) or "hwnd" not in x for x in data):
            raise ValueError("expected window records")
        for item in data:
            for key in ("texts", "buttons"):
                value = item.get(key, [])
                values = [value] if isinstance(value, str) else (value or [])
                if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                    raise ValueError(f"expected strings in {key}")
                item[key] = values
        return data
    except (ValueError, TypeError) as exc:
        raise UIError(f"Invalid UI Automation response: {exc}") from exc


def _uia(windows: list[dict], *, button: str | None = None, timeout=None) -> list[dict]:
    if timeout is None:
        try:
            timeout = float(os.environ.get("MD_MCP_UIA_TIMEOUT", "5"))
            if timeout <= 0:
                raise ValueError()
        except ValueError as exc:
            raise UIError("MD_MCP_UIA_TIMEOUT must be a positive number") from exc
    if button is not None and not button:
        raise UIError("Button name must not be empty")
    env = os.environ.copy()
    env["MD_MCP_UI_REQUEST"] = json.dumps({
        "windows": [{"hwnd": w["hwnd"], "pid": w["pid"]} for w in windows],
        "action": "read" if button is None else "invoke", "button": button,
    }, ensure_ascii=True)
    command = base64.b64encode(_UIA_SCRIPT.encode("utf-16-le")).decode("ascii")
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-OutputFormat", "Text", "-EncodedCommand", command],
            env=env, capture_output=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        extra = "; the click may have been delivered, so run md_dialogs again" if button is not None else "; the UI provider may be blocked"
        raise UIError(f"UI Automation timed out after {timeout:g}s{extra}") from exc
    except OSError as exc:
        raise UIError(f"UI Automation unavailable: {exc}") from exc
    if result.returncode:
        raise UIError(f"UI Automation failed: {(result.stderr or '')[:300].strip()}")
    return _parse_uia(result.stdout)


def _dlls():
    """Explicit pointer-sized signatures are essential on 64-bit Windows."""
    _supported()
    user = C.WinDLL("user32", use_last_error=True)
    gdi = C.WinDLL("gdi32", use_last_error=True)
    kernel = C.WinDLL("kernel32", use_last_error=True)
    signatures = [
        (user, "EnumWindows", [C.c_void_p, W.LPARAM], W.BOOL),
        (user, "IsWindowVisible", [W.HWND], W.BOOL),
        (user, "IsWindowEnabled", [W.HWND], W.BOOL),
        (user, "GetWindow", [W.HWND, W.UINT], W.HWND),
        (user, "IsIconic", [W.HWND], W.BOOL),
        (user, "GetWindowThreadProcessId", [W.HWND, C.POINTER(W.DWORD)], W.DWORD),
        (user, "GetWindowTextW", [W.HWND, W.LPWSTR, C.c_int], C.c_int),
        (user, "GetWindowRect", [W.HWND, C.POINTER(W.RECT)], W.BOOL),
        (user, "GetDC", [W.HWND], W.HDC),
        (user, "ReleaseDC", [W.HWND, W.HDC], C.c_int),
        (user, "PrintWindow", [W.HWND, W.HDC, W.UINT], W.BOOL),
        (user, "SetThreadDpiAwarenessContext", [C.c_void_p], C.c_void_p),
        (gdi, "CreateCompatibleDC", [W.HDC], W.HDC),
        (gdi, "CreateDIBSection", [W.HDC, C.c_void_p, W.UINT, C.POINTER(C.c_void_p), W.HANDLE, W.DWORD], W.HANDLE),
        (gdi, "SelectObject", [W.HDC, W.HANDLE], W.HANDLE),
        (gdi, "DeleteObject", [W.HANDLE], W.BOOL),
        (gdi, "DeleteDC", [W.HDC], W.BOOL),
        (gdi, "BitBlt", [W.HDC, C.c_int, C.c_int, C.c_int, C.c_int, W.HDC, C.c_int, C.c_int, W.DWORD], W.BOOL),
        (kernel, "CreateToolhelp32Snapshot", [W.DWORD, W.DWORD], W.HANDLE),
        (kernel, "Process32FirstW", [W.HANDLE, C.c_void_p], W.BOOL),
        (kernel, "Process32NextW", [W.HANDLE, C.c_void_p], W.BOOL),
        (kernel, "CloseHandle", [W.HANDLE], W.BOOL),
    ]
    for dll, name, args, restype in signatures:
        fn = getattr(dll, name)
        fn.argtypes, fn.restype = args, restype
    return user, gdi, kernel


@contextlib.contextmanager
def _physical_pixels(user):
    previous = user.SetThreadDpiAwarenessContext(C.c_void_p(-4))  # per-monitor v2
    if not previous:
        raise UIError("Cannot enable physical-pixel DPI awareness")
    try:
        yield
    finally:
        user.SetThreadDpiAwarenessContext(previous)


def _process_matches(executable: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(executable.casefold(), pattern.casefold())


def process_ids(pid: int | None = None) -> set[int]:
    _supported()
    explicit = pid if pid is not None else os.environ.get("MD_MCP_PID")
    if explicit is not None:
        try:
            value = int(explicit)
            if value <= 0:
                raise ValueError()
        except (ValueError, TypeError) as exc:
            raise UIError("MD_MCP_PID / pid must be a positive integer") from exc
    _, _, kernel = _dlls()

    class ProcessEntry(C.Structure):
        _fields_ = [("size", W.DWORD), ("usage", W.DWORD), ("pid", W.DWORD),
                    ("heap", C.c_size_t), ("module", W.DWORD), ("threads", W.DWORD),
                    ("parent", W.DWORD), ("priority", W.LONG), ("flags", W.DWORD),
                    ("exe", W.WCHAR * 260)]

    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == C.c_void_p(-1).value:
        raise C.WinError(C.get_last_error())
    found = set()
    pattern = os.environ.get("MD_MCP_PROCESS", "MarvelousDesigner*.exe").casefold()
    try:
        entry = ProcessEntry()
        entry.size = C.sizeof(entry)
        more = kernel.Process32FirstW(snapshot, C.byref(entry))
        while more:
            if _process_matches(entry.exe, pattern) and (explicit is None or entry.pid == value):
                found.add(entry.pid)
            more = kernel.Process32NextW(snapshot, C.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    if explicit is not None and not found:
        return set()
    return found


def list_windows(pid: int | None = None) -> list[dict]:
    pids = process_ids(pid)
    user, _, _ = _dlls()
    windows = []

    @C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    def visit(hwnd, _):
        owner = W.DWORD()
        user.GetWindowThreadProcessId(hwnd, C.byref(owner))
        if owner.value in pids and user.IsWindowVisible(hwnd):
            title = C.create_unicode_buffer(4096)
            user.GetWindowTextW(hwnd, title, len(title))
            rect = W.RECT()
            if user.GetWindowRect(hwnd, C.byref(rect)) and rect.right > rect.left and rect.bottom > rect.top:
                windows.append({"hwnd": hwnd, "pid": owner.value, "title": title.value,
                                "rect": [rect.left, rect.top, rect.right, rect.bottom],
                                "owner": user.GetWindow(hwnd, 4),
                                "enabled": bool(user.IsWindowEnabled(hwnd)),
                                "minimized": bool(user.IsIconic(hwnd))})
        return True

    with _physical_pixels(user):
        if not user.EnumWindows(visit, 0):
            raise C.WinError(C.get_last_error())
    return sorted(windows, key=lambda w: (w["pid"], w["hwnd"]))


def _area(window):
    left, top, right, bottom = window["rect"]
    return max(0, right - left) * max(0, bottom - top)


def _is_editor(text: str) -> bool:
    compact = "".join(text.casefold().split())
    return any("python" + suffix in compact for suffix in ("editor", "エディタ", "편집기"))


def classify_windows(windows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Largest non-editor window per PID is main; owned/disabled-owner windows lead.

    Python Editor / Pythonエディタ / Python 편집기 titles are excluded, including
    whitespace differences. Other small tool windows may be false positives.
    """
    candidates = [w for w in windows if not _is_editor(w["title"])]
    mains, dialogs = [], []
    for pid in sorted({w["pid"] for w in candidates}):
        group = [w for w in candidates if w["pid"] == pid]
        unowned = [w for w in group if not w.get("owner")]
        main = max(unowned or group, key=_area)
        mains.append(main)
        dialogs.extend(w for w in group if w["hwnd"] != main["hwnd"] and (
            bool(w.get("owner")) or main.get("enabled") is False
            or (0 < _area(w) < _area(main) * 0.5)
        ))
    return mains, sorted(dialogs, key=lambda w: (w["pid"], w["hwnd"]))


def dialogs(*, pid: int | None = None, timeout=None) -> list[dict]:
    _, found = classify_windows(list_windows(pid))
    if not found:
        return []
    try:
        records = {int(w["hwnd"]): w for w in _uia(found, timeout=timeout)}
    except UIError as exc:
        records = {w["hwnd"]: {"error": str(exc)} for w in found}
    result = []
    for window in found:
        info = records.get(window["hwnd"], {"error": "Window disappeared"})
        item = {**window, "title": info.get("title") or window["title"], "index": len(result),
                "texts": info.get("texts", []), "buttons": info.get("buttons", [])}
        item["text"] = "\n".join(dict.fromkeys([item["title"], *item["texts"], *item["buttons"]]))
        # Some Qt versions give the Python Editor the generic product title;
        # its editor label is only available through UIA, not GetWindowText.
        if any(_is_editor(text) for text in [item["title"], *item["texts"]]):
            continue
        if "error" in info:
            item["uia_error"] = info["error"]
        result.append(item)
    return result


def dismiss(button="OK", index=0, *, hwnd: int | None = None, pid: int | None = None) -> dict:
    if not isinstance(button, str) or not button:
        raise UIError("Button name must not be empty")
    found = dialogs(pid=pid)
    if hwnd is not None:
        matches = [w for w in found if int(w["hwnd"]) == int(hwnd)]
        if len(matches) != 1:
            raise UIError("Dialog HWND not found; call md_dialogs again")
        window = matches[0]
    elif index < 0 or index >= len(found):
        raise UIError("Dialog index out of range; call md_dialogs again")
    else:
        window = found[index]
    if button not in window.get("buttons", []):
        raise UIError("Named button not found; call md_dialogs again")
    records = _uia([window], button=button)
    if len(records) != 1 or records[0]["hwnd"] != window["hwnd"]:
        raise UIError("UI Automation returned no matching dialog")
    info = records[0]
    if "error" in info:
        raise UIError(info["error"])
    return {"hwnd": window["hwnd"], "pid": window["pid"], "button": button, "invoked": True}


def _png(width: int, height: int, bgra: bytes) -> bytes:
    rgb = bytearray(width * height * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    raw = b"".join(b"\0" + rgb[y * width * 3:(y + 1) * width * 3] for y in range(height))

    def chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!2I5B", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _capture(window: dict, path: str, screen=False) -> dict:
    user, gdi, _ = _dlls()
    hwnd = window["hwnd"]
    owner = W.DWORD()
    user.GetWindowThreadProcessId(hwnd, C.byref(owner))
    if owner.value != window["pid"] or not user.IsWindowVisible(hwnd):
        raise UIError("Window disappeared or process changed")
    if user.IsIconic(hwnd):
        raise UIError("Cannot screenshot a minimized window")
    with _physical_pixels(user):
        rect = W.RECT()
        if not user.GetWindowRect(hwnd, C.byref(rect)):
            raise C.WinError(C.get_last_error())
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width <= 0 or height <= 0 or width * height > 50_000_000:
            raise UIError("Invalid or excessive window dimensions")
        dc = user.GetDC(None)
        memory = bitmap = old = None
        try:
            if not dc:
                raise UIError("Cannot acquire screen DC")
            memory = gdi.CreateCompatibleDC(dc)
            if not memory:
                raise UIError("Cannot create capture DC")
            # BITMAPINFOHEADER, top-down 32-bit BI_RGB DIB (physical pixels).
            header = C.create_string_buffer(struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 32, 0, 0, 0, 0, 0, 0))
            bits = C.c_void_p()
            bitmap = gdi.CreateDIBSection(dc, header, 0, C.byref(bits), None, 0)
            if not bitmap or not bits.value:
                raise UIError("Cannot allocate capture bitmap")
            old = gdi.SelectObject(memory, bitmap)
            if not old or old == C.c_void_p(-1).value:
                raise UIError("Cannot select capture bitmap")
            printed = not screen and bool(user.PrintWindow(hwnd, memory, 2))  # PW_RENDERFULLCONTENT
            if not printed and not gdi.BitBlt(memory, 0, 0, width, height, dc, rect.left, rect.top, 0x40CC0020):
                raise UIError("PrintWindow and screen copy failed")
            Path(path).write_bytes(_png(width, height, C.string_at(bits, width * height * 4)))
            return {"path": path, "hwnd": hwnd, "pid": owner.value, "width": width, "height": height,
                    "method": "PrintWindow" if printed else "screen", "occlusion_possible": not printed}
        finally:
            if old and old != C.c_void_p(-1).value:
                gdi.SelectObject(memory, old)
            if bitmap:
                gdi.DeleteObject(bitmap)
            if memory:
                gdi.DeleteDC(memory)
            if dc:
                user.ReleaseDC(None, dc)


def _capture_bounded(window, path, *, timeout=5.0):
    # PrintWindow can hang on MD's blocked GUI thread. Kill only our helper on
    # timeout, then use a second helper for the screen fallback (no focus change).
    import time
    deadline = time.monotonic() + timeout
    for screen in (False, True):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise UIError("Window screenshot exceeded overall time budget")
        attempt_timeout = remaining / 2 if not screen else remaining
        try:
            response = subprocess.run(
                [sys.executable, "-I", str(Path(__file__).resolve())],
                input=json.dumps({"window": window, "path": str(path), "screen": screen}),
                capture_output=True, encoding="utf-8", errors="replace", timeout=attempt_timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            if not screen:
                continue
            raise UIError("Window screenshot exceeded overall time budget") from exc
        if response.returncode:
            raise UIError(f"Window screenshot failed: {(response.stderr or '')[:300].strip()}")
        return json.loads(response.stdout)


def screenshot(path: str, which="main", *, pid: int | None = None, overwrite=False, timeout=20.0) -> list[dict]:
    _supported()
    if which not in ("main", "dialog", "all"):
        raise UIError("which must be main, dialog, or all")
    mains, _ = classify_windows(list_windows(pid))
    found = dialogs(pid=pid) if which != "main" else []
    selected = mains if which == "main" else found if which == "dialog" else mains + found
    if not selected:
        raise UIError("No matching visible windows")
    if any(window.get("minimized") for window in selected):
        raise UIError("Cannot screenshot a minimized window")
    if str(path).startswith("\\"):
        raise UIError("Screenshot paths starting with backslash are not allowed")
    target = Path(path).resolve()
    if target.suffix.lower() != ".png":
        raise UIError("Screenshot path must end in .png")
    if timeout <= 0:
        raise UIError("Screenshot time budget must be positive")
    outputs = [target] if len(selected) == 1 else [target.with_name(f"{target.stem}-{w['pid']}-{w['hwnd']}.png") for w in selected]
    if not overwrite and any(output.exists() for output in outputs):
        raise UIError("Screenshot file already exists; pass overwrite=True to replace it")
    result = []
    import time
    deadline = time.monotonic() + timeout
    for window, output in zip(selected, outputs):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise UIError("Screenshots exceeded overall time budget")
        result.append(_capture_bounded(window, output, timeout=remaining))
    return result


if __name__ == "__main__":
    request = json.load(sys.stdin)
    print(json.dumps(_capture(**request)))
