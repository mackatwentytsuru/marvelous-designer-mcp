from mcp.server.fastmcp import FastMCP

from . import bridge
from .config import MD_HOST, MD_PORT

mcp = FastMCP("marvelous-designer")


def _md_exec(code: str) -> dict:
    """Run `code` inside MD via execute_python and flatten the listener's envelope.

    The listener returns {stdout, stderr, result, error}; this returns
    {"ok": True, "result": ...} or {"ok": False, "error": ...} (plus "stdout" if any).
    """
    try:
        resp = bridge.call("execute_python", {"code": code})
    except bridge.BridgeError as e:
        return {"ok": False, "error": f"bridge: {e}"}
    if not isinstance(resp, dict):
        return {"ok": True, "result": resp}
    out: dict = {"ok": False, "error": resp["error"]} if resp.get("error") else {"ok": True, "result": resp.get("result")}
    if resp.get("stdout"):
        out["stdout"] = resp["stdout"]
    return out


@mcp.tool()
def ping() -> dict:
    """Verify the MD listener is reachable. Returns whatever the listener echoes back."""
    try:
        return {"ok": True, "host": MD_HOST, "port": MD_PORT, "result": bridge.call("ping")}
    except bridge.BridgeError as e:
        return {"ok": False, "host": MD_HOST, "port": MD_PORT, "error": str(e)}


@mcp.tool()
def execute_python(code: str) -> dict:
    """Execute arbitrary Python inside Marvelous Designer's interpreter.

    MD's API is exposed as importable modules (import_api, export_api, fabric_api,
    pattern_api, utility_api, ...), NOT as globals — so `import` what you need.
    Bind the value you want back to a name called `result`. The API is pybind11-based,
    so calling a function with wrong args raises a TypeError that lists the accepted
    signatures — handy for discovery.

    Returns: {"stdout": str, "stderr": str, "result": any, "error": str|None}.
    """
    try:
        return bridge.call("execute_python", {"code": code})
    except bridge.BridgeError as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def shutdown_listener() -> dict:
    """Stop the MD-side listener's blocking loop and release the Marvelous Designer GUI."""
    try:
        return {"ok": True, "result": bridge.call("shutdown")}
    except bridge.BridgeError as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def scene_info() -> dict:
    """Summary of the current MD scene: project name/path, MD version, pattern & fabric counts."""
    return _md_exec(
        "import utility_api, pattern_api, fabric_api\n"
        "result = {\n"
        "    'project_name': utility_api.GetProjectName(),\n"
        "    'project_path': utility_api.GetProjectFilePath(),\n"
        "    'md_version': [utility_api.GetMajorVersion(), utility_api.GetMinorVersion(), utility_api.GetPatchVersion()],\n"
        "    'pattern_count': pattern_api.GetPatternCount(),\n"
        "    'fabric_count': fabric_api.GetFabricCount(True),\n"
        "    'fabric_styles': fabric_api.GetFabricStyleNameList(),\n"
        "}\n"
    )


@mcp.tool()
def list_patterns() -> dict:
    """List pattern pieces in the current scene: index, name, assigned fabric index."""
    return _md_exec(
        "import pattern_api\n"
        "result = [\n"
        "    {'index': i, 'name': pattern_api.GetPatternPieceName(i), 'fabric_index': pattern_api.GetPatternPieceFabricIndex(i)}\n"
        "    for i in range(pattern_api.GetPatternCount())\n"
        "]\n"
    )


@mcp.tool()
def list_fabrics() -> dict:
    """List fabrics in the current scene: index and name (plus the fabric-style name list)."""
    return _md_exec(
        "import fabric_api\n"
        "result = {\n"
        "    'fabrics': [{'index': i, 'name': fabric_api.GetFabricName(i)} for i in range(fabric_api.GetFabricCount(True))],\n"
        "    'styles': fabric_api.GetFabricStyleNameList(),\n"
        "}\n"
    )


@mcp.tool()
def assign_fabric(fabric_index: int, pattern_index: int, face: int = 2) -> dict:
    """Assign a fabric to a pattern piece via fabric_api.AssignFabricToPattern(fabric, pattern, face).

    `face` is the MD third int argument (commonly 0=front, 1=back, 2=both); default 2.
    On a signature mismatch the raw TypeError is returned so the real meaning can be found.
    """
    code = (
        "import fabric_api\n"
        f"fi, pi, fc = {int(fabric_index)}, {int(pattern_index)}, {int(face)}\n"
        "try:\n"
        "    result = {'ok': bool(fabric_api.AssignFabricToPattern(fi, pi, fc))}\n"
        "except TypeError as e:\n"
        "    result = {'ok': False, 'signature_error': str(e)}\n"
    )
    return _md_exec(code)


@mcp.tool()
def import_project(path: str) -> dict:
    """Open an MD project / garment / mesh file (.zprj, .zpac, .obj, .fbx, ...) by absolute path.

    Uses the generic import_api.ImportFile (dispatches by extension); the *W variant
    handles non-ASCII Windows paths. Returns which call ran and its bool result.

    Caveat: if MD raises a modal dialog (e.g. "save current project?"), the listener
    deadlocks because the GUI thread is stuck in our accept loop — close MD to recover.
    """
    code = (
        "import import_api\n"
        f"path = {path!r}\n"
        "result = None\n"
        "for name in ('ImportFileW', 'ImportFile'):\n"
        "    fn = getattr(import_api, name, None)\n"
        "    if fn is None:\n"
        "        continue\n"
        "    try:\n"
        "        result = {'ok': bool(fn(path)), 'used': name, 'path': path}\n"
        "        break\n"
        "    except TypeError as e:\n"
        "        result = {'ok': False, 'signature_error': str(e), 'tried': name}\n"
        "if result is None:\n"
        "    result = {'ok': False, 'error': 'no ImportFile variant in import_api', 'path': path}\n"
    )
    return _md_exec(code)


@mcp.tool()
def export_project(path: str) -> dict:
    """Save the current scene as a .zprj project file at the given absolute path.

    Uses export_api.ExportZPrjW(path, False) (the bool is the MD second arg; False to
    avoid any dialog), falling back to ExportZPrj(path). Returns the path string MD
    reports, or the raw signature error if the call shape was wrong.
    """
    code = (
        "import export_api\n"
        f"path = {path!r}\n"
        "result = None\n"
        "fnW = getattr(export_api, 'ExportZPrjW', None)\n"
        "if fnW is not None:\n"
        "    try:\n"
        "        result = {'ok': True, 'used': 'ExportZPrjW', 'returned': fnW(path, False)}\n"
        "    except TypeError as e:\n"
        "        result = {'ok': False, 'signature_error': str(e), 'tried': 'ExportZPrjW'}\n"
        "if result is None or not result.get('ok'):\n"
        "    fn = getattr(export_api, 'ExportZPrj', None)\n"
        "    if fn is not None:\n"
        "        try:\n"
        "            result = {'ok': True, 'used': 'ExportZPrj', 'returned': fn(path)}\n"
        "        except TypeError as e:\n"
        "            result = {'ok': False, 'signature_error': str(e), 'tried': 'ExportZPrj'}\n"
        "if result is None:\n"
        "    result = {'ok': False, 'error': 'no ExportZPrj variant in export_api', 'path': path}\n"
    )
    return _md_exec(code)


@mcp.tool()
def import_obj(path: str, kind: str = "garment", scale: float = 1.0,
               trace_uv_patterns: bool = True, auto_translate: bool = False, add: bool = True) -> dict:
    """Import OBJ; UV islands become patterns with the mesh's 3D placement and group order.

    MD rescales 2D patterns to 3D lengths. ImportOBJ can return False after importing,
    so inspect before/after counts. Modal dialogs can block the listener, so arrangement
    points are not requested (an avatar MD cannot fit them on raises an info box).
    `add` keeps what is already in the scene.
    """
    kinds = {"avatar": 0, "trim": 1, "garment": 2}
    if kind not in kinds:
        return {"ok": False, "error": "kind must be avatar, trim, or garment"}
    code = (
        "import import_api, export_api, pattern_api, ApiTypes\n"
        "option = ApiTypes.ImportExportOption()\n"
        f"option.ImportObjectType = {kinds[kind]!r}\n"
        f"option.scale = {scale!r}\n"
        f"option.bTrace2DPatternsUVMap = {trace_uv_patterns!r}\n"
        f"option.bAutoTranslate = {auto_translate!r}\n"
        f"option.bAdd = {add!r}\n"
        "option.bAddArrangementPoints = False\n"
        "before = {'patterns': pattern_api.GetPatternCount(), 'avatars': export_api.GetAvatarCount()}\n"
        f"import_api.ImportOBJ({path!r}, option)\n"
        "after = {'patterns': pattern_api.GetPatternCount(), 'avatars': export_api.GetAvatarCount()}\n"
        "result = {'before': before, 'after': after}\n"
    )
    return _md_exec(code)


@mcp.tool()
def render_view(path: str, view: str = "front", width: int = 800,
                height: int = 800, fit_all: bool = False) -> dict:
    """Render a standard view to PNG even while the listener freezes MD's GUI."""
    views = {"bottom": 0, "front-right": 1, "front": 2, "front-left": 3,
             "right": 4, "top": 5, "left": 6, "back": 8}
    if view not in views:
        return {"ok": False, "error": "unknown view; choose " + ", ".join(views)}
    return _md_exec(
        "import export_api\n"
        f"returned = export_api.ExportStdViewImage({views[view]!r}, {path!r}, 0, {width!r}, {height!r}, {fit_all!r})\n"
        f"result = {{'path': {path!r}, 'returned': returned}}\n"
    )


@mcp.tool()
def new_project(save_unsaved_to: str = "") -> dict:
    """Start a new project; refuse unsaved changes without a save path to avoid a modal dialog."""
    return _md_exec(
        "import utility_api, export_api\n"
        f"save_path = {save_unsaved_to!r}\n"
        "unsaved = utility_api.CheckZPRJForUnsavedChanges()\n"
        "if unsaved and not save_path:\n"
        "    result = {'ok': False, 'error': 'Unsaved changes: provide save_unsaved_to before starting a new project; a save dialog could block the listener.'}\n"
        "else:\n"
        "    if unsaved:\n"
        "        export_api.ExportZPrj(save_path, False)\n"
        "    if unsaved and utility_api.CheckZPRJForUnsavedChanges():\n"
        "        result = {'ok': False, 'error': 'Project still has unsaved changes after saving; new project refused.'}\n"
        "    else:\n"
        "        result = {'returned': utility_api.NewProject()}\n"
    )


@mcp.tool()
def pattern_lines(pattern_index: int) -> dict:
    """Return parsed GetPatternLineInfo JSON: lines, lengths, locations, endpoints and bounding box."""
    return _md_exec(
        "import json, pattern_api\n"
        f"result = json.loads(pattern_api.GetPatternLineInfo({pattern_index!r}))\n"
    )


@mcp.tool()
def add_seam(pattern_a: int, line_a: int, pattern_b: int, line_b: int,
             dir_a: bool = True, dir_b: bool = True,
             ratio_a: list[float] | None = None, ratio_b: list[float] | None = None) -> dict:
    """Sew whole lines (bool result), or partial lines (0 means success).

    Both partial ratios must be [start, end], measured from each line's start,
    with 0 <= start < end <= 1. Sew coarsely to avoid expensive remeshing per edit.
    """
    if (ratio_a is None) != (ratio_b is None):
        return {"ok": False, "error": "provide both ratio_a and ratio_b, or neither"}
    if ratio_a is not None:
        for ratio in (ratio_a, ratio_b):
            if (not isinstance(ratio, (list, tuple)) or len(ratio) != 2
                    or not all(isinstance(v, (int, float)) for v in ratio)
                    or not 0 <= ratio[0] < ratio[1] <= 1):
                return {"ok": False, "error": "ratios must be [start, end] with 0 <= start < end <= 1"}
        call = (f"pattern_api.AddSeamlinePairGroupPartial({pattern_a!r}, {line_a!r}, "
                f"{ratio_a[0]!r}, {ratio_a[1]!r}, {dir_a!r}, {pattern_b!r}, {line_b!r}, "
                f"{ratio_b[0]!r}, {ratio_b[1]!r}, {dir_b!r})")
        success = "returned == 0"
    else:
        call = (f"pattern_api.AddSeamlinePairGroup({pattern_a!r}, {line_a!r}, "
                f"{pattern_b!r}, {line_b!r}, {dir_a!r}, {dir_b!r})")
        success = "bool(returned)"
    return _md_exec(
        "import pattern_api\n"
        f"returned = {call}\n"
        f"result = {{'ok': {success}, 'returned': returned}}\n"
    )


@mcp.tool()
def set_particle_distance(mm: float, pattern_index: int | None = None) -> dict:
    """Set particle distance for all patterns or one. Each edit remeshes the garment.

    A seam on 72 small pieces took 10 s at 6 mm versus 0.95 s at 60 mm, so a coarse
    distance speeds up sewing. Caveat: re-meshing coarser than the pieces' detail loses
    the imported 3D shape (e.g. pre-folded pleats from import_obj); for those keep the
    import distance while sewing and only refine at the end.
    """
    if pattern_index is None:
        call = f"pattern_api.SetParticleDistanceOfPatterns({mm!r})"
    else:
        call = f"pattern_api.SetParticleDistanceOfPattern({pattern_index!r}, {mm!r})"
    return _md_exec("import pattern_api\n" + f"result = {{'returned': {call}}}\n")


@mcp.tool()
def simulate(steps: int = 1, quality: int | None = None, mode: int | None = None) -> dict:
    """Simulate frames, optionally setting quality (0 normal, 1 animation, 2 fitting, 3 fast).

    Mode is 0 CPU or 1 GPU. When only one setting is given, the other defaults to 0.
    """
    if quality is not None and quality not in (0, 1, 2, 3):
        return {"ok": False, "error": "quality must be 0, 1, 2, or 3"}
    if mode is not None and mode not in (0, 1):
        return {"ok": False, "error": "mode must be 0 (CPU) or 1 (GPU)"}
    code = "import utility_api\n"
    if quality is not None or mode is not None:
        code += f"utility_api.SetSimulationQuality({quality if quality is not None else 0!r}, {mode if mode is not None else 0!r})\n"
    code += (
        f"steps = {steps!r}\n"
        "try:\n"
        "    result = {'ok': True, 'returned': utility_api.Simulate(steps), 'steps': steps}\n"
        "except TypeError as e:\n"
        "    result = {'ok': False, 'signature_error': str(e)}\n"
    )
    return _md_exec(code)


@mcp.tool()
def md_api(module: str, contains: str = "") -> dict:
    """List the functions of an MD API module (import_api, export_api, fabric_api, pattern_api,
    utility_api, ...). `contains` filters names by case-insensitive substring.

    To learn a function's signature, call it via execute_python with wrong/no args — the
    TypeError lists the accepted argument types.
    """
    code = (
        "import importlib\n"
        f"mod = importlib.import_module({module!r})\n"
        f"sub = {contains.lower()!r}\n"
        "names = [n for n in dir(mod) if not n.startswith('_')]\n"
        "if sub:\n"
        "    names = [n for n in names if sub in n.lower()]\n"
        "result = sorted(names)\n"
    )
    return _md_exec(code)
