#!/usr/bin/env python3
"""Figmosha bridge: HTTP -> WS -> Figma plugin -> back.

HTTP API (clients like curl / figmosha CLI talk here):
    POST /exec     {code, timeout?, session?, want_value?} -> {ok, result, value, logs, elapsed_ms}
    GET  /status                                  -> {plugin_connected, pending, project, sessions}
    GET  /sessions[?session=X]                    -> one row per connected Figma file,
                                                     plus which of them X matches

WebSocket (the Figma plugin connects here once it's opened in Figma Desktop):
    WS   /plugin

One bridge serves one project, and within it one session per open Figma
document. Sessions are independent: a file closing fails only its own requests.

Run:
    python bridge.py                 # port from project.json
    python bridge.py --port 9000
    python bridge.py --host 0.0.0.0  # expose on LAN (not recommended)
"""

import argparse
import asyncio
import json
import sys
import time
import uuid
from aiohttp import web, WSMsgType

import project


# One entry per Figma document served by this bridge, keyed by the session id
# the plugin introduces itself with. A document is the unit that matters: it has
# its own socket, its own in-flight requests, and its own failure — one file
# closing must not disturb another.
SESSIONS: dict = {}       # sid -> Session

# How long to wait for a plugin to say who it is before assuming it is a legacy
# one. Short enough not to be felt, long enough for a hello that is already in
# flight when the socket opens.
HELLO_WAIT = 0.3

# Session id used by plugins that predate the registry. Fixed rather than
# generated so that an old plugin reconnecting lands on the same session and
# keeps exactly the single-slot behaviour it was written against.
ANON_SID = "anon-1"

# Bounds for a caller-supplied timeout. The upper one is what keeps a typo
# (`"timeout": 99999`) from holding a document's lock for a day.
MIN_TIMEOUT = 1.0
MAX_TIMEOUT = 600.0


class Session:
    """One plugin instance, in one Figma document."""

    def __init__(self, sid, ws, file="", page=""):
        self.sid = sid
        self.ws = ws
        self.file = file
        self.page = page
        self.alias = ""
        self.pending: dict = {}      # rid -> {"future", "logs", "t0"}
        # One document is one JS runtime: two scripts interleaving at their
        # await points would see each other's half-finished edits. Per session,
        # never global — different documents are genuinely parallel.
        self.lock = asyncio.Lock()
        self.queued = 0              # callers waiting for the lock right now
        self.opened = time.time()
        self.last_seen = time.monotonic()

    @property
    def live(self) -> bool:
        return self.ws is not None and not self.ws.closed

    def label(self) -> str:
        return self.alias or self.file or self.sid

    def describe(self) -> dict:
        return {
            "sid": self.sid,
            "file": self.file,
            "page": self.page,
            "alias": self.alias,
            "pending": len(self.pending),
            "queued": self.queued,
            "age_s": int(time.time() - self.opened),
        }


def live_sessions() -> list:
    return [s for s in SESSIONS.values() if s.live]


def pending_total() -> int:
    return sum(len(s.pending) for s in SESSIONS.values())

# Host: values accepted in the Host header. Populated in main() from the bind
# address. Empty set means "don't check" — chosen when the user binds a
# non-loopback address on purpose (--host 0.0.0.0).
ALLOWED_HOSTS: set = set()

# Which project this bridge serves. Reported by / and /status so a client can
# tell "my bridge" from "someone else's bridge that got here first", and so
# `init` can name the owner of a port it wants to take.
PROJECT_NAME: str = ""


def _guard(request: web.Request, *, allow_null_origin: bool = False):
    """Reject browser-driven requests. Returns an error Response, or None if OK.

    The bridge executes arbitrary JS inside the user's Figma file, so any web
    page the user happens to have open is part of the threat model — binding to
    127.0.0.1 only keeps other machines out, not other tabs.

    Two checks:
      * Origin — local clients (curl, the figmosha CLI) never send this header.
        A browser always does on cross-origin requests, so its mere presence
        means the request came from a page. This blocks CSRF, including the
        "simple request" trick of posting JSON as text/plain to dodge preflight.
      * Host — a page whose DNS is re-pointed at 127.0.0.1 (DNS rebinding)
        becomes same-origin with the bridge and could then read responses.
        Pinning Host to the loopback names we actually serve closes that.
    """
    if ALLOWED_HOSTS:
        host = (request.headers.get("Host") or "").lower()
        if host not in ALLOWED_HOSTS:
            return json_response(
                {"ok": False, "error": f"unexpected Host header: {host!r}"}, status=403,
            )

    origin = request.headers.get("Origin")
    if origin is not None:
        # The Figma plugin UI runs in a sandboxed iframe, which reports "null".
        if not (allow_null_origin and origin == "null"):
            return json_response(
                {"ok": False,
                 "error": "cross-origin requests are not allowed",
                 "hint": "the bridge only accepts local clients (curl, figmosha CLI) "
                         "and the Figma plugin"},
                status=403,
            )
    return None


ERROR_HINTS = [
    ("fills and strokes variable bindings must be set on paints directly",
     "use h.bF(node, idx, varId) to bind a fill paint to a variable"),
    ("strokes variable bindings must be set on paints directly",
     "use h.bS(node, idx, varId) to bind a stroke paint to a variable"),
    ("Cannot assign to read only property",
     "node.fills/strokes is frozen — copy via JSON.parse(JSON.stringify(...)) before mutating, or use h.bF()/h.bS()"),
    ("permission not specified in manifest",
     "manifest.json is missing a permission — edit plugin/manifest.json here, then "
     "re-import: Figma → Plugins → Development → Manage plugins → remove, then Import again "
     "(a manifest change needs a re-import, not just a re-Run)"),
    ("unloaded font",
     "use h.setText(node, text) or h.withFonts(root, fn) — they autoload fonts. Or manually: await figma.loadFontAsync(node.fontName)"),
    ("font has not been loaded",
     "use h.setText(node, text) or h.withFonts(root, fn) — they autoload fonts"),
    ("Cannot find font",
     "fontName may be missing or mixed — check node.fontName before loading"),
    ("appendChild",
     "create node, then parent.appendChild(node) BEFORE setting layoutMode/resize/itemSpacing/padding"),
    ("Unable to find a variant",
     "no variant matches those property values — check available: const v = await h.variantsOf(instance); return v.groups"),
    ("Invalid property name",
     "check available variants: const v = await h.variantsOf(instance); return v.groups"),
    ("Invalid value",
     "check variant values: const v = await h.variantsOf(instance); return v.groups"),
    ("setProperties",
     "if 'Unable to find variant' — check available values via h.variantsOf(instance)"),
    ("not a function",
     "API may be deprecated or renamed — check figma.* available methods, or use Async variants"),
]


def find_hint(error_text):
    if not error_text:
        return None
    low = error_text.lower()
    for needle, hint in ERROR_HINTS:
        if needle.lower() in low:
            return hint
    return None


def json_response(data, status: int = 200) -> web.Response:
    r"""web.json_response, minus the \uXXXX escaping.

    Figma layer and file names are routinely Cyrillic or CJK, and an escaped
    character is six on the wire — six times the tokens for whoever reads the
    body, which for `curl /exec` is the whole response.
    """
    return web.json_response(
        data, status=status,
        dumps=lambda o: json.dumps(o, ensure_ascii=False))


def clamp_timeout(value) -> float:
    """The caller's timeout, bounded. Raises ValueError on anything non-numeric.

    Bounded rather than trusted, because the deadline also holds this
    document's lock: `{"timeout": 99999}` would not merely stall its own
    caller, it would park every other script for the same Figma file behind it
    for a day.
    """
    t = float(value)
    if t != t:                                  # NaN compares unequal to itself
        raise ValueError("timeout is NaN")
    return max(MIN_TIMEOUT, min(t, MAX_TIMEOUT))


def _fail_pending(session: Session, reason: str) -> None:
    """Resolve one session's in-flight requests so its clients don't hang.

    Scoped to the session on purpose: a file being closed says nothing about
    the other files this bridge is serving, and failing their requests too was
    the bug this registry exists to remove.
    """
    for rid, entry in list(session.pending.items()):
        if not entry["future"].done():
            entry["future"].set_result({"id": rid, "type": "error", "text": reason})


async def _incumbent_answers(session: Session, timeout: float = 1.0) -> bool:
    """Ping one session's plugin and report whether it replied in time.

    `ws.closed` is not enough to tell a live plugin from a half-open socket: a
    laptop that slept keeps its socket "open" until the heartbeat gives up ~20s
    later. Asking directly distinguishes the two in about a second, which is
    what lets a reconnecting plugin take its session back immediately while a
    genuine second instance in the same document still gets turned away.
    """
    ws = session.ws
    if ws is None or ws.closed:
        return False
    before = session.last_seen
    try:
        await ws.send_str(json.dumps({"type": "ping"}))
    except Exception:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        if session.last_seen > before:
            return True
    return False


async def _first_message(ws) -> dict | None:
    """Read the plugin's opening line, if it sends one soon enough.

    Identity has to be known before the bridge can decide anything: whether
    this socket is a document reconnecting, or a second document arriving. A
    connection that says nothing within HELLO_WAIT is a plugin from before the
    registry, and is treated as the one legacy session.
    """
    try:
        msg = await asyncio.wait_for(ws.receive(), timeout=HELLO_WAIT)
    except asyncio.TimeoutError:
        return None
    if msg.type != WSMsgType.TEXT:
        return None
    try:
        return json.loads(msg.data)
    except json.JSONDecodeError:
        return None


def _apply_hello(session: Session, m: dict) -> None:
    """Fold whatever the plugin told us about itself into the session."""
    if isinstance(m.get("file"), str):
        session.file = m["file"]
    if isinstance(m.get("page"), str):
        session.page = m["page"]


async def _claim_session(sid: str, ws, request) -> Session | None:
    """Put this socket in charge of `sid`, or refuse it.

    Refusal is scoped to the session, not the bridge: another document's plugin
    is not a competitor, it is a neighbour.
    """
    old = SESSIONS.get(sid)
    if old is not None and old.live and await _incumbent_answers(old):
        # Someone is really there — the same file open in a second Figma
        # window, or in both the stable and Beta apps. Only one may serve it.
        print(f"[plugin] {sid}: rejecting a second connection from {request.remote}")
        await ws.send_str(json.dumps(
            {"type": "error", "text": "another plugin instance is already connected"}))
        await ws.close(code=1008, message=b"already connected")
        return None

    session = Session(sid, ws, file=old.file if old else "",
                      page=old.page if old else "")
    if old is not None:
        session.alias = old.alias      # a human-given name outlives the socket
    SESSIONS[sid] = session

    if old is not None and old.live:
        print(f"[plugin] {sid}: superseding a connection that stopped answering")
        _fail_pending(old, "plugin reconnected mid-request")
        try:
            await old.ws.close(code=1012, message=b"superseded")
        except Exception:
            pass
    return session


def _handle_message(session: Session, m: dict) -> None:
    """Route one plugin message into the session it belongs to."""
    mtype = m.get("type")

    if mtype == "hello":
        _apply_hello(session, m)
        print(f"[plugin] {session.sid}: hello v{m.get('version', '?')} "
              f"file={session.file or '?'}")
        return
    if mtype == "pong":
        # Answer to _incumbent_answers(); the timestamp bump is the whole
        # point, nothing else to do.
        return
    if mtype == "page":
        session.page = m.get("page", session.page)
        return

    entry = session.pending.get(m.get("id"))
    if not entry:
        # late reply for a request that already timed out — drop it
        return

    if mtype == "log":
        entry["logs"].append(m.get("text", ""))
    elif mtype in ("result", "error"):
        if not entry["future"].done():
            entry["future"].set_result(m)


async def plugin_ws_handler(request: web.Request):
    blocked = _guard(request, allow_null_origin=True)
    if blocked is not None:
        print(f"[plugin] refused connection from {request.remote} "
              f"(origin={request.headers.get('Origin')!r})")
        return blocked

    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=16 * 1024 * 1024)
    await ws.prepare(request)

    opening = await _first_message(ws)
    sid = ANON_SID
    if isinstance(opening, dict) and isinstance(opening.get("sid"), str):
        sid = opening["sid"]

    session = await _claim_session(sid, ws, request)
    if session is None:
        return ws

    if isinstance(opening, dict):
        session.last_seen = time.monotonic()
        _handle_message(session, opening)

    print(f"[plugin] {sid}: connected from {request.remote} "
          f"({len(live_sessions())} session(s))")

    try:
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                print(f"[plugin] {sid}: ws error: {ws.exception()}")
                break
            if msg.type != WSMsgType.TEXT:
                continue

            if SESSIONS.get(sid) is session:
                session.last_seen = time.monotonic()

            try:
                m = json.loads(msg.data)
            except json.JSONDecodeError:
                print(f"[plugin] {sid}: bad json: {msg.data[:200]!r}")
                continue

            _handle_message(session, m)
    finally:
        # If this socket was already superseded, the requests now in flight
        # belong to its replacement — leave them alone.
        if SESSIONS.get(sid) is session:
            del SESSIONS[sid]
            print(f"[plugin] {sid}: disconnected")
            _fail_pending(session, "plugin disconnected mid-request")
        else:
            print(f"[plugin] {sid}: stale connection closed")
    return ws


def _target_of(request: web.Request, body: dict) -> str:
    """The session the caller asked for, from body, header or query string.

    Three spellings because three kinds of client: the CLI sends a body field,
    a shell one-liner is happiest with a query string, and a long-lived process
    would rather set a header once.
    """
    for value in (body.get("session"),
                  request.headers.get("X-Figmosha-Session"),
                  request.query.get("session")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _match_sessions(sessions: list, target: str) -> list:
    """Sessions a caller could have meant by `target`, most exact first.

    Humans address files by name ("ds"), scripts by id. An exact id or alias is
    unambiguous and wins outright; a name prefix that fits two files returns
    both, so the caller is asked rather than guessed at.
    """
    low = target.lower()

    exact = [s for s in sessions if s.sid == target or s.alias.lower() == low]
    if exact:
        return exact[:1]

    named = [s for s in sessions if s.file.lower() == low]
    if named:
        return named[:1]

    return [s for s in sessions
            if s.file.lower().startswith(low) or low in s.file.lower()]


def _pick_session(request: web.Request, body: dict):
    """Choose the session to run in, or explain why we cannot.

    Never falls back to "the first one": running in the wrong Figma file is a
    quiet, expensive mistake, while a 409 with the list of candidates is a loud
    and cheap one.
    """
    sessions = live_sessions()
    if not sessions:
        return None, json_response(
            {"ok": False,
             "error": "plugin not connected - open Figmosha in Figma"},
            status=503)

    target = _target_of(request, body)
    if target:
        matches = _match_sessions(sessions, target)
        if len(matches) == 1:
            return matches[0], None
        if not matches:
            # An unknown target must never become "whoever happens to be here".
            return None, json_response(
                {"ok": False,
                 "error": f"no connected Figma file matches {target!r}",
                 "sessions": [s.describe() for s in sessions]},
                status=409)
        return None, json_response(
            {"ok": False,
             "error": f"{target!r} matches {len(matches)} files - be more specific",
             "sessions": [s.describe() for s in matches]},
            status=409)

    if len(sessions) == 1:
        return sessions[0], None
    return None, json_response(
        {"ok": False,
         "error": f"{len(sessions)} Figma files are connected - say which one",
         "sessions": [s.describe() for s in sessions]},
        status=409)


async def exec_handler(request: web.Request) -> web.Response:
    blocked = _guard(request)
    if blocked is not None:
        return blocked

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return json_response({"ok": False, "error": "invalid JSON body"}, status=400)

    session, refusal = _pick_session(request, body)
    if refusal is not None:
        return refusal

    code = body.get("code")
    if not isinstance(code, str) or not code.strip():
        return json_response({"ok": False, "error": "missing or empty 'code'"}, status=400)

    try:
        timeout = clamp_timeout(body.get("timeout", 60))
    except (TypeError, ValueError):
        return json_response(
            {"ok": False,
             "error": f"'timeout' must be a number of seconds, got "
                      f"{body.get('timeout')!r}",
             "hint": f"allowed range: {MIN_TIMEOUT:.0f}..{MAX_TIMEOUT:.0f}"},
            status=400)

    # The deadline starts here, not when the plugin picks the request up:
    # a caller who asked for 60s means 60s of waiting in total, however that
    # time splits between the queue and the run.
    queued_at = time.monotonic()
    session.queued += 1
    try:
        await asyncio.wait_for(session.lock.acquire(), timeout=timeout)
    except asyncio.TimeoutError:
        session.queued -= 1
        return json_response(
            {"ok": False,
             "error": f"timeout after {timeout:.0f}s waiting for "
                      f"«{session.label()}» to finish earlier work",
             "waited_ms": int((time.monotonic() - queued_at) * 1000),
             "ran_ms": 0,
             "hint": "another script is running in this file - see `queued` in "
                     "GET /sessions"},
            status=504)
    session.queued -= 1

    try:
        waited_ms = int((time.monotonic() - queued_at) * 1000)
        remaining = timeout - (time.monotonic() - queued_at)

        rid = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        session.pending[rid] = {"future": fut, "logs": [], "t0": time.time()}

        # want_value defaults to true so that curl one-liners and older scripts
        # keep getting the structured `value` they were written against; the
        # CLI opts out of it, because it prints `result` and nothing else.
        want_value = body.get("want_value", True) is not False
        try:
            await session.ws.send_str(json.dumps(
                {"id": rid, "type": "exec", "code": code, "want_value": want_value}))
        except Exception as e:
            session.pending.pop(rid, None)
            return json_response({"ok": False, "error": f"send to plugin failed: {e}"}, status=500)

        try:
            result = await asyncio.wait_for(fut, timeout=remaining)
        except asyncio.TimeoutError:
            entry = session.pending.pop(rid, None)
            ran_ms = int((time.time() - entry["t0"]) * 1000) if entry else 0
            return json_response(
                {"ok": False,
                 "error": f"timeout after {timeout:.0f}s",
                 "waited_ms": waited_ms,
                 "ran_ms": ran_ms},
                status=504)

        entry = session.pending.pop(rid)
        elapsed_ms = int((time.time() - entry["t0"]) * 1000)
    finally:
        session.lock.release()

    if result.get("type") == "error":
        error_text = result.get("text", "unknown error")
        return json_response(
            {
                "ok": False,
                "error": error_text,
                "hint": find_hint(error_text),
                "stack": result.get("stack"),
                "logs": entry["logs"],
                "elapsed_ms": elapsed_ms,
                "waited_ms": waited_ms,
            },
            status=500,
        )

    return json_response({
        "ok": True,
        "result": result.get("text", ""),
        "value": result.get("value"),
        "logs": entry["logs"],
        "elapsed_ms": elapsed_ms,
        "waited_ms": waited_ms,
    })


async def sessions_handler(request: web.Request) -> web.Response:
    blocked = _guard(request)
    if blocked is not None:
        return blocked

    sessions = live_sessions()
    out = {
        "project": PROJECT_NAME,
        "sessions": [s.describe() for s in sessions],
    }
    # `?session=X` asks the same question POST /exec asks, and gets it answered
    # by the same matcher: which of these would X have picked? A client that
    # re-implements that guess eventually disagrees with the bridge, and then
    # its listing quietly describes a routing that does not happen.
    target = _target_of(request, {})
    if target:
        out["target"] = target
        out["matched"] = [s.sid for s in _match_sessions(sessions, target)]
    return json_response(out)


async def status_handler(request: web.Request) -> web.Response:
    blocked = _guard(request)
    if blocked is not None:
        return blocked

    # plugin_connected and pending keep their old meaning and shape: doctor,
    # the launchers and years of curl one-liners read them.
    live = live_sessions()
    return json_response({
        "plugin_connected": bool(live),
        "pending": pending_total(),
        "project": PROJECT_NAME,
        "sessions": len(live),
    })


async def root_handler(request: web.Request) -> web.Response:
    blocked = _guard(request)
    if blocked is not None:
        return blocked

    return json_response({
        "service": "figmosha-bridge",
        "version": project.VERSION,
        "project": PROJECT_NAME,
        "endpoints": {
            "POST /exec": "{code, timeout?, session?, want_value?} -> "
                          "{ok, result, value, logs, elapsed_ms}",
            "GET /status": "{plugin_connected, pending, project, sessions}",
            "GET /sessions": "?session=X -> [{sid, file, page, alias, pending, queued, "
                             "age_s}] + matched",
            "WS /plugin": "Figma plugin connects here",
        },
    })


def build_app() -> web.Application:
    app = web.Application(client_max_size=16 * 1024 * 1024)
    app.router.add_get("/", root_handler)
    app.router.add_get("/status", status_handler)
    app.router.add_get("/sessions", sessions_handler)
    app.router.add_post("/exec", exec_handler)
    app.router.add_get("/plugin", plugin_ws_handler)
    return app


def main():
    global ALLOWED_HOSTS, PROJECT_NAME

    # Project names and Figma layer names are routinely Cyrillic; a redirected
    # Windows console defaults to cp1251/cp1252 and would kill the bridge on
    # the first print instead of starting it.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="Figmosha bridge server")
    ap.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=None,
                    help="bind port (default: this project's, from project.json)")
    args = ap.parse_args()

    cfg = project.load()
    if cfg:
        PROJECT_NAME = cfg["name"]
    if args.port is None:
        # Falling back to 8787 here would be the worst possible default: an
        # uninitialized copy would quietly answer for whichever project already
        # owns that port, and edits would land in the wrong Figma file. Refuse.
        if not cfg:
            raise SystemExit(
                "figmosha: this copy has no project.json, so it does not know"
                " which project it serves. Guessing a port could put your"
                " edits into another project's Figma file.\n\n"
                "          claim it:        python figmosha.py init --name <Project>\n"
                "          or force a port: python bridge.py --port 8787")
        args.port = cfg["port"]
    elif cfg and args.port != cfg["port"]:
        print(f"[bridge] WARNING: --port {args.port} overrides project.json "
              f"(«{cfg['name']}» expects {cfg['port']}) — the plugin will not find it")

    if args.host in ("127.0.0.1", "localhost", "::1"):
        ALLOWED_HOSTS = {
            f"localhost:{args.port}",
            f"127.0.0.1:{args.port}",
            f"[::1]:{args.port}",
        }
    else:
        # Binding beyond loopback is a deliberate choice, and the reachable
        # hostnames are unknowable from here — skip the Host check and say so.
        print(f"[bridge] WARNING: bound to {args.host} — Host check disabled, "
              f"anyone who can reach this port can run code in your Figma file")

    print(f"[bridge] serving project «{PROJECT_NAME}»" if PROJECT_NAME
          else "[bridge] running unclaimed (no project.json)")
    print(f"[bridge] listening on http://{args.host}:{args.port}")
    print(f"[bridge] plugin should connect to ws://localhost:{args.port}/plugin")
    print(f"[bridge] try: curl -X POST http://localhost:{args.port}/exec "
          f"-H 'Content-Type: application/json' "
          f"-d '{{\"code\":\"return figma.currentPage.name\"}}'")

    web.run_app(build_app(), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
