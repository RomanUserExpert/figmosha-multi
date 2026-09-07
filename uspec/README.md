# uspec — run uSpec's component documentation on the Figmosha bridge

**uSpec** (the `uspec-skills` npm package) documents a Figma component: it reads a component set
out of the file, writes a Markdown spec, and renders seven annotation frames — anatomy,
structure, property, color, API, motion, screen reader — back into Figma beside the component.

Out of the box it needs two things this repository is an alternative to: the **uSpec Extract
plugin**, and a **Figma MCP server** (`figma-console` or `figma-mcp`) for every write. Both do
one thing — execute JavaScript inside the open file — which is what Figmosha already does.

This folder is the adapter. It makes `figmosha` a third `mcpProvider`, so the whole pipeline
runs over `POST /exec` with no second plugin and no MCP server.

**Nothing in uSpec is forked.** `npx uspec-skills init` installs the skills unmodified;
`apply-adapter.py` then patches them in place, and re-patches after every upgrade.

---

## What it takes to run

| | |
|---|---|
| Figmosha | this repo, bridge up, plugin running in the target file |
| Node | for `npx uspec-skills` and the extraction bundle build |
| The component | a `COMPONENT` or `COMPONENT_SET` in the open file |
| `uspec/file-keys.json` | file name → Figma file key (see below) |

Everything else — the extraction phases, the seven templates, the render scripts — is in here
or comes from `uspec-skills`.

## Install

```bash
# 1. uSpec's own skills, unmodified, into the workspace that will run them
cd <your workspace>
npx uspec-skills init

# 2. point them at Figmosha
python <figmosha>/uspec/apply-adapter.py --skills .claude/skills

# 3. set the provider
#    uspecs.config.json  ->  "mcpProvider": "figmosha"

# 4. name your files
cp <figmosha>/uspec/file-keys.example.json <figmosha>/uspec/file-keys.json   # then edit
```

The patch is idempotent: a skill already carrying a marker is left alone, and any skill
without a provider table — your own, not uSpec's — is skipped entirely.
`apply-adapter.py --check` verifies a patched tree without changing it and exits non-zero if
anything is unpatched, so it can guard a commit. Run it after every `npx uspec-skills update`:
an upgrade that moves the ground under a substitution shows up as `UNPATCHED` rather than
silently as nothing.

## The run

```bash
# ── extract ──  read-only; nothing is written to Figma
python uspec/extract/extract.py --set-id 86:9283 --out components/_base.json \
       --expect-file "My Design System" --classify referenced

# ── spec ──  uSpec's own CLI, no Figma involved
npx uspec-skills component-md prepare  components/_base.json
#   → invoke the create-component-md skill, which writes components/<slug>.md
npx uspec-skills component-md contract components/<slug>.md   # → components/<slug>.json

# ── render ──  invoke any of the seven skills against the .md; each writes one Figma frame
#   create-anatomy  create-structure  create-property  create-color
#   create-api      create-motion     create-voice
```

The extraction step is the only one that differs in *shape* from stock uSpec. Everything after
it is uSpec's own commands and uSpec's own skills — they simply reach Figma through the bridge.

## The four substitutions

`apply-adapter.py` makes exactly four edits, each anchored on an HTML marker so a re-run is
idempotent and an upstream move is detected rather than silently skipped:

| Marker | Where | What it says |
|---|---|---|
| `figmosha-adapter` | every skill's `## MCP Adapter` | ignore both provider columns; follow `adapter.md` |
| `figmosha-template` | the template-import step | don't `importComponentByKeyAsync`; build the frame from `templates/` |
| `figmosha-render-meta` | every render skill's inputs | there is no `render-meta` block; read the sibling `.json` contract |
| `figmosha-cell-wrap` | the table-row writers | let long cells wrap instead of overflowing the frame |

Read [`adapter.md`](adapter.md) for the first — it is the file the skills are pointed at, and it
maps every row of uSpec's provider table onto a bridge call.

## Why extraction is read-only

uSpec's extraction phases F and G instantiate every variant to measure it, then delete the
instances. Under an MCP that write belongs to the plugin. Under Figmosha it would be **the
agent's write, into the designer's file, without being asked** — which this repo's operating
rule forbids.

`extract/build-bundle.mjs` patches those two phases at build time to measure the variant
in place. What that costs is bounded and declared: nothing at all for a component with no
BOOLEANs and no SLOTs, and for one with hidden boolean-gated parts, only the reflowed geometry
in `crossVariant.axisDiffs`. The output says so itself in `_extractionNotes.warnings`.

There is a `--reveal` flag that does show hidden parts. It **writes to Figma** — but only
temporary instances, which is the line it sits inside: the component itself never changes.
That is enforced, not remembered: the run counts instances before and after and fails if a
single node was left behind or the component set moved.

Details: [`extract/README.md`](extract/README.md).

## Why the templates are captured, not imported

uSpec's render skills import a template component by key from the *uSpec template (Community)*
file. That key resolves only if the template file is a published team library. For an
unpublished file it does not fail — **it hangs and never settles** (measured: 15–24 s to a
timeout, seven for seven).

So the seven templates are read out of the Community file once, node for node, into
`templates/templates.json`, and replayed as a detached frame beside the component at render
time. The skills' own next line detaches the imported instance anyway, so nothing downstream
notices the difference — the `#anchor` layers are all there.

Details: [`templates/README.md`](templates/README.md) and
[`docs/import-by-key.md`](docs/import-by-key.md).

## `file-keys.json`

A development plugin cannot read `figma.fileKey`, and uSpec's `_meta.figmaUrl` needs it. So the
file name the bridge reports is mapped to its key by hand:

```json
{ "My Design System": "AbC123…", "My Icons": "Def456…" }
```

Find the key in the file's URL: `figma.com/design/<KEY>/<name>`. This file is per project and
gitignored; `file-keys.example.json` is the template. `--file-key` overrides it for one run.

## Layout

```
uspec/
  README.md                this file
  adapter.md               the provider spec the patched skills are pointed at
  apply-adapter.py         installs and verifies the four substitutions
  file-keys.example.json   copy to file-keys.json (gitignored)
  extract/
    README.md              the extraction half, its phases and its two traps
    build-bundle.mjs       builds uspec-extract.bundle.js from pinned uSpec source
    entry/figmosha-entry.ts  the phase entry points the bundle exposes
    extract.py             the orchestrator: census → phase E → compose
    validate.mjs           runs uSpec's own validator (its CLI guard never fires on Windows)
  templates/
    README.md              capture, replay, and what the verification covers
    capture.js / .py       read the seven templates out of the Community file (read-only)
    templates.json         the capture — 330 nodes, 147 #anchor layers
    frame.js / .py         build one template as a detached frame, on demand
    replay.js / .py        the other route: build all seven as real components
    verify-all.py          property-by-property diff of a build against the capture
  docs/
    import-by-key.md       why importComponentByKeyAsync hangs, and the three ways out
```

`extract/uspec-extract.bundle.js` is built, not committed — `build-bundle.mjs` regenerates it
from a pinned uSpec commit.

## Status

Proven end to end against a copy of a real design-system library: extraction, spec, contract,
and all seven renders. It has not been run against a production library file — and under this
repo's rule it will not be, without being asked for that specific run.
