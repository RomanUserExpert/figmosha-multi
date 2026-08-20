# Figmosha 2.3 — Claude Code instructions

Drive Figma by sending JS through a local bridge connected to a plugin running
inside Figma Desktop.

This copy serves **one project** — its name and port live in `project.json` —
and, inside it, **any number of open Figma files**, one session each.

Full reference (every helper, every flag, install, troubleshooting): `README.md`.
This file is the part you need loaded while working.

## How to send code

There is a shim next to `figmosha.py`, so `.\figmosha <cmd>` (bash:
`./figmosha <cmd>`) works instead of `python figmosha.py <cmd>` — it finds this
copy's venv itself. Both spellings appear below; they are the same thing.

```bash
python figmosha.py exec "return figma.currentPage.name"
python figmosha.py exec --file script.js
python figmosha.py "return figma.currentPage.name"     # exec is implied
python figmosha.py text 185:21880 "Привіт"             # subcommands, see below
python figmosha.py status
```

Port and project come from `project.json`; never hardcode 8787.
Bridge down: `.\start-bridge.ps1` (bash: `./start-bridge.sh`).
Plugin not connected: ask the user — Plugins → Development → `Figmosha · <project>` → Run.
Anything unclear about the chain: `python figmosha.py doctor` names the fix.

Output is deliberately compact: one line per node, sizes rounded, results
capped at 64 KB and logs at 200 lines with an explicit truncation marker. Add
`--raw` when you need the untouched JSON — full precision, `value`, full stack.

## Several Figma files open at once

`figmosha sessions` lists what's connected. One session — nothing to think
about. Two or more, and a command without a target comes back **409 with the
list of candidates**:

```
figmosha: 2 Figma files are connected - say which one
   s-mt0d2eic-sg28fw  «Aurora» — page «Page 1»
   s-mt0d4mg4-rvv9h8  «Kite folio» — page «8 projects»
```

**Never retry a 409 by picking one yourself.** Editing the wrong Figma file is
silent and expensive to undo. Either the user already told you which file this
thread is about — then set it once and keep it:

```powershell
$env:FIGMOSHA_SESSION = "Aurora"     # id, alias, file name, or a name prefix
```

— or ask them. Per call it's `--session <name>`.

Rules that follow from how Figma works, not from Figmosha:

- **One session is one document, not one page.** `figma.currentPage` is
  document-wide, so two threads cannot split a single file by page — they would
  switch pages under each other. Work across pages by node id with
  `getNodeByIdAsync` (`loadAllPagesAsync` first); `h.sel()` and the `page` alias
  are off-limits in such scripts.
- **Scripts in one file run one at a time.** The bridge serialises them, so a
  long script makes the next one wait rather than interleave. Different files
  run in parallel. A 504 tells you which half of the time was which:
  `waited_ms` vs `ran_ms`.
- **The plugin is bound to the file it was run in.** Switched files? The user
  runs the plugin again there; it becomes another session, it does not move.

## CLI subcommands — use them instead of hand-written exec

| Command | Use case |
|---|---|
| `figmosha doctor` | Diagnose bridge → plugin → Figma, with the fix for each break |
| `figmosha sessions` | Which Figma files are connected right now |
| `figmosha sel` | What the user has selected — one line per node |
| `figmosha vars [filter] [--type T]` | Which variables exist and what they resolve to, per mode. No filter — a map of collections, not a dump of the file. `--library` for collections this file consumes |
| `figmosha styles [filter]` | Local paint / text / effect / grid styles with their values |
| `figmosha props <id> [--all] [-c]` | Everything set on one node, with variables and styles **by name** — the fastest answer to "is this bound or hardcoded?". `-c` adds a row per child with its sizing, for "why did this move" |
| `figmosha tree <id> [--depth N] [--layout]` | Explore structure |
| `figmosha find <id> name=Button` | Locate by exact name (`name~Btn` = substring) |
| `figmosha find <id> type=INSTANCE` | Filter by type (also `text=`, `text~`) |
| `figmosha text <id> "новий"` | Edit text, fonts loaded for you |
| `figmosha variant <id> "Property 1=Default"` | Switch variant |
| `figmosha clone <id> --right --gap 100` | Duplicate adjacent |
| `figmosha rm <id> [<id>…]` | Delete; `rm sel` takes the **whole** selection |
| `figmosha icomp <key>` | Import a library component and instantiate it |
| `figmosha init --name X` | Claim this copy for a project (port + plugin identity) |
| `figmosha update` | `git pull` **and** stamp the project's port and plugin id back into the plugin — one step instead of three, and it names the right follow-up (re-Run vs re-Import) |

Anywhere an id is taken, `page` and `sel` work too — `figmosha tree sel --layout`
dumps the selected subtree without hunting for its id first.

When the user says "this frame" or "the selected one", call `figmosha sel` —
don't ask them to find an id by hand.

## Helpers (`h.*`, available in every exec)

```
h.bF(node, idx, var) · h.bS(node, idx, var) · h.bN(node, prop, var)  bind fill / stroke / number to a variable
h.findByName(root, name) · h.findAllByName(root, name)
h.dumpTree(node, {maxDepth, showSize, showText, showLayout})         indented tree string
h.withFonts(root, fn) · h.setText(node, text)                        font loading, done right
h.cloneNext(node, {direction, gap, name})
h.variant(inst, props) · h.variantsOf(inst)                          {current, groups, all}
h.sel() · h.resolve(id | "page" | "sel") · h.node(id) · h.var_(idOrKey)
h.hex("#1a2b3c") · h.solid(hex, opacity?) · h.frame(parent, {layout, spacing, padding, fill, radius, name})
h.importComp(key) · h.importVar(key)
```

All are `await`-able where they touch Figma's async API. Using them instead of
inline boilerplate saves ~70% of a script and avoids the classic mistakes —
frozen `node.fills`, missing `loadFontAsync`, auto-layout set in the wrong order:

```js
// Bad — node.fills is frozen, and this silently mutates a copy
const f = JSON.parse(JSON.stringify(node.fills));
f[0] = figma.variables.setBoundVariableForPaint(f[0], "color", v);
node.fills = f;

// Good
await h.bF(node, 0, v);
```

Same story for `await h.setText(node, "new")` instead of `loadFontAsync` +
`node.characters`, and `h.withFonts(root, fn)` before bulk-editing text.

## How exec evaluates code

```js
new Function("figma", "print", "h", `return (async () => { <YOUR CODE> })();`)(figma, print, HELPERS)
```

- `return ...` becomes the `result` field. **Forgot `return`? That's why the
  result looks empty.**
- `await` works everywhere.
- `print(...)` collects log lines (capped at 200).
- Exceptions → `{ok:false, error, hint?, stack, logs}` with HTTP 500. The bridge
  **adds a `hint`** for common errors (paint binding, frozen array, font not
  loaded, missing permission, appendChild order, variant typo). Read it first.

## Conventions

**Async APIs.** Dynamic-page documentAccess makes lookups async:

```js
const node = await figma.getNodeByIdAsync(id)        // or: await h.node(id)
const main = await instance.getMainComponentAsync()
const comp = await figma.importComponentByKeyAsync(key)
```

**Auto-layout: order matters.** `resize()` / spacing / sizing are ignored if set
before `layoutMode`: into the tree → `layoutMode` → size → sizing mode →
spacing/padding. `h.frame` does that for you — prefer it.

**Two-stage for big builds.** Step 1: build the structure with hardcoded RGB and
*named* nodes. Step 2: walk by name and bind via `h.bF`/`h.bS`/`h.bN`. Verify
each step on its own.

**Don't take screenshots for verification.** The bridge returns data — check it:

```js
return (await h.node("...")).width
return root.findAll(n => n.type === "TEXT").map(t => t.characters)
```

`exportAsync({format:"PNG"})` exists for when you genuinely need pixels; it is
not an "is my code working" check.

## When something looks wrong

- **503 `plugin not connected`**: plugin window closed. Ask the user to Run it again.
- **504 timeout**: infinite loop or an unresolved `await` — ask the user to close
  and re-run the plugin. `waited_ms` vs `ran_ms` says whether it was queued or running.
- **409 with a list of files**: several files open, no target. Set the session; never guess.
- **`… permission not specified`**: edit `plugin/manifest.json` here, then ask the
  user to **re-import** (Manage plugins → remove → Import again). A manifest
  change needs a re-import, not just a re-Run.
- **`… truncated: N more characters`**: the result hit the cap. Narrow it
  (`tree --depth`, `find`, fewer fields) rather than re-running the same thing.
- **Switched Figma file → plugin disconnects**: it is bound to the file it started in.

## Local setup

`project.json` holds this copy's name and port — single source of truth for the
bridge and the CLI. Machine-specific notes go in `CLAUDE.local.md` (gitignored).

Figma imports the plugin **from this folder**. After editing `plugin/code.js` or
`plugin/ui.html`, ask the user to re-Run the plugin; a running plugin keeps the
code it started with. After `manifest.json`, they must re-Import.

Updating Figmosha overwrites `plugin/manifest.json` and `plugin/ui.html`, which
carry this project's id and port. `python figmosha.py update` does the whole
ritual — release those two, pull, re-stamp — and refuses if anything else is
uncommitted. By hand it is `git checkout -- plugin/manifest.json plugin/ui.html`,
`git pull`, `python figmosha.py init`, in that order.
