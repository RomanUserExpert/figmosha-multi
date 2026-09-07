// Capture the uSpec template components into a portable JSON description.
//
// Run through the bridge against the file that holds the templates:
//   python capture.py --session "uSpec template (Community)"
//
// Read-only. Nothing is created, moved or modified.
//
// Why a capture rather than a hand-written builder: AUTHORING these templates in JS is a
// trap — 147 named anchor layers whose geometry every
// render script assumes, drifting from upstream on every `uspec-skills update`. Capture-and-
// replay is a different thing: the description is read off the real component by machine, and
// replay produces real components with real #anchor layers, so the render scripts and their
// upgrades are unaffected.
//
// The templates were measured first (see ../uspec-templates.md): 330 nodes, only COMPONENT /
// FRAME / TEXT / VECTOR, and zero bound variables, styles, images, instances, effects or
// component properties. That is what makes a faithful capture possible at all — there is
// nothing in them that belongs to the file they live in. This script asserts those invariants
// and fails loudly if a future template version breaks them.

await figma.loadAllPagesAsync();

const WANT = ["Screen reader", "Color Annotation", "Anatomy", "API", "Property", "Structure", "Motion"];

const tops = figma.root
  .findAllWithCriteria({ types: ["COMPONENT"] })
  .filter((n) => !(n.parent && n.parent.type === "COMPONENT_SET"));

const missing = WANT.filter((w) => !tops.some((t) => t.name === w));
if (missing.length) throw new Error("templates not found in " + figma.root.name + ": " + missing.join(", "));

const violations = [];
const fonts = {};

// Properties worth carrying. Anything absent on a node is simply skipped, so one list serves
// all four node types.
const SCALARS = [
  "visible", "opacity", "blendMode", "isMask", "clipsContent",
  "layoutMode", "layoutWrap", "layoutAlign", "layoutGrow", "layoutPositioning",
  "primaryAxisSizingMode", "counterAxisSizingMode",
  "primaryAxisAlignItems", "counterAxisAlignItems",
  "layoutSizingHorizontal", "layoutSizingVertical",
  "itemSpacing", "counterAxisSpacing", "itemReverseZIndex",
  "paddingTop", "paddingRight", "paddingBottom", "paddingLeft",
  "topLeftRadius", "topRightRadius", "bottomLeftRadius", "bottomRightRadius",
  "strokeWeight", "strokeAlign", "strokeCap", "strokeJoin", "strokeMiterLimit", "dashPattern",
  // Per-side weights: three frames (#ruler-track, #tick, #track-area) draw a rule on one edge
  // only, which reads as `strokeWeight: mixed`. Capturing the scalar alone would replay them
  // as a full box.
  "strokeTopWeight", "strokeRightWeight", "strokeBottomWeight", "strokeLeftWeight",
  "rotation", "cornerSmoothing", "overflowDirection",
  // TEXT
  "characters", "fontSize", "textAlignHorizontal", "textAlignVertical", "textAutoResize",
  "textCase", "textDecoration", "paragraphSpacing", "paragraphIndent", "textTruncation",
  "maxLines", "autoRename", "hangingPunctuation", "hangingList", "leadingTrim",
  // VECTOR
  "handleMirroring",
];

const val = (v) => (v === figma.mixed ? "__MIXED__" : v);

function capture(n) {
  // Invariants: anything below would not survive a move to another file.
  if (n.boundVariables && Object.keys(n.boundVariables).length)
    violations.push(n.name + ": boundVariables");
  for (const p of ["fillStyleId", "strokeStyleId", "textStyleId", "effectStyleId", "gridStyleId"])
    if (typeof n[p] === "string" && n[p]) violations.push(n.name + ": " + p);
  if (n.type === "INSTANCE") violations.push(n.name + ": nested INSTANCE");
  if (Array.isArray(n.fills))
    for (const f of n.fills)
      if (f.type === "IMAGE" || f.type === "VIDEO") violations.push(n.name + ": " + f.type + " fill");

  const out = { name: n.name, type: n.type, width: n.width, height: n.height };

  for (const p of SCALARS) {
    const v = n[p];
    if (v !== undefined && v !== null) out[p] = val(v);
  }

  // A child of an auto-layout parent gets its position from the layout; a child of a plain
  // frame needs x/y. Carry both — replay decides which to apply.
  if (typeof n.x === "number") { out.x = n.x; out.y = n.y; }
  if (n.constraints) out.constraints = n.constraints;

  if (Array.isArray(n.fills)) out.fills = JSON.parse(JSON.stringify(n.fills));
  if (Array.isArray(n.strokes)) out.strokes = JSON.parse(JSON.stringify(n.strokes));
  if (Array.isArray(n.effects) && n.effects.length) out.effects = JSON.parse(JSON.stringify(n.effects));

  if (n.type === "TEXT") {
    const f = n.fontName;
    if (f === figma.mixed) {
      // Mixed runs: capture per-character-range so replay can rebuild them.
      out.fontName = "__MIXED__";
      out.runs = [];
      let i = 0;
      while (i < n.characters.length) {
        const end = n.getRangeAllFontNames(i, i + 1) && i + 1;
        const fn = n.getRangeFontName(i, i + 1);
        const last = out.runs[out.runs.length - 1];
        if (last && last.family === fn.family && last.style === fn.style) last.end = end;
        else out.runs.push({ start: i, end: end, family: fn.family, style: fn.style });
        i++;
      }
      for (const r of out.runs) fonts[r.family + "|" + r.style] = true;
    } else {
      out.fontName = { family: f.family, style: f.style };
      fonts[f.family + "|" + f.style] = true;
    }
    out.lineHeight = val(n.lineHeight);
    out.letterSpacing = val(n.letterSpacing);
  }

  if (n.type === "VECTOR") {
    out.vectorPaths = JSON.parse(JSON.stringify(n.vectorPaths));
  }

  if (n.children) out.children = n.children.map(capture);
  return out;
}

const templates = {};
for (const w of WANT) templates[w] = capture(tops.find((t) => t.name === w));

// Anything still reading `mixed` after the per-side weights are captured is a real fidelity
// gap. Surfaced rather than swallowed, so replay never quietly draws something else.
const gaps = [];
const SIDE_WEIGHTS = ["strokeTopWeight", "strokeRightWeight", "strokeBottomWeight", "strokeLeftWeight"];
const scanGaps = (o, path) => {
  for (const [k, v] of Object.entries(o)) {
    if (v !== "__MIXED__" || k === "fontName") continue;
    // A mixed strokeWeight is fully described by the four per-side weights, so it replays
    // exactly. Not a gap.
    if (k === "strokeWeight" && SIDE_WEIGHTS.every((s) => typeof o[s] === "number")) continue;
    gaps.push({ node: path + "/" + o.name, type: o.type, property: k });
  }
  for (const c of o.children || []) scanGaps(c, path + "/" + o.name);
};

for (const w of WANT) scanGaps(templates[w], w);

const countNodes = (o) => 1 + (o.children || []).reduce((n, c) => n + countNodes(c), 0);
const countAnchors = (o) =>
  (o.name.charAt(0) === "#" ? 1 : 0) + (o.children || []).reduce((n, c) => n + countAnchors(c), 0);

return {
  capturedFrom: figma.root.name,
  capturedAt: new Date().toISOString(),
  fontsRequired: Object.keys(fonts).map((k) => ({ family: k.split("|")[0], style: k.split("|")[1] })),
  violations: violations,
  fidelityGaps: gaps,
  perTemplate: WANT.map((w) => ({
    name: w,
    nodes: countNodes(templates[w]),
    anchors: countAnchors(templates[w]),
  })),
  totalNodes: WANT.reduce((n, w) => n + countNodes(templates[w]), 0),
  totalAnchors: WANT.reduce((n, w) => n + countAnchors(templates[w]), 0),
  templates: templates,
};
