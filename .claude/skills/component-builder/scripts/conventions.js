// Extract the conventions of reference components so a new one can match them.
// Read-only. Fed with:  --set 'REFS=["185:21883","185:22010"]'

if (typeof REFS === "undefined" || !REFS.length) {
  throw new Error("no REFS: pass --set 'REFS=[\"<component-set-or-component-id>\"]'");
}

const isMixed = (v) => typeof v === "symbol";

function layout(n) {
  if (!n.layoutMode || n.layoutMode === "NONE") return null;
  return {
    mode: n.layoutMode,
    itemSpacing: n.itemSpacing,
    padding: [n.paddingTop, n.paddingRight, n.paddingBottom, n.paddingLeft],
    primary: n.primaryAxisSizingMode,
    counter: n.counterAxisSizingMode,
    align: n.counterAxisAlignItems,
  };
}

// Which variables are bound where, keyed by layer name.
function bindings(root) {
  const out = {};
  const add = (name, prop, id) => {
    out[name] = out[name] || {};
    out[name][prop] = id;
  };
  const walk = (n) => {
    const bv = n.boundVariables || {};
    for (const k of Object.keys(bv)) {
      if (k === "fills" || k === "strokes") continue;
      const v = bv[k];
      add(n.name, k, v && v.id ? v.id : JSON.stringify(v));
    }
    if (!isMixed(n.fills) && Array.isArray(n.fills)) {
      n.fills.forEach((p, i) => {
        const c = p.boundVariables && p.boundVariables.color;
        add(n.name, "fill[" + i + "]", c ? c.id : (p.type === "SOLID" ? "RAW" : p.type));
      });
    }
    if (!isMixed(n.strokes) && Array.isArray(n.strokes) && n.strokes.length) {
      n.strokes.forEach((p, i) => {
        const c = p.boundVariables && p.boundVariables.color;
        add(n.name, "stroke[" + i + "]", c ? c.id : "RAW");
      });
    }
    if (n.type === "TEXT") add(n.name, "textStyleId", n.textStyleId || "RAW");
    if (n.children) for (const c of n.children) walk(c);
  };
  walk(root);
  return out;
}

// Resolve variable ids to names so the report is readable.
const varNames = {};
async function nameOf(id) {
  if (!id || typeof id !== "string" || id.indexOf("VariableID") !== 0) return id;
  if (varNames[id] !== undefined) return varNames[id];
  try {
    const v = await figma.variables.getVariableByIdAsync(id);
    varNames[id] = v ? v.name : id;
  } catch (e) { varNames[id] = id; }
  return varNames[id];
}

const report = [];

for (const id of REFS) {
  const n = await h.node(id);
  if (!n) { report.push({ id: id, error: "not found" }); continue; }

  const entry = { id: n.id, name: n.name, type: n.type };

  if (n.type === "COMPONENT_SET") {
    entry.variantGroups = n.variantGroupProperties;
    entry.variantNames = n.children.map((c) => c.name).slice(0, 30);
    entry.variantCount = n.children.length;
    entry.propertyDefs = n.componentPropertyDefinitions;
    const first = n.children[0];
    entry.layout = layout(first);
    entry.layerNames = [];
    const collect = (x, d) => {
      entry.layerNames.push("  ".repeat(d) + x.name + " [" + x.type + "]");
      if (x.children && d < 3) for (const c of x.children) collect(c, d + 1);
    };
    collect(first, 0);
    entry.bindings = bindings(first);
  } else {
    entry.layout = layout(n);
    entry.propertyDefs = n.componentPropertyDefinitions || null;
    entry.bindings = bindings(n);
    entry.layerNames = h.dumpTree(n, { maxDepth: 3, showSize: false, showText: false }).split("\n");
  }

  // swap variable ids for names
  for (const layer of Object.keys(entry.bindings)) {
    for (const prop of Object.keys(entry.bindings[layer])) {
      entry.bindings[layer][prop] = await nameOf(entry.bindings[layer][prop]);
    }
  }

  report.push(entry);
}

// The report is one entry per reference component, and its shape varies with
// what the component has — so it is rendered key by key rather than pinned to
// a fixed table. Still rows: nesting is what made this expensive to read.
const out = [];
const walk = (value, indent) => {
  if (value === null || value === undefined) { out.push(indent + "—"); return; }
  if (Array.isArray(value)) {
    if (!value.length) { out.push(indent + "(none)"); return; }
    for (const item of value) {
      if (item && typeof item === "object") walk(item, indent);
      else out.push(indent + String(item));
    }
    return;
  }
  if (typeof value === "object") {
    for (const k of Object.keys(value)) {
      const v = value[k];
      if (v && typeof v === "object") {
        out.push(indent + k + ":");
        walk(v, indent + "  ");
      } else {
        out.push(indent + k + ": " + v);
      }
    }
    return;
  }
  out.push(indent + String(value));
};
walk(report, "");
return out.join("\n");
