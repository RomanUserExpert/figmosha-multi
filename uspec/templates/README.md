# templates — the uSpec templates, captured once and built on demand

`firstrun` wants seven template components in Figma, and every `create-*` skill opens by
importing one. Neither route the plan offered works here: import by key **hangs** for a key
belonging to another unpublished file (measured — `../docs/import-by-key.md`), and pasting the
templates by hand is per-file manual work that leaves no trace in git.

This folder replaces both, and it turns out to replace `firstrun`'s template half as well.

## The load-bearing observation

Every `create-*` skill's "Import and Detach Template" step is three lines:

```javascript
const templateComponent = await figma.importComponentByKeyAsync(TEMPLATE_KEY);
const instance = templateComponent.createInstance();
const frame = instance.detachInstance();
```

It imports a component, instantiates it, and **throws the component away on the next line**.
Everything downstream works on the plain frame and finds its parts by layer name. So the
template never has to exist as a component in the file at all — building the same node tree
from a captured description produces the identical frame with the identical `#anchor` layers.

That removes the import that hangs, the `templateKeys` map, the per-file setup, and the
template half of `firstrun`. There is nothing to keep in sync because nothing persists.

## Use

```bash
# once, from the file that holds the templates — read-only
python capture.py --session "uSpec template (Community)"

# per render: build one template beside the component being documented
python frame.py --template API --component <component-set-id> \
    --expect-file "<your Figma file>" --dry-run
```

In a real render the skill injects `frame.js` in place of its import step and uses the returned
`frameId` exactly as before — one substituted step per skill, the same shape the adapter
already uses elsewhere, and steps 9 onward are untouched.

| File | What it is |
|---|---|
| `capture.js` / `capture.py` | The read. Serialises the seven templates and asserts the portability invariants; refuses if one breaks. |
| `templates.json` | The captured description. **330 nodes, 147 `#anchor` layers.** What git diffs when the templates change upstream. |
| `frame.js` | **The substitution.** Builds one template as a detached FRAME next to a component. Returns `{ frameId, pageId, pageName }` — the same shape the original step returns. |
| `frame.py` | Standalone driver for `frame.js`: dry-run, build, then read back and diff. |
| `replay.js` / `replay.py` | The other route: build all seven as real components on their own page. Only useful if someone wants them editable in Figma or published as a library. Not needed for rendering. |

## Why this is not the trap it looks like

Rebuilding the templates by hand in JS is a trap — but that warning is about *authoring*
them: a human maintaining a hand-written copy of someone else's file,
re-deriving layer assumptions that `npx uspec-skills update` rewrites its scripts against on
every upgrade.

Capture-and-build is a different operation:

- The description is **read off the real component by machine**. Upstream changes mean re-running
  `capture.py` and reading a diff, not editing anything.
- The built frame carries **real `#anchor` layers**, so every downstream step finds what it
  expects and an upgrade that rewrites those steps is harmless.
- Only one step per skill is substituted — the one whose entire output is a `frameId`.

## What made it possible

Measured before any of this was written: the templates are **330 nodes of COMPONENT / FRAME /
TEXT / VECTOR only**, with **zero bound variables, zero styles, zero images, zero nested
instances, zero effects and zero component properties**. Nothing in them belongs to the file
they live in. `capture.js` asserts every one of those and **refuses to capture** if a future
template version introduces one — at that point a build would silently produce a different
frame, and silence is the failure worth engineering against.

Fonts are the one external dependency: **Inter Regular, Medium and Bold** must be available in
the target file. `frame.js` fails loudly rather than substituting.

## Verification

Building writes, so `--yes-write` is mandatory. Afterwards the frame is **read back out of
Figma and diffed against `templates.json`** property by property — names, types, layout,
padding, radii, per-side stroke weights, text, fonts, and fills compared whole. A build that
does not match exits non-zero. Rollback is deleting the frame, whose id is printed.

Three properties are excluded from the diff, because comparing them would compare Figma to
itself rather than check the work: `strokeWeight` where the per-side weights carry the truth,
`x`/`y` because the frame is placed beside the component rather than at its source coordinates,
and `width`/`height` on any axis whose sizing is not `FIXED`, since a HUG frame is whatever its
content makes it.

## Known fidelity gaps

Reported on every capture, never swallowed:

- **`strokeCap` on 6 VECTOR nodes** — the hierarchy-indicator connectors in the API, Color
  Annotation and Structure tables, and two arrows. Their per-segment line caps differ and
  `vectorPaths` does not carry them. Cosmetic: line-end shape on decorative connectors.

Handled rather than lost: three Motion frames (`#ruler-track`, `#tick`, `#track-area`) read
`strokeWeight: mixed` because they rule one edge only. The four per-side weights are captured,
so they build exactly — all three are left-edge rules of 2, 1 and 2 px.

## Status

**All seven verified, 2026-09-07.** Each was built beside `<slug>` in the sandbox fork,
read back out of Figma, diffed property by property and removed again: **7 of 7, 330 nodes,
0 differences**, and the page ended at the node count it started with. `verify-all.py` is that
acceptance test; re-run it after any change to `frame.js` or a re-capture.

Getting there took three failed runs, and each failure was the kind that looks like success:

- **`resize()` writes both axes**, so calling it after declaring a `HUG` axis silently converts
  that axis to `FIXED`. The captured size has to land *before* the sizing modes.
- **Swallowed layout failures are indistinguishable from success.** The first version caught and
  discarded every property-assignment error, so a rejected `layoutSizingVertical` produced a
  frame that read correct in the tree and laid out wrong. Layout properties now report their
  failures by name, value and message.
- **Padding survives auto-layout being switched off.** Two Motion frames (`#ruler-track`,
  `#track-area`) carry padding 12/8/12/8 with `layoutMode: NONE`. It is visually inert, but it
  is in the capture, so it is applied regardless of layout mode and the round-trip is exact.
- **`layoutAlign` and `layoutSizing*` can contradict each other.** Three nodes report
  `layoutAlign: STRETCH` together with `layoutSizingHorizontal: FIXED`. Figma resolves that in
  favour of the sizing and normalises `layoutAlign` to `INHERIT`, so the original and the
  rebuild lay out identically and only the vestige differs. Forcing `STRETCH` back would change
  the real layout — so `layoutAlign` is excluded from the diff wherever both sizing axes are
  present, and that is a fourth "comparing Figma to itself" exclusion, not a silenced bug.

None of these was visible from the node count or the anchor count, which matched exactly on the
failed runs too. They were only visible by reading the frames back out and comparing.

Two runs also died mid-way on *"Unable to establish connection to Figma after 10 seconds"* —
transient, with the bridge answering a trivial call immediately afterwards and the file left
clean both times. `verify-all.py` now pauses a second between templates and retries a call once.
