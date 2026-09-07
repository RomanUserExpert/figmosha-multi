#!/usr/bin/env python
"""Point every uSpec skill at the Figmosha adapter.

`npx uspec-skills init/update` writes each SKILL.md from the upstream template
and knows only two providers, `figma-console` and `figma-mcp`. This script
injects one marked block per skill telling the agent to follow
`uspec/adapter.md` instead when
`uspecs.config.json` -> mcpProvider is `figmosha`.

Idempotent: a skill that already carries the marker is left alone. Re-run it
after every `npx uspec-skills update`.

    python uspec/apply-adapter.py [--check] [--skills <dir>]

--check reports what would change and exits 1 if anything is unpatched, so it
can guard a commit.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

USPEC = Path(__file__).resolve().parent          # <figmosha copy>/uspec
FIGMOSHA = USPEC.parent                          # <figmosha copy>

# uSpec's skills are installed into the workspace that *uses* Figmosha, not into Figmosha
# itself — `npx uspec-skills init` writes them to <workspace>/.claude/skills. A copy of
# Figmosha normally sits one level inside that workspace, so that is the default guess;
# `--skills <dir>` overrides it for any other layout.
def default_skills_dir() -> Path:
    return FIGMOSHA.parent / ".claude" / "skills"


ADAPTER = "uspec/adapter.md"

MARKER = "<!-- figmosha-adapter -->"

BLOCK = f"""{MARKER}
**This workspace runs a third provider: `figmosha`.** When `uspecs.config.json`
-> `mcpProvider` is `figmosha`, ignore both columns of the adapter table in this
skill and follow **`{ADAPTER}`**. It maps every row of that table onto the
Figmosha bridge, states what `fileKey` means when there is no MCP, and settles
this skill's "never pause before a Figma write" clause against the root
`CLAUDE.md` rule. Read it before the first Figma call of the run.
<!-- /figmosha-adapter -->
"""

# Where to drop the block in each skill: the heading it goes under. The eleven
# render/extract skills all carry "## MCP Adapter"; firstrun keeps its provider
# branches inline and gets the block above its first section instead.
ANCHORS = ["## MCP Adapter", "## Prerequisite"]

# create-component-md makes zero Figma calls (it reads _base.json from disk),
# so it needs no adapter.
SKIP = {"create-component-md"}

# ---------------------------------------------------------------- template step
#
# The second substitution. Every render skill opens by importing a template component and
# detaching it on the next line; under `figmosha` that import hangs, because the template file
# is an unpublished draft and a key from another unpublished file never settles. Since the
# component is discarded immediately anyway, the frame is built from the captured description
# instead. See uspec/templates/README.md.

TEMPLATE_MARKER = "<!-- figmosha-template -->"

# Which captured template each skill renders. Keys are `templates.json` -> templates.
SKILL_TEMPLATE = {
    "create-anatomy": "Anatomy",
    "create-api": "API",
    "create-color": "Color Annotation",
    "create-motion": "Motion",
    "create-property": "Property",
    "create-structure": "Structure",
    "create-voice": "Screen reader",
}

TEMPLATE_HEADING = re.compile(r"^### Step [0-9]+[a-z]?: Import and Detach Template\s*$", re.M)

# ---------------------------------------------------------------- render-meta
#
# The third substitution, and the earliest one a render trips over. Every create-* skill parses
# a `render-meta` block out of the component .md at Step 0 — but `uspec-skills` 0.3.3 does not
# emit one. The same facts live in the sibling .json contract, which is committed next to the
# .md and needs no cache. This block goes under `## Inputs Expected`, which every render skill
# has and which comes before the parse.

META_MARKER = "<!-- figmosha-render-meta -->"

META_ANCHOR = "## Inputs Expected"

META_BLOCK = f"""{META_MARKER}
**There is no `render-meta` block in the `.md`.** `uspec-skills` 0.3.3 does not emit one, so
every step below that says "parse `render-meta`" reads the **sibling `.json` contract** instead
— `components/<slug>.json`, written by `component-md contract` and committed next to the `.md`.
It is self-contained: nothing here needs `.uspec-cache/`, which is gitignored and may not exist.

| What the skill calls it | Where it is in the contract |
|---|---|
| `component.compSetNodeId` (`COMP_SET_ID`) | `sourceModel.component.compSetNodeId` |
| `propertyDefs` (raw keys, e.g. `value#883:147`) | `sourceModel.propertyDefinitions.rawDefs` |
| `booleanDefs[]` | `sourceModel.propertyDefinitions.booleans` |
| `slotContents[]` | `sourceModel.propertyDefinitions.slots`, geometry in `sourceModel.slotHostGeometry` |
| `variantAxes`, `variantAxesDefaults` | `sourceModel.variantAxes`, `sourceModel.defaultVariant` |
| `fileKey`, `nodeId` | `source.fileKey`, `source.nodeId` |
| `sourceHash` | `source.baseSourceHash` |
| `figmaUrl` | `source.figmaUrl` |
| the default variant's layout tree | `sourceModel.defaultTree`, per-variant in `sourceModel.variantTrees` |
| `subComponents[]` | `sourceModel.subComponentVariantWalks` |
| `_childComposition` | `sourceModel.childComposition` |

Two consequences. A `.md` with no contract beside it cannot be rendered — regenerate it with
`component-md contract` rather than guessing the ids. And `sourceModel.extractionNotes.warnings`
is where a read-only extraction declares what it did not reveal; read it before trusting a
dimension that a hidden, boolean-gated part would change.
<!-- /figmosha-render-meta -->
"""


# ---------------------------------------------------------------- wrapping in cloned cells
#
# The fourth substitution, and the only one that fixes an upstream bug rather than routing
# around a missing dependency. Where a skill clones one template cell into N columns it sets the
# CELL to FILL and never touches the TEXT inside it. The template's text is authored
# `textAutoResize: WIDTH_AND_HEIGHT` — it hugs its own content — so a value longer than the new
# column does not wrap: it grows sideways and runs across its neighbours.
#
# Measured on a real Color annotation: a 216 px state column holding
# "surface-container/info/bold/default · #4887FE" produced a 391 px text node, 175 px of it lying
# over the next two columns. 169 cells across eight sections. Structure's value columns share the
# code shape and only escape it because its values are short.

WRAP_MARKER = "<!-- figmosha-cell-wrap -->"

# Where the block goes in each affected skill — the heading that introduces the cloning loop.
WRAP_ANCHOR = {
    "create-color": "### Step 10: Render Variants",
    "create-structure": "#### Step 11b: Render the table",
}

WRAP_BLOCK = f"""{WRAP_MARKER}
**Make the text wrap when you clone a cell into columns.** Every script below that clones one
template cell into N columns sets the clone's `layoutSizingHorizontal = 'FILL'` and then writes
`characters` — but the template's TEXT is authored `textAutoResize: 'WIDTH_AND_HEIGHT'`, so it
hugs its own content and a value wider than the new column overflows sideways across the
neighbouring columns instead of wrapping. Nothing errors; the table just reads as garbled.

After setting the cell to `FILL` and before or after writing `characters`, do this to the TEXT
inside it:

```javascript
const txt = cell.findOne(n => n.type === 'TEXT');
if (txt) {{
  txt.characters = value;
  txt.textAutoResize = 'HEIGHT';              // fixed width, grow downwards — i.e. wrap
  try {{ txt.layoutSizingHorizontal = 'FILL'; }} catch (e) {{}}   // needs an auto-layout cell
}}
```

The font must already be loaded or `textAutoResize` throws — the `loadAllFonts` call each script
already makes covers it. Apply the same to the header cells: their labels are short today, but
nothing guarantees that.

Measured before this was added: a 216 px state column holding
`surface-container/info/bold/default · #4887FE` produced a **391 px** text node, 175 px of it
lying over the next two columns — 169 cells across eight sections of one annotation.
<!-- /figmosha-cell-wrap -->
"""


def patch_cell_wrap(path: Path, skill_name: str) -> str:
    """Insert the wrap rule above the cloning loop of skills that build dynamic columns."""
    anchor = WRAP_ANCHOR.get(skill_name)
    if not anchor:
        return ""

    text = path.read_text(encoding="utf-8")
    if WRAP_MARKER in text:
        return "cell-wrap already patched"

    needle = "\n" + anchor + "\n"
    idx = text.find(needle)
    if idx == -1:
        return ("NO CELL-WRAP ANCHOR — upstream moved %r. Cloned columns will overflow their "
                "cells; re-derive the anchor." % anchor)

    cut = idx + len(needle)
    path.write_text(text[:cut] + "\n" + WRAP_BLOCK + text[cut:], encoding="utf-8")
    return "cell-wrap patched"


def template_block(template_name: str) -> str:
    return f"""{TEMPLATE_MARKER}
**Under `figmosha`, do not run the import below.** `importComponentByKeyAsync` never settles
for a key that belongs to another unpublished file, which is what the uSpec template file is
here — measured, see `uspec/docs/import-by-key.md`. The step's own next line
detaches the instance to a plain frame anyway, so the component is not needed at all.

Build the frame from the captured description instead:

- script: `uspec/templates/frame.js`
- `SPEC`: `uspec/templates/templates.json` -> `templates["{template_name}"]`
- `COMPONENT_NODE_ID`: the same node id this step's script uses
- `FRAME_NAME`: the same name this step's script sets

It returns `{{ frameId, pageId, pageName }}` — the shape this step returns — and the frame
carries the same `#anchor` layers, so **every following step is unchanged**. Skip the
`setCurrentPageAsync` walk-up; `frame.js` appends to the component's own page itself.

All seven templates are verified against the capture, property by property
(`uspec/templates/verify-all.py`). Ignore `templateKeys` and the `firstrun`
template steps entirely under this provider.
<!-- /figmosha-template -->
"""


def patch(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        return "already patched"

    for anchor in ANCHORS:
        needle = "\n" + anchor + "\n"
        idx = text.find(needle)
        if idx == -1:
            continue
        cut = idx + len(needle)
        path.write_text(text[:cut] + "\n" + BLOCK + text[cut:], encoding="utf-8")
        return f"patched under `{anchor}`"

    return "NO ANCHOR — check upstream, this skill changed shape"


def patch_template_step(path: Path, skill_name: str) -> str:
    """Insert the template-build substitution under the skill's import step."""
    template_name = SKILL_TEMPLATE.get(skill_name)
    if not template_name:
        return ""

    text = path.read_text(encoding="utf-8")
    if TEMPLATE_MARKER in text:
        return "template step already patched"

    m = TEMPLATE_HEADING.search(text)
    if not m:
        return ("NO TEMPLATE ANCHOR — upstream renamed the import step. The frame build is NOT "
                "applied; re-derive it before rendering, or every render will hang on the import.")

    cut = m.end()
    path.write_text(text[:cut] + "\n\n" + template_block(template_name) + text[cut:], encoding="utf-8")
    return f"template step patched -> {template_name}"


def patch_render_meta(path: Path, skill_name: str) -> str:
    """Point the skill's render-meta reads at the sibling .json contract."""
    if skill_name not in SKILL_TEMPLATE:
        return ""

    text = path.read_text(encoding="utf-8")
    if META_MARKER in text:
        return "render-meta already patched"

    idx = text.find(f"\n{META_ANCHOR}\n")
    if idx == -1:
        return ("NO INPUTS ANCHOR — upstream moved `## Inputs Expected`. The render-meta "
                "substitution is NOT applied, and this skill will look for a block the .md "
                "does not contain.")

    cut = idx + len(f"\n{META_ANCHOR}\n")
    path.write_text(text[:cut] + "\n" + META_BLOCK + text[cut:], encoding="utf-8")
    return "render-meta patched"


def main() -> int:
    argv = sys.argv[1:]
    check = "--check" in argv

    skills = default_skills_dir()
    if "--skills" in argv:
        i = argv.index("--skills")
        if i + 1 >= len(argv):
            print("--skills needs a directory")
            return 1
        skills = Path(argv[i + 1]).resolve()

    if not skills.is_dir():
        print(f"no skills directory at {skills}")
        print("Point at the workspace that has uSpec installed:  --skills <path>/.claude/skills")
        return 1
    if not (FIGMOSHA / ADAPTER).is_file():
        print(f"adapter doc missing: {FIGMOSHA / ADAPTER}")
        return 1

    unpatched = 0
    for skill in sorted(skills.iterdir()):
        f = skill / "SKILL.md"
        if not f.is_file() or skill.name in SKIP:
            continue
        # Only uSpec's own skills carry a provider table; this workspace's
        # spec-* skills drive Figmosha directly and are none of our business.
        if MARKER not in f.read_text(encoding="utf-8") and not any(
            a in f.read_text(encoding="utf-8") for a in ANCHORS
        ):
            continue

        if check:
            body = f.read_text(encoding="utf-8")
            missing = []
            if MARKER not in body:
                missing.append("adapter")
            if skill.name in SKILL_TEMPLATE and TEMPLATE_MARKER not in body:
                missing.append("template-step")
            if skill.name in SKILL_TEMPLATE and META_MARKER not in body:
                missing.append("render-meta")
            if skill.name in WRAP_ANCHOR and WRAP_MARKER not in body:
                missing.append("cell-wrap")
            if missing:
                unpatched += 1
            print(f"  {'UNPATCHED' if missing else 'ok':<10} {skill.name}"
                  + (f"  (missing: {', '.join(missing)})" if missing else ""))
        else:
            notes = [patch(f)]
            for fn in (patch_template_step, patch_render_meta, patch_cell_wrap):
                r = fn(f, skill.name)
                if r:
                    notes.append(r)
            print(f"  {skill.name}: {'; '.join(notes)}")

    if check and unpatched:
        print(f"\n{unpatched} skill(s) unpatched — run this script without --check")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
