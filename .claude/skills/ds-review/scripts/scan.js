// Design-system scan — read-only. Fed with --set:
//   --set ROOT_ID=185:21880
//   const SCALE   = [0,4,8,12,16,24,32,40,48,64];

const root = (typeof ROOT_ID !== "undefined" && ROOT_ID)
  ? await h.node(ROOT_ID)
  : figma.currentPage.selection[0];
if (!root) throw new Error("no root: pass --set ROOT_ID=<id>, or select a frame in Figma");
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
      // h.mainOf, not the raw call: it caches and counts, and warns through
      // print() before the resolves add up to an out-of-memory crash.
      const main = await h.mainOf(n);
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

// ── output ───────────────────────────────────────────────────────────────
// Rows, not nested objects: the same findings cost roughly a third as much,
// and the reader is an agent paying per character. --raw still exists on the
// CLI for anything that wants structure.
const pad = (s, w) => (String(s) + "                                        ")
  .slice(0, Math.max(w, String(s).length));
const at = (r) => pad(r.id, 12) + pad(r.name + " [" + r.type + "]", 30);

const out = [];
const section = (title, rows, detail) => {
  if (!rows.length) return;
  out.push("");
  out.push(title.toUpperCase() + "  " + rows.length +
    (rows.length > CAP ? "  (showing " + CAP + ")" : ""));
  for (const r of rows.slice(0, CAP)) out.push("  " + at(r) + detail(r));
};

out.push(root.id + "  " + root.name + " [" + root.type + "]  ·  visited " + visited +
  ", instances " + instanceCount);
out.push("scale: " + scale.join(", "));

section("raw fills", rawFills, (r) => r.value + (r.idx ? "  #" + r.idx : ""));
section("raw strokes", rawStrokes, (r) => r.value + (r.idx ? "  #" + r.idx : ""));
section("raw text", rawText, (r) => pad(r.font, 24) + pad(r.size, 6) +
  JSON.stringify(r.chars));
section("raw radius", rawRadius, (r) => String(r.radius));
section("off scale", offScale, (r) => r.prop + " " + r.value);
section("likely detached", detached, () => "");

const names = Object.keys(instances).sort((a, b) => instances[b] - instances[a]);
if (names.length) {
  out.push("");
  out.push("INSTANCES  " + instanceCount + " in " + names.length + " components");
  for (const n of names) out.push("  " + pad(instances[n], 5) + n);
}

if (out.length === 2) out.push("", "nothing raw found — everything is bound or styled");
return out.join("\n");
