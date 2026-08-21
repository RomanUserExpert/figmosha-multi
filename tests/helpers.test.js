// Exercise the pure helpers from plugin/code.js against a stubbed Figma.
//
// The parts worth pinning down are the ones with no Figma in them: the hex
// conversion, and the order h.frame() applies auto-layout properties in —
// getting that order wrong makes Figma silently ignore the settings.
//
//     node tests/helpers.test.js
const fs = require("fs");
const path = require("path").join(__dirname, "..", "plugin", "code.js");
const src = fs.readFileSync(path, "utf8");

const order = [];   // records the sequence of property writes on a frame

function makeFrame() {
  const f = {
    _children: [],
    width: 100, height: 100,
    appendChild(c) { order.push("appendChild"); this._children.push(c); },
    resize(w, h) { order.push("resize"); this.width = w; this.height = h; },
  };
  return new Proxy(f, {
    set(t, k, v) {
      if (typeof v !== "function") order.push(String(k));
      t[k] = v;
      return true;
    },
  });
}

// Shaped like a real themed library: every primitive exists twice, once per
// theme collection, which is what makes a bare name ambiguous.
const COLLECTIONS = [
  { id: "C1", name: "Primitives" },
  { id: "C2", name: "Primitives Dark" },
  { id: "C3", name: "Semantics" },
];
const VARIABLES = [
  { id: "VariableID:1:1", name: "color/neutral/100", variableCollectionId: "C1" },
  { id: "VariableID:1:2", name: "color/neutral/100", variableCollectionId: "C2" },
  { id: "VariableID:1:3", name: "color/bg/default", variableCollectionId: "C3" },
  { id: "VariableID:1:4", name: "color/bg/default-alt", variableCollectionId: "C3" },
  { id: "VariableID:1:5", name: "space/md", variableCollectionId: "C3" },
];
const PAINT_STYLES = [
  { id: "S:aaa", name: "Brand/Primary" },
  { id: "S:bbb", name: "Brand/Secondary" },
];
const TEXT_STYLES = [{ id: "S:ccc", name: "Body/Regular" }];

const calls = { listVars: 0, importVar: 0, byId: 0, listPaint: 0 };
const posted = [];

const figma = {
  showUI() {},
  // code.js subscribes to page changes at load time; without this the whole
  // file throws before HELPERS is ever built.
  on() {},
  ui: { onmessage: null, postMessage(m) { posted.push(m); } },
  createFrame: makeFrame,
  currentPage: { selection: [] },
  root: { name: "Stub" },
  variables: {
    async getLocalVariablesAsync() { calls.listVars++; return VARIABLES; },
    async getLocalVariableCollectionsAsync() { return COLLECTIONS; },
    async getVariableByIdAsync(id) {
      calls.byId++;
      return VARIABLES.find((v) => v.id === id) || null;
    },
    async importVariableByKeyAsync(key) {
      calls.importVar++;
      throw new Error("stub: no library variable " + key);
    },
  },
  async getLocalPaintStylesAsync() { calls.listPaint++; return PAINT_STYLES; },
  async getLocalTextStylesAsync() { return TEXT_STYLES; },
  async getLocalEffectStylesAsync() { return []; },
  async getLocalGridStylesAsync() { return []; },
  async getStyleByIdAsync(id) {
    return PAINT_STYLES.concat(TEXT_STYLES).find((st) => st.id === id) || null;
  },
};

// code.js is written for the plugin sandbox: evaluate it with our stub in scope
// and hand back the HELPERS object it builds.
const api = new Function("figma", "__html__",
  src + "\nreturn { HELPERS, asText, MAX_RESULT_CHARS, resolveVar, resolveStyle, clearExecCaches };")(figma, "");
const h = api.HELPERS;

let failed = 0;
function check(label, actual, expected) {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  const ok = a === e;
  if (!ok) failed++;
  console.log(`${ok ? "  ok  " : " FAIL "} ${label}${ok ? "" : `\n         got ${a}\n         want ${e}`}`);
}

// hex — the /255 conversion that gets hand-rolled wrong
check("hex #ffffff", h.hex("#ffffff"), { r: 1, g: 1, b: 1 });
check("hex 000000 without #", h.hex("000000"), { r: 0, g: 0, b: 0 });
check("hex shorthand #f00", h.hex("#f00"), { r: 1, g: 0, b: 0 });
check("hex #808080", h.hex("#808080"), { r: 128 / 255, g: 128 / 255, b: 128 / 255 });

let threw = false;
try { h.hex("#12"); } catch (e) { threw = /expected #RGB/.test(e.message); }
check("hex rejects garbage", threw, true);

// solid
check("solid paint", h.solid("#ff0000"),
      [{ type: "SOLID", color: { r: 1, g: 0, b: 0 } }]);
check("solid with opacity", h.solid("#ff0000", 0.5),
      [{ type: "SOLID", color: { r: 1, g: 0, b: 0 }, opacity: 0.5 }]);

// frame — the whole point is the order Figma demands
order.length = 0;
const parent = makeFrame();
const f = h.frame(parent, {
  name: "Card", layout: "V", spacing: 16, padding: [24, 12], radius: 8,
  fill: "#ffffff", align: { primary: "CENTER", counter: "MIN" },
});
const idx = (k) => order.indexOf(k);
check("appendChild before layoutMode", idx("appendChild") < idx("layoutMode"), true);
check("layoutMode before sizing", idx("layoutMode") < idx("primaryAxisSizingMode"), true);
check("sizing before itemSpacing", idx("primaryAxisSizingMode") < idx("itemSpacing"), true);
check("hugs both axes when unsized", [f.primaryAxisSizingMode, f.counterAxisSizingMode],
      ["AUTO", "AUTO"]);
check("padding [v,h] expands", [f.paddingTop, f.paddingRight, f.paddingBottom, f.paddingLeft],
      [24, 12, 24, 12]);
check("layout shorthand V", f.layoutMode, "VERTICAL");
check("fill from hex", f.fills, [{ type: "SOLID", color: { r: 1, g: 1, b: 1 } }]);
check("align applied", [f.primaryAxisAlignItems, f.counterAxisAlignItems], ["CENTER", "MIN"]);

// a pinned width must not be overwritten by hug
order.length = 0;
const fixed = h.frame(parent, { layout: "H", w: 320 });
check("pinned width stays FIXED", fixed.primaryAxisSizingMode, undefined);
check("pinned width still hugs height", fixed.counterAxisSizingMode, "AUTO");
check("resize after layoutMode", order.indexOf("layoutMode") < order.indexOf("resize"), true);

// numeric padding
const p = h.frame(parent, { layout: "V", padding: 20 });
check("numeric padding", [p.paddingTop, p.paddingRight], [20, 20]);

// sel
figma.currentPage.selection = [
  { id: "1:2", name: "Btn", type: "FRAME", width: 100, height: 40 },
];
check("sel maps selection", h.sel(), [
  { id: "1:2", name: "Btn", type: "FRAME", w: 100, h: 40 },
]);

// asText — the shape of everything the caller reads, and pays for
check("asText is compact, not pretty-printed",
      api.asText({ a: 1, b: [2, 3] }, []), '{"a":1,"b":[2,3]}');
check("asText rounds Figma's float sizes",
      api.asText({ w: 343.99996948242188 }, []), '{"w":344}');
check("asText leaves strings alone", api.asText("plain", []), "plain");
check("asText falls back to logs", api.asText(undefined, ["one", "two"]), "one\ntwo");
check("asText says Done when there is nothing", api.asText(undefined, []), "Done");

// One call must not be able to eat a whole context window unannounced.
const huge = api.asText("x".repeat(api.MAX_RESULT_CHARS + 500), []);
check("asText truncates a huge result", huge.length < api.MAX_RESULT_CHARS + 200, true);
check("asText says how much it dropped", /truncated: 500 more characters/.test(huge), true);

// ── resolving a variable by name ─────────────────────────────────────────
// A name is what an agent has in hand after `figmosha vars`; before this,
// every binding had to go find an id first.
async function throws(fn) {
  try { await fn(); return null; } catch (e) { return e.message; }
}

(async () => {
  const { resolveVar, resolveStyle, clearExecCaches } = api;

  check("resolves an exact name", (await resolveVar("space/md")).id, "VariableID:1:5");
  check("ignores case", (await resolveVar("Space/MD")).id, "VariableID:1:5");
  check("takes a suffix on a / boundary",
        (await resolveVar("bg/default")).id, "VariableID:1:3");
  check("qualified with the collection name",
        (await resolveVar("Primitives Dark/color/neutral/100")).id, "VariableID:1:2");
  check("passes a Variable through untouched",
        (await resolveVar(VARIABLES[0])).id, "VariableID:1:1");
  check("still resolves a plain id", (await resolveVar("VariableID:1:4")).id,
        "VariableID:1:4");

  // The rung that most needs pinning down: "default" is a suffix of
  // "bg/default", but only a substring of "bg/default-alt".
  check("suffix does not match mid-segment",
        (await resolveVar("default")).id, "VariableID:1:3");

  const amb = await throws(() => resolveVar("color/neutral/100", "h.bF"));
  check("ambiguous name throws instead of guessing", /matches 2/.test(amb || ""), true);
  check("names both candidates with their collections",
        /Primitives\/color\/neutral\/100/.test(amb || "") &&
        /Primitives Dark\/color\/neutral\/100/.test(amb || ""), true);

  const miss = await throws(() => resolveVar("space/nope", "h.bN"));
  check("a miss says where to look", /figmosha vars nope/.test(miss || ""), true);
  check("a miss without a caller is null", await resolveVar("space/nope"), null);

  // A name must not cost a network round trip on its way to failing.
  const importsBefore = calls.importVar;
  await resolveVar("space/nope");
  check("a name never reaches importVariableByKeyAsync",
        calls.importVar - importsBefore, 0);
  calls.importVar = 0;
  check("a library key still does",
        (await resolveVar("a".repeat(40))) === null && calls.importVar === 1, true);

  // The list is read once per exec, not once per binding.
  clearExecCaches();
  calls.listVars = 0;
  await resolveVar("space/md");
  await resolveVar("bg/default");
  check("variable list read once per exec", calls.listVars, 1);
  clearExecCaches();
  await resolveVar("space/md");
  check("and re-read after the exec ends", calls.listVars, 2);

  // ── styles ─────────────────────────────────────────────────────────────
  check("resolves a paint style by name",
        (await resolveStyle("fill", "Brand/Primary")).id, "S:aaa");
  check("resolves a text style from its own list",
        (await resolveStyle("text", "Body/Regular")).id, "S:ccc");
  check("resolves a style by id", (await resolveStyle("fill", "S:bbb")).id, "S:bbb");
  const ambStyle = await throws(() => resolveStyle("fill", "Brand"));
  check("an unqualified style prefix is not a match",
        /no fill style named/.test(ambStyle || ""), true);
  const badKind = await throws(() => resolveStyle("shadow", "whatever"));
  check("an unknown style kind lists the real ones",
        /one of: fill, stroke, text, effect, grid/.test(badKind || ""), true);

  const applied = [];
  const textNode = {
    name: "Label", type: "TEXT",
    async setTextStyleIdAsync(id) { applied.push(["text", id]); },
  };
  await h.applyStyle(textNode, "text", "Body/Regular");
  check("applyStyle uses the setter for its kind", applied, [["text", "S:ccc"]]);
  const wrongKind = await throws(() => h.applyStyle(textNode, "fill", "Brand/Primary"));
  check("and says so when the node has no such style",
        /takes no fill style/.test(wrongKind || ""), true);

  // The caches are dropped by the exec handler itself, not only by hand.
  clearExecCaches();
  calls.listVars = 0;
  await figma.ui.onmessage({ type: "exec", id: "t1", code: "return await h.var_('space/md');" });
  await figma.ui.onmessage({ type: "exec", id: "t2", code: "return await h.var_('space/md');" });
  check("each exec starts with a fresh list", calls.listVars, 2);
  check("exec still returns its result",
        posted.filter((m) => m.type === "result").length, 2);

  console.log(failed ? `\n${failed} FAILED` : "\nall helper checks passed");
  process.exit(failed ? 1 : 0);
})();
