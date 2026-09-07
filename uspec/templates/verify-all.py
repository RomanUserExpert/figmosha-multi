#!/usr/bin/env python3
"""Build every captured template in turn, diff it against the capture, and remove it again.

    python verify-all.py --component <component-set-id> \
        --expect-file "<your Figma file>" --yes-write

This is the acceptance test for the on-demand route: it proves `frame.js` reproduces all seven
templates, not just the one that was tried first. Each template is built beside the given
component, read back out of Figma, compared property by property, then deleted — so the file
ends where it started whether or not the run passes.

WRITES to Figma (~330 nodes across the run, each removed immediately after its check), so
`--yes-write` is mandatory and means the designer approved this specific run. Anything left
behind is reported at the end rather than assumed gone.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from replay import post as _post, diff
from frame import READ_BACK

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


def post(port, code, consts, session, timeout, _tries=2):
    """`post` with one retry.

    Twenty-odd build/read/remove calls in a row occasionally draw "Unable to establish
    connection to Figma after 10 seconds" out of the plugin — transient, and the bridge answers
    a trivial call immediately afterwards. Retrying once with a pause is enough; anything that
    fails twice is a real failure and is left to fail.
    """
    import io
    import contextlib

    for attempt in range(_tries):
        if attempt:
            time.sleep(3)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stderr(buf):
                return _post(port, code, consts, session, timeout)
        except SystemExit:
            sys.stderr.write(buf.getvalue())
            if attempt == _tries - 1:
                raise
            print("       transient bridge error — retrying once after 3 s")
    raise SystemExit(1)

REMOVE = """
const n = await figma.getNodeByIdAsync(FRAME_ID);
if (!n) return { removed: false, reason: "already gone" };
const c = n.findAll(() => true).length + 1;
n.remove();
return { removed: !(await figma.getNodeByIdAsync(FRAME_ID)), nodesRemoved: c };
"""

SWEEP = """
if (figma.root.name !== EXPECT_FILE) throw new Error("wrong file: " + figma.root.name);
const c = await figma.getNodeByIdAsync(COMPONENT_NODE_ID);
let p = c; while (p.parent && p.parent.type !== "DOCUMENT") p = p.parent;
return { page: p.name, pageNodes: p.findAll(() => true).length, topLevel: p.children.length,
  leftovers: p.children.filter((x) => LEFTOVER_NAMES.indexOf(x.name) !== -1).map((x) => x.name) };
"""


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--component", required=True, help="node id the frames are placed beside")
    p.add_argument("--expect-file", required=True)
    p.add_argument("--session")
    p.add_argument("--port", type=int, default=default_port(),
                   help="bridge port (default: this copy's project.json)")
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--keep", action="store_true", help="do not delete the frames (for eyeballing)")
    p.add_argument("--yes-write", action="store_true",
                   help="required: confirms a go-ahead for this specific run")
    args = p.parse_args()

    if not args.yes_write:
        sys.exit(
            "verify-all builds every template in the open Figma file, ~330 nodes in total.\n"
            "       This workspace requires an explicit go-ahead for that specific run."
        )

    src = json.loads((HERE / "templates.json").read_text(encoding="utf-8"))
    frame_js = (HERE / "frame.js").read_text(encoding="utf-8")

    count = lambda o: 1 + sum(count(c) for c in o.get("children", []))
    anchors = lambda o: (1 if o["name"].startswith("#") else 0) + sum(anchors(c) for c in o.get("children", []))

    started = time.time()
    names, results, built_ids = list(src["templates"]), [], []

    for i, name in enumerate(names):
        if i:
            time.sleep(1.0)  # let Figma breathe between builds
        spec = src["templates"][name]
        frame_name = "VERIFY %s" % name
        v = post(args.port, frame_js,
                 {"SPEC": spec, "COMPONENT_NODE_ID": args.component,
                  "FRAME_NAME": frame_name, "GAP": 200},
                 args.session, args.timeout)
        built_ids.append((frame_name, v["frameId"]))

        failures = v.get("layoutFailures") or []
        got = post(args.port, READ_BACK, {"FRAME_ID": v["frameId"]}, args.session, args.timeout)

        want = dict(spec)
        want["name"] = got.get("name")
        want["type"] = "FRAME"
        out: list = []
        diff(want, got, name, out)

        ok = not out and not failures
        results.append((name, ok, out, failures, v))
        print("  %-18s %3d nodes %3d anchors (capture %3d/%3d)  %s"
              % (name, v["nodesCreated"], v["anchorsPresent"], count(spec), anchors(spec),
                 "OK" if ok else "%d diff, %d rejected" % (len(out), len(failures))))
        for path, prop, wv, gv in out[:8]:
            print("       %-46s %-22s want %r got %r" % (path[-46:], prop, wv, gv))
        if len(out) > 8:
            print("       ... %d more" % (len(out) - 8))
        for f in failures[:6]:
            print("       rejected %-22s on %-20s = %r" % (f["property"], f["node"][:20], f["value"]))

        if not args.keep:
            r = post(args.port, REMOVE, {"FRAME_ID": v["frameId"]}, args.session, args.timeout)
            if not r.get("removed"):
                print("       WARNING: frame %s was not removed (%s)" % (v["frameId"], r.get("reason")))

    passed = sum(1 for _, ok, _, _, _ in results if ok)
    total_nodes = sum(v["nodesCreated"] for _, _, _, _, v in results)
    print("\n%s — %d of %d templates, %d nodes built and %s"
          % ("ALL VERIFIED" if passed == len(names) else "MISMATCH",
             passed, len(names), total_nodes, "kept" if args.keep else "removed"))
    print("took %.1f s" % (time.time() - started))

    sweep = post(args.port, SWEEP,
                 {"EXPECT_FILE": args.expect_file, "COMPONENT_NODE_ID": args.component,
                  "LEFTOVER_NAMES": [n for n, _ in built_ids]},
                 args.session, args.timeout)
    print("page %r: %d nodes, %d top-level, leftovers: %s"
          % (sweep["page"], sweep["pageNodes"], sweep["topLevel"], sweep["leftovers"] or "none"))
    if sweep["leftovers"] and not args.keep:
        print("Frames still present — remove them: %s" % ", ".join(i for _, i in built_ids))
        sys.exit(1)
    if passed != len(names):
        sys.exit(1)


if __name__ == "__main__":
    main()
