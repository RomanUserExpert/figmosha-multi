// Build one uSpec template as a detached FRAME, next to the component being documented.
//
// This replaces Step "Import and Detach Template" in every `create-*` skill. That step reads:
//
//     const templateComponent = await figma.importComponentByKeyAsync(TEMPLATE_KEY);
//     const instance = templateComponent.createInstance();
//     const frame = instance.detachInstance();
//
// — it imports a component, instantiates it, and throws the component away on the next line.
// Everything downstream works on the plain frame and finds its parts by layer name. So the
// template never needs to exist as a component in the file at all: building the same node tree
// from the captured description produces the identical frame, with the identical `#anchor`
// layers, and skips the import that hangs for an unpublished cross-file key.
//
// Returns the same shape the original step returns: { frameId, pageId, pageName }.
//
// Constants the caller injects:
//   SPEC              one entry of templates.json -> templates (e.g. the "API" object)
//   COMPONENT_NODE_ID the component being documented; the frame is placed to its right
//   FRAME_NAME        e.g. "my-button API"
//   GAP               optional, defaults to 200

const _GAP = typeof GAP === "number" ? GAP : 200;

const compNode = await figma.getNodeByIdAsync(COMPONENT_NODE_ID);
if (!compNode) throw new Error("component " + COMPONENT_NODE_ID + " not found in " + figma.root.name);

// The page the component lives on. Figmosha inherits Figma Desktop's page context and does not
// need the setCurrentPageAsync walk-up the MCP columns use, but the frame still has to be
// appended to the component's own page rather than to whatever page happens to be current.
let _page = compNode;
while (_page.parent && _page.parent.type !== "DOCUMENT") _page = _page.parent;
if (_page.type !== "PAGE") throw new Error("could not find the page holding " + COMPONENT_NODE_ID);

// ---------------------------------------------------------------- fonts

const _fontKeys = {};
const _collectFonts = (n) => {
  if (n.type === "TEXT") {
    if (n.fontName && n.fontName !== "__MIXED__") _fontKeys[n.fontName.family + "|" + n.fontName.style] = 1;
    for (const r of n.runs || []) _fontKeys[r.family + "|" + r.style] = 1;
  }
  for (const c of n.children || []) _collectFonts(c);
};
_collectFonts(SPEC);
for (const k of Object.keys(_fontKeys)) {
  const f = { family: k.split("|")[0], style: k.split("|")[1] };
  try {
    await figma.loadFontAsync(f);
  } catch (e) {
    throw new Error("font unavailable in " + figma.root.name + ": " + f.family + " " + f.style);
  }
}

// ---------------------------------------------------------------- build
//
// Four passes, because Figma will not take them in one: a child's layoutSizing is only
// settable once its parent already has a layoutMode.

const _SKIP = {
  type: 1, name: 1, children: 1, width: 1, height: 1, x: 1, y: 1,
  characters: 1, fontName: 1, runs: 1, vectorPaths: 1, fills: 1, strokes: 1, effects: 1,
  layoutMode: 1, layoutWrap: 1, itemSpacing: 1, counterAxisSpacing: 1, itemReverseZIndex: 1,
  paddingTop: 1, paddingRight: 1, paddingBottom: 1, paddingLeft: 1,
  primaryAxisSizingMode: 1, counterAxisSizingMode: 1,
  primaryAxisAlignItems: 1, counterAxisAlignItems: 1,
  layoutSizingHorizontal: 1, layoutSizingVertical: 1,
  layoutAlign: 1, layoutGrow: 1, layoutPositioning: 1,
  strokeWeight: 1, overflowDirection: 1, autoRename: 1,
};

// Layout properties are load-bearing: a swallowed failure here is a frame that looks right
// and lays out wrong. They are reported; everything else may fail quietly, because the
// capture carries properties that simply do not exist on every node type.
const _failures = [];
const _LOUD = { layoutSizingHorizontal: 1, layoutSizingVertical: 1, layoutAlign: 1,
                layoutGrow: 1, layoutMode: 1, layoutPositioning: 1 };

const _set = (node, prop, value) => {
  if (value === "__MIXED__" || value === undefined || value === null) return;
  try {
    node[prop] = value;
  } catch (e) {
    if (_LOUD[prop])
      _failures.push({ node: node.name, type: node.type, property: prop, value: value,
                       message: String(e && e.message) });
  }
};

function _build(spec, isRoot) {
  // The root is a FRAME, not a COMPONENT: the original step detaches to a frame anyway.
  let n;
  if (isRoot || spec.type === "FRAME" || spec.type === "COMPONENT") n = figma.createFrame();
  else if (spec.type === "TEXT") n = figma.createText();
  else if (spec.type === "VECTOR") n = figma.createVector();
  else throw new Error("unexpected node type in the captured template: " + spec.type);

  n.name = spec.name;

  if (spec.type === "TEXT") {
    if (spec.fontName && spec.fontName !== "__MIXED__") n.fontName = spec.fontName;
    n.characters = spec.characters || "";
    if (spec.runs) for (const r of spec.runs)
      n.setRangeFontName(r.start, r.end, { family: r.family, style: r.style });
  }
  if (spec.type === "VECTOR" && spec.vectorPaths) n.vectorPaths = spec.vectorPaths;

  if (Array.isArray(spec.fills)) _set(n, "fills", spec.fills);
  if (Array.isArray(spec.strokes)) _set(n, "strokes", spec.strokes);
  if (Array.isArray(spec.effects)) _set(n, "effects", spec.effects);

  for (const [k, v] of Object.entries(spec)) if (!_SKIP[k]) _set(n, k, v);
  for (const c of spec.children || []) n.appendChild(_build(c, false));
  return n;
}

function _layout(node, spec) {
  if (spec.layoutMode && spec.layoutMode !== "NONE") {
    node.layoutMode = spec.layoutMode;
    for (const p of ["layoutWrap", "itemSpacing", "counterAxisSpacing", "primaryAxisAlignItems",
                     "counterAxisAlignItems", "itemReverseZIndex"]) _set(node, p, spec[p]);
  }
  // Padding regardless of layoutMode. Two Motion frames (#ruler-track, #track-area) keep
  // padding 12/8/12/8 with layoutMode NONE — Figma retains the values after auto-layout is
  // switched off. Inert visually, but carrying them makes the capture round-trip exactly.
  for (const p of ["paddingTop", "paddingRight", "paddingBottom", "paddingLeft"])
    _set(node, p, spec[p]);
  const kids = node.children || [];
  (spec.children || []).forEach((cs, i) => { if (kids[i]) _layout(kids[i], cs); });
}

function _sizing(node, spec) {
  _set(node, "layoutPositioning", spec.layoutPositioning);

  // Resize FIRST. resize() writes both axes, which silently converts a HUG axis to FIXED — so
  // giving the node its captured size has to happen before the sizing modes are declared, not
  // after. This is what made the root frame come back FIXED on the first build.
  if (spec.layoutSizingHorizontal === "FIXED" || spec.layoutSizingVertical === "FIXED") {
    try { node.resize(spec.width, spec.height); } catch (e) {}
  }

  // FILL and STRETCH are the same statement in two vocabularies, and Figma rejects the newer
  // one on a node whose parent axis does not support it. Set layoutAlign first so STRETCH
  // carries the intent, then let layoutSizing* refine it where it is accepted.
  _set(node, "layoutAlign", spec.layoutAlign);
  _set(node, "layoutGrow", spec.layoutGrow);
  _set(node, "layoutSizingHorizontal", spec.layoutSizingHorizontal);
  _set(node, "layoutSizingVertical", spec.layoutSizingVertical);

  const kids = node.children || [];
  (spec.children || []).forEach((cs, i) => { if (kids[i]) _sizing(kids[i], cs); });
}

function _place(node, spec) {
  const auto = spec.layoutMode && spec.layoutMode !== "NONE";
  const kids = node.children || [];
  (spec.children || []).forEach((cs, i) => {
    if (!kids[i]) return;
    if (!auto && typeof cs.x === "number") { kids[i].x = cs.x; kids[i].y = cs.y; }
    _place(kids[i], cs);
  });
}

const frame = _build(SPEC, true);
_page.appendChild(frame);
try { frame.resize(SPEC.width, SPEC.height); } catch (e) {}
_layout(frame, SPEC);
_sizing(frame, SPEC);
_place(frame, SPEC);

frame.x = compNode.x + compNode.width + _GAP;
frame.y = compNode.y;
frame.name = FRAME_NAME;

figma.currentPage.selection = _page.id === figma.currentPage.id ? [frame] : figma.currentPage.selection;
if (_page.id === figma.currentPage.id) figma.viewport.scrollAndZoomIntoView([frame]);

return {
  frameId: frame.id,
  pageId: _page.id,
  pageName: _page.name,
  nodesCreated: frame.findAll(() => true).length + 1,
  anchorsPresent: frame.findAll((n) => n.name.charAt(0) === "#").length,
  layoutFailures: _failures,
};
