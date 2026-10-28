"""Register the MCP listener with Marvelous Designer's plugin system.

Adds an entry to `pluginSettings.json` in MD's Plugins folder so that the
listener appears under `Plugins > Plug-in > md_start_listener` the next time MD
starts. Equivalent to `Plug-in Manager > +ADD`, just scripted.

Safety: the existing file is backed up first (timestamped copy next to it), the
new content is written atomically (temp file + replace), other plugins are kept,
and the change is printed. Use --dry-run to see the change without writing.

    uv run python scripts/install_md_plugin.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "md_start_listener.py"

if sys.platform == "win32":
    MD_PLUGINS = Path(r"C:\Users\Public\Documents\MarvelousDesigner\Plugins")
else:
    MD_PLUGINS = Path.home() / "Documents" / "MarvelousDesigner" / "Plugins"

SETTINGS = MD_PLUGINS / "pluginSettings.json"

ENTRY_TITLE = "md_start_listener"


def _norm(p: str) -> str:
    return p.replace("\\", "/").lower()


def merge_entry(data: dict, script_path: str) -> tuple[dict, str]:
    """Return (new_data, action) where action is 'added' or 'unchanged'. Never mutates `data`."""
    new = json.loads(json.dumps(data))
    plugins = new.setdefault("plugins", [])
    for existing in plugins:
        if _norm(existing.get("m_SourcePath", "")) == _norm(script_path):
            return new, "unchanged"
    plugins.append({
        "m_AddingPositionIndex": len(plugins) + 1,
        "m_BaseMenuTreeByObjectName": "Plugins / Plug-in",
        "m_PlugInFileName": script_path,
        "m_PlugInIconFileName": "",
        "m_PlugInTitle": ENTRY_TITLE,
        "m_SourcePath": script_path,
        "m_SourceType": "script",
    })
    return new, "added"


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    args = ap.parse_args(argv)

    if not SCRIPT.exists():
        print(f"error: launcher script not found: {SCRIPT}", file=sys.stderr)
        return 1
    if not MD_PLUGINS.exists():
        print(f"error: MD Plugins folder not found: {MD_PLUGINS}", file=sys.stderr)
        print("(Is Marvelous Designer installed and has it been launched at least once?)", file=sys.stderr)
        return 1

    script_path = str(SCRIPT).replace("\\", "/")

    if SETTINGS.exists():
        try:
            data = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"error: existing pluginSettings.json is not valid JSON ({e})", file=sys.stderr)
            print("Move it aside and rerun, or fix it by hand. Nothing was changed.", file=sys.stderr)
            return 1
    else:
        data = {"plugins": [], "version": 2}

    new_data, action = merge_entry(data, script_path)
    if action == "unchanged":
        print(f"already registered, nothing changed: {script_path}")
        print(f"  ({SETTINGS})")
        return 0

    print(f"will add plugin entry '{ENTRY_TITLE}' -> {script_path}")
    print(f"existing plugins kept: {len(data.get('plugins', []))}")
    if args.dry_run:
        print("dry run: nothing written")
        return 0

    if SETTINGS.exists():
        backup = SETTINGS.with_name(f"{SETTINGS.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(SETTINGS, backup)
        print(f"backup    : {backup}")
    write_atomic(SETTINGS, json.dumps(new_data, indent=1, ensure_ascii=False))

    print(f"updated   : {SETTINGS}")
    print()
    print("Next:")
    print("  1. Start (or restart) Marvelous Designer")
    print(f"  2. Click  Plugins > Plug-in > {ENTRY_TITLE}  to start the MCP listener")
    print("     (MD's GUI will freeze while the listener runs -- that's expected;")
    print("      the `shutdown_listener` MCP tool releases it.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
