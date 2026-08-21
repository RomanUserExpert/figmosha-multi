// Layer naming scan — read-only. Fed with:  --set ROOT_ID=185:21880

const root = (typeof ROOT_ID !== "undefined" && ROOT_ID)
  ? await h.node(ROOT_ID)
  : figma.currentPage.selection[0];
if (!root) throw new Error("no root: pass --set ROOT_ID=<id>, or select a frame in Figma");

const CAP = 60;
const DEFAULT_NAME = /^(Frame|Group|Rectangle|Ellipse|Polygon|Star|Line|Vector|Slice|Component|Union|Subtract|Intersect|Exclude)(\s\d+)?$/;

const flat = [];
const walk = (n, depth, path, insideComponent, insideInstance) => {
  flat.push({ node: n, depth: depth, path: path, inComp: insideComponent, inInst: insideInstance });
  if (n.children) {
    const inComp = insideComponent || n.type === "COMPONENT" || n.type === "COMPONENT_SET";
    const inInst = insideInstance || n.type === "INSTANCE";
    for (const c of n.children) walk(c, depth + 1, path + "/" + n.name, inComp, inInst);
  }
};
walk(root, 0, "", false, false);

const defaults = [], risky = [], unnamedText = [], duplicates = [];
let named = 0, maxDepth = 0;

for (const e of flat) {
  const n = e.node;
  maxDepth = Math.max(maxDepth, e.depth);
  const at = { id: n.id, name: n.name, type: n.type, depth: e.depth, parent: e.path.split("/").pop() || root.name };

  if (e.inInst) continue; // insides of instances are the main component's business

  if (DEFAULT_NAME.test(n.name)) defaults.push(at); else named++;

  if (n.type === "TEXT" && n.characters && n.name === n.characters) {
    unnamedText.push(Object.assign({ chars: n.characters.slice(0, 30) }, at));
  }

  if (e.inComp || n.type === "COMPONENT" || n.type === "COMPONENT_SET") {
    let why = e.inComp ? "layer inside a component (instance overrides key off this name)" : null;
    if (n.type === "COMPONENT" && n.parent && n.parent.type === "COMPONENT_SET") {
      why = "variant name — renaming breaks setProperties() callers";
    }
    if (n.type === "COMPONENT_SET") why = "component set name — referenced by consumers";
    if (why) risky.push(Object.assign({ why: why }, at));
  }
}

// duplicate names among siblings — these break h.findByName / findOne silently
const bySibling = {};
for (const e of flat) {
  if (e.inInst) continue;
  const key = e.path + "|" + e.node.name;
  (bySibling[key] = bySibling[key] || []).push(e.node.id);
}
for (const key of Object.keys(bySibling)) {
  if (bySibling[key].length > 1) {
    const parts = key.split("|");
    duplicates.push({ parent: parts[0] || "(root)", name: parts[1], ids: bySibling[key] });
  }
}

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

out.push(root.id + "  " + root.name + " [" + root.type + "]  ·  " + flat.length +
  " layers, depth " + maxDepth + ", named " +
  (flat.length ? Math.round((named / flat.length) * 100) + "%" : "n/a"));

section("default names", defaults, (r) => "in «" + r.parent + "»  depth " + r.depth);
section("text named after its content", unnamedText, (r) => JSON.stringify(r.chars));
section("risky to rename", risky, (r) => r.why || "");

// A duplicate is a group of siblings, not one node, so it has no single id to
// print in the id column.
if (duplicates.length) {
  out.push("");
  out.push("DUPLICATE SIBLINGS  " + duplicates.length +
    (duplicates.length > CAP ? "  (showing " + CAP + ")" : ""));
  for (const r of duplicates.slice(0, CAP)) {
    // The parent here is a full path; its last segment is what locates the
    // group for a human, and the rest is what makes the row unreadable.
    const parent = r.parent.split("/").filter(Boolean).pop() || "(root)";
    out.push("  " + pad(r.name, 26) + pad(r.ids.length + "×", 5) +
      pad("in «" + parent + "»", 30) + "  " + r.ids.slice(0, 4).join(", ") +
      (r.ids.length > 4 ? " …" : ""));
  }
}

if (out.length === 1) out.push("", "nothing to clean up");
return out.join("\n");
