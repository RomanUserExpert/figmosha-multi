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
  // root.children is readable without loading any page in either mode, which
  // is what makes h.pages() the cheap way to split a scan.
  root: { name: "Stub", children: [] },
  // clientStorage is absent from this stub on purpose: the sid must still work
  // when it is unavailable, which is what the try/catch around it is for.
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

  // ── dumpTree ceilings ──────────────────────────────────────────────────
  // A real page is deep and repetitive; a walk with neither ceiling is a
  // command that looks cheap and returns a context window.
  const kid = (id, name, children) => ({
    id, name, type: "FRAME", width: 10, height: 10, children: children || [],
  });
  const deep = kid("0:1", "Root", [
    kid("0:2", "A", [kid("0:3", "B", [kid("0:4", "C", [kid("0:5", "D", [])])])]),
  ]);

  const cut = await h.dumpTree(deep, { maxDepth: 2 });
  check("depth stops where it was told", /C \[FRAME\]/.test(cut), false);
  check("and says how much it hid", /… \+2 deeper/.test(cut), true);
  check("full depth still walks everything",
        /D \[FRAME\]/.test(await h.dumpTree(deep, { maxDepth: 99 })), true);

  const many = (n, name) => Array.from({ length: n }, (_, i) => kid("9:" + i, name));
  const three = await h.dumpTree(kid("9:9", "Row", many(30, "Item")), {});
  check("identical siblings collapse to one row",
        (three.match(/Item \[FRAME\]/g) || []).length, 1);
  check("and the collapsed row counts them", /… 29 more siblings named the same/.test(three), true);
  check("with the id range spelled out", /\(9:1 … 9:29\)/.test(three), true);

  const two = await h.dumpTree(kid("9:9", "Row", many(2, "Item")), {});
  check("a pair is not worth collapsing",
        (two.match(/Item \[FRAME\]/g) || []).length, 2);

  const off = await h.dumpTree(kid("9:9", "Row", many(30, "Item")), { collapse: false });
  check("--no-collapse lists them all",
        (off.match(/Item \[FRAME\]/g) || []).length, 30);

  // Same name, different type — collapsing those would claim more than it knows.
  const mixed = kid("9:9", "Row", [
    kid("9:1", "Item"), kid("9:2", "Item"),
    { id: "9:3", name: "Item", type: "TEXT", characters: "x", width: 1, height: 1 },
  ]);
  check("type is part of the sameness",
        ((await h.dumpTree(mixed, {})).match(/Item \[/g) || []).length, 3);


  // ── h.walk: the budget, the prune and the cursor ──────────────────────
  // This is the primitive every scan is supposed to be built on, so what is
  // pinned here is exactly what a scan depends on: it stops when told, it does
  // not wander into instances, and it can be picked up again where it stopped.
  const inst = (id, name, children) => ({
    id, name, type: "INSTANCE", width: 10, height: 10, children: children || [],
  });

  const tree = kid("w:0", "Root", [
    kid("w:1", "A", [kid("w:2", "A1"), kid("w:3", "A2")]),
    inst("w:4", "Card", [kid("w:5", "inside"), kid("w:6", "inside2")]),
    kid("w:7", "B"),
  ]);

  let seen = (await h.walk(tree, (n) => n.id)).found;
  check("walk skips what arrived inside an instance",
        seen, ["w:0", "w:1", "w:2", "w:3", "w:4", "w:7"]);

  seen = (await h.walk(tree, (n) => n.id, { pruneInstances: false })).found;
  check("…unless nested copies are the point",
        seen, ["w:0", "w:1", "w:2", "w:3", "w:4", "w:5", "w:6", "w:7"]);

  const capped = await h.walk(tree, (n) => n.id, { maxNodes: 2 });
  check("maxNodes stops the walk", capped.partial, true);
  check("and says why", capped.reason, "maxNodes");

  // The cursor is the point of stopping early: a scan interrupted by a budget
  // has to be resumable, or every overrun costs the whole subtree again.
  let all = [];
  let cursor = null;
  for (let round = 0; round < 10; round++) {
    const step = await h.walk(tree, (n) => n.id, { maxNodes: 2, cursor });
    all = all.concat(step.found);
    if (!step.partial) break;
    cursor = step.cursor;
  }
  check("a resumed walk visits every node exactly once",
        all, ["w:0", "w:1", "w:2", "w:3", "w:4", "w:7"]);

  const deepWalk = await h.walk(tree, (n) => n.id, { maxDepth: 1 });
  check("maxDepth applies to the walk too",
        deepWalk.found, ["w:0", "w:1", "w:4", "w:7"]);

  // An async visit is the normal case — h.mainOf is one — so it must be awaited
  // rather than collected as a pile of pending promises.
  const asyncSeen = await h.walk(tree, async (n) => n.name, { maxDepth: 1 });
  check("an async visit is awaited", asyncSeen.found, ["Root", "A", "Card", "B"]);


  // A walk may be given less than the whole exec's budget — several walks in
  // one script, or time kept back for the writing that follows.
  const slow = kid("s:0", "Root", Array.from({ length: 900 }, (_, i) => kid("s:" + i, "N")));
  const budgeted = await h.walk(slow, (n) => {
    const stop = Date.now() + 2;        // 2ms a node: 900 of them cannot fit in 20ms
    while (Date.now() < stop) {}
    return n.id;
  }, { budgetMs: 20 });
  check("budgetMs stops a walk early", budgeted.partial, true);
  check("and names the budget as the reason", budgeted.reason, "deadline");
  check("and hands back where it stopped", budgeted.cursor.length > 0, true);


  // A stride over this check would multiply the overshoot by the cost of the
  // visit, and the visit is the expensive part of any real scan.
  const wide = kid("t:0", "Root", Array.from({ length: 40 }, (_, i) => kid("t:" + i, "N")));
  const t0 = Date.now();
  const tight = await h.walk(wide, () => {
    const stop = Date.now() + 5;
    while (Date.now() < stop) {}
  }, { budgetMs: 30 });
  check("the budget is checked every node, not every Nth", tight.partial, true);
  check("so the overshoot is one visit, not many", Date.now() - t0 < 80, true);

  // ── h.mainOf: the expensive call, counted ─────────────────────────────
  let resolves = 0;
  const main = { id: "C:1", name: "Card", key: "abc" };
  const instance = {
    id: "i:1", type: "INSTANCE",
    async getMainComponentAsync() { resolves++; return main; },
  };
  check("mainOf resolves the component", (await h.mainOf(instance)).name, "Card");
  await h.mainOf(instance);
  check("and asks Figma once per instance", resolves, 1);
  check("a non-instance is null, not an error", await h.mainOf(kid("x", "F")), null);

  // ── h.importComp: a call that can never settle ────────────────────────
  const hung = new Promise(() => {});
  figma.importComponentByKeyAsync = () => hung;
  let importError = "";
  try {
    await h.importComp("deadbeef", { timeout: 30 });
  } catch (e) { importError = e.message; }
  check("a hung import fails instead of waiting forever",
        /did not settle in/.test(importError), true);
  check("and points at the way that does work",
        /h\.mainOf/.test(importError), true);

  // ── the exec handler: one at a time, and it says when it starts ───────
  posted.length = 0;
  await figma.ui.onmessage({ type: "exec", id: "q1", code: "return 1;" });
  check("an exec announces that it started",
        posted.filter((m) => m.type === "started" && m.id === "q1").length, 1);

  // Two execs delivered without waiting for the first to finish must not
  // interleave: they share CURRENT_PRINT and the per-exec caches.
  posted.length = 0;
  const first = figma.ui.onmessage({ type: "exec", id: "q2",
    code: "print('a'); await new Promise(r => setTimeout(r, 20)); print('b'); return 1;" });
  const second = figma.ui.onmessage({ type: "exec", id: "q3",
    code: "print('c'); return 2;" });
  await Promise.all([first, second]);
  const order2 = posted.filter((m) => m.type === "log").map((m) => m.text);
  check("a second exec waits for the first", order2, ["a", "b", "c"]);


  // The budget the helpers see is the caller's, less a margin: a partial result
  // that arrives after the caller gave up is the same as no result at all.
  posted.length = 0;
  await figma.ui.onmessage({ type: "exec", id: "q5", deadline_ms: 10000,
    code: "return Math.round(h.left());" });
  const left = Number(posted.filter((m) => m.type === "result" && m.id === "q5")[0].text);
  check("the walking budget stops short of the caller's deadline",
        left <= 9000 && left > 8500, true);

  // ── the deadline lives inside the loop ────────────────────────────────
  // Nothing outside the thread can stop a running script, so the only honest
  // test is that a walk started with a spent budget gives up at once.
  posted.length = 0;
  await figma.ui.onmessage({
    type: "exec", id: "q4", deadline_ms: 1,
    code: "const t = {id: 'a', type: 'FRAME', children: [{id: 'b', type: 'FRAME', children: []}]};" +
          "await new Promise(r => setTimeout(r, 10));" +
          "const r = await h.walk(t, n => n.id); return r.partial + ' ' + r.reason;",
  });
  const out4 = posted.filter((m) => m.type === "result" && m.id === "q4")[0];
  check("a walk past its deadline stops and says so", out4.text, "true deadline");

  // The caches are dropped by the exec handler itself, not only by hand.
  posted.length = 0;
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
