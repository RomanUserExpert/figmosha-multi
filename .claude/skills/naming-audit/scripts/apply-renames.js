// Apply a confirmed rename map. Prepend:
//   const RENAMES = [{ id: "185:22001", to: "Card/Content" }, ...];

if (typeof RENAMES === "undefined" || !Array.isArray(RENAMES) || !RENAMES.length) {
  throw new Error("prepend: const RENAMES = [{id, to}, ...]");
}

const applied = [], skipped = [];

for (const r of RENAMES) {
  const n = await h.node(r.id);
  if (!n) { skipped.push({ id: r.id, why: "node not found" }); continue; }
  if (n.name === r.to) { skipped.push({ id: r.id, why: "already named " + r.to }); continue; }

  // Refuse the two renames that silently break consumers.
  if (n.type === "COMPONENT" && n.parent && n.parent.type === "COMPONENT_SET") {
    skipped.push({ id: r.id, name: n.name, why: "variant name — rename by hand after checking setProperties() callers" });
    continue;
  }
  let p = n.parent, inInstance = false;
  while (p && p.type !== "PAGE" && p.type !== "DOCUMENT") {
    if (p.type === "INSTANCE") { inInstance = true; break; }
    p = p.parent;
  }
  if (inInstance) {
    skipped.push({ id: r.id, name: n.name, why: "inside an instance — rename the main component instead" });
    continue;
  }

  const before = n.name;
  n.name = r.to;
  applied.push({ id: n.id, before: before, after: n.name });
}

return { applied: applied, skipped: skipped, counts: { applied: applied.length, skipped: skipped.length } };
