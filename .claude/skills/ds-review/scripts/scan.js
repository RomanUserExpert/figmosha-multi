// Design-system scan — read-only. Prepend before running:
//   const ROOT_ID = "185:21880";
//   const SCALE   = [0,4,8,12,16,24,32,40,48,64];

const root = (typeof ROOT_ID !== "undefined" && ROOT_ID)
  ? await h.node(ROOT_ID)
  : figma.currentPage.selection[0];
if (!root) throw new Error("no root: prepend ROOT_ID or select a frame in Figma");
if (!root.children) throw new Error("root of type " + root.type + " has no children");

const scale = (typeof SCALE !== "undefined" && SCALE) ? SCALE : [0, 4, 8, 12, 16, 24, 32, 40, 48, 64];
const CAP = 40; // max rows per bucket, keeps the response small

const isMixed = (v) => typeof v === "symbol";
const styled  = (id) => typeof id === "string" && id.length > 0;
const boundPaint = (p) => !!(p && p.boundVariables && p.boundVariables.color);
const hex = (c) => "#" + [c.r, c.g, c.b]
  .map((v) => Math.round(v * 255).toString(16).padStart(2, "0")).join("");
const onScale = (v) => scale.indexOf(v) !== -1;

// Collect nodes, treating every INSTANCE as a black box (its insides belong to the main component).
const flat = [];
const walk = (n) => {
  flat.push(n);
  if (n.type === "INSTANCE") return;
  if (n.children) for (const c of n.children) walk(c);
};
walk(root);

const rawFills = [], rawStrokes = [], rawText = [], offScale = [], rawRadius = [], detached = [];
const instances = {};
let visited = 0, instanceCount = 0;

for (const n of flat) {
  visited++;
  const at = { id: n.id, name: n.name, type: n.type };

  if (n.type === "INSTANCE") {
    instanceCount++;
    let label = "?";
    try {
      const main = await n.getMainComponentAsync();
      if (main) {
        label = (main.parent && main.parent.type === "COMPONENT_SET")
          ? main.parent.name + " / " + main.name
          : main.name;
      }
    } catch (e) { label = "unresolved: " + e.message; }
    instances[label] = (instances[label] || 0) + 1;
    continue;
  }

  // A frame/group named like a known component but not an instance = likely detached.
  if ((n.type === "FRAME" || n.type === "GROUP") && /\//.test(n.name) && /=/.test(n.name)) {
    detached.push(at);
  }

  // ── fills ──────────────────────────────────────────────────────────────
  if (!isMixed(n.fills) && Array.isArray(n.fills) && !styled(n.fillStyleId)) {
    n.fills.forEach((p, i) => {
      if (p.visible === false) return;
      if (boundPaint(p)) return;
      if (p.type === "SOLID") rawFills.push(Object.assign({ idx: i, value: hex(p.color) }, at));
      else rawFills.push(Object.assign({ idx: i, value: p.type }, at));
    });
  }

  // ── strokes ────────────────────────────────────────────────────────────
  if (!isMixed(n.strokes) && Array.isArray(n.strokes) && n.strokes.length && !styled(n.strokeStyleId)) {
    n.strokes.forEach((p, i) => {
      if (boundPaint(p)) return;
      rawStrokes.push(Object.assign({ idx: i, value: p.type === "SOLID" ? hex(p.color) : p.type }, at));
    });
  }

  // ── text style ─────────────────────────────────────────────────────────
  if (n.type === "TEXT" && !styled(n.textStyleId)) {
    const f = isMixed(n.fontName) ? { family: "MIXED", style: "MIXED" } : n.fontName;
    rawText.push(Object.assign({
      font: f.family + " " + f.style,
      size: isMixed(n.fontSize) ? "MIXED" : n.fontSize,
      chars: n.characters.slice(0, 30),
    }, at));
  }

  // ── radius ─────────────────────────────────────────────────────────────
  const bv = n.boundVariables || {};
  if (typeof n.cornerRadius === "number" && n.cornerRadius > 0
      && !bv.topLeftRadius && !onScale(n.cornerRadius)) {
    rawRadius.push(Object.assign({ radius: n.cornerRadius }, at));
  }

  // ── auto-layout spacing / padding ──────────────────────────────────────
  if (n.layoutMode && n.layoutMode !== "NONE") {
    const props = ["itemSpacing", "paddingTop", "paddingRight", "paddingBottom", "paddingLeft"];
    for (const p of props) {
      const v = n[p];
      if (typeof v !== "number" || v === 0) continue;
      if (bv[p]) continue;                       // bound to a variable → fine
      if (onScale(v)) continue;
      offScale.push(Object.assign({ prop: p, value: v }, at));
    }
  }
}

const trim = (arr) => ({ count: arr.length, rows: arr.slice(0, CAP) });

return {
  root: { id: root.id, name: root.name, type: root.type },
  scale: scale,
  totals: { visited: visited, instances: instanceCount },
  rawFills: trim(rawFills),
  rawStrokes: trim(rawStrokes),
  rawText: trim(rawText),
  rawRadius: trim(rawRadius),
  offScale: trim(offScale),
  detached: trim(detached),
  instanceUsage: instances,
};
