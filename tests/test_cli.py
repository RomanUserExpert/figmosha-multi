"""CLI tests: argument parsing and the shapes the agent actually reads.

No bridge and no Figma here — everything below is pure argv/text handling,
which is exactly the part that used to have no tests and broke silently
(`figmosha --session X "<js>"` was an "invalid choice" error for a whole
release).

    pytest -q
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import figmosha  # noqa: E402


def parse(*argv):
    return figmosha.build_parser().parse_args(figmosha.normalize_argv(list(argv)))


# ─── the `figmosha "<js>"` shorthand ──────────────────────────────────────

def test_bare_code_becomes_exec():
    args = parse("return 1")
    assert args.cmd == "exec"
    assert args.code == "return 1"


def test_shorthand_survives_a_global_flag():
    args = parse("--session", "kite", "return 1")
    assert (args.cmd, args.session, args.code) == ("exec", "kite", "return 1")


def test_shorthand_survives_a_global_flag_written_with_equals():
    args = parse("--session=kite", "return 1")
    assert (args.cmd, args.session, args.code) == ("exec", "kite", "return 1")


def test_shorthand_survives_a_subcommand_flag():
    args = parse("-t", "5", "return 1")
    assert (args.cmd, args.timeout, args.code) == ("exec", 5, "return 1")


def test_shorthand_survives_flags_on_both_sides():
    args = parse("--port", "8800", "--raw", "return 1")
    assert (args.cmd, args.port, args.raw, args.code) == ("exec", 8800, True, "return 1")


def test_a_real_subcommand_is_left_alone():
    args = parse("--session", "ds", "tree", "page", "--depth", "2")
    assert (args.cmd, args.node_id, args.depth, args.session) == ("tree", "page", 2, "ds")


def test_a_global_flag_after_the_subcommand_still_lands_globally():
    # People write the flag where they think of it; both orders must work.
    args = parse("tree", "page", "--session", "ds")
    assert (args.cmd, args.node_id, args.session) == ("tree", "page", "ds")


def test_help_is_not_turned_into_code():
    with pytest.raises(SystemExit) as e:
        parse("--help")
    assert e.value.code == 0


def test_no_arguments_is_not_turned_into_code():
    args = parse()
    assert args.cmd is None


# ─── filters ──────────────────────────────────────────────────────────────

def test_find_accepts_a_value_containing_the_other_separator():
    args = parse("find", "page", "name~a=b")
    assert args.filter == "name~a=b"


# ─── output shaping ───────────────────────────────────────────────────────

def test_stack_is_cut_to_the_first_frames(capsys):
    stack = "\n".join(["Error: boom"] + [f"    at frame{i}" for i in range(10)])
    figmosha._emit({"ok": False, "error": "boom", "stack": stack})
    err = capsys.readouterr().err
    assert "frame1" in err
    assert "frame9" not in err
    assert "more frames" in err


# ─── the JS the CLI writes ────────────────────────────────────────────────
#
# These snippets never run in Python and never run in Figma during a test, so
# nothing else catches a typo in them: a missing brace shows up as a plugin-side
# SyntaxError, in the middle of somebody's work. Node runs them here instead,
# against a stub document, the same way the plugin does.

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")

STUB = """
const nodes = [
  {id:'1:2', name:'Btn', type:'FRAME', width:343.99996948242188, height:40,
   remove(){ removed.push(this.id); }},
  {id:'1:3', name:'Label', type:'TEXT', width:50, height:12,
   characters:'Привіт, це дуже довгий рядок тексту який треба вкоротити',
   remove(){ removed.push(this.id); }},
];
const removed = [];
const figma = {
  currentPage: {
    selection: nodes,
    findAll: (f) => nodes.filter(f),
    id: 'page', name: 'Page 1', type: 'PAGE',
  },
  getNodeByIdAsync: async (id) => nodes.find(n => n.id === id) || null,
};
const h = {
  resolve: async (x) => x === 'page' ? figma.currentPage
    : x === 'sel' ? figma.currentPage.selection[0]
    : await figma.getNodeByIdAsync(x),
};
"""


def run_js(code):
    """Run CLI-generated code the way the plugin does: as an async body."""
    program = (STUB + "(async () => {" + code + "})()"
               ".then(v => console.log(typeof v === 'string' ? v : JSON.stringify(v)))"
               ".catch(e => { console.error(e.message); process.exit(1); });")
    out = subprocess.run([NODE, "-e", program], capture_output=True, text=True,
                         encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def captured(monkeypatch):
    """Run a command without a bridge, keeping the code it would have sent."""
    seen = {}

    def fake_exec(code, timeout=60, want_value=False):
        seen["code"] = code
        seen["want_value"] = want_value
        return 200, {"ok": True, "result": "", "elapsed_ms": 1}

    monkeypatch.setattr(figmosha, "_exec", fake_exec)
    return seen


@needs_node
def test_sel_prints_one_row_per_node(monkeypatch):
    seen = captured(monkeypatch)
    figmosha.cmd_sel(parse("sel"))
    rows = run_js(seen["code"]).splitlines()
    assert rows[0] == "1:2  FRAME  344×40  Btn"
    assert rows[1].startswith('1:3  TEXT  50×12  Label  "Привіт')
    assert len(rows[1]) < 100, "text preview is not capped"
    assert seen["want_value"] is False


@needs_node
def test_find_counts_and_lists(monkeypatch):
    seen = captured(monkeypatch)
    figmosha.cmd_find(parse("find", "page", "type=TEXT"))
    out = run_js(seen["code"])
    assert out.splitlines()[0] == "1 found"
    assert "1:3  TEXT" in out


@needs_node
def test_find_with_no_matches_says_zero(monkeypatch):
    seen = captured(monkeypatch)
    figmosha.cmd_find(parse("find", "page", "name=nothing"))
    assert run_js(seen["code"]) == "0 found"


@needs_node
def test_raw_find_returns_structure(monkeypatch):
    seen = captured(monkeypatch)
    figmosha.cmd_find(parse("find", "page", "type=TEXT", "--raw"))
    assert seen["want_value"] is True
    assert json.loads(run_js(seen["code"]))[0]["id"] == "1:3"


@needs_node
def test_rm_sel_removes_the_whole_selection(monkeypatch):
    """`sel` is the first node everywhere else, but deleting only the first of
    three selected layers and reporting success is a silent wrong answer."""
    seen = captured(monkeypatch)
    figmosha.cmd_rm(parse("rm", "sel"))
    assert len(json.loads(run_js(seen["code"]))) == 2


@needs_node
def test_rm_reports_the_ids_it_could_not_find(monkeypatch):
    seen = captured(monkeypatch)
    figmosha.cmd_rm(parse("rm", "1:2", "9:9"))
    program = (STUB + "(async () => {" + seen["code"] + "})()"
               ".then(() => process.exit(1))"
               ".catch(e => console.log(e.message));")
    out = subprocess.run([NODE, "-e", program], capture_output=True, text=True,
                         encoding="utf-8")
    assert "9:9" in out.stdout and "removed 1" in out.stdout


# ─── doctor ───────────────────────────────────────────────────────────────

def test_doctor_reads_the_file_it_reports(monkeypatch, capsys):
    """doctor is the one caller that reads `value`, so it must ask for it.

    Without want_value the plugin skips the structured copy and doctor's last
    line degrades to «None» — page «None» — a diagnostic that lies while
    everything it diagnoses is fine.
    """
    asked = []

    def fake_request(method, path, payload=None, timeout=None):
        if path == "/status":
            return 200, {"plugin_connected": True, "pending": 0,
                         "project": "Proj", "sessions": 1}
        return 200, {"sessions": [{"sid": "s1", "file": "Proj DS", "page": "Page 1"}]}

    def fake_exec(code, timeout=60, want_value=False):
        asked.append(want_value)
        value = 2 if "1 + 1" in code else {"file": "Proj DS", "page": "Page 1", "pages": 3}
        return 200, {"ok": True, "value": value, "elapsed_ms": 4}

    monkeypatch.setattr(figmosha, "_request", fake_request)
    monkeypatch.setattr(figmosha, "_exec", fake_exec)

    assert figmosha.cmd_doctor(parse("doctor")) == 0
    out = capsys.readouterr().out
    assert "editing «Proj DS» — page «Page 1» of 3" in out
    assert asked == [True, True], "doctor must ask for the structured value"


# ─── props ────────────────────────────────────────────────────────────────

PROPS_STUB = """
const VARS = {
  'VariableID:1:1': {name: 'color/bg/surface'},
  'VariableID:1:2': {name: 'radius/lg'},
  'VariableID:1:3': {name: 'space/16'},
};
const STYLES = {'S:abc': {name: 'shadow/sm'}};
const card = {
  id: '1:2', name: 'Card', type: 'FRAME',
  width: 343.99996948242188, height: 192, x: 24, y: 48,
  parent: {name: 'Section'},
  visible: true, locked: false, rotation: 0, opacity: 1,
  blendMode: 'PASS_THROUGH', clipsContent: true,
  constraints: {horizontal: 'MIN', vertical: 'MIN'},
  layoutMode: 'VERTICAL', itemSpacing: 16,
  paddingTop: 24, paddingRight: 16, paddingBottom: 24, paddingLeft: 16,
  layoutSizingHorizontal: 'FILL', layoutSizingVertical: 'HUG',
  primaryAxisAlignItems: 'MIN', counterAxisAlignItems: 'CENTER',
  fills: [{type: 'SOLID', color: {r: 1, g: 1, b: 1}}],
  strokes: [{type: 'SOLID', color: {r: 0.9, g: 0.9, b: 0.9}}],
  strokeWeight: 1, strokeAlign: 'INSIDE',
  cornerRadius: 12, effectStyleId: 'S:abc',
  effects: [{type: 'DROP_SHADOW', offset: {x: 0, y: 2}, radius: 4,
             color: {r: 0, g: 0, b: 0, a: 0.08}}],
  children: [
    {id: '1:3', name: 'Title', type: 'TEXT', width: 311, height: 24,
     characters: 'Замовити консультацію',
     layoutSizingHorizontal: 'FILL', layoutSizingVertical: 'HUG'},
    {id: '1:4', name: 'Row', type: 'FRAME', width: 311, height: 48,
     layoutSizingHorizontal: 'FIXED', layoutSizingVertical: 'FIXED', layoutGrow: 1},
  ],
  boundVariables: {
    fills: [{type: 'VARIABLE_ALIAS', id: 'VariableID:1:1'}],
    cornerRadius: {type: 'VARIABLE_ALIAS', id: 'VariableID:1:2'},
    itemSpacing: {type: 'VARIABLE_ALIAS', id: 'VariableID:1:3'},
    paddingTop: {type: 'VARIABLE_ALIAS', id: 'VariableID:1:3'},
    paddingBottom: {type: 'VARIABLE_ALIAS', id: 'VariableID:1:3'},
  },
};
const figma = {
  currentPage: {selection: [card]},
  getNodeByIdAsync: async (id) => (id === '1:2' ? card : null),
  getStyleByIdAsync: async (id) => STYLES[id] || null,
  variables: {getVariableByIdAsync: async (id) => VARS[id] || null},
};
const h = {
  resolve: async (x) => x === 'sel' ? figma.currentPage.selection[0]
    : await figma.getNodeByIdAsync(x),
};
"""


def run_props(monkeypatch, *argv):
    seen = captured(monkeypatch)
    figmosha.cmd_props(parse("props", *argv))
    program = (PROPS_STUB + "(async () => {" + seen["code"] + "})()"
               ".then(v => console.log(v))"
               ".catch(e => { console.error(e.stack); process.exit(1); });")
    out = subprocess.run([NODE, "-e", program], capture_output=True, text=True,
                         encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


@needs_node
def test_props_resolves_bindings_to_token_names(monkeypatch):
    """The `→ name` is the point: ids would cost a second round trip."""
    out = run_props(monkeypatch, "1:2")
    assert "1:2  Card [FRAME]  344×192  @ 24,48  in «Section»" in out
    assert "fill     #FFFFFF → color/bg/surface" in out
    assert "radius   12 → radius/lg" in out
    assert "gap:16 → space/16" in out
    # Vertical padding is bound, horizontal is not — and the line says which
    # is which rather than trailing two arrows after both numbers.
    assert "pad:24 → space/16, 16 " in out
    assert "style    effect:shadow/sm" in out
    assert "stroke   1px #E6E6E6" in out
    assert "DROP_SHADOW 0,2 blur 4 #000000 8%" in out


@needs_node
def test_props_hides_defaults_but_all_shows_them(monkeypatch):
    out = run_props(monkeypatch, "1:2")
    assert "opacity" not in out, "opacity 1 is on every node and means nothing"
    assert "constr" not in out
    assert "flags" not in out

    out_all = run_props(monkeypatch, "1:2", "--all")
    assert "opacity  1" in out_all
    assert "constr   H:MIN  V:MIN" in out_all


@needs_node
def test_props_asks_figma_once_per_variable(monkeypatch):
    """Four bound corners must not be four lookups."""
    seen = captured(monkeypatch)
    figmosha.cmd_props(parse("props", "sel"))
    program = (PROPS_STUB.replace(
        "async (id) => VARS[id] || null",
        "async (id) => { calls.push(id); return VARS[id] || null; }")
        + "const calls = [];"
        + "(async () => {" + seen["code"] + "})()"
        ".then(() => console.log(JSON.stringify(calls)))"
        ".catch(e => { console.error(e.stack); process.exit(1); });")
    out = subprocess.run([NODE, "-e", program], capture_output=True, text=True,
                         encoding="utf-8")
    assert out.returncode == 0, out.stderr
    calls = json.loads(out.stdout)
    # space/16 is bound to three properties on this node; the cache turns that
    # into one lookup, and a padding bound to the same token prints once.
    assert sorted(calls) == ["VariableID:1:1", "VariableID:1:2", "VariableID:1:3"]


@needs_node
def test_props_lists_children_only_when_asked(monkeypatch):
    """The child's sizing is usually the answer to "why did this move"."""
    plain = run_props(monkeypatch, "1:2")
    assert "children 2" in plain
    assert "1:3" not in plain

    kids = run_props(monkeypatch, "1:2", "--children")
    assert "1:3  TEXT  311×24  H=FILL V=HUG  Title" in kids
    assert "1:4  FRAME  311×48  H=FIXED V=FIXED  grow:1  Row" in kids


# ─── vars / styles ────────────────────────────────────────────────────────

VARS_STUB = """
const VARS = {
  V1: {name: 'color/bg/surface', resolvedType: 'COLOR',
       valuesByMode: {m1: {r: 1, g: 1, b: 1}, m2: {r: 0.08, g: 0.09, b: 0.1}}},
  V2: {name: 'color/text/primary', resolvedType: 'COLOR',
       valuesByMode: {m1: {r: 0.12, g: 0.12, b: 0.12}, m2: {r: 1, g: 1, b: 1}}},
  V3: {name: 'space/16', resolvedType: 'FLOAT', valuesByMode: {m1: 16, m2: 16}},
  V4: {name: 'color/bg/card', resolvedType: 'COLOR',
       valuesByMode: {m1: {type: 'VARIABLE_ALIAS', id: 'V1'},
                      m2: {type: 'VARIABLE_ALIAS', id: 'V1'}}},
};
for (const id of Object.keys(VARS)) {
  VARS[id].id = id;
  VARS[id].variableCollectionId = 'C1';
}
const figma = {
  variables: {
    getLocalVariableCollectionsAsync: async () => [{
      id: 'C1',
      name: 'Core',
      modes: [{modeId: 'm1', name: 'Light'}, {modeId: 'm2', name: 'Dark'}],
      variableIds: ['V1', 'V2', 'V3', 'V4'],
    }],
    getLocalVariablesAsync: async (type) => Object.keys(VARS).map((k) => VARS[k])
      .filter((v) => !type || v.resolvedType === type),
    getVariableByIdAsync: async (id) => VARS[id] || null,
  },
  teamLibrary: {
    getAvailableLibraryVariableCollectionsAsync: async () =>
      [{key: 'k1', name: 'Tokens', libraryName: 'Acme DS'}],
    getVariablesInLibraryCollectionAsync: async () =>
      [{key: 'vk1', name: 'color/brand/primary', resolvedType: 'COLOR'},
       {key: 'vk2', name: 'space/8', resolvedType: 'FLOAT'}],
  },
};
const h = {};
"""

STYLES_STUB = """
const figma = {
  getLocalPaintStylesAsync: async () => [
    {name: 'color/brand/primary', paints: [{type: 'SOLID', color: {r: 0.12, g: 0.35, b: 0.94}}]},
  ],
  getLocalTextStylesAsync: async () => [
    {name: 'text/body/md', fontName: {family: 'Inter', style: 'Regular'},
     fontSize: 16, lineHeight: {unit: 'PIXELS', value: 24},
     letterSpacing: {unit: 'PERCENT', value: 0}},
  ],
  getLocalEffectStylesAsync: async () => [
    {name: 'shadow/sm', effects: [{type: 'DROP_SHADOW'}]},
  ],
  getLocalGridStylesAsync: async () => [],
};
const h = {};
"""


def run_with(stub, monkeypatch, cmd, *argv):
    seen = captured(monkeypatch)
    cmd(parse(*argv))
    program = (stub + "(async () => {" + seen["code"] + "})()"
               ".then(v => console.log(v))"
               ".catch(e => { console.error(e.stack); process.exit(1); });")
    out = subprocess.run([NODE, "-e", program], capture_output=True, text=True,
                         encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


@needs_node
def test_vars_without_a_filter_maps_instead_of_dumping(monkeypatch):
    """A design system is hundreds of variables; the map is what fits."""
    out = run_with(VARS_STUB, monkeypatch, figmosha.cmd_vars, "vars")
    assert "Core  modes: Light, Dark  ·  4 of 4" in out
    assert "color/ 3   space/ 1" in out
    assert "#FFFFFF" not in out, "values are for a filtered query, not the map"


@needs_node
def test_vars_with_a_filter_shows_a_column_per_mode(monkeypatch):
    out = run_with(VARS_STUB, monkeypatch, figmosha.cmd_vars, "vars", "color/bg")
    assert "2 matching of 4" in out
    assert "color/bg/surface" in out and "#FFFFFF   #14171A" in out
    # An alias must read as the token it points at, not as an id.
    assert "→ color/bg/surface   → color/bg/surface" in out


@needs_node
def test_vars_skips_collections_with_nothing_matching(monkeypatch):
    """A file has as many empty headers as it has collections."""
    out = run_with(VARS_STUB, monkeypatch, figmosha.cmd_vars, "vars", "nothing-here")
    assert "Core" not in out

    out = run_with(VARS_STUB, monkeypatch, figmosha.cmd_vars, "vars")
    assert "Core" in out, "the map still lists every collection"


@needs_node
def test_vars_sorts_a_scale_the_way_it_reads(monkeypatch):
    """Figma hands them back in creation order: sp-1, sp-10, sp-0."""
    stub = VARS_STUB.replace("'space/16'", "'space/2'").replace(
        "V3: {name: 'space/2'",
        "V5: {name: 'space/10', resolvedType: 'FLOAT',"
        " valuesByMode: {m1: 10, m2: 10}}, V3: {name: 'space/2'")
    out = run_with(stub, monkeypatch, figmosha.cmd_vars, "vars", "space/")
    assert out.index("space/2") < out.index("space/10")


@needs_node
def test_vars_can_filter_by_type(monkeypatch):
    out = run_with(VARS_STUB, monkeypatch, figmosha.cmd_vars, "vars", "--type", "float")
    assert "space/16" in out and "16   16" in out
    assert "color/" not in out
    assert "1 matching of 4" in out


@needs_node
def test_vars_library_lists_collections_then_variables(monkeypatch):
    out = run_with(VARS_STUB, monkeypatch, figmosha.cmd_vars, "vars", "--library")
    assert "Acme DS / Tokens  k1" in out
    assert "color/brand/primary" not in out, "collections first; variables cost a call each"

    out = run_with(VARS_STUB, monkeypatch, figmosha.cmd_vars, "vars", "--library", "brand")
    assert "color/brand/primary  [COLOR]  vk2" not in out
    assert "color/brand/primary  [COLOR]  vk1" in out


@needs_node
def test_styles_lists_each_kind_with_its_value(monkeypatch):
    out = run_with(STYLES_STUB, monkeypatch, figmosha.cmd_styles, "styles")
    assert "paint  1" in out and "color/brand/primary" in out and "#1F59F0" in out
    assert "text  1" in out and "Inter Regular  16/24" in out
    assert "effect  1" in out and "DROP_SHADOW" in out
    assert "grid" not in out, "empty kinds are not printed"


@needs_node
def test_props_prints_bare_padding_without_room_for_arrows(monkeypatch):
    stub = PROPS_STUB.replace(
        "paddingTop: {type: 'VARIABLE_ALIAS', id: 'VariableID:1:3'},", "").replace(
        "paddingBottom: {type: 'VARIABLE_ALIAS', id: 'VariableID:1:3'},", "")
    seen = captured(monkeypatch)
    figmosha.cmd_props(parse("props", "1:2"))
    program = (stub + "(async () => {" + seen["code"] + "})()"
               ".then(v => console.log(v));")
    out = subprocess.run([NODE, "-e", program], capture_output=True, text=True,
                         encoding="utf-8")
    assert "pad:24,16 " in out.stdout


@needs_node
def test_props_names_the_token_per_padding_side(monkeypatch):
    """`pad:6,8 → a → b` does not say which number is which."""
    stub = PROPS_STUB.replace(
        "paddingBottom: {type: 'VARIABLE_ALIAS', id: 'VariableID:1:3'},",
        "paddingBottom: {type: 'VARIABLE_ALIAS', id: 'VariableID:1:3'},"
        " paddingRight: {type: 'VARIABLE_ALIAS', id: 'VariableID:1:2'},")
    seen = captured(monkeypatch)
    figmosha.cmd_props(parse("props", "1:2"))
    program = (stub + "(async () => {" + seen["code"] + "})()"
               ".then(v => console.log(v))"
               ".catch(e => { console.error(e.stack); process.exit(1); });")
    out = subprocess.run([NODE, "-e", program], capture_output=True, text=True,
                         encoding="utf-8")
    assert out.returncode == 0, out.stderr
    assert "pad:24 → space/16, 16 → radius/lg, 24 → space/16, 16" in out.stdout


# ─── update ───────────────────────────────────────────────────────────────

class FakeGit:
    """Enough git to drive cmd_update: a HEAD that moves when you pull."""

    def __init__(self, *, is_repo=True, dirty="", remote="origin",
                 pull_ok=True, changed="plugin/code.js"):
        self.is_repo = is_repo
        self.dirty = dirty
        self.remote = remote
        self.pull_ok = pull_ok
        self.changed = changed
        self.head = "old"
        self.calls = []

    def __call__(self, *args, cwd=None):
        self.calls.append(args)
        if args[:2] == ("rev-parse", "--git-dir"):
            return (0, ".git") if self.is_repo else (128, "not a git repository")
        if args[0] == "status":
            return 0, self.dirty
        if args[0] == "remote":
            return 0, self.remote
        if args[:2] == ("rev-parse", "HEAD"):
            return 0, self.head
        if args[0] == "pull":
            if not self.pull_ok:
                return 1, "error: Your local changes would be overwritten"
            self.head = "new"
            return 0, "Updating old..new"
        if args[0] == "log":
            return 0, "abc1234 something\ndef5678 something else"
        if args[0] == "diff":
            return 0, self.changed
        return 0, ""

    def ran(self, verb):
        return [c for c in self.calls if c[0] == verb]


def with_git(monkeypatch, git):
    monkeypatch.setattr(figmosha, "_git", git)
    seen = {"init": 0}
    monkeypatch.setattr(figmosha, "cmd_init",
                        lambda args, previous_id=None: seen.__setitem__("init", 1) or 0)
    return seen


def test_update_refuses_to_discard_uncommitted_work(monkeypatch, capsys):
    git = FakeGit(dirty=" M figmosha.py\n M plugin/ui.html")
    with_git(monkeypatch, git)
    assert figmosha.cmd_update(parse("update")) == 1
    err = capsys.readouterr().err
    assert "figmosha.py" in err and "stash" in err
    assert not git.ran("pull"), "nothing may be pulled over local changes"


def test_update_releases_the_stamped_files_before_pulling(monkeypatch):
    """git refuses to overwrite them, so the ritual has that step first."""
    git = FakeGit(dirty=" M plugin/ui.html\n M plugin/manifest.json")
    with_git(monkeypatch, git)
    assert figmosha.cmd_update(parse("update")) == 0
    checkout = git.ran("checkout")[0]
    assert set(figmosha.STAMPED) <= set(checkout)
    assert git.calls.index(checkout) < git.calls.index(git.ran("pull")[0])


def test_update_always_ends_in_init(monkeypatch):
    """The pull just overwrote the port and the plugin id."""
    git = FakeGit()
    seen = with_git(monkeypatch, git)
    assert figmosha.cmd_update(parse("update")) == 0
    assert seen["init"] == 1


def test_update_says_re_import_only_when_the_manifest_changed(monkeypatch, capsys):
    git = FakeGit(changed="plugin/code.js")
    with_git(monkeypatch, git)
    figmosha.cmd_update(parse("update"))
    out = capsys.readouterr().out
    assert "re-Run" in out and "re-IMPORT" not in out

    git = FakeGit(changed="plugin/manifest.json\nplugin/code.js")
    with_git(monkeypatch, git)
    figmosha.cmd_update(parse("update"))
    assert "re-IMPORT" in capsys.readouterr().out


def test_update_explains_a_copy_that_was_never_cloned(monkeypatch, capsys):
    with_git(monkeypatch, FakeGit(is_repo=False))
    assert figmosha.cmd_update(parse("update")) == 2
    assert "figmosha.py init" in capsys.readouterr().err


def test_update_without_a_remote_says_so(monkeypatch, capsys):
    with_git(monkeypatch, FakeGit(remote=""))
    assert figmosha.cmd_update(parse("update")) == 2
    assert "no git remote" in capsys.readouterr().err


def test_a_failed_pull_reminds_you_to_re_stamp(monkeypatch, capsys):
    git = FakeGit(dirty=" M plugin/ui.html", pull_ok=False)
    with_git(monkeypatch, git)
    assert figmosha.cmd_update(parse("update")) == 1
    err = capsys.readouterr().err
    assert "figmosha init" in err, "the stamped files were just reverted"


def test_a_mistyped_command_is_not_sent_to_figma_as_js(monkeypatch, capsys):
    """`figmosha updat` used to come back as «'updat' is not defined»."""
    monkeypatch.setattr(sys, "argv", ["figmosha", "updat"])
    with pytest.raises(SystemExit) as e:
        figmosha.main()
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "unknown command" in err and "did you mean: update" in err


@pytest.mark.parametrize("code", [
    "return 1",
    "figma.currentPage.name",
    "await h.node('1:2')",
    "n.remove();",
])
def test_real_code_still_runs(code, monkeypatch):
    """Anything with a space, dot, bracket or semicolon is code, not a typo."""
    assert figmosha.looks_like_a_command(code) is False


@pytest.mark.parametrize("word", ["updat", "propz", "vars2"])
def test_bare_words_read_as_commands(word):
    assert figmosha.looks_like_a_command(word) is True


def test_update_reads_status_paths_whatever_the_columns(monkeypatch):
    """A leading space in the first line used to shift the path by one char."""
    git = FakeGit(dirty="M  plugin/manifest.json\n M plugin/ui.html")
    with_git(monkeypatch, git)
    assert figmosha.cmd_update(parse("update")) == 0, "both are stamped files"


def test_update_does_not_invent_a_re_import(monkeypatch, capsys):
    """After the pull the manifest holds the repo's default id, not Figma's.

    Comparing against it would tell the user to remove and re-import the
    plugin every single update — advice that is wrong and expensive to follow.
    """
    git = FakeGit()
    monkeypatch.setattr(figmosha, "_git", git)
    monkeypatch.setattr(figmosha, "_manifest_id", lambda: "figmosha-test")
    passed = {}
    monkeypatch.setattr(figmosha, "cmd_init",
                        lambda args, previous_id=None: passed.setdefault("id", previous_id) and 0 or 0)
    figmosha.cmd_update(parse("update"))
    assert passed["id"] == "figmosha-test", "init must compare against the imported id"


# ─── the shim ─────────────────────────────────────────────────────────────

def test_the_shim_runs_the_cli_from_its_own_folder(tmp_path):
    """`figmosha props sel` beats `python figmosha.py props sel` by 14 keystrokes.

    Run from an unrelated working directory on purpose: the shim resolves the
    copy from where *it* lives, so a copy stays addressable by full path.
    """
    root = Path(figmosha.__file__).resolve().parent
    shim = root / ("figmosha.cmd" if os.name == "nt" else "figmosha")
    assert shim.exists(), f"missing {shim.name}"

    out = subprocess.run([str(shim)], cwd=tmp_path, capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    assert "usage: figmosha" in (out.stdout + out.stderr)
    assert out.returncode == 2, "no arguments prints help"


def test_the_default_host_is_an_address_not_a_name():
    """"localhost" resolves to ::1 first on Windows; the bridge binds IPv4.

    Every command then waited out a failed IPv6 attempt: 2217ms measured
    against 157ms. This is a one-word setting that costs a second and a half
    per call, so it gets a test rather than a comment alone.
    """
    assert figmosha.DEFAULT_HOST == "127.0.0.1"
    import project
    import inspect
    assert inspect.signature(project.occupant).parameters["host"].default == "127.0.0.1"
