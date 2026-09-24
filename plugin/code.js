figma.showUI(__html__, { width: 220, height: 28, title: "Figmosha Bridge" });

// ─── who this plugin instance is ─────────────────────────────────────────

// One plugin instance serves one Figma document, so this id is what the bridge
// routes by.
//
// It is kept in clientStorage rather than regenerated per run, because a re-Run
// is not a new piece of work: switching files in Figma closes the plugin
// window, and a long scan has to come back to the same session afterwards
// rather than re-address itself. Not setPluginData — that writes into the
// document, marks the file as edited and leaves a trail in version history for
// something that is not part of the design.
//
// clientStorage is scoped to (this plugin, this user), not to the file, so the
// key has to be the file's name. Two different files with the same name in one
// account therefore share a stored id; the bridge refuses the second with 1008
// and the loser generates a fresh one (see ui.html).
function newSid() {
  return "s-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
}

let SID = newSid();
let SID_LOADED = false;

function sidKey() {
  return "sid:" + figma.root.name;
}

async function loadSid() {
  if (SID_LOADED) return SID;
  SID_LOADED = true;
  try {
    const saved = await figma.clientStorage.getAsync(sidKey());
    if (typeof saved === "string" && saved) { SID = saved; return SID; }
    await figma.clientStorage.setAsync(sidKey(), SID);
  } catch (e) {
    // No storage, or an older Figma: the generated id still serves this run.
  }
  return SID;
}

async function regenerateSid() {
  SID = newSid();
  try { await figma.clientStorage.setAsync(sidKey(), SID); } catch (e) {}
  return SID;
}

async function identity() {
  await loadSid();
  return {
    type: "identity",
    sid: SID,
    file: figma.root.name,
    page: figma.currentPage.name,
  };
}

// The page is part of how a human recognises a session in `figmosha sessions`,
// and it changes under us while the plugin stays put.
figma.on("currentpagechange", () => {
  figma.ui.postMessage({ type: "page", page: figma.currentPage.name });
});

function safeStringify(value) {
  if (value === undefined) return null;
  try { return JSON.parse(JSON.stringify(value)); } catch (e) {
    try { return String(value); } catch (e2) { return null; }
  }
}

// Ceilings on what one exec may send back. Whoever reads this — a person or an
// agent with a context window — pays for every character, and `figmosha tree
// page` on a real file or a print() inside a loop can produce hundreds of
// kilobytes from a command that looked cheap. Truncation that says so out loud
// is fixable with a narrower query; a silently flooded context is not.
const MAX_RESULT_CHARS = 64 * 1024;
const MAX_LOGS = 200;

// Figma reports sizes as floats: 343.99996948242188 is 344 with ~12 tokens of
// noise attached, on every width and height. Rounded in the text only — the
// structured `value` (--raw) keeps full precision.
function roundish(key, value) {
  if (typeof value === "number" && isFinite(value) && !Number.isInteger(value)) {
    return Math.round(value * 100) / 100;
  }
  return value;
}

function truncate(text) {
  if (text.length <= MAX_RESULT_CHARS) return text;
  return text.slice(0, MAX_RESULT_CHARS) +
    "\n… truncated: " + (text.length - MAX_RESULT_CHARS) + " more characters." +
    " Narrow it down (tree --depth N, find, or return fewer fields).";
}

function asText(value, logs) {
  try {
    if (value !== undefined) {
      // Compact, not pretty-printed: indentation is a third of the tokens of a
      // typical result, and nothing downstream reads this as JSON.
      return truncate(typeof value === "object"
        ? JSON.stringify(value, roundish)
        : String(value));
    }
  } catch (e) { /* unserializable — fall back to logs */ }
  return logs.length > 0 ? truncate(logs.join("\n")) : "Done";
}

// ─── helpers exposed as `h.*` to every exec ──────────────────────────────

// Set for the duration of one exec so helpers can surface warnings through the
// same `print()` the user's code gets. No-op outside an exec.
let CURRENT_PRINT = () => {};

// Read once per exec, dropped in the same `finally` as CURRENT_PRINT. A design
// system is ~400 variables, so binding five names in one script would
// otherwise ask Figma for the whole list five times. Dropped rather than kept,
// because a script that creates variables must not see a stale list on the
// next call.
let VAR_CACHE = null;
let STYLE_CACHE = null;

// When the caller stops waiting, in plugin time. Figma runs plugins on one
// synchronous JS thread that nothing outside can interrupt, so a time limit
// only means something if the code being limited looks at it — which is why
// every walking helper checks h.tick() between nodes, and why a bare findAll
// over a big subtree is still the one thing that can run away.
let DEADLINE = 0;

// Resolving a main component loads the backing component - remote library ones
// included - and what is loaded this way is never released. One scan resolved
// 876 849 of them and took the tab with it. The cache makes a repeated ask
// free; the counter is what lets h.mainOf warn while there is still something
// to narrow down.
let MAIN_CACHE = null;
let MAIN_RESOLVED = 0;
let MAIN_WARNED = 0;
const MAIN_WARN_AT = [5000, 20000, 50000, 100000];

// Pages loaded by hand during this run, for `doctor` to report: under
// documentAccess "dynamic-page" the memory a scan costs is mostly this list.
const PAGES_LOADED = new Set();

function clearExecCaches() {
  VAR_CACHE = null;
  STYLE_CACHE = null;
  MAIN_CACHE = null;
  MAIN_RESOLVED = 0;
  MAIN_WARNED = 0;
  DEADLINE = 0;
}

// Race a promise against a timer. For APIs that can simply never settle:
// importComponentByKeyAsync on an unpublished key from another file returns
// no result and no error, ever, and without this the caller just waits.
function withTimeout(promise, ms, what) {
  let timer = null;
  const bail = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(what)), ms);
  });
  const done = () => { if (timer !== null) clearTimeout(timer); };
  return Promise.race([promise, bail]).then(
    (v) => { done(); return v; },
    (e) => { done(); throw e; }
  );
}

// A library key is 32+ hex characters. A local id is "VariableID:1:23" or
// "S:1:23"; a name looks like neither, and checking the shape up front is what
// keeps a name out of importVariableByKeyAsync — a network round trip that can
// only fail.
const LIBRARY_KEY = /^[0-9a-f]{32,}$/i;

// A snapshot of {name, group}, not the variables themselves. Every property
// read on a Figma object crosses into the engine — measured at ~14 ms to read
// `name` across 438 variables — so a ladder of four rungs over the live list
// would pay that toll four times, on every single resolve. Read once, match in
// plain JS, hand back the original through `ref`.
async function localVars() {
  if (!VAR_CACHE) {
    const cols = await figma.variables.getLocalVariableCollectionsAsync();
    const names = {};
    for (const c of cols) names[c.id] = c.name;
    const vars = await figma.variables.getLocalVariablesAsync();
    VAR_CACHE = vars.map((v) => ({
      name: v.name,
      group: names[v.variableCollectionId] || "?",
      ref: v,
    }));
  }
  return VAR_CACHE;
}

// Name lookup, most specific rung first. The first rung that matches anything
// wins outright: an exact hit in one collection must not be diluted by a
// suffix hit in another.
//
//   color/bg/default            exact, case-sensitive
//   Color/BG/Default            exact, ignoring case
//   Semantics/color/bg/default  qualified with the group (collection) name
//   bg/default                  suffix, on a "/" boundary only
//
// That boundary is the whole point of the last rung: "default" must not match
// "bg/default-alt".
function matchByName(items, query, withGroup) {
  const q = String(query).trim();
  const ql = q.toLowerCase();
  const rungs = [
    (i) => i.name === q,
    (i) => i.name.toLowerCase() === ql,
  ];
  if (withGroup) {
    rungs.push((i) => (i.group + "/" + i.name).toLowerCase() === ql);
  }
  rungs.push((i) => {
    const n = i.name.toLowerCase();
    return n.length > ql.length + 1 && n.slice(-(ql.length + 1)) === "/" + ql;
  });
  for (const rung of rungs) {
    const hit = items.filter(rung);
    if (hit.length) return hit;
  }
  return [];
}

// Ambiguity is an error rather than a guess on purpose: in a themed library
// every primitive exists twice, and binding the wrong one of the pair stays
// invisible until someone opens the file in the other mode.
function ambiguous(who, query, labels) {
  const shown = labels.slice(0, 5);
  return new Error(
    who + ": " + JSON.stringify(query) + " matches " + labels.length +
    " — qualify it:\n  " + shown.join("\n  ") +
    (labels.length > shown.length
      ? "\n  … " + (labels.length - shown.length) + " more" : "")
  );
}

// The narrowest filter that still stands a chance of listing what was meant:
// the last segment is the part the caller is surest about.
function lastSegment(name) {
  const parts = String(name).split("/");
  return parts[parts.length - 1] || String(name);
}

// `who` is the helper this runs on behalf of. Given, a miss becomes an error
// naming where to look; omitted (h.var_), a miss is null and the caller
// decides. An ambiguous *name* throws either way.
async function resolveVar(varOrId, who) {
  if (varOrId == null) return null;
  if (typeof varOrId !== "string") return varOrId;

  if (varOrId.indexOf("VariableID:") === 0) {
    const byId = await figma.variables.getVariableByIdAsync(varOrId);
    if (byId || !who) return byId;
    throw new Error(who + ": no variable with id " + varOrId);
  }

  if (LIBRARY_KEY.test(varOrId)) {
    try {
      return await figma.variables.importVariableByKeyAsync(varOrId);
    } catch (e) {
      if (!who) return null;
      throw new Error(who + ": no library variable with key " + varOrId +
        " — list them: figmosha vars --library");
    }
  }

  const hits = matchByName(await localVars(), varOrId, true);
  if (hits.length === 1) return hits[0].ref;
  if (hits.length > 1) {
    throw ambiguous(who || "h.var_", varOrId,
      hits.map((i) => i.group + "/" + i.name));
  }
  if (!who) return null;
  throw new Error(
    who + ": no variable named " + JSON.stringify(varOrId) +
    " — see what exists: figmosha vars " + lastSegment(varOrId));
}

// Styles have no collections, so the qualified rung does not apply. Fill and
// stroke share one list, because Figma has one kind of paint style.
const STYLE_KINDS = {
  fill:   { list: "getLocalPaintStylesAsync",  apply: "setFillStyleIdAsync" },
  stroke: { list: "getLocalPaintStylesAsync",  apply: "setStrokeStyleIdAsync" },
  text:   { list: "getLocalTextStylesAsync",   apply: "setTextStyleIdAsync" },
  effect: { list: "getLocalEffectStylesAsync", apply: "setEffectStyleIdAsync" },
  grid:   { list: "getLocalGridStylesAsync",   apply: "setGridStyleIdAsync" },
};

function styleKind(kind, who) {
  const spec = STYLE_KINDS[kind];
  if (!spec) {
    throw new Error(who + ": unknown style kind " + JSON.stringify(kind) +
      " — one of: " + Object.keys(STYLE_KINDS).join(", "));
  }
  return spec;
}

async function localStyles(spec) {
  if (!STYLE_CACHE) STYLE_CACHE = {};
  if (!STYLE_CACHE[spec.list]) {
    const list = await figma[spec.list]();
    STYLE_CACHE[spec.list] = list.map((st) => ({ name: st.name, ref: st }));
  }
  return STYLE_CACHE[spec.list];
}

async function resolveStyle(kind, nameOrId, who) {
  who = who || "h.applyStyle";
  const spec = styleKind(kind, who);
  if (nameOrId == null) return null;
  if (typeof nameOrId !== "string") return nameOrId;

  if (nameOrId.indexOf("S:") === 0) {
    const byId = await figma.getStyleByIdAsync(nameOrId);
    if (byId) return byId;
    throw new Error(who + ": no style with id " + nameOrId);
  }

  if (LIBRARY_KEY.test(nameOrId)) {
    try {
      return await figma.importStyleByKeyAsync(nameOrId);
    } catch (e) {
      throw new Error(who + ": no library style with key " + nameOrId);
    }
  }

  const hits = matchByName(await localStyles(spec), nameOrId, false);
  if (hits.length === 1) return hits[0].ref;
  if (hits.length > 1) throw ambiguous(who, nameOrId, hits.map((i) => i.name));
  throw new Error(
    who + ": no " + kind + " style named " + JSON.stringify(nameOrId) +
    " — see what exists: figmosha styles " + lastSegment(nameOrId));
}

// "#1a2b3c" / "1a2b3c" / "#f00" -> {r,g,b} in Figma's 0..1 range.
function hexToRgb(value) {
  let s = String(value).trim().replace(/^#/, "");
  if (s.length === 3) s = s[0] + s[0] + s[1] + s[1] + s[2] + s[2];
  if (!/^[0-9a-fA-F]{6}$/.test(s)) {
    throw new Error("h.hex: expected #RGB or #RRGGBB, got " + JSON.stringify(value));
  }
  const n = parseInt(s, 16);
  return {
    r: ((n >> 16) & 255) / 255,
    g: ((n >> 8) & 255) / 255,
    b: (n & 255) / 255,
  };
}

// Normalise padding given as a number, [v, h], or {top,right,bottom,left}.
function paddingOf(p) {
  if (p == null) return null;
  if (typeof p === "number") return { top: p, right: p, bottom: p, left: p };
  if (Array.isArray(p)) {
    const v = p[0], hz = p.length > 1 ? p[1] : p[0];
    return { top: v, right: hz, bottom: v, left: hz };
  }
  return {
    top: p.top || 0, right: p.right || 0,
    bottom: p.bottom || 0, left: p.left || 0,
  };
}

// Copy a paint array before mutating it — node.fills/strokes are frozen.
function copyPaints(node, prop, who) {
  const paints = node[prop];
  if (typeof paints === "symbol") {
    throw new Error(
      who + ": '" + node.name + "' has mixed " + prop +
      "; set the paint per-range, or unify " + prop + " on the node first"
    );
  }
  if (!Array.isArray(paints)) {
    throw new Error(who + ": '" + node.name + "' has no " + prop);
  }
  return JSON.parse(JSON.stringify(paints));
}

const HELPERS = {
  // Bind fill paint at index to a variable, given as a token name, a local id,
  // or a library key
  async bF(node, idx, varOrId) {
    const v = await resolveVar(varOrId, "h.bF");
    if (!v) throw new Error("h.bF: no variable given");
    const f = copyPaints(node, "fills", "h.bF");
    if (!f[idx]) throw new Error("h.bF: '" + node.name + "' has no fill at index " + idx);
    f[idx] = figma.variables.setBoundVariableForPaint(f[idx], "color", v);
    node.fills = f;
    return v;
  },

  // Bind stroke paint at index
  async bS(node, idx, varOrId) {
    const v = await resolveVar(varOrId, "h.bS");
    if (!v) throw new Error("h.bS: no variable given");
    const s = copyPaints(node, "strokes", "h.bS");
    if (!s[idx]) throw new Error("h.bS: '" + node.name + "' has no stroke at index " + idx);
    s[idx] = figma.variables.setBoundVariableForPaint(s[idx], "color", v);
    node.strokes = s;
    return v;
  },

  // Bind numeric property (radii, padding, sizes, itemSpacing, etc.)
  async bN(node, prop, varOrId) {
    const v = await resolveVar(varOrId, "h.bN");
    if (!v) throw new Error("h.bN: no variable given");
    node.setBoundVariable(prop, v);
    return v;
  },

  // How much of the caller's budget is left, in ms. Infinity when nobody set
  // one. A script with its own loop should check this; everything in h.* does.
  left() {
    return DEADLINE ? DEADLINE - Date.now() : Infinity;
  },

  // True while there is still time. The only way to stop a long synchronous
  // loop before the caller's timeout turns into minutes of a blocked file.
  //
  //     for (const n of nodes) { if (!h.tick()) break; ... }
  tick() {
    return h_left() > 0;
  },

  // Under documentAccess: "dynamic-page" a page nobody opened is not in
  // memory, and children / findAll / findOne on it throw until it is loaded.
  // Idempotent, and free for a page that is already there, so callers never
  // have to know which mode the manifest is in.
  async loadPageOf(node) {
    let n = node;
    while (n && n.type !== "PAGE") n = n.parent;
    if (n && typeof n.loadAsync === "function" && !PAGES_LOADED.has(n.id)) {
      await n.loadAsync();
      PAGES_LOADED.add(n.id);
    }
    return n;
  },

  // Every page, without loading any of them: figma.root.children stays cheap
  // in both modes. This is the right place to split a scan - one request per
  // page, or per top-level frame - instead of walking the file in one call.
  pages() {
    return figma.root.children.map((p) => ({ id: p.id, name: p.name }));
  },

  // An instance's main component. The one supported way to get there.
  //
  // Under dynamic-page `instance.mainComponent` throws outright; in both modes
  // this is the expensive call in any scan, so it is also where the counting
  // happens. The warning arrives while the run can still be narrowed, rather
  // than as an out-of-memory crash afterwards.
  async mainOf(node) {
    if (!node || node.type !== "INSTANCE") return null;
    if (MAIN_CACHE === null) MAIN_CACHE = new Map();
    if (MAIN_CACHE.has(node.id)) return MAIN_CACHE.get(node.id);

    const main = await node.getMainComponentAsync();
    MAIN_CACHE.set(node.id, main);
    MAIN_RESOLVED++;
    if (MAIN_WARNED < MAIN_WARN_AT.length && MAIN_RESOLVED >= MAIN_WARN_AT[MAIN_WARNED]) {
      MAIN_WARNED++;
      CURRENT_PRINT(
        "h.mainOf: resolved " + MAIN_RESOLVED + " main components in this exec. " +
        "Each one loads its component and does not release it — narrow the subtree " +
        "or filter before resolving, or this run ends in an out-of-memory crash."
      );
    }
    return main;
  },

  // Walk a subtree under a budget, and stop cleanly when it runs out.
  //
  // Every expensive thing in a Figma file is shaped like this walk, and the
  // defaults are what keep it from taking the tab with it:
  //
  //   pruneInstances  an instance's children arrived with its component: they
  //                   are copies, not placements somebody made. That is where
  //                   the volume is — one frame in the file this was measured
  //                   on holds 53 439 of them, and 0 of 339 real hits were
  //                   nested. Pass false when nested copies are the point.
  //   budgetMs        how long this walk may take, when it should be less than
  //                   what is left of the caller's timeout — several walks in
  //                   one script, or time reserved for the writing afterwards.
  //                   The caller's own deadline always applies as well, and the
  //                   thread cannot be interrupted from outside, so both are
  //                   checked inside the loop.
  //   maxNodes        the same ceiling counted in nodes rather than ms.
  //   skipInvisible   while walking, figma.skipInvisibleInstanceChildren is on:
  //                   Figma does not build the hidden layers of an instance the
  //                   walk meets. Measured on 2026-09-24 on table-heavy screens:
  //                   a cold section took 3.1–3.7 s without it, 0.2–0.7 s with
  //                   it — the hidden rows and states were the cost, not the
  //                   walk. Pass false when hidden layers inside instances are
  //                   what you are looking for.
  //
  // Out of budget it returns {partial: true, cursor}. Pass that cursor back to
  // carry on from where it stopped — which is what makes a long scan resumable
  // instead of restartable.
  async walk(root, visit, opts) {
    opts = opts || {};
    if (opts.skipInvisible === false) return walkSubtree(root, visit, opts);
    // Restored rather than reset: a script that set the flag itself keeps it.
    const before = figma.skipInvisibleInstanceChildren;
    figma.skipInvisibleInstanceChildren = true;
    try {
      return await walkSubtree(root, visit, opts);
    } finally {
      figma.skipInvisibleInstanceChildren = before;
    }
  },

  // First descendant by exact name
  async findByName(root, name) {
    await HELPERS.loadPageOf(root);
    return root.findOne((n) => n.name === name);
  },

  // All descendants by exact name
  async findAllByName(root, name) {
    await HELPERS.loadPageOf(root);
    return root.findAll((n) => n.name === name);
  },

  // Dump subtree as indented text.
  //
  // Two ceilings, because the shape of a real file defeats a plain walk: it is
  // deep, and it is repetitive. A depth cut that does not say how much it hid
  // leaves the caller guessing whether to dig; a list of forty identical rows
  // costs forty rows to say one thing.
  async dumpTree(node, opts) {
    await HELPERS.loadPageOf(node);
    opts = opts || {};
    const maxDepth = opts.maxDepth == null ? 99 : opts.maxDepth;
    const showSize = opts.showSize !== false;
    const showText = opts.showText !== false;
    const showLayout = opts.showLayout === true;
    const collapse = opts.collapse !== false;
    const lines = [];

    // Counting what a depth cut hides is worth a few thousand nodes, not a
    // full walk of a 53 000-node branch — which is exactly what `tree --depth 1`
    // was reached for to avoid. Past the budget the number becomes a floor and
    // says so, which answers "dig or not" just as well.
    const countBudget = opts.countBudget == null ? 2000 : opts.countBudget;
    const descendants = (n) => {
      let count = 0;
      const stack = n.children ? n.children.slice() : [];
      while (stack.length) {
        if (count >= countBudget) return "≥" + count;
        const c = stack.pop();
        count++;
        if (c.children && c.children.length) for (const k of c.children) stack.push(k);
      }
      return String(count);
    };

    const walk = (n, d) => {
      const pad = "  ".repeat(d);
      let line = pad + n.name + " [" + n.type + "] " + n.id;
      if (showSize && n.width !== undefined) {
        line += " " + Math.round(n.width) + "×" + Math.round(n.height);
      }
      if (showLayout && n.layoutMode && n.layoutMode !== "NONE") {
        line += " {" + n.layoutMode[0] +
          " gap:" + n.itemSpacing +
          " pad:" + n.paddingTop + "," + n.paddingRight + "," + n.paddingBottom + "," + n.paddingLeft +
          " " + n.primaryAxisSizingMode + "/" + n.counterAxisSizingMode + "}";
      }
      if (showText && n.type === "TEXT") line += ' "' + n.characters + '"';
      lines.push(line);

      if (!n.children || !n.children.length) return;
      if (d >= maxDepth) {
        // The number is the whole point: "dig or not" should be a decision
        // made against a count, not a guess.
        const hidden = descendants(n);
        lines.push("  ".repeat(d + 1) + "… +" + hidden + " deeper (--depth)");
        return;
      }

      const kids = n.children;
      for (let i = 0; i < kids.length; i++) {
        let run = 1;
        if (collapse) {
          while (i + run < kids.length &&
                 kids[i + run].name === kids[i].name &&
                 kids[i + run].type === kids[i].type) run++;
        }
        walk(kids[i], d + 1);
        // Three is the threshold: collapsing a pair saves nothing and hides
        // half of what it describes.
        if (run >= 3) {
          const last = kids[i + run - 1];
          lines.push("  ".repeat(d + 1) + "… " + (run - 1) +
            " more siblings named the same (" + kids[i + 1].id + " … " + last.id + ")");
          i += run - 1;
        }
      }
    };
    walk(node, 0);
    return lines.join("\n");
  },

  // Load every unique font in subtree, then run async fn
  async withFonts(rootNode, asyncFn) {
    await HELPERS.loadPageOf(rootNode);
    const texts = rootNode.findAll
      ? rootNode.findAll((n) => n.type === "TEXT")
      : (rootNode.type === "TEXT" ? [rootNode] : []);
    const seen = new Set();
    const fonts = [];
    const skipped = [];
    for (const t of texts) {
      // Mixed-font nodes can't be loaded wholesale; editing one later throws a
      // confusing "font not loaded" far from here, so say it out loud now.
      if (typeof t.fontName === "symbol") { skipped.push(t.name); continue; }
      const fn = t.fontName;
      const key = fn.family + "|" + fn.style;
      if (!seen.has(key)) { seen.add(key); fonts.push(fn); }
    }
    if (skipped.length) {
      CURRENT_PRINT(
        "h.withFonts: skipped " + skipped.length + " mixed-font text node(s): " +
        skipped.slice(0, 5).join(", ") + (skipped.length > 5 ? ", …" : "") +
        " — editing them will fail unless you load each range manually"
      );
    }
    await Promise.all(fonts.map((f) => figma.loadFontAsync(f)));
    return await asyncFn();
  },

  // Set a text node's characters with auto font load (single-font texts only)
  async setText(node, text) {
    if (typeof node.fontName === "symbol") {
      throw new Error("h.setText: text '" + node.name + "' has mixed fonts; load each range manually");
    }
    await figma.loadFontAsync(node.fontName);
    node.characters = text;
  },

  // Clone node and place it next to the original
  cloneNext(node, opts) {
    opts = opts || {};
    const direction = opts.direction || "right";
    const gap = opts.gap == null ? 100 : opts.gap;
    const c = node.clone();
    node.parent.appendChild(c);
    if (direction === "right") { c.x = node.x + node.width + gap; c.y = node.y; }
    else if (direction === "left")  { c.x = node.x - node.width - gap; c.y = node.y; }
    else if (direction === "down")  { c.x = node.x; c.y = node.y + node.height + gap; }
    else if (direction === "up")    { c.x = node.x; c.y = node.y - node.height - gap; }
    if (opts.name) c.name = opts.name;
    return c;
  },

  // Set instance variant properties
  async variant(instance, props) {
    await instance.setProperties(props);
    return instance;
  },

  // Available variants for an instance's component
  async variantsOf(instance) {
    const main = await instance.getMainComponentAsync();
    if (!main) return null;
    const set = main.parent && main.parent.type === "COMPONENT_SET" ? main.parent : null;
    return set
      ? { current: main.name, groups: set.variantGroupProperties, all: set.children.map(c => c.name) }
      : { current: main.name, groups: null, all: null };
  },

  // What the user has selected right now — the bridge between "this one here"
  // and a node id you can act on.
  sel() {
    return figma.currentPage.selection.map((n) => ({
      id: n.id, name: n.name, type: n.type,
      w: n.width, h: n.height,
      chars: n.type === "TEXT" ? n.characters : undefined,
    }));
  },

  // Hex string -> {r,g,b}. Hand-rolling this is where the missing /255 lives.
  hex(value) { return hexToRgb(value); },

  // Ready-to-assign paint array: node.fills = h.solid("#1a2b3c")
  solid(value, opacity) {
    const paint = { type: "SOLID", color: hexToRgb(value) };
    if (opacity != null) paint.opacity = opacity;
    return [paint];
  },

  // Create a frame with auto-layout applied in the order Figma demands:
  // into the tree -> layoutMode -> size -> sizing mode -> spacing/padding.
  // Getting that order wrong silently drops the settings.
  frame(parent, opts) {
    opts = opts || {};
    const f = figma.createFrame();
    if (parent) parent.appendChild(f);

    if (opts.name) f.name = opts.name;

    if (opts.layout) {
      const l = String(opts.layout).toUpperCase();
      f.layoutMode = l === "V" ? "VERTICAL" : l === "H" ? "HORIZONTAL" : l;
    }

    if (opts.w != null || opts.h != null) {
      f.resize(opts.w == null ? f.width : opts.w, opts.h == null ? f.height : opts.h);
    }

    if (f.layoutMode && f.layoutMode !== "NONE") {
      // Hug by default on axes the caller didn't pin to a number.
      if (opts.hug !== false) {
        const horizontalIsPrimary = f.layoutMode === "HORIZONTAL";
        const primaryFixed = horizontalIsPrimary ? opts.w != null : opts.h != null;
        const counterFixed = horizontalIsPrimary ? opts.h != null : opts.w != null;
        if (!primaryFixed) f.primaryAxisSizingMode = "AUTO";
        if (!counterFixed) f.counterAxisSizingMode = "AUTO";
      }
      if (opts.spacing != null) f.itemSpacing = opts.spacing;
      if (opts.align) {
        if (opts.align.primary) f.primaryAxisAlignItems = opts.align.primary;
        if (opts.align.counter) f.counterAxisAlignItems = opts.align.counter;
      }
      const pad = paddingOf(opts.padding);
      if (pad) {
        f.paddingTop = pad.top; f.paddingRight = pad.right;
        f.paddingBottom = pad.bottom; f.paddingLeft = pad.left;
      }
    }

    if (opts.fill != null) f.fills = opts.fill === false ? [] : HELPERS.solid(opts.fill);
    if (opts.radius != null) f.cornerRadius = opts.radius;
    return f;
  },

  // Accept "page" / "sel" alongside a real node id, so callers can say
  // "the thing I'm looking at" without first hunting for its id.
  async resolve(idOrAlias) {
    if (idOrAlias === "page") return figma.currentPage;
    if (idOrAlias === "sel") {
      const s = figma.currentPage.selection;
      if (!s.length) throw new Error("nothing selected in Figma");
      return s[0];
    }
    const node = await figma.getNodeByIdAsync(idOrAlias);
    // Under dynamic-page a node can be handed back while its page is still
    // unloaded, and then children / findAll / findOne on it throw. Loading it
    // here is what lets every CLI subcommand work on any page without knowing
    // this rule — they all resolve their target through here.
    if (node) await HELPERS.loadPageOf(node);
    return node;
  },

  // Apply a local or library style by name, id, or key. kind is one of
  // fill / stroke / text / effect / grid.
  async applyStyle(node, kind, nameOrId) {
    const spec = styleKind(kind, "h.applyStyle");
    const style = await resolveStyle(kind, nameOrId, "h.applyStyle");
    if (!style) throw new Error("h.applyStyle: no style given");
    if (typeof node[spec.apply] !== "function") {
      throw new Error("h.applyStyle: '" + node.name + "' [" + node.type +
        "] takes no " + kind + " style");
    }
    await node[spec.apply](style.id);
    return style;
  },

  // Quick async accessors
  async node(id)      { return await HELPERS.resolve(id); },
  async var_(idOrKey) { return await resolveVar(idOrKey); },
  async style_(kind, nameOrId) { return await resolveStyle(kind, nameOrId); },

  // Raced against a timer, because this call can simply never come back: a key
  // that belongs to another file and was never published produces no result
  // and no error. The 180 s in the field report was the CLI giving up, not the
  // API answering.
  async importComp(key, opts) {
    const ms = (opts && opts.timeout) || 15000;
    return await withTimeout(
      figma.importComponentByKeyAsync(key), ms,
      "importComponentByKeyAsync did not settle in " + Math.round(ms / 1000) +
      "s for key " + key + " — that key is unpublished, or belongs to a file this " +
      "one cannot reach. To find a library component's instances, resolve from the " +
      "consuming side instead: compare (await h.mainOf(inst)).key against the keys " +
      "you are looking for. See uspec/docs/import-by-key.md");
  },
  async importVar(key, opts) {
    const ms = (opts && opts.timeout) || 15000;
    return await withTimeout(
      figma.variables.importVariableByKeyAsync(key), ms,
      "importVariableByKeyAsync did not settle in " + Math.round(ms / 1000) +
      "s for key " + key + " — that key is unpublished, or belongs to a file this " +
      "one cannot reach");
  },

  // What this run has cost so far, for `doctor` and for a scan that wants to
  // stop before the tab does.
  stats() {
    return {
      mainComponentsResolved: MAIN_RESOLVED,
      pagesLoadedByUs: PAGES_LOADED.size,
      pagesInFile: figma.root.children.length,
      msLeft: h_left(),
    };
  },
};

// The body of h.walk, outside HELPERS so the flag h.walk sets around it is
// set and restored in exactly one place.
async function walkSubtree(root, visit, opts) {
  const prune = opts.pruneInstances !== false;
  const maxNodes = opts.maxNodes == null ? 500000 : opts.maxNodes;
  const maxDepth = opts.maxDepth == null ? Infinity : opts.maxDepth;
  const maxHits = opts.maxHits == null ? 100000 : opts.maxHits;
  // `find` means "descendants of", the way findAll always did; a walk that
  // reported its own starting node would quietly change every count.
  const includeRoot = opts.includeRoot !== false;
  // Whichever runs out first: this walk's own budget, or the caller's.
  const until = opts.budgetMs == null ? 0 : Date.now() + opts.budgetMs;
  const spent = () => (until && Date.now() >= until) || h_left() <= 0;
  const out = [];

  // Children we are willing to descend into. Pruning the root would make
  // walking an instance pointless, so it only applies below it.
  const kidsOf = (n, depth) => {
    if (depth >= maxDepth) return null;
    if (prune && depth > 0 && n.type === "INSTANCE") return null;
    return n.children && n.children.length ? n.children : null;
  };

  // A frame is "this node, and which of its children comes next" — the state
  // a recursive walk keeps on the call stack, made explicit so it can be
  // paused, handed out as a cursor and picked up again.
  const frames = [];
  let visited = 0;
  let pruned = 0;
  let resumed = false;

  if (opts.cursor && opts.cursor.length) {
    resumed = true;
    let node = root;
    frames.push({ node, i: opts.cursor[0], depth: 0 });
    for (let d = 1; d < opts.cursor.length; d++) {
      const kids = kidsOf(node, d - 1);
      // i is one past the child we descended into, so that child is at i-1.
      const child = kids && kids[opts.cursor[d - 1] - 1];
      if (!child) break;
      frames.push({ node: child, i: opts.cursor[d], depth: d });
      node = child;
    }
  } else {
    frames.push({ node: root, i: 0, depth: 0 });
  }

  const cursor = () => frames.map((f) => f.i);
  const record = async (n) => {
    const r = visit(n);
    const value = r && typeof r.then === "function" ? await r : r;
    if (value !== undefined && value !== null && value !== false) out.push(value);
  };

  if (!resumed && includeRoot) await record(root);

  while (frames.length) {
    // Every node, not every Nth. A stride only looks like a saving: measured
    // inside Figma, Date.now() costs 0.19 µs while reading one node's `type`
    // and `children` costs 17.63 µs — 95 times more — so checking always adds
    // about 1% to the walk. A stride of 256, meanwhile, multiplies the
    // overshoot by whatever the visit costs, and the visit is the expensive
    // part: at 10 ms a node it walked 2.5 s past the deadline and the partial
    // result arrived after its caller had already gone.
    //
    // One slow visit can still overshoot by its own duration; that is what
    // the margin on DEADLINE absorbs.
    if (spent()) {
      return { visited, pruned, found: out, partial: true, cursor: cursor(),
               reason: "deadline" };
    }
    if (visited >= maxNodes) {
      return { visited, pruned, found: out, partial: true, cursor: cursor(),
               reason: "maxNodes" };
    }
    if (out.length >= maxHits) {
      return { visited, pruned, found: out, partial: true, cursor: cursor(),
               reason: "maxHits" };
    }

    const f = frames[frames.length - 1];
    const kids = kidsOf(f.node, f.depth);
    if (!kids || f.i >= kids.length) { frames.pop(); continue; }

    const child = kids[f.i++];
    visited++;
    // Counted, not just skipped: a result that silently left out half the
    // file is worse than a slow one, so the caller is told when pruning
    // actually happened and can pass pruneInstances: false if it mattered.
    if (prune && child.type === "INSTANCE" && child.children && child.children.length) {
      pruned++;
    }
    await record(child);
    frames.push({ node: child, i: 0, depth: f.depth + 1 });
  }
  return { visited, pruned, found: out, partial: false, cursor: null, reason: null };
}

// h.left() by another name, so helpers can ask without going through the
// object literal they are being defined inside.
function h_left() {
  return DEADLINE ? DEADLINE - Date.now() : Infinity;
}

// ──────────────────────────────────────────────────────────────────────────

// One document is one JS runtime, and two execs interleaving at their await
// points would share CURRENT_PRINT and the per-exec caches — the first one's
// `finally` wipes the second's. The bridge refuses to stack requests, but a
// bridge that predates that, or one whose caller gave up, can still deliver a
// second one, so the line is held here too.
let BUSY = false;
const QUEUE = [];

figma.ui.onmessage = async (msg) => {
  // The UI asks rather than being told, because a message posted right after
  // showUI can arrive before the iframe has a listener for it.
  if (msg.type === "whoami") {
    figma.ui.postMessage(await identity());
    return;
  }
  // The bridge turned us away because this sid is taken. If that is a name
  // collision between two same-named files, a fresh id fixes it outright.
  if (msg.type === "newsid") {
    await regenerateSid();
    figma.ui.postMessage(await identity());
    return;
  }
  if (msg.type !== "exec") return;

  QUEUE.push(msg);
  if (BUSY) return;
  BUSY = true;
  try {
    while (QUEUE.length) await runExec(QUEUE.shift());
  } finally {
    BUSY = false;
  }
};

async function runExec(msg) {
  const { id, code } = msg;

  // Dispatched is not the same as running. Without this the bridge cannot tell
  // "the thread is busy with somebody else" from "this script is slow", and
  // every ran_ms it reported was really time since it sent the request.
  figma.ui.postMessage({ type: "started", id });

  // The caller's budget, in our clock, minus a margin.
  //
  // Nothing can interrupt this thread, so the deadline only bites where the
  // code looks at it: h.walk, h.tick and the helpers that walk a subtree. And
  // it has to bite *before* the caller stops listening, not at the same
  // instant — a partial result that arrives after the 504 is the same as no
  // result, and serialising a big one is not free. 10% of the budget, between
  // a quarter of a second and two.
  const margin = Math.min(2000, Math.max(250, Math.round((msg.deadline_ms || 0) * 0.1)));
  DEADLINE = msg.deadline_ms
    ? Date.now() + Math.max(msg.deadline_ms - margin, 1)
    : 0;

  const logs = [];
  let suppressed = 0;
  const print = (...args) => {
    // Past the cap, stop building strings and stop posting: a print() inside a
    // 10 000-iteration loop is otherwise 10 000 socket messages on its way to
    // an unreadable wall of output.
    if (logs.length >= MAX_LOGS) { suppressed++; return; }
    const text = args.map((a) =>
      typeof a === "object" ? JSON.stringify(a, roundish) : String(a)
    ).join(" ");
    logs.push(text);
    figma.ui.postMessage({ type: "log", id, text });
  };

  CURRENT_PRINT = print;
  try {
    const fn = new Function(
      "figma", "print", "h",
      `return (async () => { ${code} })();`
    );
    const result = await fn(figma, print, HELPERS);

    if (suppressed > 0) {
      const note = "… " + suppressed + " more print() line(s) suppressed (cap: " +
        MAX_LOGS + ")";
      logs.push(note);
      figma.ui.postMessage({ type: "log", id, text: note });
    }

    figma.ui.postMessage({
      type: "result",
      id,
      text: asText(result, logs),
      // The same result serialised a second time, for callers that read the
      // structure rather than the text. The CLI asks for it only with --raw;
      // on a large tree this is a full extra walk and a full extra copy.
      value: msg.want_value === false ? undefined : safeStringify(result),
    });
  } catch (e) {
    figma.ui.postMessage({
      type: "error",
      id,
      text: (e && e.message) || String(e),
      stack: (e && e.stack) || null,
    });
  } finally {
    CURRENT_PRINT = () => {};
    clearExecCaches();
  }
}
