#!/usr/bin/env python3
"""Build one uSpec template as a detached frame next to a component — the on-demand route.

    python frame.py --template API --component <component-set-id> \
        --expect-file "<your Figma file>" --dry-run

This is the standalone driver for `frame.js`, used to test the substitution before it is wired
into the `create-*` skills. In a real render the skill injects `frame.js` itself, in place of
its "Import and Detach Template" step, and uses the returned `frameId` exactly as before.

Building WRITES to Figma, so `--yes-write` is mandatory and means the designer approved that
specific run. Afterwards the frame is read back and diffed against the captured description —
the same verification `replay.py` does.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from replay import post, diff  # same transport and the same property-by-property comparison

def default_port() -> int:
    """This copy's port, from project.json — the same number the bridge binds."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        import project
        cfg = project.load()
        if cfg:
            return int(cfg["port"])
    except Exception:
        pass
    return 8787


HERE = Path(__file__).resolve().parent

READ_BACK = """
const _f = await figma.getNodeByIdAsync(FRAME_ID);
const _val = (v) => (v === figma.mixed ? "__MIXED__" : v);
const READ = ["visible","opacity","blendMode","layoutMode","layoutWrap","layoutAlign","layoutGrow",
  "layoutPositioning","primaryAxisAlignItems","counterAxisAlignItems","layoutSizingHorizontal",
  "layoutSizingVertical","itemSpacing","counterAxisSpacing","paddingTop","paddingRight",
  "paddingBottom","paddingLeft","topLeftRadius","topRightRadius","bottomLeftRadius",
  "bottomRightRadius","strokeAlign","strokeTopWeight","strokeRightWeight","strokeBottomWeight",
  "strokeLeftWeight","characters","fontSize","textAlignHorizontal","textAlignVertical",
  "textAutoResize","textCase","textDecoration","clipsContent"];
function readBack(n) {
  const o = { name: n.name, type: n.type, width: n.width, height: n.height };
  for (const p of READ) { const v = n[p]; if (v !== undefined && v !== null) o[p] = _val(v); }
  if (n.type === "TEXT" && n.fontName && n.fontName !== figma.mixed)
    o.fontName = { family: n.fontName.family, style: n.fontName.style };
  if (Array.isArray(n.fills)) o.fills = JSON.parse(JSON.stringify(n.fills));
  if (n.children) o.children = n.children.map(readBack);
  return o;
}
return readBack(_f);
"""


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--template", required=True,
                   help="Screen reader | Color Annotation | Anatomy | API | Property | Structure | Motion")
    p.add_argument("--component", required=True, help="node id of the component being documented")
    p.add_argument("--expect-file", required=True)
    p.add_argument("--session")
    p.add_argument("--frame-name", help="defaults to '<component name> <template>'")
    p.add_argument("--gap", type=int, default=200)
    p.add_argument("--port", type=int, default=default_port(),
                   help="bridge port (default: this copy's project.json)")
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes-write", action="store_true",
                   help="required to build: confirms a go-ahead for this specific run")
    args = p.parse_args()

    src = json.loads((HERE / "templates.json").read_text(encoding="utf-8"))
    if args.template not in src["templates"]:
        sys.exit("unknown template %r. Have: %s" % (args.template, ", ".join(src["templates"])))
    spec = src["templates"][args.template]

    count = lambda o: 1 + sum(count(c) for c in o.get("children", []))
    anchors = lambda o: (1 if o["name"].startswith("#") else 0) + sum(anchors(c) for c in o.get("children", []))

    if args.dry_run or not args.yes_write:
        probe = (
            'const c = await figma.getNodeByIdAsync(COMPONENT_NODE_ID);\n'
            'if (figma.root.name !== EXPECT_FILE) throw new Error("wrong file: " + figma.root.name);\n'
            'if (!c) throw new Error("component not found: " + COMPONENT_NODE_ID);\n'
            'let p = c; while (p.parent && p.parent.type !== "DOCUMENT") p = p.parent;\n'
            'return { file: figma.root.name, component: c.name, page: p.name,\n'
            '  placeAt: { x: c.x + c.width + GAP, y: c.y } };\n'
        )
        v = post(args.port, probe,
                 {"COMPONENT_NODE_ID": args.component, "EXPECT_FILE": args.expect_file, "GAP": args.gap},
                 args.session, args.timeout)
        print("DRY RUN — nothing written to %s" % v["file"])
        print("  template : %s — %d nodes, %d #anchor layers" % (args.template, count(spec), anchors(spec)))
        print("  component: %s on page %r" % (v["component"], v["page"]))
        print("  frame at : x=%.0f y=%.0f" % (v["placeAt"]["x"], v["placeAt"]["y"]))
        if not args.dry_run:
            print("\n  Refusing to build without --yes-write.")
            sys.exit(1)
        return

    consts = {
        "SPEC": spec,
        "COMPONENT_NODE_ID": args.component,
        "FRAME_NAME": args.frame_name or ("%s %s" % (args.component, args.template)),
        "GAP": args.gap,
    }
    t0 = time.time()
    v = post(args.port, (HERE / "frame.js").read_text(encoding="utf-8"), consts, args.session, args.timeout)
    print("built %r on page %r in %.1f s" % (v["frameId"], v["pageName"], time.time() - t0))
    print("  %d nodes, %d #anchor layers (captured: %d / %d)"
          % (v["nodesCreated"], v["anchorsPresent"], count(spec), anchors(spec)))

    for f in v.get("layoutFailures") or []:
        print("  layout property rejected: %-24s %-22s = %-10r  %s"
              % (f["node"][:24], f["property"], f["value"], f["message"]))

    got = post(args.port, READ_BACK, {"FRAME_ID": v["frameId"]}, args.session, args.timeout)

    # The root's name is the frame name the render wants, not the template's; compare the rest.
    want = dict(spec)
    want["name"] = got.get("name")
    want["type"] = "FRAME"

    out: list = []
    diff(want, got, args.template, out)
    print("\nverifying — read back and diffed against templates.json")
    for path, prop, wv, gv in out[:20]:
        print("    %-52s %-22s want %r got %r" % (path[-52:], prop, wv, gv))
    if len(out) > 20:
        print("    ... %d more" % (len(out) - 20))
    print("\n%s — %d difference(s)" % ("VERIFIED" if not out else "MISMATCH", len(out)))
    print("Rollback: delete frame %s." % v["frameId"])
    if out:
        sys.exit(1)


if __name__ == "__main__":
    main()
