// Dev-handoff extraction — read-only. Fed with:  --set ROOT_ID=185:21880
// Every numeric/colour value is reported together with the token behind it.

const root = (typeof ROOT_ID !== "undefined" && ROOT_ID)
  ? await h.node(ROOT_ID)
  : figma.currentPage.selection[0];
if (!root) throw new Error("no root: pass --set ROOT_ID=<id>, or select a frame in Figma");

const isMixed = (v) => typeof v === "symbol";
const hex = (c) => "#" + [c.r, c.g, c.b]
  .map((v) => Math.round(v * 255).toString(16).padStart(2, "0")).join("");

const varCache = {};
async function tokenName(id) {
  if (!id) return null;
  if (varCache[id] !== undefined) return varCache[id];
  try {
    const v = await figma.variables.getVariableByIdAsync(id);
    varCache[id] = v ? v.name : null;
  } catch (e) { varCache[id] = null; }
  return varCache[id];
}
const styleCache = {};
async function styleName(id) {
  if (!id || typeof id !== "string") return null;
  if (styleCache[id] !== undefined) return styleCache[id];
  try {
    const s = await figma.getStyleByIdAsync(id);
    styleCache[id] = s ? s.name : null;
  } catch (e) { styleCache[id] = null; }
  return styleCache[id];
}
async function boundToken(node, prop) {
  const bv = node.boundVariables && node.boundVariables[prop];
  return bv && bv.id ? await tokenName(bv.id) : null;
}

// Instances are black boxes: report component + props, not their internals.
const flat = [];
const walk = (n) => {
  flat.push(n);
  if (n.type === "INSTANCE") return;
  if (n.children) for (const c of n.children) walk(c);
};
walk(root);

const layout = [], typography = [], colors = [], radius = [], sizing = [], instances = [], untokenized = [];

for (const n of flat) {
  const at = { id: n.id, name: n.name, type: n.type };

  if (n.type === "INSTANCE") {
    const row = Object.assign({ w: Math.round(n.width), h: Math.round(n.height) }, at);
    try {
      const main = await n.getMainComponentAsync();
      row.component = main
        ? ((main.parent && main.parent.type === "COMPONENT_SET") ? main.parent.name : main.name)
        : "?";
      row.variant = main ? main.name : "?";
    } catch (e) { row.component = "unresolved"; }
    const props = {};
    for (const k of Object.keys(n.componentProperties || {})) props[k] = n.componentProperties[k].value;
    row.props = props;
    instances.push(row);
    continue;
  }

  // ── auto-layout ────────────────────────────────────────────────────────
  if (n.layoutMode && n.layoutMode !== "NONE") {
    const tk = {};
    for (const p of ["itemSpacing", "paddingTop", "paddingRight", "paddingBottom", "paddingLeft"]) {
      const t = await boundToken(n, p);
      if (t) tk[p] = t;
      else if (n[p]) untokenized.push(Object.assign({ prop: p, value: n[p] }, at));
    }
    layout.push(Object.assign({
      direction: n.layoutMode,
      gap: n.itemSpacing,
      padding: [n.paddingTop, n.paddingRight, n.paddingBottom, n.paddingLeft].join(" "),
      sizing: n.primaryAxisSizingMode + "/" + n.counterAxisSizingMode,
      align: n.counterAxisAlignItems,
      tokens: tk,
    }, at));
  }

  // ── typography ─────────────────────────────────────────────────────────
  if (n.type === "TEXT") {
    const st = await styleName(n.textStyleId);
    let color = null, colorToken = null;
    if (!isMixed(n.fills) && Array.isArray(n.fills) && n.fills[0] && n.fills[0].type === "SOLID") {
      color = hex(n.fills[0].color);
      const cb = n.fills[0].boundVariables && n.fills[0].boundVariables.color;
      colorToken = cb && cb.id ? await tokenName(cb.id) : null;
    }
    const lh = isMixed(n.lineHeight) ? "MIXED"
      : (n.lineHeight.unit === "AUTO" ? "auto" : n.lineHeight.value + (n.lineHeight.unit === "PERCENT" ? "%" : "px"));
    typography.push(Object.assign({
      chars: n.characters.slice(0, 40),
      font: isMixed(n.fontName) ? "MIXED" : n.fontName.family + " " + n.fontName.style,
      size: isMixed(n.fontSize) ? "MIXED" : n.fontSize,
      lineHeight: lh,
      letterSpacing: isMixed(n.letterSpacing) ? "MIXED" : n.letterSpacing.value,
      color: color,
      textStyle: st,
      colorToken: colorToken,
    }, at));
    if (!st) untokenized.push(Object.assign({ prop: "textStyle", value: "unstyled" }, at));
    if (color && !colorToken) untokenized.push(Object.assign({ prop: "text color", value: color }, at));
  }

  // ── colours ────────────────────────────────────────────────────────────
  const paintRows = async (list, kind, styleId) => {
    if (isMixed(list) || !Array.isArray(list)) return;
    const sName = await styleName(styleId);
    for (let i = 0; i < list.length; i++) {
      const p = list[i];
      if (p.visible === false) continue;
      const cb = p.boundVariables && p.boundVariables.color;
      const token = cb && cb.id ? await tokenName(cb.id) : sName;
      const value = p.type === "SOLID" ? hex(p.color) : p.type;
      colors.push(Object.assign({ property: kind + "[" + i + "]", value: value, token: token || "— (raw)" }, at));
      if (!token) untokenized.push(Object.assign({ prop: kind, value: value }, at));
    }
  };
  if (n.type !== "TEXT") await paintRows(n.fills, "fill", n.fillStyleId);
  await paintRows(n.strokes, "stroke", n.strokeStyleId);

  // ── radius ─────────────────────────────────────────────────────────────
  if (typeof n.cornerRadius === "number" && n.cornerRadius > 0) {
    const t = await boundToken(n, "topLeftRadius");
    radius.push(Object.assign({ value: n.cornerRadius, token: t || "— (raw)" }, at));
    if (!t) untokenized.push(Object.assign({ prop: "cornerRadius", value: n.cornerRadius }, at));
  } else if (isMixed(n.cornerRadius)) {
    radius.push(Object.assign({
      value: [n.topLeftRadius, n.topRightRadius, n.bottomRightRadius, n.bottomLeftRadius].join(" "),
      token: "— (per-corner)",
    }, at));
  }

  // ── explicit sizing ────────────────────────────────────────────────────
  if (n.width !== undefined) {
    const tw = await boundToken(n, "width");
    const th = await boundToken(n, "height");
    if (tw || th || n === root) {
      sizing.push(Object.assign({
        w: Math.round(n.width), h: Math.round(n.height),
        widthToken: tw || "— (raw)", heightToken: th || "— (raw)",
      }, at));
    }
  }
}

const CAP = 80;
// Rows, not nested objects: the same findings cost roughly a third as much,
// and the reader is an agent paying per character.
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

const tok = (t) => t ? "  → " + t : "";

out.push(root.id + "  " + root.name + "  " + Math.round(root.width) + "×" +
  Math.round(root.height));

// A token per value is the whole point of the spec, so it goes on the row
// rather than into a nested object the reader has to cross-reference.
const tokensOf = (tk) => {
  const keys = Object.keys(tk || {});
  return keys.length ? "  → " + keys.map((k) => k + ":" + tk[k]).join(" ") : "";
};

section("layout", layout, (r) =>
  pad(r.direction, 11) + "gap:" + pad(r.gap, 5) + "pad:" + pad(r.padding, 14) +
  pad(r.sizing, 10) + "align:" + r.align + tokensOf(r.tokens));
section("typography", typography, (r) =>
  pad(r.font, 22) + pad(r.size + "/" + r.lineHeight, 9) + pad(r.color || "—", 9) +
  tok(r.textStyle || r.colorToken) +
  (r.chars ? "  " + JSON.stringify(r.chars) : ""));
section("colors", colors, (r) => pad(r.property, 10) + pad(r.value, 10) + tok(r.token));
section("radius", radius, (r) => pad(r.value, 14) + tok(r.token));
section("sizing", sizing, (r) => pad(r.w + "×" + r.h, 12) +
  tok(r.widthToken + " / " + r.heightToken));
section("instances", instances, (r) =>
  pad(r.component || "?", 24) + pad(r.variant || "", 18) +
  (r.props && Object.keys(r.props).length ? JSON.stringify(r.props) : ""));
section("untokenized", untokenized, (r) => pad(r.prop, 14) + r.value);

return out.join("\n");
