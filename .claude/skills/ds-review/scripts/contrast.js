// WCAG contrast pass for TEXT nodes. Read-only. Prepend:
//   const ROOT_ID = "185:21880";

const root = (typeof ROOT_ID !== "undefined" && ROOT_ID)
  ? await h.node(ROOT_ID)
  : figma.currentPage.selection[0];
if (!root) throw new Error("no root: prepend ROOT_ID or select a frame in Figma");

const isMixed = (v) => typeof v === "symbol";
const hex = (c) => "#" + [c.r, c.g, c.b]
  .map((v) => Math.round(v * 255).toString(16).padStart(2, "0")).join("");

// sRGB relative luminance per WCAG 2.1
const lum = (c) => {
  const f = (x) => (x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4));
  return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
};
const ratio = (a, b) => {
  const la = lum(a), lb = lum(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
};

// Resolve a paint's actual colour, following a bound variable if there is one.
async function paintColor(paint, node) {
  if (!paint || paint.type !== "SOLID") return null;
  const bv = paint.boundVariables && paint.boundVariables.color;
  if (bv && bv.id) {
    try {
      const v = await figma.variables.getVariableByIdAsync(bv.id);
      const r = v && v.resolveForConsumer(node);
      if (r && r.value && typeof r.value.r === "number") return r.value;
    } catch (e) { /* fall through to the literal colour */ }
  }
  return paint.color;
}

// First ancestor (self included) painting an opaque solid background.
async function backgroundOf(node) {
  let n = node.parent;
  while (n && n.type !== "PAGE" && n.type !== "DOCUMENT") {
    if (!isMixed(n.fills) && Array.isArray(n.fills)) {
      for (let i = n.fills.length - 1; i >= 0; i--) {
        const p = n.fills[i];
        if (p.visible === false) continue;
        if (p.type !== "SOLID") return { unresolved: p.type, owner: n };
        if ((p.opacity == null ? 1 : p.opacity) < 1) continue;
        const c = await paintColor(p, n);
        if (c) return { color: c, owner: n };
      }
    }
    n = n.parent;
  }
  return null;
}

// Same black-box rule as scan.js: text inside an INSTANCE belongs to the main component.
const texts = [];
(function collect(n) {
  if (n.type === "TEXT") { texts.push(n); return; }
  if (n.type === "INSTANCE") return;
  if (n.children) for (const c of n.children) collect(c);
})(root);
const rows = [];

for (const t of texts) {
  if (!t.visible) continue;
  const at = { id: t.id, name: t.name, chars: t.characters.slice(0, 30) };

  if (isMixed(t.fills) || !Array.isArray(t.fills) || !t.fills.length) {
    rows.push(Object.assign({ verdict: "needs manual check", why: "mixed or empty fill" }, at));
    continue;
  }
  const fg = await paintColor(t.fills[0], t);
  if (!fg) {
    rows.push(Object.assign({ verdict: "needs manual check", why: "non-solid text fill" }, at));
    continue;
  }

  const bg = await backgroundOf(t);
  if (!bg || !bg.color) {
    rows.push(Object.assign({
      verdict: "needs manual check",
      why: bg ? "background is " + bg.unresolved : "no opaque background found",
      fg: hex(fg),
    }, at));
    continue;
  }

  const size = isMixed(t.fontSize) ? null : t.fontSize;
  const style = isMixed(t.fontName) ? "" : t.fontName.style;
  const bold = /bold|black|heavy|semibold/i.test(style);
  const large = size != null && (size >= 24 || (bold && size >= 18.66));
  const need = large ? 3.0 : 4.5;
  const r = Math.round(ratio(fg, bg.color) * 100) / 100;

  rows.push(Object.assign({
    fg: hex(fg),
    bg: hex(bg.color),
    bgFrom: bg.owner.name,
    size: size == null ? "MIXED" : size,
    required: need,
    ratio: r,
    verdict: size == null ? "needs manual check" : (r >= need ? "pass" : "FAIL"),
  }, at));
}

const fails = rows.filter((r) => r.verdict === "FAIL");
const manual = rows.filter((r) => r.verdict === "needs manual check");

return {
  root: { id: root.id, name: root.name },
  totals: { texts: rows.length, fail: fails.length, manual: manual.length },
  fail: fails,
  manual: manual.slice(0, 30),
};
