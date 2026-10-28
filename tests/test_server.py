import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from marvelous_designer_mcp import server  # noqa: E402


@pytest.fixture
def captured(monkeypatch):
    codes = []

    def capture(code):
        compile(code, "<md>", "exec")
        codes.append(code)
        return {"ok": True}

    monkeypatch.setattr(server, "_md_exec", capture)
    return codes


@pytest.mark.parametrize("name,kwargs,calls", [
    ("import_obj", {"path": "mesh'\\file.obj"}, ["ImportExportOption()", "ImportOBJ(", "GetPatternCount()", "GetAvatarCount()", "ImportObjectType = 2", "bTrace2DPatternsUVMap = True", "bAutoTranslate = False"]),
    ("import_obj", {"path": "mesh.obj", "kind": "avatar", "scale": 2.0}, ["ImportObjectType = 0", "option.scale = 2.0"]),
    ("import_obj", {"path": "mesh.obj", "kind": "trim"}, ["ImportObjectType = 1"]),
    ("render_view", {"path": "view'\\image.png"}, ["ExportStdViewImage(2,", ", 0, 800, 800, False)"]),
    ("new_project", {}, ["CheckZPRJForUnsavedChanges()", "ExportZPrj(save_path, False)", "NewProject()"]),
    ("new_project", {"save_unsaved_to": "save'\\project.zprj"}, ["ExportZPrj(save_path, False)"]),
    ("pattern_lines", {"pattern_index": 3}, ["json.loads(pattern_api.GetPatternLineInfo(3))"]),
    ("add_seam", {"pattern_a": 0, "line_a": 1, "pattern_b": 2, "line_b": 3}, ["AddSeamlinePairGroup(0, 1, 2, 3, True, True)", "bool(returned)"]),
    ("add_seam", {"pattern_a": 0, "line_a": 1, "pattern_b": 2, "line_b": 3, "dir_b": False, "ratio_a": [0, 0.5], "ratio_b": [0.2, 1]}, ["AddSeamlinePairGroupPartial(0, 1, 0, 0.5, True, 2, 3, 0.2, 1, False)", "returned == 0"]),
    ("set_particle_distance", {"mm": 60.0}, ["SetParticleDistanceOfPatterns(60.0)"]),
    ("set_particle_distance", {"mm": 6.0, "pattern_index": 2}, ["SetParticleDistanceOfPattern(2, 6.0)"]),
    ("simulate", {}, ["Simulate(steps)", "steps = 1"]),
    ("simulate", {"steps": 10, "quality": 3, "mode": 1}, ["SetSimulationQuality(3, 1)", "steps = 10"]),
    ("simulate", {"mode": 1}, ["SetSimulationQuality(0, 1)"]),
    ("simulate", {"quality": 2}, ["SetSimulationQuality(2, 0)"]),
])
def test_generated_code(captured, name, kwargs, calls):
    assert getattr(server, name)(**kwargs) == {"ok": True}
    assert len(captured) == 1
    for call in calls:
        assert call in captured[0]
    if name == "simulate" and kwargs:
        assert captured[0].index("SetSimulationQuality(") < captured[0].index("Simulate(")


@pytest.mark.parametrize("view,index", [("bottom", 0), ("front-right", 1), ("front", 2),
    ("front-left", 3), ("right", 4), ("top", 5), ("left", 6), ("back", 8)])
def test_view_mapping(captured, view, index):
    server.render_view("image.png", view)
    assert f"ExportStdViewImage({index}," in captured[0]


@pytest.mark.parametrize("name,kwargs", [
    ("import_obj", {"path": "mesh.obj", "kind": "unknown"}),
    ("render_view", {"path": "image.png", "view": "unknown"}),
    ("simulate", {"quality": 4}),
    ("simulate", {"mode": 2}),
])
def test_invalid_inputs(captured, name, kwargs):
    assert getattr(server, name)(**kwargs)["ok"] is False
    assert not captured


@pytest.mark.parametrize("a,b", [([0, 1], None), (None, [0, 1]), ([-0.1, 1], [0, 1]),
    ([0, 1], [0, 1.1]), ([0.5, 0.5], [0, 1]), ([1, 0], [0, 1]),
    ([0], [0, 1]), ([0, 1, 2], [0, 1]), ("bad", [0, 1]),
    (["x", 1], [0, 1]), ([0, float("nan")], [0, 1])])
def test_invalid_ratios(captured, a, b):
    assert server.add_seam(0, 1, 2, 3, ratio_a=a, ratio_b=b)["ok"] is False
    assert not captured


@pytest.mark.parametrize("unsaved,path,saved,expected", [
    (False, "", False, ["new"]),
    (True, "", False, []),
    (True, "project.zprj", True, ["save", "new"]),
    (True, "project.zprj", False, ["save"]),
])
def test_new_project_saves_before_reset(captured, monkeypatch, unsaved, path, saved, expected):
    events = []
    state = {"unsaved": unsaved}

    def save(save_path, thumbnail):
        assert save_path == path and thumbnail is False
        events.append("save")
        state["unsaved"] = not saved

    monkeypatch.setitem(sys.modules, "export_api", SimpleNamespace(ExportZPrj=save))
    monkeypatch.setitem(sys.modules, "utility_api", SimpleNamespace(
        CheckZPRJForUnsavedChanges=lambda: state["unsaved"],
        NewProject=lambda: events.append("new")))
    server.new_project(path)
    scope = {}
    exec(captured[0], scope)
    assert events == expected
    if "new" not in expected:
        assert scope["result"]["ok"] is False
