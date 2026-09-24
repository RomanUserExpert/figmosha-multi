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


@pytest.fixture(autouse=True)
def _no_real_figma_tabs(monkeypatch):
    """`each` reads the Figma tab's memory from outside. Not this machine's, here."""
    monkeypatch.setattr(figmosha, "_figma_tabs", lambda: [])


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
  showUI() {}, on() {},
  ui: { onmessage: null, postMessage() {} },
  root: { name: 'Stub', children: [] },
  currentPage: {
    selection: nodes,
    children: nodes,
    findAll: (f) => nodes.filter(f),
    findOne: (f) => nodes.find(f) || null,
    id: 'page', name: 'Page 1', type: 'PAGE',
  },
  getNodeByIdAsync: async (id) => nodes.find(n => n.id === id) || null,
};
// The real helpers, not a stand-in: the generated JS leans on h.walk's pruning
// and on h.resolve loading a page, and a hand-written stub of those would
// drift from the plugin exactly where it matters.
const h = new Function("figma", "__html__",
  require("fs").readFileSync(PLUGIN_SRC_PATH, "utf8") + "\\nreturn HELPERS;")(figma, "");
"""

# Prepended rather than interpolated further down, because two tests build
# their own program out of STUB and neither should have to know about this.
STUB = ("const PLUGIN_SRC_PATH = "
        + json.dumps(str(Path(__file__).resolve().parent.parent / "plugin" / "code.js"))
        + ";\n" + STUB)


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
    # --raw carries the walk itself, not just its hits: on a big subtree
    # `partial` and `cursor` are the difference between a short answer and a
    # wrong one.
    out = json.loads(run_js(seen["code"]))
    assert out["found"][0]["id"] == "1:3"
    assert out["partial"] is False and out["visited"] == 2


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



# ─── set / bind ───────────────────────────────────────────────────────────
#
# A richer stub than the one above: these commands write, so the document has
# to push back the way Figma does — auto-layout properties refuse to be set
# before layoutMode, and paints are frozen.

MUTATE_STUB = """
const order = [];
const solid = (hex, op) => {
  const n = parseInt(String(hex).replace('#',''), 16);
  const p = {type:'SOLID', color:{r:((n>>16)&255)/255, g:((n>>8)&255)/255, b:(n&255)/255}};
  if (op != null) p.opacity = op;
  return [p];
};
function makeNode(over) {
  const base = {
    id:'1:2', name:'Card', type:'FRAME',
    width:100, height:50, x:0, y:0, opacity:1, visible:true,
    fills: solid('#ffffff'), strokes: [], strokeWeight: 1,
    layoutMode:'NONE', layoutWrap:'NO_WRAP', itemSpacing:0,
    paddingTop:0, paddingRight:0, paddingBottom:0, paddingLeft:0,
    topLeftRadius:0, topRightRadius:0, bottomRightRadius:0, bottomLeftRadius:0,
    primaryAxisAlignItems:'MIN', counterAxisAlignItems:'MIN',
    layoutSizingHorizontal:'FIXED', layoutSizingVertical:'FIXED',
    boundVariables:{},
    resize(w, h) { order.push('resize'); this.width = w; this.height = h; },
    setBoundVariable(prop, v) {
      order.push('setBoundVariable:' + prop);
      if (v) this.boundVariables[prop] = {id: v.id};
      else delete this.boundVariables[prop];
    },
  };
  const n = Object.assign(base, over || {});
  return new Proxy(n, {
    set(t, k, v) {
      // Figma ignores spacing and padding on a frame with no auto-layout, and
      // throws on the sizing modes. The stub throws for both, so a test can
      // tell that the command reported the refusal instead of swallowing it.
      const needsLayout = ['itemSpacing','paddingTop','paddingRight','paddingBottom','paddingLeft'];
      if (needsLayout.indexOf(String(k)) !== -1 && t.layoutMode === 'NONE') {
        throw new Error(String(k) + ' is only available on an auto-layout frame');
      }
      if (typeof v !== 'function') order.push(String(k));
      t[k] = v;
      return true;
    },
  });
}
const nodes = [makeNode({}), makeNode({id:'1:3', name:'Card 2'})];
const VARS = {
  'space/md': {id:'VariableID:9:1', name:'space/md'},
  'color/bg': {id:'VariableID:9:2', name:'color/bg'},
};
const figma = {
  currentPage: {
    selection: nodes, id:'page', name:'Page 1', type:'PAGE',
    findAll: (f) => nodes.filter(f),
  },
  getNodeByIdAsync: async (id) => nodes.find(n => n.id === id) || null,
  variables: {
    getVariableByIdAsync: async (id) => {
      for (const k in VARS) if (VARS[k].id === id) return VARS[k];
      return null;
    },
    setBoundVariableForPaint(paint, field, v) {
      order.push('setBoundVariableForPaint:' + field);
      const copy = JSON.parse(JSON.stringify(paint));
      if (v) copy.boundVariables = {[field]: {id: v.id, type:'VARIABLE_ALIAS'}};
      else if (copy.boundVariables) delete copy.boundVariables[field];
      return copy;
    },
  },
};
const h = {
  resolve: async (x) => x === 'page' ? figma.currentPage
    : x === 'sel' ? figma.currentPage.selection[0]
    : await figma.getNodeByIdAsync(x),
  solid,
  var_: async (name) => VARS[name] || null,
  setText: async (n, t) => { order.push('setText'); n.characters = t; },
  bF: async (n, i, name) => {
    const v = VARS[name];
    if (!v) throw new Error('h.bF: no variable named "' + name + '"');
    const copy = JSON.parse(JSON.stringify(n.fills));
    copy[i] = figma.variables.setBoundVariableForPaint(copy[i], 'color', v);
    n.fills = copy;
    return v;
  },
  bS: async (n, i, name) => {
    const v = VARS[name];
    const copy = JSON.parse(JSON.stringify(n.strokes));
    copy[i] = figma.variables.setBoundVariableForPaint(copy[i], 'color', v);
    n.strokes = copy;
    return v;
  },
};
"""


def run_mutation(code):
    """Run a set/bind program and hand back its text plus what it touched."""
    program = (MUTATE_STUB + "(async () => {" + code + "})()"
               ".then(text => console.log(JSON.stringify({text, order, "
               "nodes: nodes.map(n => ({name:n.name, gap:n.itemSpacing, "
               "fills:n.fills, bound:n.boundVariables, w:n.width, "
               "layout:n.layoutMode}))})))"
               ".catch(e => { console.error(e.message); process.exit(1); });")
    out = subprocess.run([NODE, "-e", program], capture_output=True, text=True,
                         encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def mutation(monkeypatch, *argv):
    seen = captured(monkeypatch)
    args = parse(*argv)
    (figmosha.cmd_bind if argv[0] == "bind" else figmosha.cmd_set)(args)
    return run_mutation(seen["code"])


@needs_node
def test_set_applies_layout_before_spacing(monkeypatch):
    """Typed in the wrong order on purpose: auto-layout silently drops
    itemSpacing set before layoutMode, so the command must reorder."""
    r = mutation(monkeypatch, "set", "1:2", "gap=16", "layout=v")
    assert r["order"].index("layoutMode") < r["order"].index("itemSpacing")
    assert r["nodes"][0]["gap"] == 16


@needs_node
def test_set_reports_a_refusal_per_key_and_keeps_going(monkeypatch):
    """One key the node cannot take must not cost the whole command."""
    r = mutation(monkeypatch, "set", "1:2", "gap=16", "name=Renamed")
    assert "auto-layout" in r["text"]
    assert r["nodes"][0]["name"] == "Renamed"


@needs_node
def test_set_sel_writes_every_selected_node(monkeypatch):
    r = mutation(monkeypatch, "set", "sel", "name=Both")
    assert [n["name"] for n in r["nodes"]] == ["Both", "Both"]


@needs_node
def test_set_prints_before_and_after(monkeypatch):
    r = mutation(monkeypatch, "set", "1:2", "fill=#f5f5f5")
    assert "#FFFFFF" in r["text"] and "#F5F5F5" in r["text"] and "→" in r["text"]


@needs_node
def test_set_counts_unchanged_instead_of_listing_them(monkeypatch):
    r = mutation(monkeypatch, "set", "1:2", "name=Card", "x=0")
    assert "2 unchanged" in r["text"]


@needs_node
def test_dry_run_writes_nothing(monkeypatch):
    r = mutation(monkeypatch, "set", "1:2", "name=Renamed", "--dry-run")
    assert r["nodes"][0]["name"] == "Card"
    assert "Renamed" in r["text"] and "--dry-run" in r["text"]


@needs_node
def test_bind_paint_goes_through_the_paint_api(monkeypatch):
    """setBoundVariable on fills is the error the bridge hints about first."""
    r = mutation(monkeypatch, "bind", "1:2", "fill=color/bg")
    assert "setBoundVariableForPaint:color" in r["order"]
    assert not any(o.startswith("setBoundVariable:") for o in r["order"])


@needs_node
def test_bind_number_names_the_token_in_the_diff(monkeypatch):
    r = mutation(monkeypatch, "bind", "1:2", "gap=space/md")
    assert r["nodes"][0]["bound"]["itemSpacing"]["id"] == "VariableID:9:1"
    assert "space/md" in r["text"]


@needs_node
def test_bind_none_unbinds(monkeypatch):
    r = mutation(monkeypatch, "bind", "1:2", "gap=space/md", "gap=none")
    assert "itemSpacing" not in r["nodes"][0]["bound"]


@needs_node
def test_bind_refuses_a_key_that_is_not_bindable(monkeypatch):
    seen = captured(monkeypatch)
    figmosha.cmd_bind(parse("bind", "1:2", "visible=space/md"))
    program = (MUTATE_STUB + "(async () => {" + seen["code"] + "})()"
               ".then(() => process.exit(1))"
               ".catch(e => console.log(e.message));")
    out = subprocess.run([NODE, "-e", program], capture_output=True, text=True,
                         encoding="utf-8")
    assert "not bindable" in out.stdout or "unknown key" in out.stdout


def test_set_rejects_an_argument_without_an_equals_sign(monkeypatch, capsys):
    captured(monkeypatch)
    assert figmosha.cmd_set(parse("set", "1:2", "gap")) == 2
    assert "key=value" in capsys.readouterr().err


# ─── ceilings and exec --set ──────────────────────────────────────────────

@needs_node
def test_find_prints_a_limited_number_of_rows(monkeypatch):
    """Two hits, room for one: the count must still be honest."""
    seen = captured(monkeypatch)
    figmosha.cmd_find(parse("find", "page", "name~", "--limit", "1"))
    out = run_js(seen["code"])
    assert out.startswith("2 found")
    assert "showing 1 of 2" in out
    assert len([l for l in out.splitlines() if l.startswith("1:")]) == 1


@needs_node
def test_find_says_nothing_extra_when_everything_fits(monkeypatch):
    seen = captured(monkeypatch)
    figmosha.cmd_find(parse("find", "page", "name~"))
    out = run_js(seen["code"])
    assert "showing" not in out


def test_tree_defaults_to_three_levels():
    assert parse("tree", "page").depth == 3


def test_exec_set_defines_a_string_const():
    assert figmosha.prelude(["ROOT=185:21880"]) == 'const ROOT = "185:21880";\n'


def test_exec_set_parses_json_when_it_is_json():
    assert figmosha.prelude(["SCALE=[0,4,8]"]) == "const SCALE = [0, 4, 8];\n"
    assert figmosha.prelude(["N=12"]) == "const N = 12;\n"


def test_exec_set_keeps_non_ascii_readable():
    assert figmosha.prelude(['NAME=Привіт']) == 'const NAME = "Привіт";\n'


def test_exec_set_refuses_a_name_that_is_not_an_identifier():
    with pytest.raises(ValueError, match="not a JS identifier"):
        figmosha.prelude(["my-const=1"])


def test_exec_set_refuses_an_argument_without_a_value():
    with pytest.raises(ValueError, match="NAME=value"):
        figmosha.prelude(["ROOT"])


def test_exec_set_lands_before_the_code(monkeypatch):
    seen = captured(monkeypatch)
    figmosha.cmd_exec(parse("exec", "return ROOT;", "--set", "ROOT=1:2"))
    assert seen["code"] == 'const ROOT = "1:2";\nreturn ROOT;'


def test_exec_without_set_is_untouched(monkeypatch):
    seen = captured(monkeypatch)
    figmosha.cmd_exec(parse("exec", "return 1;"))
    assert seen["code"] == "return 1;"


# ─── overrides / where ────────────────────────────────────────────────────

TREE_STUB = """
const page = {id:'0:1', name:'stuff', type:'PAGE'};
const frame = {id:'10:238', name:'Fluent Icons', type:'FRAME', width:1440, height:2150,
               parent: page, layoutSizingHorizontal:'FIXED', layoutSizingVertical:'FIXED'};
const inner = {id:'I10:239;88:9705', name:'Title', type:'TEXT', width:298, height:56};
const main = {id:'88:1', name:'Mode=Day', parent:{type:'COMPONENT_SET', name:'Header_Stylesheet'}};
const inst = {
  id:'10:239', name:'Headline', type:'INSTANCE', width:1440, height:148,
  parent: frame, layoutMode:'VERTICAL',
  layoutSizingHorizontal:'FIXED', layoutSizingVertical:'HUG',
  componentProperties: {'Mode#8:0': {value:'Day', type:'VARIANT'}},
  overrides: [
    {id:'10:239', overriddenFields:['fillStyleId','name']},
    {id:'I10:239;88:9705', overriddenFields:['characters']},
  ],
  getMainComponentAsync: async () => main,
};
const plain = {id:'10:240', name:'Just a frame', type:'FRAME', width:10, height:10, parent: page};
const bare = Object.assign({}, inst, {id:'10:241', overrides: [],
  getMainComponentAsync: async () => main});
const all = [page, frame, inst, inner, plain, bare];
const figma = {
  currentPage: page,
  getNodeByIdAsync: async (id) => all.find(n => n.id === id) || null,
};
const h = { resolve: async (x) => x === 'page' ? page : await figma.getNodeByIdAsync(x) };
"""


def run_tree_js(code):
    program = (TREE_STUB + "(async () => {" + code + "})()"
               ".then(v => console.log(v))"
               ".catch(e => { console.log('THREW: ' + e.message); });")
    out = subprocess.run([NODE, "-e", program], capture_output=True, text=True,
                         encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def tree_cmd(monkeypatch, fn, *argv):
    seen = captured(monkeypatch)
    fn(parse(*argv))
    return run_tree_js(seen["code"])


@needs_node
def test_overrides_lists_fields_with_layer_names(monkeypatch):
    out = tree_cmd(monkeypatch, figmosha.cmd_overrides, "overrides", "10:239")
    assert "Header_Stylesheet" in out and "Mode=Day" in out
    assert "(this instance)" in out and "fillStyleId" in out
    assert "Title" in out and "characters" in out


@needs_node
def test_overrides_names_the_set_once(monkeypatch):
    """The variant's own name is its property list, so printing both is noise."""
    out = tree_cmd(monkeypatch, figmosha.cmd_overrides, "overrides", "10:239")
    assert out.splitlines()[0].count("Mode=Day") == 1


@needs_node
def test_overrides_says_so_when_there_are_none(monkeypatch):
    out = tree_cmd(monkeypatch, figmosha.cmd_overrides, "overrides", "10:241")
    assert "nothing overridden" in out


@needs_node
def test_overrides_refuses_a_node_that_is_not_an_instance(monkeypatch):
    out = tree_cmd(monkeypatch, figmosha.cmd_overrides, "overrides", "10:240")
    assert "THREW" in out and "not an INSTANCE" in out


@needs_node
def test_where_walks_up_to_the_page(monkeypatch):
    out = tree_cmd(monkeypatch, figmosha.cmd_where, "where", "10:239")
    lines = out.splitlines()
    assert lines[0] == "Page «stuff»"
    assert "Fluent Icons" in lines[1] and "Headline" in lines[2]


@needs_node
def test_where_carries_sizing_on_every_row(monkeypatch):
    """Sizing is why the question gets asked, so an ancestor must show its own."""
    out = tree_cmd(monkeypatch, figmosha.cmd_where, "where", "10:239")
    assert out.count("H=FIXED") == 2 and "V=HUG" in out


@needs_node
def test_where_on_the_page_is_one_line(monkeypatch):
    out = tree_cmd(monkeypatch, figmosha.cmd_where, "where", "page")
    assert out == "Page «stuff»"


# ─── the vars cap ─────────────────────────────────────────────────────────

@needs_node
def test_vars_warns_about_the_cap_once_per_command(monkeypatch):
    """`shown` counts the whole file, but `break` only left the inner loop —
    so a file with several collections repeated the warning per collection."""
    seen = captured(monkeypatch)
    figmosha.cmd_vars(parse("vars", "color"))

    stub = """
    const cols = [];
    const vars = [];
    for (let c = 0; c < 7; c++) {
      cols.push({id: 'C' + c, name: 'Col' + c, modes: [{modeId: 'm', name: 'Mode 1'}],
                 variableIds: []});
      for (let i = 0; i < 60; i++) {
        vars.push({id: 'V' + c + '_' + i, name: 'color/x' + c + '/' + i,
                   resolvedType: 'FLOAT', variableCollectionId: 'C' + c,
                   valuesByMode: {m: i}});
      }
    }
    cols.forEach(c => { c.variableIds = vars.filter(v => v.variableCollectionId === c.id)
                                            .map(v => v.id); });
    const figma = { variables: {
      getLocalVariableCollectionsAsync: async () => cols,
      getLocalVariablesAsync: async () => vars,
      getVariableByIdAsync: async (id) => vars.find(v => v.id === id) || null,
    }};
    """
    program = (stub + "(async () => {" + seen["code"] + "})()"
               ".then(v => console.log(v))"
               ".catch(e => { console.error(e.message); process.exit(1); });")
    out = subprocess.run([NODE, "-e", program], capture_output=True, text=True,
                         encoding="utf-8")
    assert out.returncode == 0, out.stderr
    assert out.stdout.count("stopped at") == 1, out.stdout[-400:]

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
                        lambda args, previous_id=None, announce_next=True:
                        seen.__setitem__("init", 1) or 0)
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
                        lambda args, previous_id=None, announce_next=True:
                        passed.setdefault("id", previous_id) and 0 or 0)
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


# ─── each: the runner that survives a long scan ───────────────────────────


def _each_args(tmp_path, script="return 1;", **over):
    f = tmp_path / "unit.js"
    f.write_text(script, encoding="utf-8")
    argv = ["each", "page", "-f", str(f)]
    for k, v in over.items():
        argv += ["--" + k.replace("_", "-")] + ([] if v is True else [str(v)])
    return parse(*argv)


def _fake_bridge(monkeypatch, handler):
    """Stand in for the bridge: `handler(code)` returns (status, body)."""
    seen = []

    def fake_exec(code, timeout=60, want_value=False):
        seen.append(code)
        return handler(code)

    monkeypatch.setattr(figmosha, "_exec", fake_exec)
    return seen


CHILDREN = "(n.children || []).map"


def test_each_splits_before_it_runs_anything(monkeypatch, tmp_path, capsys):
    """The whole point of --split: the unit size is chosen while it is free."""
    listed = []

    def handler(code):
        if CHILDREN in code:
            listed.append(code)
            # One level: the page has two frames, and they have no children.
            if len(listed) == 1:
                return 200, {"ok": True, "value": [
                    {"id": "1:1", "name": "Screen A", "type": "FRAME"},
                    {"id": "1:2", "name": "Screen B", "type": "FRAME"}]}
            return 200, {"ok": True, "value": []}
        return 200, {"ok": True, "value": "done"}

    seen = _fake_bridge(monkeypatch, handler)
    rc = figmosha.cmd_each(_each_args(tmp_path))
    assert rc == 0
    ran = [c for c in seen if CHILDREN not in c]
    assert len(ran) == 2, "one run per leaf"
    assert 'const ROOT_ID = "1:1";' in ran[0]
    assert 'const ROOT_ID = "1:2";' in ran[1]


def test_each_splits_a_unit_that_times_out_instead_of_retrying(monkeypatch, tmp_path):
    """An overrun costs minutes of a blocked file; running it again costs them twice."""
    def handler(code):
        if CHILDREN in code:
            if '"page"' in code:
                return 200, {"ok": True, "value": [
                    {"id": "1:1", "name": "Big", "type": "FRAME"}]}
            return 200, {"ok": True, "value": [
                {"id": "2:1", "name": "Section", "type": "FRAME"}]}
        if 'const ROOT_ID = "1:1";' in code:
            return 504, {"ok": False, "error": "timeout after 60s", "busy": True}
        return 200, {"ok": True, "value": "done"}

    seen = _fake_bridge(monkeypatch, handler)
    rc = figmosha.cmd_each(_each_args(tmp_path))
    ran = [c for c in seen if CHILDREN not in c]
    assert len([c for c in ran if 'const ROOT_ID = "1:1";' in c]) == 1, "never retried"
    assert any('const ROOT_ID = "2:1";' in c for c in ran), "its children ran instead"
    assert rc == 0


def test_each_writes_state_after_every_unit(monkeypatch, tmp_path):
    """Written as it goes, because the next unit may be the crash."""
    def handler(code):
        if CHILDREN in code:
            return 200, {"ok": True, "value": [
                {"id": "1:1", "name": "A", "type": "FRAME"},
                {"id": "1:2", "name": "B", "type": "FRAME"}]}
        if 'const ROOT_ID = "1:2";' in code:
            return 500, {"ok": False, "error": "n is not defined"}
        return 200, {"ok": True, "value": 7}

    _fake_bridge(monkeypatch, handler)
    state = tmp_path / "run.jsonl"
    rc = figmosha.cmd_each(_each_args(tmp_path, state=str(state)))
    assert rc == figmosha.EXIT_SCRIPT
    rows = [json.loads(l) for l in state.read_text(encoding="utf-8").splitlines()]
    assert [r["status"] for r in rows] == ["started", "ok", "started", "failed"]
    assert rows[1]["value"] == 7


def test_each_resume_skips_what_is_already_done(monkeypatch, tmp_path):
    def handler(code):
        if CHILDREN in code:
            return 200, {"ok": True, "value": [
                {"id": "1:1", "name": "A", "type": "FRAME"},
                {"id": "1:2", "name": "B", "type": "FRAME"}]}
        return 200, {"ok": True, "value": "done"}

    state = tmp_path / "run.jsonl"
    state.write_text(json.dumps({"id": "1:1", "status": "ok"}) + "\n", encoding="utf-8")
    seen = _fake_bridge(monkeypatch, handler)
    rc = figmosha.cmd_each(_each_args(tmp_path, state=str(state), resume=True))
    assert rc == 0
    ran = [c for c in seen if CHILDREN not in c]
    assert len(ran) == 1 and 'const ROOT_ID = "1:2";' in ran[0]


def test_each_splits_a_unit_whose_result_says_partial(monkeypatch, tmp_path):
    """A script that stopped at its own deadline returned normally, but is not done."""
    def handler(code):
        if CHILDREN in code:
            if '"page"' in code:
                return 200, {"ok": True, "value": [
                    {"id": "1:1", "name": "Big", "type": "FRAME"}]}
            return 200, {"ok": True, "value": [
                {"id": "2:1", "name": "Section", "type": "FRAME"}]}
        if 'const ROOT_ID = "1:1";' in code:
            # What a script returning JSON.stringify({...}) looks like on the wire.
            return 200, {"ok": True, "value": json.dumps(json.dumps(
                {"partial": True, "reason": "deadline"}))}
        return 200, {"ok": True, "value": json.dumps({"partial": False})}

    state = tmp_path / "run.jsonl"
    seen = _fake_bridge(monkeypatch, handler)
    rc = figmosha.cmd_each(_each_args(tmp_path, state=str(state)))
    assert rc == 0
    ran = [c for c in seen if CHILDREN not in c]
    assert any('const ROOT_ID = "2:1";' in c for c in ran), "its children ran instead"
    rows = [json.loads(l) for l in state.read_text(encoding="utf-8").splitlines()]
    last = {r["id"]: r["status"] for r in rows}
    assert last == {"1:1": "split", "2:1": "ok"}


def test_each_partial_is_ok_keeps_the_old_behaviour(monkeypatch, tmp_path):
    def handler(code):
        if CHILDREN in code:
            return 200, {"ok": True, "value": [{"id": "1:1", "name": "A", "type": "FRAME"}]}
        return 200, {"ok": True, "value": json.dumps({"partial": True})}

    seen = _fake_bridge(monkeypatch, handler)
    rc = figmosha.cmd_each(_each_args(tmp_path, partial_is_ok=True))
    assert rc == 0
    assert len([c for c in seen if CHILDREN not in c]) == 1


def test_each_does_not_split_an_instance_into_its_sublayers(monkeypatch, tmp_path):
    """Split, an instance hands its sublayers to the script and never itself.

    2026-09-24: a `custom-control` placed loose on a page became three units
    (`head-card-cardcontrol`, `content-box`, `button-group`) and was not counted.
    """
    listed = []

    def handler(code):
        if CHILDREN in code:
            listed.append(code)
            if '"page"' in code:
                return 200, {"ok": True, "value": [
                    {"id": "1:1", "name": "custom-control", "type": "INSTANCE"},
                    {"id": "1:2", "name": "Screen", "type": "FRAME"}]}
            return 200, {"ok": True, "value": [
                {"id": "2:1", "name": "Section", "type": "FRAME"}]}
        return 200, {"ok": True, "value": "done"}

    seen = _fake_bridge(monkeypatch, handler)
    rc = figmosha.cmd_each(_each_args(tmp_path, split=2))
    assert rc == 0
    ran = [c for c in seen if CHILDREN not in c]
    assert any('const ROOT_ID = "1:1";' in c for c in ran), "the instance is a unit"
    assert any('const ROOT_ID = "2:1";' in c for c in ran), "the frame was split"
    assert not any('"1:1"' in c for c in listed), "its children were never listed"


def test_each_split_instances_goes_inside_when_asked(monkeypatch, tmp_path):
    def handler(code):
        if CHILDREN in code:
            if '"page"' in code:
                return 200, {"ok": True, "value": [
                    {"id": "1:1", "name": "card", "type": "INSTANCE"}]}
            return 200, {"ok": True, "value": [
                {"id": "I1:1;5:5", "name": "content", "type": "FRAME"}]}
        return 200, {"ok": True, "value": "done"}

    seen = _fake_bridge(monkeypatch, handler)
    rc = figmosha.cmd_each(_each_args(tmp_path, split=2, split_instances=True))
    assert rc == 0
    ran = [c for c in seen if CHILDREN not in c]
    assert len(ran) == 1 and 'const ROOT_ID = "I1:1;5:5";' in ran[0]


def test_each_does_not_split_an_instance_that_overruns(monkeypatch, tmp_path, capsys):
    """Going inside would lose the instance; it fails with a way to opt in."""
    def handler(code):
        if CHILDREN in code:
            return 200, {"ok": True, "value": [
                {"id": "1:1", "name": "card", "type": "INSTANCE"}]}
        return 504, {"ok": False, "error": "timeout after 60s", "busy": True}

    seen = _fake_bridge(monkeypatch, handler)
    rc = figmosha.cmd_each(_each_args(tmp_path))
    assert rc == figmosha.EXIT_BUSY
    assert len([c for c in seen if CHILDREN in c]) == 1, "never listed to split"
    assert "--split-instances" in capsys.readouterr().err


def test_children_listing_leaves_out_hidden_instance_sublayers():
    """With invisible instance children skipped, Figma says they do not exist."""
    seen = []

    def fake_exec(code, timeout=60, want_value=False):
        seen.append(code)
        return 200, {"ok": True, "value": []}

    orig = figmosha._exec
    figmosha._exec = fake_exec
    try:
        figmosha._children_of("1:1", 10)
    finally:
        figmosha._exec = orig
    assert "c.visible === false && c.id.startsWith('I')" in seen[0]


def _tab(monkeypatch, readings):
    """A Figma tab whose private memory reads `readings` in turn, then the last."""
    it = iter(readings)
    last = [readings[0]]

    def read(pid):
        last[0] = next(it, last[0])
        return last[0]

    monkeypatch.setattr(figmosha, "_figma_tabs", lambda: [(4242, readings[0])])
    monkeypatch.setattr(figmosha, "_private_mb", read)
    monkeypatch.setattr(figmosha.os, "name", "nt")


def _two_frames(code):
    if CHILDREN in code:
        return 200, {"ok": True, "value": [
            {"id": "1:1", "name": "A", "type": "FRAME"},
            {"id": "1:2", "name": "B", "type": "FRAME"}]}
    return 200, {"ok": True, "value": "done"}


def test_each_pauses_before_a_unit_once_the_tab_is_over_its_limit(
        monkeypatch, tmp_path, capsys):
    """Past the limit the next unit may be the one that takes the tab down."""
    # attach, before splitting, before 1:1, after 1:1, before 1:2
    _tab(monkeypatch, [900, 1000, 1000, 1750, 1750])
    monkeypatch.setattr(figmosha, "_session_ids", lambda: {"s-old"})
    seen = _fake_bridge(monkeypatch, _two_frames)
    state = tmp_path / "run.jsonl"
    rc = figmosha.cmd_each(_each_args(tmp_path, state=str(state), reopen_wait=0))
    assert rc == figmosha.EXIT_TAB_MEMORY
    ran = [c for c in seen if CHILDREN not in c]
    assert len(ran) == 1 and 'const ROOT_ID = "1:1";' in ran[0]
    rows = [json.loads(l) for l in state.read_text(encoding="utf-8").splitlines()]
    assert rows[1]["tab_mb"] == 1750, "the reading goes into the state"
    assert rows[-1] == dict(rows[-1], id="1:2", status="paused", reason="tab-memory")
    assert "open it again on a light page" in capsys.readouterr().err
    # and the paused unit is not done: a resume runs it
    assert figmosha._load_state(str(state))["1:2"]["status"] != "ok"


def test_each_carries_on_by_itself_once_the_file_is_reopened(monkeypatch, tmp_path):
    _tab(monkeypatch, [900, 1000, 1000, 1750, 1750, 950, 960])
    monkeypatch.setattr(figmosha, "_session_ids", lambda: {"s-old"})
    monkeypatch.setattr(figmosha, "_wait_for_reopen", lambda old, s: True)
    seen = _fake_bridge(monkeypatch, _two_frames)
    rc = figmosha.cmd_each(_each_args(tmp_path))
    assert rc == 0
    assert len([c for c in seen if CHILDREN not in c]) == 2


def test_each_does_not_even_split_when_the_tab_is_already_over(monkeypatch, tmp_path):
    """Listing a page's children loads the page — the step that tips a full tab."""
    _tab(monkeypatch, [1900])
    monkeypatch.setattr(figmosha, "_session_ids", lambda: {"s-old"})
    seen = _fake_bridge(monkeypatch, _two_frames)
    rc = figmosha.cmd_each(_each_args(tmp_path, reopen_wait=0))
    assert rc == figmosha.EXIT_TAB_MEMORY
    assert seen == [], "nothing was sent to the tab"


def test_each_tab_limit_zero_turns_the_guard_off(monkeypatch, tmp_path):
    _tab(monkeypatch, [3000])
    seen = _fake_bridge(monkeypatch, _two_frames)
    rc = figmosha.cmd_each(_each_args(tmp_path, tab_limit=0))
    assert rc == 0
    assert len([c for c in seen if CHILDREN not in c]) == 2


def test_wait_for_reopen_wants_a_session_that_was_not_there(monkeypatch):
    """The old tab keeps its session, and its memory, while it stays open."""
    answers = iter([{"s-old"}, {"s-old", "s-new"}])
    monkeypatch.setattr(figmosha, "_session_ids", lambda: next(answers))
    monkeypatch.setattr(figmosha, "_plugin_is_back", lambda: True)
    monkeypatch.setattr(figmosha.time, "sleep", lambda s: None)
    assert figmosha._wait_for_reopen({"s-old"}, 60) is True


def test_load_state_takes_the_last_record_per_id(tmp_path):
    """After a crash and a resume a unit is `started`, `failed`, then `ok`."""
    state = tmp_path / "run.jsonl"
    state.write_text("\n".join(json.dumps(r) for r in [
        {"id": "1:1", "status": "started"},
        {"id": "1:1", "status": "failed"},
        {"id": "1:2", "status": "started"},
        {"id": "1:1", "status": "started"},
        {"id": "1:1", "status": "ok"},
    ]) + "\n", encoding="utf-8")
    last = figmosha._load_state(str(state))
    assert last["1:1"]["status"] == "ok"
    assert last["1:2"]["status"] == "started"


def test_each_resume_does_not_rerun_a_unit_it_already_split(monkeypatch, tmp_path, capsys):
    """Running it again would overrun again; its children are the work."""
    def handler(code):
        if CHILDREN in code:
            return 200, {"ok": True, "value": [{"id": "1:1", "name": "Big", "type": "FRAME"}]}
        return 200, {"ok": True, "value": "done"}

    state = tmp_path / "run.jsonl"
    state.write_text("\n".join(json.dumps(r) for r in [
        {"id": "1:1", "status": "split",
         "into": [{"id": "2:1", "name": "S1"}, {"id": "2:2", "name": "S2"}]},
        {"id": "2:1", "status": "ok"},
        {"id": "2:2", "name": "S2", "status": "started"},
    ]) + "\n", encoding="utf-8")
    seen = _fake_bridge(monkeypatch, handler)
    rc = figmosha.cmd_each(_each_args(tmp_path, state=str(state), resume=True))
    assert rc == 0
    ran = [c for c in seen if CHILDREN not in c]
    assert len(ran) == 1 and 'const ROOT_ID = "2:2";' in ran[0]
    assert "2:2 «S2» was running when the previous attempt ended" in capsys.readouterr().err


def test_each_stops_when_the_plugin_is_gone(monkeypatch, tmp_path):
    """Going on would fail every remaining unit, slowly, and lose the state."""
    monkeypatch.setattr(figmosha, "_wait_for_plugin", lambda s: False)

    def handler(code):
        if CHILDREN in code:
            return 200, {"ok": True, "value": [
                {"id": "1:1", "name": "A", "type": "FRAME"},
                {"id": "1:2", "name": "B", "type": "FRAME"}]}
        return 503, {"ok": False, "error": "plugin not connected - open Figmosha in Figma"}

    seen = _fake_bridge(monkeypatch, handler)
    rc = figmosha.cmd_each(_each_args(tmp_path))
    assert rc == figmosha.EXIT_NO_PLUGIN
    assert len([c for c in seen if CHILDREN not in c]) == 1, "stopped at the first one"


# ─── exit codes: what a runner branches on ────────────────────────────────

def test_exit_codes_tell_the_three_failures_apart():
    assert figmosha._exit_code(200, {"ok": True}) == 0
    assert figmosha._exit_code(500, {"ok": False, "error": "x is not defined"}) == 1
    assert figmosha._exit_code(503, {"ok": False, "error": "plugin not connected"}) == 3
    assert figmosha._exit_code(
        500, {"ok": False, "error": "plugin disconnected mid-request"}) == 3
    assert figmosha._exit_code(504, {"ok": False, "error": "timeout after 60s"}) == 4


# ─── --set: three spellings, because guessing is wrong expensively ────────

def test_set_str_keeps_json_text_as_a_string():
    """The measured trap: --set KEYS={"a":"b"} arrives as an object, and the
    script's JSON.parse(KEYS) throws on the first line."""
    assert figmosha.prelude([("auto", 'KEYS={"a":"b"}')]) == \
        'const KEYS = {"a": "b"};\n'
    assert figmosha.prelude([("str", 'KEYS={"a":"b"}')]) == \
        'const KEYS = "{\\"a\\":\\"b\\"}";\n'


def test_set_json_refuses_what_is_not_json():
    with pytest.raises(ValueError, match="not valid JSON"):
        figmosha.prelude([("json", "IDS=185:21880")])


def test_set_reads_a_value_from_a_file(tmp_path):
    f = tmp_path / "keys.json"
    f.write_text('["a", "b"]', encoding="utf-8")
    assert figmosha.prelude([("auto", f"KEYS=@{f}")]) == 'const KEYS = ["a", "b"];\n'


def test_set_says_which_file_it_could_not_read(tmp_path):
    with pytest.raises(ValueError, match="KEYS=@"):
        figmosha.prelude([("auto", f"KEYS=@{tmp_path / 'missing.json'}")])


def test_each_waits_out_the_busy_thread_before_calling_a_unit_unsplittable(
        monkeypatch, tmp_path):
    """The unit that just overran still owns the thread, so the listing that
    decides whether it can be split must be asked with room to wait."""
    budgets = []

    def handler(code):
        if CHILDREN in code:
            if '"page"' in code:
                return 200, {"ok": True, "value": [
                    {"id": "1:1", "name": "Big", "type": "FRAME"}]}
            return 200, {"ok": True, "value": [
                {"id": "2:1", "name": "Section", "type": "FRAME"}]}
        if 'const ROOT_ID = "1:1";' in code:
            return 504, {"ok": False, "error": "timeout after 2s", "busy": True}
        return 200, {"ok": True, "value": "done"}

    def fake_exec(code, timeout=60, want_value=False):
        budgets.append((code, timeout))
        return handler(code)

    monkeypatch.setattr(figmosha, "_exec", fake_exec)
    rc = figmosha.cmd_each(_each_args(tmp_path, timeout=2))
    assert rc == 0
    split_listing = [t for c, t in budgets if CHILDREN in c and '"page"' not in c]
    assert split_listing and min(split_listing) >= 120, \
        "the split listing must outlast the script that is still running"


def test_update_never_prints_two_contradictory_follow_ups(monkeypatch, capsys):
    """`init` can only see that the identity is unchanged, so it would say
    "re-Run" a line before `update` says "re-IMPORT". Following the first one
    leaves Figma running the old manifest, which looks like a failed update."""
    with_git(monkeypatch, FakeGit(changed="plugin/manifest.json"))
    figmosha.cmd_update(parse("update"))
    out = capsys.readouterr().out
    assert "re-IMPORT" in out
    assert "re-Run" not in out, "init must not contradict update's own advice"


def test_update_asks_init_to_stay_quiet_about_the_follow_up(monkeypatch):
    """The suppression is explicit, so a plain `init` keeps saying what to do."""
    monkeypatch.setattr(figmosha, "_git", FakeGit())
    passed = {}

    def fake_init(args, previous_id=None, announce_next=True):
        passed["announce"] = announce_next
        return 0

    monkeypatch.setattr(figmosha, "cmd_init", fake_init)
    figmosha.cmd_update(parse("update"))
    assert passed["announce"] is False
