// Derive the file's actual spacing scale instead of assuming one. Read-only.
// Optional:  --set ROOT_ID=185:21880   (default: whole current page)

const root = (typeof ROOT_ID !== "undefined" && ROOT_ID)
  ? await h.node(ROOT_ID)
  : figma.currentPage;

const hist = {};
const bump = (v) => { if (typeof v === "number" && v > 0) hist[v] = (hist[v] || 0) + 1; };

const frames = root.findAll
  ? root.findAll((n) => n.layoutMode && n.layoutMode !== "NONE")
  : [];
for (const n of frames) {
  bump(n.itemSpacing);
  bump(n.paddingTop); bump(n.paddingRight); bump(n.paddingBottom); bump(n.paddingLeft);
}

const sorted = Object.keys(hist)
  .map(Number)
  .sort((a, b) => hist[b] - hist[a] || a - b);

// Spacing variables, if the file defines them — these outrank the histogram.
let spacingVars = [];
try {
  const cols = await figma.variables.getLocalVariableCollectionsAsync();
  const vars = await figma.variables.getLocalVariablesAsync("FLOAT");
  const byId = {};
  for (const c of cols) byId[c.id] = c.name;
  spacingVars = vars
    .filter((v) => /spac|gap|pad|size|scale/i.test(v.name) || /spac|layout/i.test(byId[v.variableCollectionId] || ""))
    .map((v) => ({
      name: v.name,
      collection: byId[v.variableCollectionId],
      values: v.valuesByMode ? Object.keys(v.valuesByMode).map((m) => v.valuesByMode[m]) : [],
    }))
    .slice(0, 60);
} catch (e) {
  spacingVars = [{ error: e.message }];
}

return {
  root: { id: root.id, name: root.name, type: root.type },
  autoLayoutFrames: frames.length,
  // most frequent first — the top cluster is the de-facto scale
  histogram: sorted.slice(0, 25).map((v) => ({ value: v, uses: hist[v] })),
  spacingVariables: spacingVars,
};
