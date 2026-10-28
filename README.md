# marvelous-designer-mcp

> Hardened fork of [ysk424/marvelous-designer-mcp](https://github.com/ysk424/marvelous-designer-mcp)
> (MIT). All credit for the original design and implementation goes to its author,
> ysk424. This fork adds token authentication, an exclusive port bind, request
> limits, a safer installer and tests; see [Security](#security).

A [Model Context Protocol](https://modelcontextprotocol.io/) server that lets an
LLM (e.g. Claude) drive **Marvelous Designer** (and CLO, which shares the same
Python API) by running code inside MD's embedded Python interpreter.

It does **not** need the C++ SDK, a plugin build, or a CLO-SET API key — just MD's
built-in Python Editor.

## How it works

```
LLM  ──MCP(stdio)──▶  MCP server (this repo, FastMCP)
                          │  JSON-per-line over TCP 127.0.0.1:7421
                          ▼
                      md_listener.py  (you paste it into MD's Python Editor)
                          │  exec() in MD's interpreter
                          ▼
                      MD Python API:  import_api / export_api / fabric_api /
                                      pattern_api / utility_api / ...
```

MD's embedded Python (3.11) does **not** schedule background threads, so the
listener is a plain blocking accept loop that runs on MD's GUI thread. **While the
listener is running, MD's window is unresponsive** — that's expected. Stop it with
the `shutdown_listener` tool (or by closing MD).

## Requirements

- Marvelous Designer 2026 (or CLO 2026) — `Plugins ▸ Python Editor` must be available
- Python 3.10+ and [`uv`](https://docs.astral.sh/uv/) on the machine running the MCP server
- The MCP server and MD run on the **same machine** (the listener binds `127.0.0.1`)

## Setup

```powershell
git clone https://github.com/mackatwentytsuru/marvelous-designer-mcp.git
cd marvelous-designer-mcp
uv sync
```

## Running

### 1. Start the listener inside Marvelous Designer

`scripts/md_start_listener.py` starts a blocking socket listener on
`127.0.0.1:7421`. **MD's GUI freezes while it runs — that's the ready state.**
Stop it later with the `shutdown_listener` tool (or by closing MD). See
[Why does MD freeze?](#why-does-md-freeze-while-the-listener-runs) below.

**Option C — auto-register (one shot, recommended).** Run

```powershell
uv run python scripts/install_md_plugin.py
```

once. It writes the launcher into MD's `pluginSettings.json`, so on the next MD
launch `Plugins ▸ Plug-in ▸ md_start_listener` is there waiting to be clicked.

**Option B — register manually.** `Plugins ▸ Plug-in Manager ▸ +ADD`, pick
`scripts/md_start_listener.py`. Same outcome as Option C, just hand-driven.

**Option A — paste into the Python Editor.** Open `Plugins ▸ Python Editor`, paste
**the whole contents of `scripts/md_start_listener.py`**, run it. (Paste it in
one go from a text editor — pasting line by line can mangle indentation.) Good
for development; the editor's console shows prints.

Either way, status and errors are appended to `~\md_mcp_listener.log` (handy in
plugin mode, where stdout may not be visible).

### 2. Start the MCP server

```powershell
uv run python -m marvelous_designer_mcp
```

Or let Claude Desktop launch it (below).

### 3. Claude Desktop config

Add to `claude_desktop_config.json`
(`%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "marvelous-designer": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "C:\\Users\\you\\git\\marvelous-designer-mcp",
        "python",
        "-m",
        "marvelous_designer_mcp"
      ]
    }
  }
}
```

Restart Claude Desktop. The MD listener must already be running (step 1) for the
tools to work.

## Tools

| Tool | What it does |
|---|---|
| `ping` | Check the listener is reachable. |
| `execute_python(code)` | Run arbitrary Python in MD's interpreter. `import` the `*_api` modules; bind your return value to a name called `result`. Returns `{stdout, stderr, result, error}`. |
| `shutdown_listener()` | Stop the listener loop and release the MD GUI. |
| `scene_info()` | Project name/path, MD version, pattern & fabric counts. |
| `list_patterns()` | Pattern pieces: index, name, assigned fabric index. |
| `list_fabrics()` | Fabrics: index, name (+ fabric-style list). |
| `assign_fabric(fabric_index, pattern_index, face=2)` | `fabric_api.AssignFabricToPattern`. |
| `import_project(path)` | Open a `.zprj` / `.zpac` / `.obj` / `.fbx` / ... by absolute path (`import_api.ImportFile`). |
| `export_project(path)` | Save the scene as a `.zprj` (`export_api.ExportZPrjW`). |
| `simulate(steps=1, quality=None, mode=None)` | Simulate frames; optionally set quality and CPU/GPU mode first. |
| `md_api(module, contains="")` | List a module's functions; substring-filtered. Discover signatures by calling a function with wrong args and reading the `TypeError`. |

Anything not covered by a wrapper: use `execute_python` directly.

## Building garments from code

Build an OBJ in its intended 3D placement around the avatar, including pieces
behind or wrapped around the body. Use `import_obj(path, kind="garment")` with
`trace_uv_patterns=True` and `auto_translate=False` (the defaults). Each UV
island becomes a 2D pattern, the mesh retains its 3D shape, and pattern order
follows OBJ group order. MD rescales the 2D patterns to the 3D lengths. This
3D-first workflow can place pieces when the avatar has no arrangement points.
`kind` also accepts `avatar` and `trim`; `scale` defaults to 1.0. Inspect the
returned pattern/avatar counts before and after import: `ImportOBJ` may return
False even when it imported successfully.

Use `pattern_lines(pattern_index)` to inspect line indices, lengths, locations,
endpoints and the bounding box. `add_seam(pattern_a, line_a, pattern_b, line_b)`
sews whole lines through `AddSeamlinePairGroup` (bool result). Supply both
`ratio_a=[start, end]` and `ratio_b=[start, end]` for
`AddSeamlinePairGroupPartial` (0 means success). Ratios run from each line's
start and must satisfy `0 <= start < end <= 1`; `dir_a` and `dir_b` default to True.

Every edit remeshes the whole garment. In a measurement with 72 small pieces,
one seam took 10 s at 6 mm particle distance and 0.95 s at 60 mm, so set the
fine distance once at the end rather than before sewing. A coarse distance
(`set_particle_distance(60)`) makes sewing fast, but re-meshing coarser than the
pieces' detail throws away the imported 3D shape (thin pre-folded pleats came
back ballooned); for 3D-first imports keep the import distance (20 mm) while
sewing. Pass `pattern_index` to change one piece. Deleting many sewn patterns
one by one took over 10 minutes; `new_project()` is fast. It refuses unsaved
changes unless `save_unsaved_to` is supplied, saves first with
`ExportZPrj(path, False)`, and refuses to reset if changes remain unsaved.

`simulate(steps=1, quality=None, mode=None)` counts steps as frames. Quality is
0 normal, 1 animation, 2 fitting or 3 fast; mode is 0 CPU or 1 GPU. When either
setting is supplied, the omitted setting defaults to 0.

`render_view(path, view="front", width=800, height=800, fit_all=False)` writes a
PNG even while the listener freezes the GUI. Supported views are bottom,
front-right, front, front-left, right, top, left and back.

## When MD shows a dialog

An MD modal dialog blocks the listener until its button is clicked. On Windows,
use `md_dialogs()` to read dialog titles, text and button names, then
`dismiss_dialog(button="OK", index=0)` to invoke one button. These tools run in
the MCP server, outside MD, so they work independently of the blocked listener.
Qt buttons are read and invoked through UI Automation, without changing focus
or sending keys. Names must match exactly, including localized button names.
Timeout errors also include a best-effort dialog diagnostic.

Detection uses a heuristic: the largest visible non-Python-editor top-level
window per process is the main window. Owned windows and windows shown while the
main window is disabled are preferred as dialog candidates; area under half the
main window is a fallback. Small palettes can match and large unowned dialogs
can be missed. Python Editor titles and UIA labels are excluded; if UIA cannot
read a generic-title editor, it may still appear as a candidate with `uia_error`.
Indexes are ordered by PID/HWND and can change: inspect before
invoking. No dialogs are dismissed automatically. Recurring meshing errors may
reappear even while idle; fix the garment rather than repeatedly dismissing them.
A timeout does not cancel the operation already running in MD.

`screenshot(path, which="main", overwrite=False)` captures physical-pixel PNGs; `which` also accepts
`"dialog"` or `"all"`. Multiple windows produce files named with the supplied
stem plus PID and window handle. Paths starting with a backslash are rejected;
existing files require `overwrite=True`. Minimized windows are rejected. A bounded
overall time budget limits multi-window captures. The parent directory must exist. PrintWindow
with PW_RENDERFULLCONTENT captures background windows; a bounded helper falls
back to screen-copying the window rectangle on failure or timeout. The returned
method indicates the fallback, which may include occluding windows. GPU surfaces
may be blank even when PrintWindow reports success. Windows UI tools require an
interactive desktop and appropriate process permissions; on other OSes they
return a clear not-supported error. No additional Python dependencies are used.

During an authenticated request the listener writes `busy.json` beside its token
file, containing only method, request ID and UTC start time. It removes the file
when the request ends, including errors. Bridge timeout errors include this busy
status when available; a timeout does not cancel the running operation.

## Why does MD freeze while the listener runs?

MD's embedded Python (3.11) doesn't give CPU to background threads — a daemon
thread spawned from a script is `is_alive() == True` but never actually
executes. So the socket server has to run on whatever thread the Python Editor
uses, which is MD's GUI thread. Empirically (verified live), API calls work
fine while the GUI is frozen because they are synchronous main-thread calls;
the GUI just doesn't repaint or accept input until the listener returns.

A C++ plugin could avoid the freeze, but **MD's loader scans only `.py` files**
and ignores `.dll`s entirely (verified with a zero-dep probe DLL), so the unused
C++ plugin was removed from this fork.

## Caveats

- **MD freezes while the listener runs.** Use `shutdown_listener` when you want the
  GUI back. For an LLM-driven workflow this is usually fine — Claude does the work.
- **Modal-dialog deadlock.** Any API call that pops a modal dialog (unsaved-changes
  prompt, error popup, file picker) hangs the listener forever, because MD's GUI
  thread is blocked. On Windows inspect with `md_dialogs`, then invoke the
  appropriate button with `dismiss_dialog`. The wrappers pick dialog-free call
  variants where possible.
- **Heavy ops are slow.** Exporting a large `.zprj` (e.g. with an embedded Alembic
  animation), simulation and rendering can take a long time; the bridge timeout
  defaults to 120s — raise `MD_MCP_TIMEOUT` (seconds) for very long renders.
- Generated MD code must `import` the `*_api` module(s) it uses (they are not
  globals) and bind output to `result`. The API is pybind11-based.

## Config (environment variables)

| Var | Default | Meaning |
|---|---|---|
| `MD_MCP_HOST` | `127.0.0.1` | listener host the bridge connects to |
| `MD_MCP_PORT` | `7421` | listener port |
| `MD_MCP_TIMEOUT` | `120.0` | bridge socket timeout, seconds |
| `MD_MCP_PROCESS` | `MarvelousDesigner*.exe` | Windows UI executable-name wildcard (case insensitive) |
| `MD_MCP_PID` | unset | explicit positive Windows process ID; selected only when its executable matches `MD_MCP_PROCESS` |

`MD_MCP_TOKEN_FILE` — token file path (both sides). The listener's host/port are
constants in `md_addon/md_listener.py` — keep them in sync if you change the defaults.

## Security

This server **executes arbitrary Python inside Marvelous Designer by design**
(`execute_python`). Anything that can talk to the listener has full control of
MD and of your user account's files.

- **Localhost only.** The listener binds `127.0.0.1`, never a network interface.
- **Token on every request.** When the listener starts it generates a random
  token (`secrets.token_urlsafe`) and writes it to
  `%LOCALAPPDATA%\marvelous-designer-mcp\token` (override with
  `MD_MCP_TOKEN_FILE`; on Windows the file is restricted to the current user with
  `icacls`). Every request, including `shutdown`, must carry it; it is compared
  with `hmac.compare_digest`. The MCP server reads the same file, so both must run
  as the same user. The file is deleted when the listener stops, and a new token is
  made on each start.
- **Exclusive port.** On Windows the listener uses `SO_EXCLUSIVEADDRUSE`, so no
  other process can take over port 7421. Starting a second listener fails with
  "already running" and leaves the running one's token untouched.
- **Limits.** 5 s receive timeout per connection, 16 MB request cap, one request
  per connection. `sys.exit()` or any other `BaseException` in user code is caught
  and returned as an error instead of stopping the listener.
- Do not expose port 7421 through tunnels or port forwarding, and do not run the
  listener on a shared machine you do not trust.

## Windows notes

- `Plugins > Python Editor` (Japanese UI: プラグイン > Pythonエディタ) is where you
  paste and run `scripts/md_start_listener.py`. A pasted copy has no `__file__`:
  set the environment variable `MD_MCP_ADDON_DIR` to this repo's `md_addon` folder
  (or fill in `_FALLBACK_ADDON_DIR` at the top of the script) first.
- `scripts/install_md_plugin.py` edits
  `C:\Users\Public\Documents\MarvelousDesigner\Plugins\pluginSettings.json`.
  It makes a timestamped `.bak-*` copy first, writes atomically, keeps other
  plugins, and prints what it did. `--dry-run` shows the change only.
- The launcher log is `%USERPROFILE%\md_mcp_listener.log`.
- Tests: `uv sync --group dev`, then `uv run pytest`.

## Uninstall

There's nothing installed inside MD — just stop the listener (`shutdown_listener`
or close MD) and remove the Claude Desktop config entry. Delete the repo to remove
the rest.

## License

MIT — see `LICENSE`.
