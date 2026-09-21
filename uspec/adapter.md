# uSpec on the Figmosha bridge

A third `mcpProvider` for this workspace: **`figmosha`**. It replaces both MCP
columns in every uSpec skill's `## MCP Adapter` table. Read this file whenever
`uspecs.config.json` → `mcpProvider` is `figmosha`, before the first Figma call
of the run.

Why it works: the only operation uSpec's render skills genuinely need is
*"execute plugin JS in the open file"*, and the skills already state that the JS
is identical across providers. Figmosha does exactly that. Everything else in
the table is either a convenience wrapper around the same JS or a CLI
subcommand Figmosha already ships.

The transport is documented in `Figmosha\CLAUDE.md` — read it too if a call
fails. Commands below run from your Figmosha copy; the shim
(`./figmosha …`, PowerShell `.\figmosha …`) finds the project venv itself.

---

## Before the first call

1. `curl -s http://localhost:<port>/status` → `{"plugin_connected": true}`.
   The port is this Figmosha copy's, written in `project.json`.
   Bridge down: `./start-bridge.sh` (PowerShell `.\start-bridge.ps1`).
   Plugin not connected: ask the user — Plugins → Development → `Figmosha Bridge` → Run.
2. `./figmosha exec "return figma.root.name + ' / ' + figma.currentPage.name"` —
   **which file is actually open.** Everything below happens in that file and
   nowhere else.
3. `./figmosha sessions` if more than one file may be connected.

## Operation map

Replaces the `figma-console` / `figma-mcp` columns row for row.

| Operation (uSpec's row) | Under `figmosha` |
|---|---|
| Verify connection | `curl -s http://localhost:<port>/status`, then step 2 above. `./figmosha doctor` names the broken link when it fails. |
| Navigate to file | **Not available.** The plugin is bound to the file it was run in and does not follow a URL. Verify the open file by name; if it is the wrong one, ask the user to open the right file and Run the plugin there. |
| Execute Plugin JS | `./figmosha exec --file <script.js>` — **JS is identical to both MCP columns, no wrapper changes.** `return …` is the result, `print(…)` collects logs, `await` works everywhere, and `h.*` helpers are in scope. Pass values in with `--set NAME=value` (emitted as a `const` before the code). |
| Take screenshot | `exportAsync({format:"PNG", constraint:{type:"SCALE", value:2}})` inside an exec, base64 out, decoded straight to disk — never through the conversation. Skills use screenshots only for visual confirmation; prefer reading the value back instead. |
| Search components | Open file only: `./figmosha find <root-id> type=COMPONENT_SET` / `name~Foo`, or an exec over `figma.root.findAll(…)` — which needs
`await figma.loadAllPagesAsync()` first under `documentAccess: dynamic-page`, and
brings the whole document into memory with it. On a large file prefer `h.pages()`
plus `h.loadPageOf(page)` one page at a time, or `figmosha each`. **There is no cross-library component search** — the library must be the open session. See *firstrun* below. |
| Get file / component data | `./figmosha tree <id> [--depth N] [--layout]`, `./figmosha props <id> [-c]`, `./figmosha where <id>`, `./figmosha overrides <id>`. |
| Get variables (file-wide) | `./figmosha vars [filter] [--type T]`, `--library` for collections this file consumes. |
| Get token values | Same command — `vars` resolves per mode. |
| Get styles | `./figmosha styles [filter]`. |
| Get selection | `./figmosha sel`. |

## What changes in the skills' assumptions

**`fileKey` is meaningless here.** Figmosha addresses nodes by id inside the one
open document. A `.md`'s `render-meta` block still carries `fileKey` + `nodeId`
from extraction — treat `fileKey` as a *claim about which file must be open*,
not as a call argument, and treat `nodeId` as valid only while that file is the
open session. A node id from another file resolves to `null`, silently.

**Ignore the `figma-mcp` page-context block.** `use_figma` resets
`figma.currentPage` on every call; Figmosha does not — the plugin inherits the
Desktop page context, exactly like `figma_execute`. Do not paste the
`setCurrentPageAsync` walk-up unless a script genuinely reaches across pages,
and in that case `await figma.loadAllPagesAsync()` first and address everything
by id (`h.sel()` and the `page` alias are off-limits in a cross-page script).

**Long scripts go in a file, not on the command line.** The render skills emit
hundreds of lines of JS. Write it to the session scratchpad and use
`exec --file`; a script passed as a shell argument will hit Windows' argument
limit or get mangled by quoting.

**The 64 KB cap is on the display text, not on the data.** `truncate` is applied
only inside `asText` (`Figmosha\plugin\code.js` L40, L53–71), which builds the
human-readable `text` field and the 200-line `print` log. The structured `value`
is serialised untruncated and travels under the bridge's 16 MiB ceiling per
message (`bridge.py` L360, L658), with the exec timeout clamped to 1–600 s
(L52, L213). A single call has carried **1.78 MB** of extraction output.

So: a large *dump* is fine, a large *printout* is not. Post to `/exec` with
`want_value: true` and read `value`; the CLI defaults to `want_value: false`
(`figmosha.py` L190–201) and `--raw` would push megabytes through stdout. Narrow
the query when the answer is meant to be read, batch it when it is meant to be
stored.

**Scripts in one file are serialised.** A long render blocks the next call
rather than interleaving with it. A 504 reports `waited_ms` vs `ran_ms`.

**With two or more files connected, a bare call returns 409 and lists them.**
Never pick one yourself — set `FIGMOSHA_SESSION` once for the thread
(PowerShell: `$env:FIGMOSHA_SESSION = "<file name>"`) or pass
`--session <name>` per call.

## Writing to Figma — this workspace's rule wins

Every `create-*` skill's execution contract says never to pause before a Figma
write. The root `CLAUDE.md` says nothing may change in Figma without an explicit
go-ahead for that specific run. Resolution:

- **Invoking a `create-*` skill by name is the go-ahead for that one render.**
  The user asked for an annotation frame; drawing it is the deliverable, and the
  skill runs to completion without asking.
- **It authorises nothing outside the frame it creates.** Editing the documented
  component, its variants, its bindings, or anything else on the page is a
  separate run and needs a separate yes.
- **The extract half writes nothing.** `create-component-md` and the four
  `extract-*` skills read `_base.json` from disk and make zero Figma calls;
  their optional "Step 3-delta escape hatch" is a read.

There is no undo. Figma's history is unreachable from a plugin, so note the
frame id the render returns — deleting it is the rollback.

## Extraction runs through the bridge too

`_base.json` no longer needs the **uSpec Extract** plugin. `uspec/extract/`
runs uSpec's own phase code — bundled from a pinned commit — as exec scripts, and assembles the
file on disk exactly as the plugin's `code.ts` assembles it:

```bash
python uspec/extract/extract.py --set-id <component-set-id> \
    --expect-file "<your Figma file>" --out <slug>-_base.json
```

A 160-variant set of 1 089 nodes takes three calls and 16 s; a 720-variant one
takes ten calls and 162 s with `--batch 100`. Pass the result as `baseJsonPath`.

**It is read-only by default.** Phases F and G are uSpec's only Figma writers — they measure
temporary instances. The bundle is built with those patched to measure the variant nodes
instead, so a default extraction changes nothing.

`--reveal` restores the plugin's behaviour, and **does not ask per run**. The designer's rule,
2026-09-07: *the component itself must never change; on a temporary instance anything goes.*
Phases F and G only ever touch instances they created, so they sit inside it. Every reveal run
then proves it: node counts on the current page, the component's page and the component set are
compared before and after, and every `createInstance` must have a matching `remove`. A run that
leaves anything behind fails and writes no file.

This is the one carve-out. Everything else in this workspace still needs an explicit go-ahead
for that specific run — including anything that adds a node meant to stay, such as a rendered
annotation frame.

What read-only costs, measured: **nothing** for a component with no BOOLEANs and no SLOTs, and
for one with hidden boolean-gated parts, only the reflowed geometry in `crossVariant.axisDiffs`
— up to 48 px on the component tested. The file says so itself in
`_extractionNotes.warnings`. See `uspec/extract/README.md` -> **Read-only, and what it costs**.

## firstrun under `figmosha`

- **Step 1b (verify connection)** — the two checks at the top of this file.
- **Steps 4 through 7b** — the template half. **Skipped entirely**: nothing under this provider
  reads `templateKeys`, so there is no library to point at and no keys to collect. See the next
  section. `fontFamily` is `Inter`, read off the captured templates.
- **Steps 7, 8, 9** are file edits on disk — unchanged.

## There is no `render-meta` block — read the contract instead

Every `create-*` skill opens by parsing a `render-meta` block out of the component `.md`.
**`uspec-skills` 0.3.3 does not emit one.** The same facts are in the sibling `.json` contract,
which `component-md contract` writes next to the `.md` and which is committed — no
`.uspec-cache/` needed, and that folder is gitignored anyway.

| What the skill calls it | Where it is in the contract |
|---|---|
| `component.compSetNodeId` (`COMP_SET_ID`) | `sourceModel.component.compSetNodeId` |
| `propertyDefs` (raw keys, e.g. `value#883:147`) | `sourceModel.propertyDefinitions.rawDefs` |
| `booleanDefs[]` | `sourceModel.propertyDefinitions.booleans` |
| `slotContents[]` | `sourceModel.propertyDefinitions.slots`, geometry in `sourceModel.slotHostGeometry` |
| `variantAxes`, `variantAxesDefaults` | `sourceModel.variantAxes`, `sourceModel.defaultVariant` |
| `fileKey`, `nodeId`, `sourceHash`, `figmaUrl` | `source.*` |
| the default variant's layout tree | `sourceModel.defaultTree`, per-variant in `sourceModel.variantTrees` |
| `subComponents[]` | `sourceModel.subComponentVariantWalks` |
| `_childComposition` | `sourceModel.childComposition` |

`apply-figmosha-adapter.py` injects this table into each render skill under `## Inputs
Expected`, which sits before the parse — the adapter itself is read at the connection check,
which is too late.

A `.md` with no contract beside it cannot be rendered: regenerate it rather than guessing ids.
And `sourceModel.extractionNotes.warnings` is where a read-only extraction declares what it did
not reveal — read it before trusting a dimension a hidden boolean-gated part would change.

## Templates: `firstrun`'s template half does not apply here

**Skip it.** Under this provider the render skills do not import a template at all, so
`templateKeys`, the library link, and `firstrun` steps 4–7b are all moot. The seven templates
are captured into `uspec/templates/templates.json` and built on demand.

Why: every `create-*` skill's "Import and Detach Template" step imports a component and
**detaches it on the next line** — the component is discarded immediately and everything
downstream works on a plain frame, finding its parts by layer name. And the import cannot work
here anyway. Measured behaviour of `importComponentByKeyAsync`, three kinds and three outcomes:

| Key | Behaviour |
|---|---|
| Published in a reachable team library | resolves fast (907 ms, 4 ms) |
| Unpublished, belongs to the **open** file | rejects fast (264 ms) |
| Unpublished, belongs to **another** file | **never settles** — all seven template keys, 15–24 s |

A Community template file duplicated into drafts is the third kind. Full write-up in
`uspec/docs/import-by-key.md`.

`apply-figmosha-adapter.py` injects the substitution under each import step: build with
`uspec/templates/frame.js`, passing `SPEC` from `templates.json`, the component's
node id and the frame name. It returns `{ frameId, pageId, pageName }` — the shape the step it
replaces returns — and the frame carries the same `#anchor` layers, so no other step changes.
All seven are verified against the capture property by property (`templates/verify-all.py`:
7 of 7, 330 nodes, 0 differences).

`h.importComp(key)` and `./figmosha icomp <key>` still exist and work for a **published**
library component. They are simply not how templates are obtained here.

## When something looks wrong

| Symptom | Fix |
|---|---|
| `503 plugin not connected` | The plugin window was closed. Ask the user to Run it again. |
| `504 timeout` | Infinite loop or an unresolved `await`. Ask the user to close and re-run the plugin. |
| `409` with a list of files | Several files open. Set the session; never guess. |
| `… permission not specified` | Edit `Figmosha\plugin\manifest.json`, then the user must **re-import** the plugin, not just re-Run it. |
| `… truncated: N more characters` | The 64 KB cap. Narrow the query. |
| A node id resolves to `null` | Almost always the wrong file is open. Check `figma.root.name` before blaming the id. |

---

**Re-applying after an upgrade.** `npx uspec-skills update` overwrites every
`SKILL.md` and drops the pointer to this file. Run
`python uspec/apply-adapter.py` afterwards — it is
idempotent and reports what it touched.
