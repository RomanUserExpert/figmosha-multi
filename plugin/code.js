figma.showUI(__html__, { width: 220, height: 28, title: "Figmosha Bridge" });

// ─── who this plugin instance is ─────────────────────────────────────────

// One plugin instance serves one Figma document, so this id is what the bridge
// routes by. Generated per run and kept in memory on purpose: storing it with
// setPluginData would write into the document, marking the file as edited and
// leaving a trail in version history for something that is not part of the
// design at all. Nothing needs it to survive a re-Run — a re-Run is a new
// session by definition.
const SID = "s-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);

function identity() {
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

function clearExecCaches() {
  VAR_CACHE = null;
  STYLE_CACHE = null;
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

  // First descendant by exact name
  findByName(root, name) {
    return root.findOne((n) => n.name === name);
  },

  // All descendants by exact name
  findAllByName(root, name) {
    return root.findAll((n) => n.name === name);
  },

  // Dump subtree as indented text.
  //
  // Two ceilings, because the shape of a real file defeats a plain walk: it is
  // deep, and it is repetitive. A depth cut that does not say how much it hid
  // leaves the caller guessing whether to dig; a list of forty identical rows
  // costs forty rows to say one thing.
  dumpTree(node, opts) {
    opts = opts || {};
    const maxDepth = opts.maxDepth == null ? 99 : opts.maxDepth;
    const showSize = opts.showSize !== false;
    const showText = opts.showText !== false;
    const showLayout = opts.showLayout === true;
    const collapse = opts.collapse !== false;
    const lines = [];

    const descendants = (n) => {
      let count = 0;
      if (n.children) for (const c of n.children) count += 1 + descendants(c);
      return count;
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
    return await figma.getNodeByIdAsync(idOrAlias);
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
  async node(id)      { return await figma.getNodeByIdAsync(id); },
  async var_(idOrKey) { return await resolveVar(idOrKey); },
  async style_(kind, nameOrId) { return await resolveStyle(kind, nameOrId); },
  async importComp(key) { return await figma.importComponentByKeyAsync(key); },
  async importVar(key)  { return await figma.variables.importVariableByKeyAsync(key); },
};

// ──────────────────────────────────────────────────────────────────────────

figma.ui.onmessage = async (msg) => {
  // The UI asks rather than being told, because a message posted right after
  // showUI can arrive before the iframe has a listener for it.
  if (msg.type === "whoami") {
    figma.ui.postMessage(identity());
    return;
  }
  if (msg.type !== "exec") return;
  const { id, code } = msg;

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
};
