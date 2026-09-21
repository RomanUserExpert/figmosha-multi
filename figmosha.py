#!/usr/bin/env python3
"""figmosha — CLI client for the Figmosha bridge.

Commands:
    figmosha exec "<js>"             # run arbitrary JS in plugin context
    figmosha exec --file f.js
    figmosha exec --stdin
    figmosha each <id> -f s.js --split 2 --state run.jsonl [--resume]
    figmosha status                   # check server / plugin connection
    figmosha sessions                 # which Figma files are connected
    figmosha doctor                   # diagnose the whole chain, with fixes
    figmosha sel                      # what is selected in Figma right now
    figmosha props <id> [--all]       # everything set on one node, tokens by name
    figmosha vars [filter] [--library]  # which variables exist, and their values
    figmosha styles [filter]          # local paint / text / effect / grid styles
    figmosha tree <id> [--depth N] [--layout]
    figmosha find <id> <filter>       # find descendants (name=X, name~X, type=X, text=X)
                                      #   instances are not descended into; --nested does
    figmosha text <id> "<new text>"   # set TEXT node characters (autoloads font)
    figmosha variant <id> "P=V" ...   # set INSTANCE variant property values
    figmosha clone <id> [--right|--left|--up|--down] [--gap N] [--name N]
    figmosha rm <id> [<id> ...]       # remove one or more nodes
    figmosha import-component <key>   # import library component, instantiate, focus
    figmosha update                   # git pull + re-stamp this project's plugin
    figmosha "<js>"                   # shorthand for `exec`

Output is compact by design — one line per node, sizes rounded, results capped
— because whoever reads it pays per character. `--raw` prints the untouched JSON
response instead, with full precision and no truncation.

Anywhere a node id is taken, `page` (current page) and `sel` (first selected
node) work too; `rm sel` takes the whole selection, not just the first node.
Bridge address comes from FIGMOSHA_HOST / FIGMOSHA_PORT; when a project has
several Figma files open, --session / FIGMOSHA_SESSION says which.

Helpers available inside exec'd code (as `h.*`):
    h.bF(node, idx, varOrId)    h.bS(node, idx, varOrId)   h.bN(node, prop, varOrId)
    h.findByName(root, name)    h.findAllByName(root, name)
    h.dumpTree(node, {maxDepth, showSize, showText, showLayout})
    h.withFonts(root, asyncFn)  h.setText(node, text)
    h.cloneNext(node, {direction, gap, name})
    h.variant(instance, props)  h.variantsOf(instance)
    h.sel()                     h.resolve(idOrAlias)
    h.hex("#1a2b3c")            h.solid("#1a2b3c", opacity?)
    h.frame(parent, {layout, w, h, spacing, padding, align, fill, radius, name})
    h.node(id)                  h.var_(idOrKey)
    h.importComp(key)           h.importVar(key)
    h.walk(root, visit, {pruneInstances, budgetMs, maxNodes, maxDepth, cursor})
    h.mainOf(instance)          h.loadPageOf(node)         h.pages()
    h.tick()                    h.left()                   h.stats()

On a big file, h.walk is the one to reach for: it skips the inside of
instances, stops when the caller's timeout runs out, and hands back a cursor to
carry on with — none of which a bare findAll can do.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import date
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import project

PROJECT = project.load()

# 127.0.0.1 and not "localhost". On Windows the name resolves to ::1 first,
# the bridge binds IPv4 only, and every single command paid the IPv6 attempt's
# timeout before falling back: measured 2217ms per call against 157ms. Thirty
# commands in a session is a minute of waiting for nothing.
DEFAULT_HOST = os.environ.get("FIGMOSHA_HOST", "127.0.0.1")
# Port precedence: --port flag > FIGMOSHA_PORT (manual override, kept for
# scripts that predate project.json) > this copy's project > the zero copy.
DEFAULT_PORT = int(os.environ.get("FIGMOSHA_PORT")
                   or (PROJECT["port"] if PROJECT else 8787))

# Which open Figma file to talk to, when the project has more than one. Lives
# in the environment of one shell, never in a file: two agents sharing a project
# would otherwise overwrite each other's choice and silently edit the wrong file.
DEFAULT_SESSION = os.environ.get("FIGMOSHA_SESSION", "")

# Seconds to wait for the plugin to reappear before giving up. 0 keeps the old
# behaviour — fail at once. Anything above that is for long runs, which lose
# the plugin at least once: switching files in Figma closes its window.
DEFAULT_WAIT_PLUGIN = int(os.environ.get("FIGMOSHA_WAIT_PLUGIN") or 0)

HOST = DEFAULT_HOST
PORT = DEFAULT_PORT
SESSION = DEFAULT_SESSION
WAIT_PLUGIN = DEFAULT_WAIT_PLUGIN

KNOWN_CMDS = {
    "exec", "status", "doctor", "sel", "tree", "find", "text", "variant",
    "clone", "rm", "import-component", "icomp", "init", "sessions", "props",
    "vars", "styles", "update", "set", "bind", "overrides", "where", "each",
}

# Options that belong to the parser itself rather than to a subcommand, and
# take a value. They are the reason `figmosha "<js>"` cannot simply look at
# argv[1]: in a project with two open files every command carries --session.
GLOBAL_VALUE_FLAGS = {"--host", "--port", "--session", "--wait-plugin"}

# How many stack frames of a plugin-side error are worth printing. The first
# one names the line that threw; everything after ~3 is the plugin runtime.
STACK_FRAMES = 3

# Most rows one listing may print before it stops and says so. A design system
# is hundreds of variables; a command that dumps all of them to answer "is
# there a spacing scale" costs more than the answer is worth.
MAX_ROWS = 200

# Longest text kept when a row prints a TEXT node's contents. Enough to tell
# two labels apart, short enough that a page of copy is not a page of output.
CHARS_PREVIEW = 40


def normalize_argv(argv):
    """`figmosha [flags] "<js>"` -> `figmosha [global flags] exec [flags] "<js>"`.

    The shorthand has to survive flags on both sides of the code, because the
    two most common ones sit on opposite sides of it: `--session` is a global
    option, `--timeout` belongs to `exec`. So global options are lifted to the
    front, and `exec` is inserted before whatever is left — which is the code
    plus its own flags.
    """
    head, tail = [], []
    i = 0
    while i < len(argv):
        arg = argv[i]
        name = arg.split("=", 1)[0]
        if name in GLOBAL_VALUE_FLAGS:
            head.append(arg)
            if "=" not in arg and i + 1 < len(argv):
                head.append(argv[i + 1])
                i += 1
        else:
            tail.append(arg)
        i += 1

    if not tail or tail[0] in KNOWN_CMDS or tail[0] in ("-h", "--help"):
        return head + tail
    return head + ["exec"] + tail


def looks_like_a_command(text):
    """A bare word is a mistyped subcommand, not JavaScript.

    Without this, `figmosha updat` becomes `exec "updat"`, which travels all
    the way into Figma to come back as «'updat' is not defined» — an error
    about the wrong thing entirely. Real code all but always carries a space,
    a dot, a bracket or a semicolon.
    """
    return bool(text) and not text.startswith("-") and text.isidentifier()


def force_utf8_output():
    """Windows consoles default to cp1252 and raise on anything outside it.

    Layer names are routinely Cyrillic, CJK or emoji, so without this a plain
    `figmosha tree` blows up with UnicodeEncodeError instead of printing.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


# Exit codes, because a runner has to tell these apart without parsing prose.
# 1 is "your script is wrong" — stop and show it. 3 and 4 are "the file is not
# available right now" — wait and come back. Conflating them is what made every
# long run treat a closed plugin window as a broken script.
EXIT_SCRIPT = 1       # the code threw, or the bridge refused the request
EXIT_USAGE = 2        # wrong arguments, or no bridge at all
EXIT_NO_PLUGIN = 3    # no plugin connected, or it vanished mid-request
EXIT_BUSY = 4         # timed out, or the file is busy with an earlier script


def _exit_code(status, resp):
    """Classify a bridge response into one of the codes above."""
    if resp.get("ok") is not False:
        return 0
    if status == 504:
        return EXIT_BUSY
    if status == 503 or "disconnected mid-request" in str(resp.get("error", "")):
        return EXIT_NO_PLUGIN
    return EXIT_SCRIPT


def node_expr(id_or_alias):
    """JS that resolves a node id, or the aliases `page` / `sel`."""
    return f"await h.resolve({json.dumps(id_or_alias)})"


def _request(method, path, payload=None, timeout=None):
    # A GET here only reads bridge state, so it either answers at once or the
    # thing on that port is not a bridge. Waiting a minute to find that out is
    # how `status` and the first step of `doctor` used to hang on a port held
    # by an unrelated process that accepted the connection and went quiet.
    if timeout is None:
        timeout = 5 if method == "GET" else 65
    url = f"http://{HOST}:{PORT}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read() or b"{}"
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"ok": False, "error": body.decode("utf-8", "replace")}
    except urllib.error.URLError as e:
        return 0, {"ok": False, "error": f"connection: {e.reason}"}
    except TimeoutError:
        return 0, {"ok": False, "error": "request timed out"}


def _exec(code, timeout=60, want_value=False):
    # The socket deadline has to outlast the server-side one, otherwise a long
    # --timeout dies here at the default 65s while the bridge is still waiting,
    # and we lose the bridge's own error payload (including any hint).
    #
    # want_value=False because the plugin otherwise serialises the result twice
    # — once as text, once as JSON — and the CLI prints only the text. On a big
    # tree that is a second full walk and a second copy over the socket.
    payload = {"code": code, "timeout": timeout, "want_value": want_value}
    if SESSION:
        payload["session"] = SESSION
    status, resp = _request("POST", "/exec", payload, timeout=timeout + 5)

    # Switching files in Figma closes the plugin window, and every long run
    # meets that at least once. With --wait-plugin the run pauses instead of
    # dying on a step it would have completed a few seconds later.
    if WAIT_PLUGIN and _exit_code(status, resp) == EXIT_NO_PLUGIN:
        if _wait_for_plugin(WAIT_PLUGIN):
            status, resp = _request("POST", "/exec", payload, timeout=timeout + 5)
    return status, resp


def _plugin_is_back():
    """Is there exactly one connected file that this invocation would target?"""
    path = "/sessions" + (f"?session={urllib.parse.quote(SESSION)}" if SESSION else "")
    status, resp = _request("GET", path)
    if status != 200:
        return False
    rows = resp.get("sessions") or []
    if not rows:
        return False
    if SESSION:
        return len(resp.get("matched") or []) == 1
    return True


def _wait_for_plugin(seconds):
    """Poll until the plugin is back, up to `seconds`. True if it returned.

    Polled rather than pushed because the plugin reconnects on its own schedule
    — re-Run in Figma is a human action, and the bridge has nothing to notify.
    """
    print(f"figmosha: plugin not connected — waiting up to {seconds}s "
          f"(run it in Figma: Plugins → Development)", file=sys.stderr)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _plugin_is_back():
            print("figmosha: plugin is back — continuing", file=sys.stderr)
            return True
        time.sleep(1.0)
    print(f"figmosha: gave up waiting after {seconds}s", file=sys.stderr)
    return False


def _send(code, args, timeout=None):
    """Run code in Figma and print the answer. Every command ends here."""
    status, resp = _exec(code, timeout or args.timeout, want_value=args.raw)
    return _emit(resp, raw=args.raw, status=status)


def _emit(resp, raw=False, status=None):
    if raw:
        # ensure_ascii=False because layer names are routinely Cyrillic, and an
        # escaped \uXXXX is six characters — and six times the tokens — each.
        print(json.dumps(resp, indent=2, ensure_ascii=False))
        return _exit_code(status, resp) if status is not None else (
            0 if resp.get("ok") else EXIT_SCRIPT)

    for line in resp.get("logs") or []:
        print(f"  log: {line}", file=sys.stderr)

    if resp.get("ok") is False:
        print(f"figmosha: {resp.get('error', 'unknown')}", file=sys.stderr)
        for row in resp.get("sessions") or []:
            print(f"   {row['sid']:<18} «{row.get('file') or '?'}»"
                  f" — page «{row.get('page') or '?'}»", file=sys.stderr)
        if resp.get("sessions"):
            print("   pick one:  --session <sid or file name>"
                  "   (or $env:FIGMOSHA_SESSION)", file=sys.stderr)
        if resp.get("hint"):
            print(f"   hint: {resp['hint']}", file=sys.stderr)
        if resp.get("stack"):
            # Past the first few frames the stack is the plugin's own runtime,
            # not the caller's code — pages of it, every time.
            frames = resp["stack"].splitlines()
            print("\n".join(frames[:STACK_FRAMES]), file=sys.stderr)
            if len(frames) > STACK_FRAMES:
                print(f"   … {len(frames) - STACK_FRAMES} more frames (--raw for all)",
                      file=sys.stderr)
        return _exit_code(status, resp) if status is not None else EXIT_SCRIPT

    if resp.get("result"):
        print(resp["result"])
    print(f"  ({resp.get('elapsed_ms', '?')}ms)", file=sys.stderr)
    return 0


# ─── command builders ─────────────────────────────────────────────────────

def cmd_status(args):
    status, resp = _request("GET", "/status")
    if args.raw:
        print(json.dumps(resp, indent=2, ensure_ascii=False))
        return 0 if status == 200 else 2
    if status != 200:
        print(f"figmosha: {resp.get('error', 'bridge not answering')} "
              f"on {HOST}:{PORT}", file=sys.stderr)
        return 2
    plugin = (f"{resp['sessions']} file(s) connected" if resp.get("sessions")
              else "plugin connected" if resp.get("plugin_connected")
              else "plugin not connected")
    who = f"«{resp['project']}»" if resp.get("project") else "unclaimed bridge"
    # `pending` says how many callers are waiting, which is not the same as how
    # many files are stuck; `busy` is the one a runner should branch on.
    busy = resp.get("busy", 0)
    print(f"{who} {HOST}:{PORT} · {plugin} · {resp.get('pending', 0)} pending"
          + (f" · {busy} busy" if busy else ""))
    return 0


# One node per line: `id  TYPE  W×H  name  "text"`. Built in the plugin rather
# than in Python because the result then crosses the wire once, already in the
# shape the caller reads — pretty-printed JSON of the same rows costs ~3x the
# tokens for the same facts.
ROW_JS = (
    "const row = n => n.id + '  ' + n.type"
    " + (n.width === undefined ? '' : '  ' + Math.round(n.width) + '×' + Math.round(n.height))"
    " + '  ' + n.name"
    " + (n.type === 'TEXT' ? '  ' + JSON.stringify(n.characters.slice(0, "
    f"{CHARS_PREVIEW}"
    ")) : '');"
)

# Same rows as objects, for --raw: the full precision and the full text.
NODE_FIELDS_JS = (
    "n => ({id: n.id, name: n.name, type: n.type, w: n.width, h: n.height,"
    " chars: n.type === 'TEXT' ? n.characters : undefined})"
)


# Everything you would otherwise read off one node by hand, with variable and
# style ids already resolved to names. Written as one JS blob because the
# resolving has to happen inside the plugin: `boundVariables` gives ids, and a
# caller who gets ids back has to spend a second round trip turning them into
# the names that are the whole point.
#
# The `→ name` after a value is the answer to "is this bound or hardcoded?",
# which is the question this command exists for.
#
# Defaults are not printed unless --all: `constraints MIN/MIN`, `opacity 1` and
# `rotation 0` are on every node in the file and mean nothing.
OVERRIDES_JS = r"""
const n = await h.resolve(__ID__);
if (!n) throw new Error('node not found: ' + __ID__);
if (n.type !== 'INSTANCE') {
  throw new Error('not an INSTANCE (got ' + n.type + ') — overrides only exist on instances');
}

const pad = (s, w) => (s + '                                ').slice(0, Math.max(w, s.length));
const main = await n.getMainComponentAsync();
const set = main && main.parent && main.parent.type === 'COMPONENT_SET' ? main.parent : null;

// Variant values first: half the "why does this look different" questions end
// there, before any override is involved.
const props = [];
for (const k in (n.componentProperties || {})) {
  const p = n.componentProperties[k];
  const value = (p && p.value !== undefined) ? p.value : p;
  props.push(k.split('#')[0] + '=' + value);
}

// A variant's own name is its property list ("Mode=Day"), which the row
// already carries — so the set's name is the useful half, not both.
const out = [n.id + '  ' +
  (set ? set.name : (main ? main.name : 'detached')) +
  (props.length ? '  ·  ' + props.join(', ') : '')];

const list = n.overrides || [];
if (!list.length) {
  out.push('  nothing overridden');
  return out.join('\n');
}

for (const o of list) {
  const fields = (o.overriddenFields || []).join(', ');
  // The first entry is usually the instance itself; saying so beats printing
  // its name a second time and leaving the reader to match ids.
  let name = o.id === n.id ? '(this instance)' : o.id;
  if (o.id !== n.id) {
    const inner = await figma.getNodeByIdAsync(o.id);
    if (inner) name = inner.name;
  }
  out.push('  ' + pad(name, 18) + pad(o.id, 22) + fields);
}
return out.join('\n');
"""


WHERE_JS = r"""
let n = await h.resolve(__ID__);
if (!n) throw new Error('node not found: ' + __ID__);

// Up rather than down: "why did this move" is answered by the ancestors, and
// each one contributes its own sizing to the answer.
const chain = [];
while (n) {
  chain.unshift(n);
  if (n.type === 'PAGE' || n.type === 'DOCUMENT') break;
  n = n.parent;
}

const num = (v) => (v === undefined || v === null || typeof v === 'symbol')
  ? '?' : String(Math.round(v));
const pad = (s, w) => (s + '                                ').slice(0, Math.max(w, s.length));

const out = [];
for (let i = 0; i < chain.length; i++) {
  const node = chain[i];
  if (node.type === 'PAGE' || node.type === 'DOCUMENT') {
    out.push(node.type === 'PAGE' ? 'Page «' + node.name + '»' : node.name);
    continue;
  }
  const indent = i ? ' '.repeat(2 * i - 1) + '└ ' : '';
  // Ids inside an instance are long (I10:239;88:9705), and a column that
  // collides with the next one is worse than a wide one.
  let line = indent + pad(node.name, Math.max(2, 22 - 2 * i)) + pad(node.id, 17) + node.type;
  if (node.width !== undefined) line += '  ' + num(node.width) + '×' + num(node.height);
  // Sizing is the reason the question gets asked, so it goes on every row that
  // has it, not only the last.
  if (node.layoutSizingHorizontal) {
    line += '  H=' + node.layoutSizingHorizontal + ' V=' + node.layoutSizingVertical;
  }
  if (node.layoutGrow) line += '  grow:' + node.layoutGrow;
  if (node.layoutMode && node.layoutMode !== 'NONE') line += '  [' + node.layoutMode[0] + ']';
  out.push(line);
}
return out.join('\n');
"""


MUTATE_JS = r"""
const MODE = __MODE__;          // 'set' — literal values; 'bind' — token names
const DRY = __DRY__;
const PAIRS = __PAIRS__;

// `sel` means every selected layer, as in `rm`. Styling five layers in one call
// is the whole point of the command; touching only the first and reporting
// success is the kind of quiet wrong answer nobody re-reads.
let targets;
if (__ID__ === 'sel') {
  targets = figma.currentPage.selection.slice();
  if (!targets.length) throw new Error('nothing selected in Figma');
} else {
  const one = await h.resolve(__ID__);
  if (!one) throw new Error('node not found: ' + __ID__);
  targets = [one];
}

const round = (v) => Math.round(v * 100) / 100;
const num = (v) => (v === undefined || v === null || typeof v === 'symbol')
  ? '—' : String(round(v));
const hex = (c) => '#' + [c.r, c.g, c.b]
  .map((v) => ('0' + Math.round(v * 255).toString(16)).slice(-2)).join('').toUpperCase();

const numOr = (raw, what) => {
  const v = Number(raw);
  if (!isFinite(v)) throw new Error(what + ' expects a number, got ' + JSON.stringify(raw));
  return v;
};
const listOf = (raw, counts, what) => {
  const parts = String(raw).split(',').map((p) => numOr(p.trim(), what));
  if (counts.indexOf(parts.length) === -1) {
    throw new Error(what + ' expects ' + counts.join(' or ') + ' numbers, got ' + parts.length);
  }
  return parts;
};

// "#rrggbb", "#rgb", "#rrggbb@50", "none"
const paintOf = (raw) => {
  if (raw === 'none') return [];
  const at = String(raw).indexOf('@');
  if (at === -1) return h.solid(raw);
  return h.solid(raw.slice(0, at), numOr(raw.slice(at + 1), 'opacity') / 100);
};
const paintText = (node, prop) => {
  const paints = node[prop];
  if (typeof paints === 'symbol') return 'mixed';
  if (!Array.isArray(paints) || !paints.length) return 'none';
  const p = paints[0];
  if (p.type !== 'SOLID') return p.type.toLowerCase();
  const op = (p.opacity === undefined || p.opacity === 1) ? '' : '@' + Math.round(p.opacity * 100);
  return hex(p.color) + op + (paints.length > 1 ? ' +' + (paints.length - 1) : '');
};

const CORNERS = ['topLeftRadius', 'topRightRadius', 'bottomRightRadius', 'bottomLeftRadius'];
const SIDES = ['paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft'];
const collapse = (v) => {
  if (v[0] === v[1] && v[1] === v[2] && v[2] === v[3]) return String(round(v[0]));
  if (v[0] === v[2] && v[1] === v[3]) return round(v[0]) + ',' + round(v[1]);
  return v.map(round).join(',');
};

// Sizing on an axis: a number pins it, `fill` and `hug` are the auto-layout
// modes, which Figma exposes as a different property entirely.
const sizeOf = (node, axis) => {
  const mode = axis === 'H' ? node.layoutSizingHorizontal : node.layoutSizingVertical;
  const px = axis === 'H' ? node.width : node.height;
  return mode && mode !== 'FIXED' ? mode.toLowerCase() : num(px);
};
const setSize = (node, axis, raw) => {
  const word = String(raw).toLowerCase();
  if (word === 'fill' || word === 'hug') {
    const prop = axis === 'H' ? 'layoutSizingHorizontal' : 'layoutSizingVertical';
    node[prop] = word.toUpperCase();
    return;
  }
  const v = numOr(raw, axis === 'H' ? 'w' : 'h');
  node.resize(axis === 'H' ? v : node.width, axis === 'H' ? node.height : v);
};

// Applied in a fixed order regardless of the order they were typed in:
// auto-layout silently drops sizing and spacing set before layoutMode.
const RANK = {
  layout: 0, w: 1, h: 1, gap: 2, pad: 2, padTop: 2, padRight: 2,
  padBottom: 2, padLeft: 2, align: 3,
};

const KEYS = {
  name:    { read: (n) => n.name, write: (n, v) => { n.name = v; }, show: (v) => v },
  text:    { read: (n) => n.type === 'TEXT' ? n.characters : '—',
             write: async (n, v) => {
               if (n.type !== 'TEXT') throw new Error('text: not a TEXT node (got ' + n.type + ')');
               await h.setText(n, v);
             }, show: (v) => v },
  fill:    { read: (n) => paintText(n, 'fills'),
             write: (n, v) => { n.fills = paintOf(v); },
             show: (v) => v === 'none' ? 'none' : paintText({ fills: paintOf(v) }, 'fills') },
  stroke:  { read: (n) => paintText(n, 'strokes'),
             write: (n, v) => { n.strokes = paintOf(v); },
             show: (v) => v === 'none' ? 'none' : paintText({ strokes: paintOf(v) }, 'strokes') },
  sw:      { read: (n) => num(n.strokeWeight),
             write: (n, v) => { n.strokeWeight = numOr(v, 'sw'); } },
  radius:  { read: (n) => collapse(CORNERS.map((c) => n[c])),
             write: (n, v) => {
               const p = listOf(v, [1, 4], 'radius');
               if (p.length === 1) n.cornerRadius = p[0];
               else CORNERS.forEach((c, i) => { n[c] = p[i]; });
             },
             show: (v) => {
               const p = listOf(v, [1, 4], 'radius');
               return p.length === 1 ? String(round(p[0])) : collapse(p);
             } },
  gap:     { read: (n) => num(n.itemSpacing),
             write: (n, v) => { n.itemSpacing = numOr(v, 'gap'); } },
  pad:     { read: (n) => collapse(SIDES.map((s) => n[s])),
             write: (n, v) => {
               const p = listOf(v, [1, 2, 4], 'pad');
               const four = p.length === 1 ? [p[0], p[0], p[0], p[0]]
                          : p.length === 2 ? [p[0], p[1], p[0], p[1]] : p;
               SIDES.forEach((s, i) => { n[s] = four[i]; });
             } },
  w:       { read: (n) => sizeOf(n, 'H'), write: (n, v) => setSize(n, 'H', v) },
  h:       { read: (n) => sizeOf(n, 'V'), write: (n, v) => setSize(n, 'V', v) },
  x:       { read: (n) => num(n.x), write: (n, v) => { n.x = numOr(v, 'x'); } },
  y:       { read: (n) => num(n.y), write: (n, v) => { n.y = numOr(v, 'y'); } },
  opacity: { read: (n) => num(n.opacity),
             write: (n, v) => {
               const s = String(v).trim();
               const pct = s.slice(-1) === '%';
               n.opacity = pct ? numOr(s.slice(0, -1), 'opacity') / 100 : numOr(s, 'opacity');
             } },
  visible: { read: (n) => String(n.visible),
             write: (n, v) => {
               const s = String(v).toLowerCase();
               if (s !== 'true' && s !== 'false') throw new Error('visible expects true or false');
               n.visible = s === 'true';
             } },
  layout:  { read: (n) => !n.layoutMode ? '—'
               : (n.layoutMode === 'NONE' ? 'none'
                  : n.layoutMode[0].toLowerCase() + (n.layoutWrap === 'WRAP' ? ' wrap' : '')),
             write: (n, v) => {
               const s = String(v).toLowerCase();
               if (s === 'wrap') { n.layoutMode = 'HORIZONTAL'; n.layoutWrap = 'WRAP'; return; }
               if (s !== 'v' && s !== 'h' && s !== 'none') {
                 throw new Error('layout expects v, h, none or wrap, got ' + JSON.stringify(v));
               }
               n.layoutMode = s === 'v' ? 'VERTICAL' : s === 'h' ? 'HORIZONTAL' : 'NONE';
             } },
  align:   { read: (n) => n.layoutMode && n.layoutMode !== 'NONE'
               ? n.primaryAxisAlignItems + '/' + n.counterAxisAlignItems : '—',
             write: (n, v) => {
               const parts = String(v).toUpperCase().split('/');
               if (parts.length !== 2) throw new Error('align expects PRIMARY/COUNTER, e.g. CENTER/MIN');
               n.primaryAxisAlignItems = parts[0];
               n.counterAxisAlignItems = parts[1];
             } },
};

// What `bind` may attach a variable to, and under which Figma property name.
// Paints are their own API — setBoundVariableForPaint, not setBoundVariable —
// and confusing the two is the first entry in the bridge's error hints.
const BINDABLE = {
  fill: 'paint:fills', stroke: 'paint:strokes',
  sw: 'strokeWeight', gap: 'itemSpacing',
  pad: SIDES, padTop: 'paddingTop', padRight: 'paddingRight',
  padBottom: 'paddingBottom', padLeft: 'paddingLeft',
  radius: CORNERS, radiusTL: 'topLeftRadius', radiusTR: 'topRightRadius',
  radiusBR: 'bottomRightRadius', radiusBL: 'bottomLeftRadius',
  w: 'width', h: 'height', opacity: 'opacity', text: 'characters',
};

const varNames = {};
const varName = async (id) => {
  if (varNames[id] === undefined) {
    let name = id;
    try { const v = await figma.variables.getVariableByIdAsync(id); if (v) name = v.name; } catch (e) {}
    varNames[id] = name;
  }
  return varNames[id];
};

// The token currently attached to a key, so that "before" carries it too.
const boundName = async (node, key) => {
  const spec = BINDABLE[key];
  if (!spec) return '';
  if (String(spec).indexOf('paint:') === 0) {
    const paints = node[spec.slice(6)];
    if (typeof paints === 'symbol' || !Array.isArray(paints) || !paints[0]) return '';
    const b = paints[0].boundVariables && paints[0].boundVariables.color;
    return b && b.id ? ' → ' + await varName(b.id) : '';
  }
  const prop = Array.isArray(spec) ? spec[0] : spec;
  const b = node.boundVariables && node.boundVariables[prop];
  const one = Array.isArray(b) ? b[0] : b;
  return one && one.id ? ' → ' + await varName(one.id) : '';
};

const bindOne = async (node, key, value) => {
  const spec = BINDABLE[key];
  if (!spec) {
    throw new Error('bind: ' + key + ' is not bindable — one of: ' +
      Object.keys(BINDABLE).join(', '));
  }
  const clearing = value === 'none';
  if (String(spec).indexOf('paint:') === 0) {
    const which = spec.slice(6);
    if (clearing) {
      const copy = JSON.parse(JSON.stringify(node[which]));
      if (!copy[0]) throw new Error(key + ': nothing to unbind');
      copy[0] = figma.variables.setBoundVariableForPaint(copy[0], 'color', null);
      node[which] = copy;
      return null;
    }
    return which === 'fills' ? await h.bF(node, 0, value) : await h.bS(node, 0, value);
  }
  const props = Array.isArray(spec) ? spec : [spec];
  if (clearing) {
    for (const p of props) node.setBoundVariable(p, null);
    return null;
  }
  const v = await h.var_(value);
  if (!v) {
    throw new Error('bind: no variable named ' + JSON.stringify(value) +
      ' — see what exists: figmosha vars ' + String(value).split('/').pop());
  }
  for (const p of props) node.setBoundVariable(p, v);
  return v;
};

// ── apply, node by node ───────────────────────────────────────────────────
const ordered = PAIRS.slice().sort((a, b) =>
  (RANK[a[0]] === undefined ? 4 : RANK[a[0]]) - (RANK[b[0]] === undefined ? 4 : RANK[b[0]]));

for (const [key] of ordered) {
  const known = MODE === 'bind' ? BINDABLE[key] : KEYS[key];
  if (!known) {
    throw new Error('unknown key ' + JSON.stringify(key) + ' — one of: ' +
      Object.keys(MODE === 'bind' ? BINDABLE : KEYS).join(', '));
  }
}

// Reading a key is the same question in both modes; only writing differs.
const readerFor = (key) => {
  if (KEYS[key]) return KEYS[key].read;
  const spec = BINDABLE[key];
  if (String(spec).indexOf('paint:') === 0) return (n) => paintText(n, spec.slice(6));
  const prop = Array.isArray(spec) ? spec[0] : spec;
  return (n) => num(n[prop]);
};

const out = [];
const label = (s) => (s + '            ').slice(0, 10);

for (const node of targets) {
  out.push(node.id + '  ' + node.name + ' [' + node.type + ']');
  let quiet = 0;
  for (const [key, value] of ordered) {
    const reader = readerFor(key);
    let before;
    try { before = reader(node) + await boundName(node, key); }
    catch (e) { before = '—'; }
    try {
      if (DRY) {
        // The diff is worth printing even unapplied, but a bound value is not
        // knowable until the variable is attached — so it is shown as the token
        // in parentheses rather than guessed at.
        const after = MODE === 'bind'
          ? (value === 'none' ? '(unbound)' : '(' + value + ')')
          : (KEYS[key].show ? KEYS[key].show(value) : String(value));
        if (after === before) { quiet++; continue; }
        out.push('  ' + label(key) + before + '  →  ' + after);
        continue;
      }
      if (MODE === 'bind') await bindOne(node, key, value);
      else await KEYS[key].write(node, value);

      const after = reader(node) + await boundName(node, key);
      if (after === before) { quiet++; continue; }
      out.push('  ' + label(key) + before + '  →  ' + after);
    } catch (e) {
      // One node missing auto-layout must not cancel the other four: losing the
      // whole command to one bad target is exactly the wasted turn this command
      // exists to avoid.
      out.push('  ' + label(key) + '! ' + String(e.message).split('\n')[0]);
    }
  }
  if (quiet) out.push('  ' + quiet + ' unchanged');
}
if (DRY) out.push('(--dry-run: nothing was written)');
return out.join('\n');
"""


PROPS_JS = r"""
const n = await h.resolve(__ID__);
if (!n) throw new Error('node not found: ' + __ID__);
const ALL = __ALL__;
const KIDS = __KIDS__;
const out = [];
const push = (label, text) => { if (text) out.push((label + '        ').slice(0, 9) + text); };

// One lookup per id, not per use: a node with a bound radius on four corners
// would otherwise ask Figma the same question four times.
const cache = {};
const named = async (id, get) => {
  if (cache[id] === undefined) {
    let name = id;
    try { const x = await get(id); if (x) name = x.name; } catch (e) {}
    cache[id] = name;
  }
  return cache[id];
};
const varName = (id) => named(id, (i) => figma.variables.getVariableByIdAsync(i));
const styleName = (id) => named(id, (i) => figma.getStyleByIdAsync(i));

const bound = async (prop) => {
  const b = n.boundVariables && n.boundVariables[prop];
  return (b && b.id) ? ' → ' + await varName(b.id) : '';
};
const boundAt = async (prop, i) => {
  const arr = n.boundVariables && n.boundVariables[prop];
  const b = arr && arr[i];
  return (b && b.id) ? ' → ' + await varName(b.id) : '';
};
const uniq = async (props) => {
  const seen = [];
  for (const p of props) { const b = await bound(p); if (b && seen.indexOf(b) === -1) seen.push(b); }
  return seen.join('');
};
const hex = (c) => '#' + [c.r, c.g, c.b]
  .map((v) => ('0' + Math.round(v * 255).toString(16)).slice(-2)).join('').toUpperCase();
const num = (v) => (v === undefined || v === null || typeof v === 'symbol')
  ? '' : String(Math.round(v * 100) / 100);

let head = n.id + '  ' + n.name + ' [' + n.type + ']';
if (n.width !== undefined) {
  head += '  ' + num(n.width) + await bound('width') + '×' + num(n.height) + await bound('height');
}
if (n.x !== undefined) head += '  @ ' + num(n.x) + ',' + num(n.y);
if (n.parent) head += '  in «' + n.parent.name + '»';
out.push(head);

const flags = [];
if (n.visible === false) flags.push('hidden');
if (n.locked) flags.push('locked');
if (n.rotation) flags.push('rotation ' + num(n.rotation));
push('flags', flags.join('  '));

if (n.layoutMode && n.layoutMode !== 'NONE') {
  let s = n.layoutMode[0] + '  gap:' + num(n.itemSpacing) + await bound('itemSpacing');
  // Padding is where several tokens land on one line, and `pad:6,8 → a → b`
  // does not say which number is which. So: collapsed when the sides agree,
  // per-side the moment they do not.
  const sides = ['paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft'];
  const p = sides.map((k) => n[k]);
  const pv = [];
  for (const k of sides) pv.push(await bound(k));
  const oneToken = pv.every((x) => x === pv[0]);
  if (oneToken && !pv[0]) {
    // Nothing bound: the numbers alone, without room for arrows that are not
    // there.
    s += '  pad:' + ((p[0] === p[2] && p[1] === p[3]) ? p[0] + ',' + p[1] : p.join(','));
  } else if (oneToken) {
    s += '  pad:' + ((p[0] === p[2] && p[1] === p[3]) ? p[0] + ',' + p[1] : p.join(',')) + pv[0];
  } else if (p[0] === p[2] && p[1] === p[3] && pv[0] === pv[2] && pv[1] === pv[3]) {
    s += '  pad:' + p[0] + pv[0] + ', ' + p[1] + pv[1];
  } else {
    s += '  pad:' + p.map((v, i) => v + pv[i]).join(', ');
  }
  s += '  ' + (n.layoutSizingHorizontal
    ? 'H=' + n.layoutSizingHorizontal + ' V=' + n.layoutSizingVertical
    : n.primaryAxisSizingMode + '/' + n.counterAxisSizingMode);
  s += '  align:' + n.primaryAxisAlignItems + '/' + n.counterAxisAlignItems;
  if (n.layoutWrap === 'WRAP') s += '  wrap';
  push('layout', s);
} else if (ALL && n.layoutMode) push('layout', 'NONE');

const inparent = [];
if (n.layoutGrow) inparent.push('grow:' + n.layoutGrow);
if (n.layoutAlign && n.layoutAlign !== 'INHERIT') inparent.push('align:' + n.layoutAlign);
if (n.layoutPositioning === 'ABSOLUTE') inparent.push('absolute');
push('inparent', inparent.join('  '));

const c = n.constraints;
if (c && (ALL || c.horizontal !== 'MIN' || c.vertical !== 'MIN')) {
  push('constr', 'H:' + c.horizontal + '  V:' + c.vertical);
}

const paints = async (list, prop) => {
  if (typeof list === 'symbol') return 'mixed';
  if (!list || !list.length) return ALL ? 'none' : '';
  const parts = [];
  for (let i = 0; i < list.length; i++) {
    const p = list[i];
    let t = p.type === 'SOLID' ? hex(p.color) : p.type;
    if (p.opacity !== undefined && p.opacity !== 1) t += ' ' + Math.round(p.opacity * 100) + '%';
    if (p.visible === false) t += ' (hidden)';
    parts.push(t + await boundAt(prop, i));
  }
  return parts.join('  ');
};
push('fill', await paints(n.fills, 'fills'));

let stroke = await paints(n.strokes, 'strokes');
if (stroke && stroke !== 'none') {
  const w = typeof n.strokeWeight === 'number' ? num(n.strokeWeight) + 'px ' : '';
  stroke = w + stroke + await bound('strokeWeight');
  if (n.strokeAlign && n.strokeAlign !== 'INSIDE') stroke += '  ' + n.strokeAlign;
  if (n.dashPattern && n.dashPattern.length) stroke += '  dash:' + n.dashPattern.join(',');
}
push('stroke', stroke);

const styles = [];
for (const pair of [['fillStyleId', 'fill'], ['strokeStyleId', 'stroke'],
                    ['textStyleId', 'text'], ['effectStyleId', 'effect'],
                    ['gridStyleId', 'grid']]) {
  const id = n[pair[0]];
  if (typeof id === 'string' && id) styles.push(pair[1] + ':' + await styleName(id));
}
push('style', styles.join('  '));

const corners = ['topLeftRadius', 'topRightRadius', 'bottomRightRadius', 'bottomLeftRadius'];
if (n.cornerRadius !== undefined) {
  const uniform = typeof n.cornerRadius === 'number';
  const value = uniform ? num(n.cornerRadius) : corners.map((k) => num(n[k])).join(',');
  const vars = (await bound('cornerRadius')) + await uniq(corners);
  if (ALL || vars || (uniform ? n.cornerRadius !== 0 : value.replace(/[0,]/g, '') !== '')) {
    push('radius', value + vars);
  }
}

if (n.opacity !== undefined && (ALL || n.opacity !== 1)) {
  push('opacity', num(n.opacity) + await bound('opacity'));
}
if (n.blendMode && (ALL || (n.blendMode !== 'PASS_THROUGH' && n.blendMode !== 'NORMAL'))) {
  push('blend', n.blendMode);
}
if (n.clipsContent === false) push('clip', 'off');

if (n.effects && n.effects.length) {
  push('effects', n.effects.map((e) => {
    let t = e.type;
    if (e.offset) t += ' ' + num(e.offset.x) + ',' + num(e.offset.y);
    if (e.radius !== undefined) t += ' blur ' + num(e.radius);
    if (e.spread) t += ' spread ' + num(e.spread);
    if (e.color) {
      t += ' ' + hex(e.color);
      const a = e.color.a === undefined ? 1 : e.color.a;
      if (a !== 1) t += ' ' + Math.round(a * 100) + '%';
    }
    if (e.visible === false) t += ' (hidden)';
    return t;
  }).join('  |  '));
} else if (ALL) push('effects', 'none');

if (n.type === 'TEXT') {
  const chars = n.characters || '';
  push('text', JSON.stringify(chars.slice(0, 120)) + (chars.length > 120 ? ' …' : ''));
  const f = n.fontName;
  let font = (typeof f === 'symbol' ? 'mixed' : f.family + ' ' + f.style) + '  ' +
    (typeof n.fontSize === 'symbol' ? 'mixed' : num(n.fontSize));
  const lh = n.lineHeight;
  if (lh && lh.unit && lh.unit !== 'AUTO') font += '/' + num(lh.value) + (lh.unit === 'PERCENT' ? '%' : '');
  const ls = n.letterSpacing;
  if (ls && ls.value) font += '  ls ' + num(ls.value) + (ls.unit === 'PERCENT' ? '%' : '');
  push('font', font + await bound('fontSize'));
  const t = [];
  if (n.textAlignHorizontal && n.textAlignHorizontal !== 'LEFT') t.push(n.textAlignHorizontal);
  if (n.textAlignVertical && n.textAlignVertical !== 'TOP') t.push(n.textAlignVertical);
  if (n.textAutoResize && n.textAutoResize !== 'NONE') t.push(n.textAutoResize);
  if (n.textCase && n.textCase !== 'ORIGINAL') t.push(n.textCase);
  if (n.textDecoration && n.textDecoration !== 'NONE') t.push(n.textDecoration);
  push('align', t.join('  '));
}

if (n.type === 'INSTANCE') {
  const main = await n.getMainComponentAsync();
  const set = main && main.parent && main.parent.type === 'COMPONENT_SET' ? main.parent.name + ' / ' : '';
  const props = [];
  for (const k in (n.componentProperties || {})) {
    props.push(k.split('#')[0] + '=' + n.componentProperties[k].value);
  }
  push('main', (main ? set + main.name : '?') + (props.length ? '  (' + props.join(', ') + ')' : ''));
}
if (n.type === 'COMPONENT_SET') push('variants', n.children.map((c) => c.name).join('  |  '));
if (n.children) {
  push('children', String(n.children.length));
  // Why a layout moved is almost always a child's sizing rather than the
  // parent's, so the row carries FILL/HUG/FIXED and grow, not just the size.
  if (KIDS) for (const c of n.children) {
    let line = '  ' + c.id + '  ' + c.type + '  ' + num(c.width) + '×' + num(c.height);
    if (c.layoutSizingHorizontal) line += '  H=' + c.layoutSizingHorizontal + ' V=' + c.layoutSizingVertical;
    if (c.layoutGrow) line += '  grow:' + c.layoutGrow;
    if (c.layoutPositioning === 'ABSOLUTE') line += '  absolute';
    if (c.visible === false) line += '  hidden';
    line += '  ' + c.name;
    if (c.type === 'TEXT') line += '  ' + JSON.stringify(String(c.characters).slice(0, 40));
    out.push(line);
  }
}

return out.join('\n');
"""


def cmd_props(args):
    code = (PROPS_JS
            .replace("__ID__", json.dumps(args.node_id))
            .replace("__ALL__", "true" if args.all else "false")
            .replace("__KIDS__", "true" if args.children else "false"))
    return _send(code, args)


# What tokens exist at all — the other half of `props`, which can only say
# which token a value is bound to, never whether a suitable one exists.
#
# Without a filter this deliberately does not dump the file: a design system is
# hundreds of variables, and "here are all of them" is tens of thousands of
# tokens spent to answer "is there a spacing scale". So: a map first (modes,
# counts, name prefixes), rows only for what was asked about.
VARS_JS = r"""
const FILTER = __FILTER__.toLowerCase();
const TYPE = __TYPE__;
const out = [];
const hex = (c) => '#' + [c.r, c.g, c.b]
  .map((v) => ('0' + Math.round(v * 255).toString(16)).slice(-2)).join('').toUpperCase()
  + (c.a === undefined || c.a === 1 ? '' : ' ' + Math.round(c.a * 100) + '%');
const num = (v) => String(Math.round(v * 100) / 100);
const pad = (s, n) => (s + '                                        ').slice(0, Math.max(n, s.length));

const localNames = {};
const value = async (v, modeId) => {
  const raw = v.valuesByMode[modeId];
  if (raw === undefined || raw === null) return '—';
  if (raw.type === 'VARIABLE_ALIAS') {
    // Local aliases are already in hand; only a pointer into a library costs
    // a lookup.
    if (localNames[raw.id]) return '→ ' + localNames[raw.id];
    const target = await figma.variables.getVariableByIdAsync(raw.id);
    return '→ ' + (target ? target.name : raw.id);
  }
  if (v.resolvedType === 'COLOR') return hex(raw);
  if (typeof raw === 'number') return num(raw);
  return String(raw);
};

const cols = await figma.variables.getLocalVariableCollectionsAsync();
if (!cols.length) {
  return 'no local variables in this file' +
    '\n(a file that consumes a design system has none of its own — try: figmosha vars --library)';
}

// One call for the whole file rather than one per variable: a real design
// system is ~400 of them, and asking Figma 400 times cost half a second.
// Unfiltered even when --type is given, so that the duplicate count below sees
// every collection a name lives in; filtering here instead costs nothing.
const everything = await figma.variables.getLocalVariablesAsync();
for (const v of everything) localNames[v.id] = v.name;

// A themed library holds every primitive twice — once per theme collection —
// so a bare name is not something you can bind by. Those rows are printed
// qualified, which is the form h.bF/h.bN take back; the rest stay short.
const seenIn = {};
for (const v of everything) {
  (seenIn[v.name] = seenIn[v.name] || {})[v.variableCollectionId] = 1;
}
const ambiguous = (v) => Object.keys(seenIn[v.name]).length > 1;

const all = TYPE ? everything.filter((v) => v.resolvedType === TYPE) : everything;
const byCollection = {};
for (const v of all) {
  (byCollection[v.variableCollectionId] = byCollection[v.variableCollectionId] || []).push(v);
}

let shown = 0;
let qualified = 0;
// `shown` counts the whole file, so the cap is a property of the command, not
// of a collection — and `break` only leaves the inner loop. Without this flag
// a file with seven collections printed the same warning seven times.
let stopped = false;
for (const col of cols) {
  if (stopped) break;
  const picked = (byCollection[col.id] || []).filter(
    (v) => !FILTER || v.name.toLowerCase().indexOf(FILTER) !== -1);
  // A collection with nothing matching is not an answer, it is a line of
  // noise — and a file has as many of them as it has collections.
  if (!picked.length && (FILTER || TYPE)) continue;
  const modes = col.modes.map((m) => m.name).join(', ');
  out.push(col.name + '  modes: ' + modes + '  ·  ' + picked.length +
    (FILTER || TYPE ? ' matching' : '') + ' of ' + col.variableIds.length);
  if (!picked.length) continue;

  // Figma hands variables back in creation order, so a scale reads sp-1,
  // sp-10, sp-0. Digit runs are padded before comparing, so sp-2 sorts
  // before sp-10 rather than after it.
  const key = (s) => s.replace(/[0-9]+/g, (m) => ('000000' + m).slice(-6));
  picked.sort((a, b) => {
    const ka = key(a.name), kb = key(b.name);
    return ka < kb ? -1 : ka > kb ? 1 : 0;
  });

  if (!FILTER && !TYPE) {
    // The map: how many tokens live under each top-level prefix. Enough to
    // know what to ask for next, at two lines instead of two hundred.
    const groups = {};
    for (const v of picked) {
      const key = v.name.indexOf('/') === -1 ? v.name : v.name.slice(0, v.name.indexOf('/') + 1);
      groups[key] = (groups[key] || 0) + 1;
    }
    out.push('  ' + Object.keys(groups).sort()
      .map((k) => k + ' ' + groups[k]).join('   '));
    continue;
  }

  const label = (v) => ambiguous(v) ? col.name + '/' + v.name : v.name;
  const width = Math.min(38, Math.max.apply(null, picked.map((v) => label(v).length)) + 2);
  for (const v of picked) {
    if (shown++ >= __MAX__) {
      out.push('  … stopped at __MAX__ rows — narrow the filter');
      stopped = true;
      break;
    }
    if (ambiguous(v)) qualified++;
    const cells = [];
    for (const m of col.modes) cells.push(await value(v, m.modeId));
    out.push('  ' + pad(label(v), width) + cells.join('   '));
  }
}
if (qualified) {
  out.push('(' + qualified + ' name(s) printed as Collection/name live in more than ' +
    'one collection — bind by that full form, a bare name is ambiguous)');
}
return out.join('\n');
"""

# Library collections are a network call per collection, so the variables
# inside one are fetched only when something is actually being looked for.
LIB_VARS_JS = r"""
const FILTER = __FILTER__.toLowerCase();
const cols = await figma.teamLibrary.getAvailableLibraryVariableCollectionsAsync();
if (!cols.length) return 'no library variable collections available to this file';
if (!FILTER) {
  return cols.map((c) => c.libraryName + ' / ' + c.name + '  ' + c.key).join('\n') +
    '\n(add a filter to list the variables inside, with the keys h.importVar() takes)';
}
const out = [];
for (const c of cols) {
  const vars = await figma.teamLibrary.getVariablesInLibraryCollectionAsync(c.key);
  const hit = vars.filter((v) => v.name.toLowerCase().indexOf(FILTER) !== -1);
  if (!hit.length) continue;
  out.push(c.libraryName + ' / ' + c.name + '  ·  ' + hit.length + ' of ' + vars.length);
  for (const v of hit) out.push('  ' + v.name + '  [' + v.resolvedType + ']  ' + v.key);
}
return out.length ? out.join('\n') : 'nothing matches ' + JSON.stringify(FILTER) + ' in any library collection';
"""

STYLES_JS = r"""
const FILTER = __FILTER__.toLowerCase();
const hex = (c) => '#' + [c.r, c.g, c.b]
  .map((v) => ('0' + Math.round(v * 255).toString(16)).slice(-2)).join('').toUpperCase();
const num = (v) => String(Math.round(v * 100) / 100);
const pad = (s, n) => (s + '                                        ').slice(0, Math.max(n, s.length));

const paintText = (s) => (s.paints || []).map((p) => {
  let t = p.type === 'SOLID' ? hex(p.color) : p.type;
  if (p.opacity !== undefined && p.opacity !== 1) t += ' ' + Math.round(p.opacity * 100) + '%';
  return t;
}).join('  ');
const textText = (s) => {
  let t = s.fontName.family + ' ' + s.fontName.style + '  ' + num(s.fontSize);
  if (s.lineHeight && s.lineHeight.unit && s.lineHeight.unit !== 'AUTO') {
    t += '/' + num(s.lineHeight.value) + (s.lineHeight.unit === 'PERCENT' ? '%' : '');
  }
  if (s.letterSpacing && s.letterSpacing.value) {
    t += '  ls ' + num(s.letterSpacing.value) + (s.letterSpacing.unit === 'PERCENT' ? '%' : '');
  }
  return t;
};
const effectText = (s) => (s.effects || []).map((e) => e.type).join('  ');
const gridText = (s) => (s.layoutGrids || []).map((g) => g.pattern +
  (g.count ? ' ×' + g.count : '')).join('  ');

const kinds = [
  ['paint', await figma.getLocalPaintStylesAsync(), paintText],
  ['text', await figma.getLocalTextStylesAsync(), textText],
  ['effect', await figma.getLocalEffectStylesAsync(), effectText],
  ['grid', await figma.getLocalGridStylesAsync(), gridText],
];

const out = [];
for (const kind of kinds) {
  const hit = kind[1].filter((s) => !FILTER || s.name.toLowerCase().indexOf(FILTER) !== -1);
  if (!hit.length) continue;
  out.push(kind[0] + '  ' + hit.length + (FILTER ? ' matching of ' + kind[1].length : ''));
  const width = Math.min(38, Math.max.apply(null, hit.map((s) => s.name.length)) + 2);
  for (const s of hit) out.push('  ' + pad(s.name, width) + (kind[2](s) || '—'));
}
return out.length ? out.join('\n')
  : 'no local styles' + (FILTER ? ' match ' + JSON.stringify(FILTER) : ' in this file');
"""


def cmd_vars(args):
    js = LIB_VARS_JS if args.library else VARS_JS
    code = (js
            .replace("__FILTER__", json.dumps(args.filter or ""))
            .replace("__TYPE__", json.dumps(args.type.upper()) if args.type else "null")
            .replace("__MAX__", str(MAX_ROWS)))
    return _send(code, args)


def cmd_styles(args):
    code = STYLES_JS.replace("__FILTER__", json.dumps(args.filter or ""))
    return _send(code, args)


def cmd_sel(args):
    if args.raw:
        code = f"return figma.currentPage.selection.map({NODE_FIELDS_JS});"
    else:
        code = (
            f"{ROW_JS}"
            f"const s = figma.currentPage.selection;"
            f"return s.length ? s.map(row).join('\\n') : 'nothing selected';"
        )
    return _send(code, args)


def cmd_doctor(args):
    """Walk the chain bridge -> plugin -> Figma, naming the fix at each break."""
    def ok(text):
        print(f"  ✓  {text}")

    def fail(text, fix):
        print(f"  ✗  {text}")
        print(f"     → {fix}")

    if PROJECT:
        ok(f"project «{PROJECT['name']}», port {PROJECT['port']}")
    else:
        fail("uninitialized copy — no project.json",
             "claim it for a project:  python figmosha.py init --name <Project>")

    status, resp = _request("GET", "/status")
    if status == 0:
        fail(f"bridge not answering on {HOST}:{PORT} — {resp.get('error')}",
             "start it:  bash start-bridge.sh   (Windows: .\\start-bridge.ps1)")
        return 1
    if status == 403:
        fail(f"bridge refused the request: {resp.get('error')}",
             "a proxy is rewriting Host/Origin — talk to the bridge directly")
        return 1
    serving = resp.get("project")
    if PROJECT and serving and serving != PROJECT["name"]:
        fail(f"port {PORT} is held by project «{serving}», not «{PROJECT['name']}»",
             "another project took this port — re-run `init` here to move to a free one")
        return 1
    ok(f"bridge answering on {HOST}:{PORT}" + (f" — «{serving}»" if serving else ""))

    plugin_menu = f"Figmosha · {PROJECT['name']}" if PROJECT else "Figmosha Bridge"
    if not resp.get("plugin_connected"):
        fail("plugin not connected",
             f"in Figma Desktop: Plugins → Development → {plugin_menu}")
        return 1
    listing_status, listing = _request("GET", "/sessions")
    rows = listing.get("sessions") or []
    busy_rows = [r for r in rows if r.get("busy")]
    if listing_status == 404:
        ok("plugin connected (bridge predates sessions — restart it to list files)")
    elif rows:
        ok(f"connected Figma files: {len(rows)}")
        for row in rows:
            print(f"       {row['sid']:<18} «{row.get('file') or '?'}»"
                  f" — page «{row.get('page') or '?'}»")
    else:
        # Connected, but the plugin never said which file it is in.
        ok("plugin connected (pre-2.2 plugin: does not report its file)")

    if busy_rows:
        # Worth stopping for, and worth not prescribing a re-Run: the thread
        # nearly always comes back, and a re-Run costs the session with it.
        for row in busy_rows:
            secs = int(row.get("running_ms", 0) / 1000)
            fail(f"«{row.get('file') or row['sid']}» is busy — a script has owned its "
                 f"JS thread for {secs}s",
                 "wait. Figma cannot interrupt a running plugin script, and it usually "
                 "returns on its own; only this file is blocked. If it never does: "
                 f"figmosha sessions --reset {row['sid']}")
        return 1

    if len(rows) > 1:
        print()
        print("  Several files are open, so a bare command has no single target.")
        print("  Pick one per shell:  $env:FIGMOSHA_SESSION = '<sid or file name>'")
        print("  Or per call:         figmosha --session <sid or file name> ...")
        return 0

    status, r = _exec("return 1 + 1;", 10, want_value=True)
    if not r.get("ok") or r.get("value") != 2:
        fail(f"round trip failed: {r.get('error', r)}",
             "if it mentions `busy`, wait — this file is still running an earlier "
             "script. Otherwise close the plugin window in Figma and run it again")
        return 1
    ok(f"round trip works ({r.get('elapsed_ms', '?')}ms)")

    status, r = _exec(
        "return {file: figma.root.name, page: figma.currentPage.name, "
        "pages: figma.root.children.length, "
        "helpers: typeof h.walk === 'function'};", 10, want_value=True)
    if r.get("ok"):
        v = r.get("value") or {}
        ok(f"editing «{v.get('file')}» — page «{v.get('page')}» "
           f"of {v.get('pages')}")
        # The manifest is the only honest source for this: the plugin cannot
        # read its own documentAccess, and how much memory a scan costs before
        # it starts follows directly from it.
        access = _manifest_document_access()
        if access == "dynamic-page":
            ok("documentAccess: dynamic-page — pages load when something asks")
        else:
            fail("documentAccess: legacy — Figma preloaded every page of this file "
                 "before the plugin's first line",
                 "on a big file that is most of an out-of-memory crash spent up front. "
                 "Move over: python figmosha.py init --document-access dynamic-page "
                 "(then re-import the plugin). See CLAUDE.md → Dynamic pages")
        if not v.get("helpers"):
            fail("this plugin predates h.walk",
                 "re-Run the plugin in Figma to pick up the current code.js")
        print("\n  The plugin is bound to whichever file was open when you ran it.")
        print("  Switched files? Run the plugin again in the new one.")
    return 0


def _default_name():
    """Guess the project from the folder, since that is what defines it.

    Figmosha usually sits in a subfolder of the project it serves, so the
    parent's name is the useful one: E:/Work/Northwind/figmosha -> Northwind.
    """
    here = project.ROOT
    if here.name.lower().startswith("figmosha") and here.parent.name:
        return here.parent.name
    return here.name


def _choose_port(name, existing, requested):
    """Pick the port and say why, without ever silently taking someone else's."""
    if requested:
        holder = None if project.port_is_free(requested) else project.occupant(requested)
        if holder and holder != name:
            print(f"  !  port {requested} is held by project «{holder}» — "
                  f"using it anyway, as asked", file=sys.stderr)
        return requested, "asked for"

    if existing:
        # Never move a port that a plugin was already imported against.
        return existing["port"], "kept from project.json"

    wanted = project.port_for(name)
    for step in range(project.PORT_SPAN):
        port = project.PORT_BASE + (wanted - project.PORT_BASE + step) % project.PORT_SPAN
        if project.port_is_free(port):
            return port, ("derived from the name" if step == 0
                          else f"derived port {wanted} was busy")
        holder = project.occupant(port)
        if holder == name:
            # Our own bridge is already up on it — that is not a conflict.
            return port, "already served by this project's bridge"
        if step == 0:
            who = f"project «{holder}»" if holder else "something else"
            print(f"  !  port {wanted} is held by {who}", file=sys.stderr)
    raise SystemExit(f"figmosha: no free port in "
                     f"{project.PORT_BASE}..{project.PORT_BASE + project.PORT_SPAN - 1}")


def _manifest_document_access():
    """What the imported plugin's manifest says, or None if it cannot be read."""
    try:
        with open(project.ROOT / "plugin" / "manifest.json", encoding="utf-8") as f:
            return json.load(f).get("documentAccess", "legacy")
    except (OSError, json.JSONDecodeError):
        return None


def _write_manifest(name, port, document_access="dynamic-page"):
    """Patch the plugin manifest in place; returns (path, old_id, new_id).

    Patched rather than generated from a template so that permissions,
    editorType and anything Figma adds later survive an `init`.
    """
    path = project.ROOT / "plugin" / "manifest.json"
    with open(path, encoding="utf-8") as f:
        m = json.load(f)

    old_id = m.get("id")
    m["id"] = f"figmosha-{project.slug(name)}"
    m["name"] = f"Figmosha · {name}"
    m.setdefault("networkAccess", {})["allowedDomains"] = [
        f"http://localhost:{port}", f"ws://localhost:{port}",
    ]
    # Without this field Figma guarantees the whole document is loaded before
    # the plugin's first line runs — every page, every node, 20–30 s of it, and
    # it is never released. On a 15-page file that is most of an out-of-memory
    # crash spent before a single command has been sent. With it, pages load
    # when something asks for them; h.resolve does the asking.
    #
    # The cost is that the synchronous lookups throw: .mainComponent,
    # getNodeById, component.instances and friends. Everything under h.* is
    # already async, so this only bites hand-written exec scripts — which is
    # what `legacy` is for while they are being migrated.
    if document_access == "legacy":
        m.pop("documentAccess", None)
    else:
        m["documentAccess"] = document_access

    with open(path, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return path, old_id, m["id"]


def _write_ui(port):
    """Point the plugin UI at this project's bridge."""
    path = project.ROOT / "plugin" / "ui.html"
    text = open(path, encoding="utf-8").read()
    server = f'const SERVER = "ws://localhost:{port}/plugin";'
    patched = re.sub(r'const SERVER = "ws://localhost:\d+/plugin";', server, text, count=1)
    if patched == text and server not in text:
        raise SystemExit(f"figmosha: could not find the SERVER line in {path}")
    open(path, "w", encoding="utf-8", newline="").write(patched)
    return path


# The two files `init` stamps with this project's identity. They are tracked,
# so an update overwrites them — which is the whole reason `update` exists.
STAMPED = ("plugin/manifest.json", "plugin/ui.html")


def _git(*args, cwd=None):
    """Run git, returning (code, stdout+stderr). Imported here to keep the

    common commands' startup cheap — subprocess costs ~10ms to import and no
    other subcommand needs it.
    """
    import subprocess
    r = subprocess.run(["git", *args], cwd=str(cwd or project.ROOT),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    # rstrip only: `git status --porcelain` marks the index column with a
    # leading space, and stripping it shifts every path by one character.
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).rstrip()


def cmd_update(args):
    """Pull, then stamp this project's identity back into the plugin.

    Three steps that must happen in this order, and the middle one destroys
    the first one's output — which is exactly the kind of ritual that gets
    half-performed. Half-performed here means `plugin/ui.html` back on port
    8787 and a plugin that no longer finds its bridge.
    """
    # Read before anything touches the manifest: after the pull the file holds
    # the repository's default id, not the one Figma has imported.
    imported_id = _manifest_id()

    code, _ = _git("rev-parse", "--git-dir")
    if code != 0:
        print("figmosha: this copy is not a git clone, so there is nothing to pull",
              file=sys.stderr)
        print("   update it by copying the new files over (all but project.json),",
              file=sys.stderr)
        print("   then run:  python figmosha.py init", file=sys.stderr)
        print("   and if plugin/manifest.json was among them, re-IMPORT the plugin in",
              file=sys.stderr)
        print("   Figma rather than re-Running it — a manifest change needs the import",
              file=sys.stderr)
        return 2

    code, dirty = _git("status", "--porcelain", "--untracked-files=no")
    # "XY path", where X or Y may be a space — so split on whitespace rather
    # than trusting the column width.
    edited = [line.strip().split(None, 1)[-1] for line in dirty.splitlines() if line.strip()]
    unexpected = [f for f in edited if f not in STAMPED]
    if unexpected:
        # Refusing beats guessing: the next step throws away local changes.
        print("figmosha: uncommitted changes would be lost — commit or stash first",
              file=sys.stderr)
        for f in unexpected:
            print(f"   {f}", file=sys.stderr)
        return 1

    code, remote = _git("remote")
    if not remote.strip():
        print("figmosha: no git remote — this clone has nowhere to pull from",
              file=sys.stderr)
        return 2

    before = _git("rev-parse", "HEAD")[1]
    if edited:
        # Let go of the stamped files, or the pull refuses to overwrite them.
        _git("checkout", "--", *STAMPED)

    code, out = _git("pull", "--ff-only")
    if code != 0:
        print(f"figmosha: git pull failed\n{out}", file=sys.stderr)
        print("   the stamped files were restored; run `figmosha init` "
              "before using this copy", file=sys.stderr)
        return 1

    after = _git("rev-parse", "HEAD")[1]
    if after == before:
        print("  ✓  already up to date")
    else:
        _, log = _git("log", "--oneline", f"{before}..{after}")
        count = len(log.splitlines())
        print(f"  ✓  pulled {count} commit(s)")
        for line in log.splitlines()[:10]:
            print(f"       {line}")
        if count > 10:
            print(f"       … {count - 10} more")

    _, changed = _git("diff", "--name-only", f"{before}..{after}")
    changed = changed.splitlines()

    rc = cmd_init(args, previous_id=imported_id)

    # A manifest change is the difference between "re-Run the plugin" and
    # "remove it in Figma and import it again", and getting that wrong looks
    # exactly like the update not working.
    if "plugin/manifest.json" in changed:
        print("  !  manifest.json changed — re-IMPORT the plugin in Figma:")
        print("     Plugins → Development → Manage plugins → remove, then Import again")
    elif any(f.startswith("plugin/") for f in changed):
        print("  →  plugin code changed — re-Run the plugin in Figma")
    if any(f in ("bridge.py", "project.py") for f in changed):
        print(r"  →  bridge changed — restart it:  .\start-bridge.ps1 -Restart")
    return rc


def _manifest_id():
    """The plugin id this copy currently carries, or None if unreadable."""
    try:
        with open(project.ROOT / "plugin" / "manifest.json", encoding="utf-8") as f:
            return json.load(f).get("id")
    except (OSError, json.JSONDecodeError):
        return None


def cmd_init(args, previous_id=None):
    """Claim this copy for a project: its own port, its own plugin entry.

    `previous_id` is for `update`, which has just overwritten the manifest with
    the repository's default: the id in the file is then no longer the id Figma
    imported, and comparing against it would advise a re-import that is not
    needed. The caller that knows better says so.
    """
    existing = project.load()
    name = args.name or (existing["name"] if existing else _default_name())

    # A rename changes the plugin's id and menu entry, but never the port: a
    # plugin imported against the old number would stop finding the bridge.
    port, why = _choose_port(name, existing, args.port)

    cfg = dict(existing or {})
    cfg["name"] = name
    cfg["port"] = port
    cfg.setdefault("created", date.today().isoformat())
    # Sticky, like the port: `update` re-stamps the manifest from the
    # repository's default, and a project that deliberately stayed on legacy
    # must not be moved back by a routine pull.
    if getattr(args, "document_access", None):
        cfg["document_access"] = args.document_access
    document_access = cfg.get("document_access", "dynamic-page")
    cfg_path = project.save(cfg)

    manifest_path, old_id, new_id = _write_manifest(name, port, document_access)
    if previous_id is not None:
        old_id = previous_id
    _write_ui(port)

    print(f"  ✓  {cfg_path.name:<22} {name}, port {port} ({why})")
    print(f"  ✓  {'plugin/manifest.json':<22} id {new_id} · «Figmosha · {name}»")
    print(f"  ✓  {'plugin/ui.html':<22} ws://localhost:{port}/plugin")
    if document_access == "legacy":
        print(f"  !  {'documentAccess':<22} legacy — Figma preloads the whole document "
              f"on the first run")
        print(f"     {'':<22} (deprecated; see CLAUDE.md → Dynamic pages)")
    else:
        print(f"  ✓  {'documentAccess':<22} {document_access} — pages load when something "
              f"asks for them")
    print()

    if old_id != new_id:
        print("  →  import the plugin in Figma: Plugins → Development →")
        print(f"     Import plugin from manifest… → {manifest_path}")
        if existing:
            print(f"     (the id changed from {old_id} — remove the old entry first)")
    else:
        print("  →  identity unchanged; re-Run the plugin in Figma")
    print(r"  →  start the bridge:  .\start-bridge.ps1   (bash: ./start-bridge.sh)")
    return 0


def cmd_sessions(args):
    """Which Figma files this project's bridge is serving right now."""
    # The bridge is asked to do the matching, rather than the CLI repeating it:
    # a `*` that means something other than "this is where commands go" is
    # worse than no marker at all, and a name prefix like `kite` for «Kite
    # folio» is exactly the case where the two implementations drifted apart.
    if args.reset:
        return _reset_session(args.reset)

    path = "/sessions" + (f"?session={urllib.parse.quote(SESSION)}" if SESSION else "")
    status, resp = _request("GET", path)
    if status == 404:
        print("figmosha: this bridge is older than sessions — restart it from "
              "this folder", file=sys.stderr)
        return 1
    if status != 200:
        print(f"figmosha: {resp.get('error', 'bridge not answering')}", file=sys.stderr)
        return 1
    rows = resp.get("sessions") or []
    if not rows:
        print("no Figma file connected — run the plugin in Figma")
        return 1
    matched = resp.get("matched")
    # File name first: it is what a human addresses a session by, and unlike
    # the sid it survives re-running the plugin.
    for row in rows:
        marker = "*" if matched is not None and row["sid"] in matched else " "
        state = "idle"
        if row.get("busy"):
            state = f"BUSY {int(row.get('running_ms', 0) / 1000)}s"
            if row.get("orphaned"):
                state += " (its caller gave up)"
            elif not row.get("started", True):
                state += " (dispatched, not started)"
        print(f"{marker} «{row.get('file') or '?'}»  {row['sid']}"
              f"  page «{row.get('page') or '?'}»  {state}"
              f"  pending {row.get('pending', 0)}  {row.get('age_s', 0)}s")
    if any(r.get("busy") for r in rows):
        print("  BUSY means a script still owns that file's JS thread. Figma cannot "
              "interrupt it;\n  it usually returns on its own. `figmosha sessions "
              "--reset <sid>` only clears the\n  bridge's memory of it — the script "
              "keeps running.", file=sys.stderr)
    if SESSION and matched is not None and len(matched) != 1:
        print(f"figmosha: FIGMOSHA_SESSION={SESSION!r} matches {len(matched)} of them — "
              f"commands will be refused until it matches exactly one", file=sys.stderr)
        return 1
    return 0


def _reset_session(sid):
    """Make the bridge forget what a wedged session is running. Last resort."""
    status, resp = _request("POST", f"/sessions/{urllib.parse.quote(sid)}/reset")
    if status == 404:
        print(f"figmosha: {resp.get('error', 'no such session')}", file=sys.stderr)
        for row in resp.get("sessions") or []:
            print(f"   {row['sid']:<18} «{row.get('file') or '?'}»", file=sys.stderr)
        return 1
    if status != 200:
        print(f"figmosha: {resp.get('error', 'reset failed')}", file=sys.stderr)
        return 1
    cleared = resp.get("cleared") or []
    if not cleared:
        print(f"{sid}: nothing was running")
        return 0
    for c in cleared:
        print(f"{sid}: forgot {c['rid']} after {int(c['running_ms'] / 1000)}s")
    print(f"  ! {resp.get('warning', '')}", file=sys.stderr)
    return 0


IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")


def prelude(assignments):
    """`--set ROOT=185:21880` -> `const ROOT = "185:21880";` before the code.

    A script kept in a file almost always needs one or two ids from the caller,
    and the alternative is editing the file before every run — a step in four
    skills, and a copy of the script per invocation.

    Three spellings, because guessing is right most of the time and wrong
    expensively:

      --set      JSON when it parses, a string when it does not. `--set N=[0,4,8]`
                 is an array; `--set ID=185:21880` is not a syntax error. The
                 trap is `--set KEYS={"a":"b"}`, which arrives as an object, so
                 a script that calls JSON.parse(KEYS) throws.
      --set-str  always a string. This is the fix for the trap above.
      --set-json always JSON; a value that does not parse is an error here
                 rather than a silent string that breaks two lines later.

    `NAME=@path` reads the value from a file, which is how a long key list gets
    past the command-line length limit (~8k on Windows) and past quoting.
    """
    lines = []
    for item in assignments or []:
        # Plain strings mean --set, so callers that predate the other two forms
        # (and the tests that pin them) keep working unchanged.
        mode, item = item if isinstance(item, tuple) else ("auto", item)
        if "=" not in item:
            raise ValueError(f"expected NAME=value, got {item!r}")
        name, raw = item.split("=", 1)
        name = name.strip()
        if not IDENTIFIER.match(name):
            raise ValueError(f"{name!r} is not a JS identifier — cannot be a const name")
        if raw.startswith("@"):
            try:
                raw = Path(raw[1:]).read_text(encoding="utf-8")
            except OSError as e:
                raise ValueError(f"{name}=@{raw[1:]}: {e}")
        if mode == "str":
            value = raw
        elif mode == "json":
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ValueError(f"--set-json {name}: not valid JSON ({e})")
        else:
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                value = raw
        lines.append(f"const {name} = {json.dumps(value, ensure_ascii=False)};")
    return "\n".join(lines) + ("\n" if lines else "")


class SetAction(argparse.Action):
    """Collect --set / --set-str / --set-json into one list, in typed order."""

    MODES = {"--set": "auto", "--set-str": "str", "--set-json": "json"}

    def __call__(self, parser, namespace, value, option_string=None):
        current = getattr(namespace, self.dest, None) or []
        setattr(namespace, self.dest,
                current + [(self.MODES.get(option_string, "auto"), value)])


def cmd_exec(args):
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            code = f.read()
    elif args.stdin:
        code = sys.stdin.read()
    elif args.code:
        code = args.code
    else:
        print("figmosha: provide code (positional, --file, or --stdin)", file=sys.stderr)
        return 2
    try:
        code = prelude(args.set) + code
    except ValueError as e:
        print(f"figmosha: {e}", file=sys.stderr)
        return 2
    return _send(code, args)


def cmd_tree(args):
    opts = json.dumps({
        "maxDepth": args.depth,
        "showSize": not args.no_size,
        "showText": not args.no_text,
        "showLayout": args.layout,
        "collapse": not args.no_collapse,
    })
    code = (
        f"const n = {node_expr(args.node_id)};"
        f"if (!n) throw new Error('node not found: ' + {json.dumps(args.node_id)});"
        f"return h.dumpTree(n, {opts});"
    )
    return _send(code, args)


def cmd_find(args):
    if "=" not in args.filter and "~" not in args.filter:
        print("figmosha: filter must be key=value or key~value", file=sys.stderr)
        print("  forms: name=X (exact), name~X (substring), type=X, text=X (substring)", file=sys.stderr)
        return 2

    # Whichever separator comes first decides the mode, so a value may contain
    # the other character (name~a=b is a substring search for "a=b").
    tilde = args.filter.find("~")
    equals = args.filter.find("=")
    substring_mode = tilde != -1 and (equals == -1 or tilde < equals)

    if substring_mode:
        key, value = args.filter.split("~", 1)
        if key == "name":
            predicate = f"n.name.includes({json.dumps(value)})"
        elif key == "text":
            predicate = f"n.type === 'TEXT' && n.characters.includes({json.dumps(value)})"
        else:
            print(f"figmosha: substring filter only supports name~ and text~ (got {key}~)", file=sys.stderr)
            return 2
    else:
        key, value = args.filter.split("=", 1)
        if key == "name":
            predicate = f"n.name === {json.dumps(value)}"
        elif key == "type":
            predicate = f"n.type === {json.dumps(value.upper())}"
        elif key == "text":
            predicate = f"n.type === 'TEXT' && n.characters === {json.dumps(value)}"
        else:
            print(f"figmosha: unknown filter key '{key}'. Use name, type, text", file=sys.stderr)
            return 2

    # h.walk, not root.findAll: findAll is synchronous, returns every match as
    # a live node proxy, and descends into instances — which is where the
    # volume is. One frame in the file this was measured on holds 53 439
    # instances, and 0 of 339 real hits were nested inside one. h.walk prunes
    # there by default, checks the caller's deadline between nodes, and hands
    # back rows rather than proxies.
    prune = "false" if args.nested else "true"
    opts = f"{{pruneInstances: {prune}, includeRoot: false}}"
    preamble = (
        f"const root = {node_expr(args.node_id)};"
        f"if (!root) throw new Error('node not found: ' + {json.dumps(args.node_id)});"
        f"const hit = n => {predicate};"
    )
    stopped = (
        "(r.partial"
        " ? '\\n… stopped after ' + r.visited + ' nodes (' + r.reason + ')"
        " — scan a smaller subtree, or raise --timeout' : '')"
    )
    if args.count:
        # Counting must not build the array at all: the count is often the
        # whole question, and on a big subtree the array is the cost.
        code = (
            f"{preamble}"
            f"let count = 0;"
            f"const r = await h.walk(root, n => {{ if (hit(n)) count++; }}, {opts});"
            f"return count + ' found (' + r.visited + ' nodes visited)' + {stopped};"
        )
    elif args.raw:
        # --raw is an explicit ask for the structure, and a truncated array is
        # worse than a long one: nothing in it says a tail is missing.
        code = (
            f"{preamble}"
            f"const r = await h.walk(root, n => hit(n) ? ({NODE_FIELDS_JS})(n) : undefined,"
            f" {opts});"
            f"return r;"
        )
    else:
        # Only when it actually happened: a note on every search is noise, and
        # a search that met no instances has nothing to disclose.
        pruned_note = (
            "" if args.nested else
            " + (r.pruned ? '\\n(' + r.pruned + ' instance(s) not descended"
            " — --nested looks inside them)' : '')"
        )
        code = (
            f"{ROW_JS}{preamble}"
            f"const r = await h.walk(root, n => hit(n) ? row(n) : undefined, {opts});"
            f"const found = r.found;"
            f"const LIMIT = {args.limit};"
            f"const shown = found.slice(0, LIMIT);"
            f"return found.length + ' found'"
            f" + (shown.length ? '\\n' + shown.join('\\n') : '')"
            f" + (found.length > LIMIT"
            f"    ? '\\n… showing ' + LIMIT + ' of ' + found.length +"
            f"      ' — narrow the filter, or --limit ' + found.length : '')"
            f" + {stopped}{pruned_note};"
        )
    return _send(code, args)


def cmd_text(args):
    code = (
        f"const n = {node_expr(args.node_id)};"
        f"if (!n) throw new Error('node not found');"
        f"if (n.type !== 'TEXT') throw new Error('not a TEXT node (got ' + n.type + ')');"
        f"const before = n.characters;"
        f"await h.setText(n, {json.dumps(args.text)});"
        f"return {{id: n.id, before, after: n.characters}};"
    )
    return _send(code, args)


def _mutate(args, mode):
    """set and bind are one program: same targets, same order, same diff.

    Two commands rather than one flag, because what stands on the right differs
    — a literal against a token name — and so does the way each is undone. A
    merged command would have to guess which the caller meant.
    """
    pairs = []
    for kv in args.pairs:
        if "=" not in kv:
            print(f"figmosha: expected key=value, got {kv!r}", file=sys.stderr)
            return 2
        k, v = kv.split("=", 1)
        pairs.append([k.strip(), v.strip()])

    code = (MUTATE_JS
            .replace("__ID__", json.dumps(args.node_id))
            .replace("__PAIRS__", json.dumps(pairs))
            .replace("__MODE__", json.dumps(mode))
            .replace("__DRY__", "true" if args.dry_run else "false"))
    return _send(code, args)


def cmd_set(args):
    return _mutate(args, "set")


def cmd_bind(args):
    return _mutate(args, "bind")


def cmd_overrides(args):
    code = OVERRIDES_JS.replace("__ID__", json.dumps(args.node_id))
    return _send(code, args)


def cmd_where(args):
    code = WHERE_JS.replace("__ID__", json.dumps(args.node_id))
    return _send(code, args)


def cmd_variant(args):
    props = {}
    for kv in args.props:
        if "=" not in kv:
            print(f"figmosha: variant prop must be 'Property=Value', got {kv!r}", file=sys.stderr)
            return 2
        k, v = kv.split("=", 1)
        props[k.strip()] = v.strip()

    code = (
        f"const n = {node_expr(args.node_id)};"
        f"if (!n) throw new Error('node not found');"
        f"if (n.type !== 'INSTANCE') throw new Error('not an INSTANCE (got ' + n.type + ')');"
        f"await n.setProperties({json.dumps(props)});"
        f"const out = {{}};"
        f"for (const k in n.componentProperties) out[k] = n.componentProperties[k].value;"
        f"return {{id: n.id, applied: {json.dumps(props)}, current: out}};"
    )
    return _send(code, args)


def cmd_clone(args):
    direction = "right"
    for d in ("left", "right", "up", "down"):
        if getattr(args, d, False):
            direction = d
    opts = {"direction": direction, "gap": args.gap}
    if args.name:
        opts["name"] = args.name

    code = (
        f"const n = {node_expr(args.node_id)};"
        f"if (!n) throw new Error('node not found');"
        f"const c = h.cloneNext(n, {json.dumps(opts)});"
        f"figma.viewport.scrollAndZoomIntoView([n, c]);"
        f"return {{clone_id: c.id, x: c.x, y: c.y, name: c.name}};"
    )
    return _send(code, args)


def cmd_rm(args):
    # Cleaning up after an experiment is almost always plural — including the
    # `sel` alias, which elsewhere means "the first selected node". Deleting
    # one of three selected layers and reporting success is the kind of quiet
    # wrong answer nobody re-reads, so here `sel` means the whole selection.
    code = (
        f"const asked = {json.dumps(args.node_ids)};"
        f"const ids = [];"
        f"for (const id of asked) {{"
        f"  if (id === 'sel') {{"
        f"    const s = figma.currentPage.selection;"
        f"    if (!s.length) throw new Error('nothing selected in Figma');"
        f"    for (const n of s) ids.push(n.id);"
        f"  }} else ids.push(id);"
        f"}}"
        f"const removed = [], missing = [];"
        f"for (const id of ids) {{"
        f"  const n = await h.resolve(id);"
        f"  if (!n) {{ missing.push(id); continue; }}"
        f"  removed.push({{id: n.id, name: n.name, type: n.type}});"
        f"  n.remove();"
        f"}}"
        f"if (missing.length) throw new Error('node not found: ' + missing.join(', ') + "
        f"  (removed.length ? ' (removed ' + removed.length + ' before that)' : ''));"
        f"return removed;"
    )
    return _send(code, args)



# ─── each: one script over a subtree, a unit at a time ─────────────────────

def _children_of(node_id, timeout):
    """List one node's direct children. Returns (rows, error_exit_code).

    This is the cheap operation the whole runner is built on: reading
    `n.children` does no deep walk, while `findAll` on the same node walks
    everything below it. Choosing the unit of work up front therefore costs
    nothing, and discovering it after a 100-second overrun costs minutes of a
    blocked file — and sometimes the plugin.
    """
    code = (
        f"const n = {node_expr(node_id)};"
        f"if (!n) throw new Error('node not found: ' + {json.dumps(node_id)});"
        f"return (n.children || []).map(c => ({{id: c.id, name: c.name, type: c.type}}));"
    )
    status, resp = _exec(code, timeout, want_value=True)
    if resp.get("ok") is False:
        print(f"figmosha: {resp.get('error', 'could not list children')}", file=sys.stderr)
        return None, _exit_code(status, resp)
    return resp.get("value") or [], None


def _split_ahead(root_id, levels, timeout):
    """Descend `levels` levels of children and return the leaves to run over."""
    frontier = [{"id": root_id, "name": root_id, "type": "ROOT"}]
    for _ in range(max(levels, 0)):
        nxt = []
        for node in frontier:
            kids, err = _children_of(node["id"], timeout)
            if err is not None:
                return None, err
            # A node with no children is a unit in its own right, not a gap.
            nxt.extend(kids if kids else [node])
        frontier = nxt
    return frontier, None


def _load_state(path):
    """Ids this run already finished, from a previous attempt's state file."""
    done = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("status") == "ok":
                    done[rec.get("id")] = rec
    except FileNotFoundError:
        pass
    return done


def cmd_each(args):
    """Run one script over a subtree, one piece at a time, writing state as it goes.

    Every long run this was built from has been interrupted at least once — by
    an overrun, by a plugin that went away when someone switched files, by an
    out-of-memory crash. What turned those into delays rather than restarts was
    writing down each unit as it finished, so that is what this does. Plus the
    two rules that keep a run alive:

      * split before the overrun, not after. `--split N` descends N levels of
        children first, because listing children is free and a page-sized unit
        is not small enough for a mockup file.
      * a busy file means "go smaller", not "failed". A unit that times out is
        not retried — it is split into its children and they are queued ahead
        of everything else, so the branch finishes before the run moves on.
    """
    try:
        code = Path(args.file).read_text(encoding="utf-8")
    except OSError as e:
        print(f"figmosha: {e}", file=sys.stderr)
        return EXIT_USAGE

    try:
        extra = prelude(args.set)
    except ValueError as e:
        print(f"figmosha: {e}", file=sys.stderr)
        return EXIT_USAGE

    # A long run loses the plugin sooner or later — switching files in Figma
    # closes its window — so waiting for it is the default here, unlike
    # everywhere else where failing fast is the friendlier answer.
    global WAIT_PLUGIN
    if not WAIT_PLUGIN:
        WAIT_PLUGIN = 300

    state = open(args.state, "a", encoding="utf-8") if args.state else None
    done = _load_state(args.state) if (args.state and args.resume) else {}
    if done:
        print(f"figmosha: resuming — {len(done)} unit(s) already done", file=sys.stderr)

    units, err = _split_ahead(args.node_id, args.split, args.timeout)
    if err is not None:
        if state:
            state.close()
        return err
    print(f"figmosha: {len(units)} unit(s) after splitting {args.split} level(s)",
          file=sys.stderr)

    queue = [dict(u, depth=0) for u in units]
    tally = {"ok": 0, "failed": 0, "split": 0, "skipped": 0}
    outcome = 0

    def write(rec):
        if state:
            state.write(json.dumps(rec, ensure_ascii=False) + "\n")
            state.flush()      # after every unit: the next one may be the crash

    try:
        while queue:
            unit = queue.pop(0)
            if unit["id"] in done:
                tally["skipped"] += 1
                continue

            body = prelude([("str", f"ROOT_ID={unit['id']}")]) + extra + code
            t0 = time.monotonic()
            status, resp = _exec(body, args.timeout, want_value=True)
            rc = _exit_code(status, resp)
            ms = int((time.monotonic() - t0) * 1000)
            label = f"{unit['id']} «{unit.get('name', '?')}»"

            if rc == 0:
                tally["ok"] += 1
                print(f"  ok    {label}  {ms}ms", file=sys.stderr)
                write({"id": unit["id"], "name": unit.get("name"),
                       "status": "ok", "ms": ms, "value": resp.get("value")})
                continue

            if rc == EXIT_BUSY:
                # Not a retry: the same unit would overrun again, and each
                # overrun costs the file's thread for minutes. Go smaller.
                if unit["depth"] >= args.max_split:
                    tally["failed"] += 1
                    outcome = EXIT_BUSY
                    print(f"  FAIL  {label}  too slow, and already split "
                          f"{unit['depth']} time(s)", file=sys.stderr)
                    write({"id": unit["id"], "name": unit.get("name"),
                           "status": "failed", "ms": ms,
                           "error": resp.get("error")})
                    continue
                # Generously, and on purpose. The unit that just overran still
                # owns the file's thread, so this listing has to wait that out
                # — asking with the unit's own budget would hit the same busy
                # plugin and report "cannot be split", which is a wrong answer
                # to a question we never got to ask.
                kids, kerr = _children_of(unit["id"], max(args.timeout * 5, 120))
                if kerr is not None:
                    tally["failed"] += 1
                    outcome = kerr
                    print(f"  FAIL  {label}  too slow, and the file never freed up "
                          f"long enough to list its children", file=sys.stderr)
                    write({"id": unit["id"], "name": unit.get("name"),
                           "status": "failed", "ms": ms,
                           "error": "could not list children to split"})
                    continue
                if not kids:
                    tally["failed"] += 1
                    outcome = EXIT_BUSY
                    print(f"  FAIL  {label}  too slow, and it has no children to "
                          f"split into", file=sys.stderr)
                    write({"id": unit["id"], "name": unit.get("name"),
                           "status": "failed", "ms": ms, "error": resp.get("error")})
                    continue
                tally["split"] += 1
                print(f"  split {label}  → {len(kids)} child(ren)", file=sys.stderr)
                write({"id": unit["id"], "name": unit.get("name"),
                       "status": "split", "ms": ms, "into": [k["id"] for k in kids]})
                # Depth-first: finish this branch before moving on, so a run
                # that is interrupted has whole subtrees done rather than a
                # scattering of pieces.
                queue = [dict(k, depth=unit["depth"] + 1) for k in kids] + queue
                continue

            if rc == EXIT_NO_PLUGIN:
                # _exec already waited; if we are still here it is not coming
                # back, and going on would just fail every remaining unit.
                tally["failed"] += 1
                print(f"  FAIL  {label}  plugin gone — run it in Figma and "
                      f"re-run with --resume", file=sys.stderr)
                write({"id": unit["id"], "name": unit.get("name"),
                       "status": "failed", "error": resp.get("error")})
                outcome = EXIT_NO_PLUGIN
                break

            tally["failed"] += 1
            print(f"  FAIL  {label}  {resp.get('error', 'unknown')}", file=sys.stderr)
            if resp.get("hint"):
                print(f"        hint: {resp['hint']}", file=sys.stderr)
            write({"id": unit["id"], "name": unit.get("name"),
                   "status": "failed", "ms": ms, "error": resp.get("error")})
            outcome = EXIT_SCRIPT
            if not args.keep_going:
                # A script error is usually the script, not the subtree: the
                # next 400 units would fail the same way, slowly.
                print("figmosha: stopping on the first script error "
                      "(--keep-going to carry on)", file=sys.stderr)
                break
    finally:
        if state:
            state.close()

    print(f"figmosha: {tally['ok']} ok, {tally['split']} split, "
          f"{tally['failed']} failed, {tally['skipped']} skipped"
          + (f" — state in {args.state}" if args.state else ""), file=sys.stderr)
    return outcome


def cmd_import_component(args):
    # Through h.importComp, not the raw API: importing a key that belongs to
    # another file and was never published never settles — no error, no return.
    # h.importComp races it against a timer so this fails in seconds with an
    # explanation instead of sitting there until the CLI gives up.
    code = (
        f"const comp = await h.importComp({json.dumps(args.key)},"
        f" {{timeout: {args.import_timeout * 1000}}});"
        f"const inst = comp.createInstance();"
        f"figma.currentPage.appendChild(inst);"
        f"figma.viewport.scrollAndZoomIntoView([inst]);"
        f"return {{component: comp.name, instance_id: inst.id, w: inst.width, h: inst.height}};"
    )
    return _send(code, args)


# ─── argparse / dispatch ───────────────────────────────────────────────────

def _add_common_flags(p):
    p.add_argument("--timeout", "-t", type=int, default=60)
    p.add_argument("--raw", action="store_true", help="print full JSON response")


def build_parser():
    ap = argparse.ArgumentParser(prog="figmosha", description=__doc__.splitlines()[0],
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=DEFAULT_HOST,
                    help="bridge host (env FIGMOSHA_HOST)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="bridge port (env FIGMOSHA_PORT)")
    ap.add_argument("--session", default=DEFAULT_SESSION,
                    help="which open Figma file to talk to (env FIGMOSHA_SESSION)")
    ap.add_argument("--wait-plugin", type=int, default=DEFAULT_WAIT_PLUGIN,
                    metavar="N",
                    help="if the plugin is gone, wait up to N seconds for it to come "
                         "back instead of failing (env FIGMOSHA_WAIT_PLUGIN)")

    sub = ap.add_subparsers(dest="cmd")

    p_update = sub.add_parser(
        "update", help="git pull, then stamp this project's identity back into the plugin")
    # cmd_update ends in cmd_init, which reads these; without them `update`
    # would need its own copy of the stamping logic.
    p_update.set_defaults(name=None, port=None, document_access=None)

    p_init = sub.add_parser("init", help="claim this copy for a project (port + plugin id)")
    p_init.add_argument("--name", help="project name (default: the folder's)")
    p_init.add_argument("--port", type=int, help="override the derived port")
    p_init.add_argument("--document-access", choices=("dynamic-page", "legacy"),
                        help="dynamic-page (default) loads pages on demand. legacy makes "
                             "Figma preload the whole document before the plugin runs — "
                             "only for a project whose exec scripts still use the "
                             "synchronous APIs; see CLAUDE.md → Dynamic pages")

    p_status = sub.add_parser("status")
    p_status.add_argument("--raw", action="store_true", help="print full JSON response")
    p_sessions = sub.add_parser(
        "sessions", help="which Figma files are connected, and which are busy")
    p_sessions.add_argument("--reset", metavar="SID",
                            help="make the bridge forget what this session is running "
                                 "— it does not stop the script (last resort)")

    p_doctor = sub.add_parser("doctor", help="diagnose the bridge -> plugin -> Figma chain")
    _add_common_flags(p_doctor)

    p_sel = sub.add_parser("sel", help="what is selected in Figma right now")
    _add_common_flags(p_sel)

    p_exec = sub.add_parser("exec")
    _add_common_flags(p_exec)
    g = p_exec.add_mutually_exclusive_group()
    g.add_argument("code", nargs="?")
    g.add_argument("--file", "-f")
    g.add_argument("--stdin", action="store_true")
    p_exec.add_argument("--set", dest="set", action=SetAction, metavar="NAME=value",
                        help="define a const before the code, JSON when it parses and a "
                             "string when it does not; NAME=@file reads the value from a "
                             "file (repeatable)")
    p_exec.add_argument("--set-str", dest="set", action=SetAction, metavar="NAME=value",
                        help="same, but always a string — use it for ids and for JSON "
                             "text your script parses itself")
    p_exec.add_argument("--set-json", dest="set", action=SetAction, metavar="NAME=value",
                        help="same, but always JSON — a value that does not parse is an "
                             "error here rather than a surprise in the script")

    p_tree = sub.add_parser("tree")
    _add_common_flags(p_tree)
    p_tree.add_argument("node_id", help="node id, or `page` / `sel`")
    # 3 rather than everything: the old default was survivable only because
    # the result cap cut it off, which is a flood with a lid, not an answer.
    p_tree.add_argument("--depth", type=int, default=3)
    p_tree.add_argument("--no-collapse", action="store_true",
                        help="list identical siblings one by one")
    p_tree.add_argument("--no-size", action="store_true")
    p_tree.add_argument("--no-text", action="store_true")
    p_tree.add_argument("--layout", action="store_true",
                        help="show layoutMode, gap, padding and sizing modes")

    p_props = sub.add_parser("props", help="everything set on one node, tokens resolved")
    _add_common_flags(p_props)
    p_props.add_argument("node_id", help="node id, or `page` / `sel`")
    p_props.add_argument("--all", action="store_true",
                         help="print defaults too (opacity 1, constraints MIN/MIN, ...)")
    p_props.add_argument("--children", "-c", action="store_true",
                         help="one row per direct child, with its sizing (why a layout moved)")

    p_vars = sub.add_parser("vars", help="variables in this file: what tokens exist")
    _add_common_flags(p_vars)
    p_vars.add_argument("filter", nargs="?",
                        help="substring of the name; without it, a map of collections")
    p_vars.add_argument("--type", help="COLOR | FLOAT | STRING | BOOLEAN")
    p_vars.add_argument("--library", action="store_true",
                        help="library collections instead of local ones (a network call)")

    p_styles = sub.add_parser("styles", help="local paint / text / effect / grid styles")
    _add_common_flags(p_styles)
    p_styles.add_argument("filter", nargs="?", help="substring of the name")

    p_find = sub.add_parser("find")
    _add_common_flags(p_find)
    p_find.add_argument("node_id")
    p_find.add_argument("filter", help="name=X | name~X | type=X | text=X | text~X")
    p_find.add_argument("--limit", type=int, default=100,
                        help="rows to print before saying how many were left out")
    p_find.add_argument("--nested", action="store_true",
                        help="descend into instances too. Off by default: an instance's "
                             "children are copies that came with its component, not "
                             "placements, and they are where a big file's node count is")
    p_find.add_argument("--count", action="store_true",
                        help="just how many, without building the list of matches")

    p_text = sub.add_parser("text")
    _add_common_flags(p_text)
    p_text.add_argument("node_id")
    p_text.add_argument("text")

    for name, helptext in (("set", "literal values"), ("bind", "variable names")):
        p = sub.add_parser(name)
        _add_common_flags(p)
        p.add_argument("node_id", help="node id, `page`, or `sel` (the whole selection)")
        p.add_argument("pairs", nargs="+", metavar="key=value", help=helptext)
        p.add_argument("--dry-run", action="store_true",
                       help="print the same diff without writing anything")

    p_overrides = sub.add_parser("overrides",
                                 help="what an instance overrides against its main component")
    _add_common_flags(p_overrides)
    p_overrides.add_argument("node_id", help="instance id, or `sel`")

    p_where = sub.add_parser("where", help="path from the page down to a node, with sizing")
    _add_common_flags(p_where)
    p_where.add_argument("node_id", help="node id, or `sel`")

    p_variant = sub.add_parser("variant")
    _add_common_flags(p_variant)
    p_variant.add_argument("node_id")
    p_variant.add_argument("props", nargs="+")

    p_clone = sub.add_parser("clone")
    _add_common_flags(p_clone)
    p_clone.add_argument("node_id")
    p_clone.add_argument("--right", action="store_true")
    p_clone.add_argument("--left", action="store_true")
    p_clone.add_argument("--up", action="store_true")
    p_clone.add_argument("--down", action="store_true")
    p_clone.add_argument("--gap", type=int, default=100)
    p_clone.add_argument("--name")

    p_rm = sub.add_parser("rm")
    _add_common_flags(p_rm)
    p_rm.add_argument("node_ids", nargs="+", help="one or more node ids, or `sel`")

    p_each = sub.add_parser(
        "each", help="run one script over a subtree, a unit at a time, resumably")
    _add_common_flags(p_each)
    p_each.add_argument("node_id", help="the subtree to cover, or `page` / `sel`")
    p_each.add_argument("--file", "-f", required=True,
                        help="the script to run for each unit; it gets ROOT_ID")
    p_each.add_argument("--split", type=int, default=1, metavar="N",
                        help="descend N levels of children before running anything "
                             "(default 1). Listing children is free; discovering the "
                             "right size after an overrun is not")
    p_each.add_argument("--state", metavar="FILE",
                        help="append one JSON line per unit here, as it finishes")
    p_each.add_argument("--resume", action="store_true",
                        help="skip units this state file already records as done")
    p_each.add_argument("--keep-going", action="store_true",
                        help="carry on after a script error instead of stopping")
    p_each.add_argument("--max-split", type=int, default=4, metavar="N",
                        help="how many times a unit may be split before it is called "
                             "failed (default 4)")
    p_each.add_argument("--set", dest="set", action=SetAction, metavar="NAME=value",
                        help="extra consts for the script, as in `exec`")
    p_each.add_argument("--set-str", dest="set", action=SetAction, metavar="NAME=value",
                        help="the same, always as a string")
    p_each.add_argument("--set-json", dest="set", action=SetAction, metavar="NAME=value",
                        help="the same, always as JSON")

    for name in ("import-component", "icomp"):
        p = sub.add_parser(name)
        _add_common_flags(p)
        p.add_argument("key")
        # 20s, not 60: a key that is going to resolve does so in under a second
        # (measured 4–907 ms), and one that never settles costs the full wait.
        p.set_defaults(timeout=25)
        p.add_argument("--import-timeout", type=int, default=20, metavar="N",
                       help="give up on the import after N seconds (default 20)")

    return ap


def main():
    force_utf8_output()

    ap = build_parser()
    argv = normalize_argv(sys.argv[1:])

    if argv[:1] == ["exec"] and looks_like_a_command(argv[1] if len(argv) > 1 else ""):
        word = argv[1]
        near = sorted(c for c in KNOWN_CMDS if c.startswith(word[:3]))
        print(f"figmosha: unknown command {word!r}", file=sys.stderr)
        if near:
            print(f"   did you mean: {', '.join(near)}", file=sys.stderr)
        print(f'   to run it as JavaScript:  figmosha exec "{word}"', file=sys.stderr)
        sys.exit(2)

    args = ap.parse_args(argv)

    if args.cmd is None:
        ap.print_help()
        sys.exit(2)

    global HOST, PORT, SESSION, WAIT_PLUGIN
    HOST = args.host
    PORT = args.port
    SESSION = args.session
    WAIT_PLUGIN = args.wait_plugin

    dispatch = {
        "init": cmd_init,
        "update": cmd_update,
        "status": cmd_status,
        "sessions": cmd_sessions,
        "doctor": cmd_doctor,
        "sel": cmd_sel,
        "props": cmd_props,
        "vars": cmd_vars,
        "styles": cmd_styles,
        "exec": cmd_exec,
        "each": cmd_each,
        "tree": cmd_tree,
        "find": cmd_find,
        "set": cmd_set,
        "overrides": cmd_overrides,
        "where": cmd_where,
        "bind": cmd_bind,
        "text": cmd_text,
        "variant": cmd_variant,
        "clone": cmd_clone,
        "rm": cmd_rm,
        "import-component": cmd_import_component,
        "icomp": cmd_import_component,
    }
    sys.exit(dispatch[args.cmd](args))


if __name__ == "__main__":
    main()
