// Rebuild the captured uSpec templates as real components in the open file.
//
// WRITES TO FIGMA. Driven by replay.py, which refuses to run without --yes-write.
//
// Constants injected by replay.py: TEMPLATES, EXPECT_FILE, PAGE_NAME, DRY_RUN, ONLY.
//
// Shape of the run:
//   pass 1  create the tree, set content and paint, size everything
//   pass 2  auto-layout, top-down (a child's sizing needs its parent's layoutMode already set)
//   pass 3  sizing / align / grow, top-down
//   pass 4  absolute x/y for children of non-auto-layout parents
// then re-read what was built and hand it back for the diff. The verification is not in this
// file — replay.py compares the returned description against templates.json.

if (figma.root.name !== EXPECT_FILE)
  throw new Error("wrong file open: " + figma.root.name + " (expected " + EXPECT_FILE + ")");

await figma.loadAllPagesAsync();

const wanted = ONLY && ONLY.length ? ONLY : Object.keys(TEMPLATES);

// ---------------------------------------------------------------- fonts

const fontKeys = {};
const collectFonts = (n) => {
  if (n.type === "TEXT") {
    if (n.fontName && n.fontName !== "__MIXED__") fontKeys[n.fontName.family + "|" + n.fontName.style] = 1;
    for (const r of n.runs || []) fontKeys[r.family + "|" + r.style] = 1;
  }
  for (const c of n.children || []) collectFonts(c);
};
for (const w of wanted) collectFonts(TEMPLATES[w]);

const fonts = Object.keys(fontKeys).map((k) => ({ family: k.split("|")[0], style: k.split("|")[1] }));
const fontErrors = [];
for (const f of fonts) {
  try { await figma.loadFontAsync(f); } catch (e) { fontErrors.push(f.family + " " + f.style + ": " + String(e && e.message)); }
}
if (fontErrors.length) throw new Error("fonts unavailable in this file: " + fontErrors.join("; "));

const existing = figma.root.children.filter((p) => p.name === PAGE_NAME).map((p) => p.name);

if (DRY_RUN) {
  const count = (o) => 1 + (o.children || []).reduce((n, c) => n + count(c), 0);
  return {
    dryRun: true,
    file: figma.root.name,
    pageName: PAGE_NAME,
    pageAlreadyExists: existing.length > 0,
    fontsLoaded: fonts,
    wouldCreate: wanted.map((w) => ({ name: w, nodes: count(TEMPLATES[w]) })),
    totalNodes: wanted.reduce((n, w) => n + count(TEMPLATES[w]), 0),
  };
}

// ---------------------------------------------------------------- build

const page = figma.createPage();
page.name = PAGE_NAME;

const SKIP = {
  // derived or set through another channel
  type: 1, name: 1, children: 1, width: 1, height: 1, x: 1, y: 1,
  characters: 1, fontName: 1, runs: 1, vectorPaths: 1, fills: 1, strokes: 1, effects: 1,
  // pass 2 and 3 own these
  layoutMode: 1, layoutWrap: 1, itemSpacing: 1, counterAxisSpacing: 1, itemReverseZIndex: 1,
  paddingTop: 1, paddingRight: 1, paddingBottom: 1, paddingLeft: 1,
  primaryAxisSizingMode: 1, counterAxisSizingMode: 1,
  primaryAxisAlignItems: 1, counterAxisAlignItems: 1,
  layoutSizingHorizontal: 1, layoutSizingVertical: 1,
  layoutAlign: 1, layoutGrow: 1, layoutPositioning: 1,
  // a mixed scalar is described by its per-side siblings
  strokeWeight: 1,
  // read-only in the plugin API
  overflowDirection: 1, autoRename: 1,
};

const set = (node, prop, value) => {
  if (value === "__MIXED__" || value === undefined || value === null) return;
  try { node[prop] = value; } catch (e) { /* property not writable on this node type */ }
};

function build(spec, isRoot) {
  let n;
  if (isRoot) n = figma.createComponent();
  else if (spec.type === "FRAME") n = figma.createFrame();
  else if (spec.type === "TEXT") n = figma.createText();
  else if (spec.type === "VECTOR") n = figma.createVector();
  else throw new Error("unexpected node type in capture: " + spec.type);

  n.name = spec.name;

  if (spec.type === "TEXT") {
    if (spec.fontName && spec.fontName !== "__MIXED__") n.fontName = spec.fontName;
    n.characters = spec.characters || "";
    if (spec.runs) for (const r of spec.runs)
      n.setRangeFontName(r.start, r.end, { family: r.family, style: r.style });
  }
  if (spec.type === "VECTOR" && spec.vectorPaths) n.vectorPaths = spec.vectorPaths;

  if (Array.isArray(spec.fills)) set(n, "fills", spec.fills);
  if (Array.isArray(spec.strokes)) set(n, "strokes", spec.strokes);
  if (Array.isArray(spec.effects)) set(n, "effects", spec.effects);

  for (const [k, v] of Object.entries(spec)) if (!SKIP[k]) set(n, k, v);

  for (const c of spec.children || []) n.appendChild(build(c, false));
  return n;
}

// Auto-layout, parent before child: a child's layoutSizing depends on its parent's layoutMode.
function layout(node, spec) {
  if (spec.layoutMode && spec.layoutMode !== "NONE") {
    node.layoutMode = spec.layoutMode;
    for (const p of ["layoutWrap", "paddingTop", "paddingRight", "paddingBottom", "paddingLeft",
                     "itemSpacing", "counterAxisSpacing", "primaryAxisAlignItems",
                     "counterAxisAlignItems", "itemReverseZIndex"])
      set(node, p, spec[p]);
  }
  const kids = node.children || [];
  (spec.children || []).forEach((cs, i) => { if (kids[i]) layout(kids[i], cs); });
}

function sizing(node, spec) {
  set(node, "layoutPositioning", spec.layoutPositioning);

  // Resize FIRST: resize() writes both axes and silently converts a HUG axis to FIXED, so the
  // captured size has to land before the sizing modes are declared. Getting this backwards is
  // what made the first trial build come back with a FIXED root. See frame.js.
  if (spec.layoutSizingHorizontal === "FIXED" || spec.layoutSizingVertical === "FIXED") {
    try { node.resize(spec.width, spec.height); } catch (e) {}
  }

  set(node, "layoutAlign", spec.layoutAlign);
  set(node, "layoutGrow", spec.layoutGrow);
  set(node, "layoutSizingHorizontal", spec.layoutSizingHorizontal);
  set(node, "layoutSizingVertical", spec.layoutSizingVertical);

  const kids = node.children || [];
  (spec.children || []).forEach((cs, i) => { if (kids[i]) sizing(kids[i], cs); });
}

function place(node, spec) {
  const auto = spec.layoutMode && spec.layoutMode !== "NONE";
  const kids = node.children || [];
  (spec.children || []).forEach((cs, i) => {
    if (!kids[i]) return;
    if (!auto && typeof cs.x === "number") { kids[i].x = cs.x; kids[i].y = cs.y; }
    place(kids[i], cs);
  });
}

const built = [];
let cursorY = 0;
for (const w of wanted) {
  const spec = TEMPLATES[w];
  const comp = build(spec, true);
  page.appendChild(comp);
  try { comp.resize(spec.width, spec.height); } catch (e) {}
  layout(comp, spec);
  sizing(comp, spec);
  place(comp, spec);
  comp.x = 0;
  comp.y = cursorY;
  cursorY += Math.ceil(comp.height) + 200;
  built.push({ name: comp.name, id: comp.id, key: comp.key });
}

// ---------------------------------------------------------------- read back
//
// The same projection capture.js produces, so replay.py can diff shape for shape rather than
// trusting that the writes landed.

const val = (v) => (v === figma.mixed ? "__MIXED__" : v);
const READ = ["visible", "opacity", "blendMode", "layoutMode", "layoutWrap", "layoutAlign",
  "layoutGrow", "layoutPositioning", "primaryAxisAlignItems", "counterAxisAlignItems",
  "layoutSizingHorizontal", "layoutSizingVertical", "itemSpacing", "counterAxisSpacing",
  "paddingTop", "paddingRight", "paddingBottom", "paddingLeft",
  "topLeftRadius", "topRightRadius", "bottomLeftRadius", "bottomRightRadius",
  "strokeAlign", "strokeTopWeight", "strokeRightWeight", "strokeBottomWeight", "strokeLeftWeight",
  "characters", "fontSize", "textAlignHorizontal", "textAlignVertical", "textAutoResize",
  "textCase", "textDecoration", "clipsContent"];

function readBack(n) {
  const o = { name: n.name, type: n.type, width: n.width, height: n.height };
  for (const p of READ) { const v = n[p]; if (v !== undefined && v !== null) o[p] = val(v); }
  if (n.type === "TEXT" && n.fontName && n.fontName !== figma.mixed)
    o.fontName = { family: n.fontName.family, style: n.fontName.style };
  if (Array.isArray(n.fills)) o.fills = JSON.parse(JSON.stringify(n.fills));
  if (n.children) o.children = n.children.map(readBack);
  return o;
}

const result = {};
for (const c of page.children) result[c.name] = readBack(c);

return {
  dryRun: false,
  file: figma.root.name,
  pageName: page.name,
  pageId: page.id,
  built: built,
  readBack: result,
};
