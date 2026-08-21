"""Bridge tests driven by a fake plugin over the real WebSocket.

Everything here runs without Figma: a small asyncio client plays the plugin's
part, which is enough to exercise the parts most likely to regress — the session
registry, the handover within a session, and the origin/host guard.

    pip install pytest aiohttp
    pytest -q
"""

import asyncio
import time
import json
import sys
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bridge  # noqa: E402


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def clean_state():
    """The registry is module state; give every test a fresh one."""
    bridge.SESSIONS.clear()
    bridge.ALLOWED_HOSTS = set()
    yield
    bridge.SESSIONS.clear()
    bridge.ALLOWED_HOSTS = set()


class FakePlugin:
    """Stands in for plugin/ui.html: echoes exec requests, answers pings."""

    def __init__(self, client, *, answer_ping=True, reply=None, sid=None, file=""):
        self.client = client
        self.sid = sid
        self.file = file
        self.answer_ping = answer_ping
        self.reply = reply or (lambda code: {"text": "ok", "value": 42})
        self.ws = None
        self._task = None
        self.seen_codes = []
        self.seen_execs = []

    async def __aenter__(self):
        self.ws = await self.client.ws_connect("/plugin", headers={"Origin": "null"})
        hello = {"type": "hello", "version": "test"}
        if self.sid:
            hello["sid"] = self.sid
            hello["file"] = self.file or self.sid
        await self.ws.send_str(json.dumps(hello))
        self._task = asyncio.create_task(self._pump())
        await asyncio.sleep(0.1)
        return self

    async def __aexit__(self, *exc):
        if self._task:
            self._task.cancel()
        if self.ws and not self.ws.closed:
            await self.ws.close()

    def go_silent(self):
        """Stop answering without closing — a laptop that went to sleep."""
        if self._task:
            self._task.cancel()

    async def _pump(self):
        async for msg in self.ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            m = json.loads(msg.data)
            if m.get("type") == "ping":
                if self.answer_ping:
                    await self.ws.send_str(json.dumps({"type": "pong"}))
            elif m.get("type") == "exec":
                self.seen_codes.append(m["code"])
                self.seen_execs.append(m)
                out = self.reply(m["code"])
                await self.ws.send_str(json.dumps(
                    {"type": out.pop("type", "result"), "id": m["id"], **out}))


async def make_client():
    client = TestClient(TestServer(bridge.build_app()))
    await client.start_server()
    return client


# ─── guard ────────────────────────────────────────────────────────────────

def test_local_request_allowed():
    async def go():
        c = await make_client()
        r = await c.get("/status")
        assert r.status == 200
        assert (await r.json())["plugin_connected"] is False
        await c.close()
    run(go())


def test_request_with_origin_is_refused():
    async def go():
        c = await make_client()
        for path in ("/", "/status"):
            r = await c.get(path, headers={"Origin": "https://evil.example"})
            assert r.status == 403, path
        r = await c.post("/exec", data='{"code":"return 1"}',
                         headers={"Content-Type": "text/plain",
                                  "Origin": "https://evil.example"})
        assert r.status == 403
        await c.close()
    run(go())


def test_unexpected_host_is_refused():
    async def go():
        c = await make_client()
        bridge.ALLOWED_HOSTS = {"localhost:8787"}
        r = await c.get("/status", headers={"Host": "evil.example"})
        assert r.status == 403
        assert "Host" in (await r.json())["error"]
        await c.close()
    run(go())


def test_cross_origin_websocket_is_refused():
    async def go():
        c = await make_client()
        with pytest.raises(aiohttp.WSServerHandshakeError) as e:
            await c.ws_connect("/plugin", headers={"Origin": "https://evil.example"})
        assert e.value.status == 403
        await c.close()
    run(go())


# ─── exec round trip ──────────────────────────────────────────────────────

def test_exec_without_plugin_is_503():
    async def go():
        c = await make_client()
        r = await c.post("/exec", json={"code": "return 1"})
        assert r.status == 503
        await c.close()
    run(go())


def test_exec_round_trip():
    async def go():
        c = await make_client()
        async with FakePlugin(c) as plugin:
            r = await c.post("/exec", json={"code": "return 40 + 2"})
            body = await r.json()
            assert r.status == 200 and body["ok"] is True
            assert body["value"] == 42
            assert plugin.seen_codes == ["return 40 + 2"]
            assert bridge.pending_total() == 0, "request left behind in pending"
        await c.close()
    run(go())


def test_error_response_carries_a_hint():
    async def go():
        c = await make_client()
        reply = lambda code: {"type": "error", "text": "in an unloaded font"}
        async with FakePlugin(c, reply=reply):
            r = await c.post("/exec", json={"code": "n.characters = 'x'"})
            body = await r.json()
            assert r.status == 500 and body["ok"] is False
            assert "h.setText" in body["hint"]
            assert bridge.pending_total() == 0
        await c.close()
    run(go())


def test_timeout_returns_504_and_clears_pending():
    async def go():
        c = await make_client()
        async with FakePlugin(c, reply=lambda code: {"type": "__drop__"}):
            # plugin never answers; the bridge must give up on its own
            r = await c.post("/exec", json={"code": "sleep", "timeout": 0.3})
            assert r.status == 504
            assert bridge.pending_total() == 0
        await c.close()
    run(go())


def test_disconnect_fails_inflight_requests():
    async def go():
        c = await make_client()
        plugin = FakePlugin(c, reply=lambda code: {"type": "__drop__"})
        await plugin.__aenter__()
        task = asyncio.create_task(
            c.post("/exec", json={"code": "slow", "timeout": 10}))
        await asyncio.sleep(0.2)
        await plugin.__aexit__()
        r = await asyncio.wait_for(task, timeout=5)
        body = await r.json()
        assert r.status == 500
        assert "disconnected" in body["error"]
        await c.close()
    run(go())


# ─── the single plugin slot ───────────────────────────────────────────────

def test_live_plugin_keeps_the_slot():
    async def go():
        c = await make_client()
        async with FakePlugin(c, answer_ping=True):
            second = await c.ws_connect("/plugin", headers={"Origin": "null"})
            msg = await asyncio.wait_for(second.receive(), timeout=5)
            assert "already connected" in json.loads(msg.data)["text"]
            await asyncio.wait_for(second.receive(), timeout=5)
            assert second.close_code == 1008
        await c.close()
    run(go())


def test_silent_plugin_is_superseded():
    async def go():
        c = await make_client()
        async with FakePlugin(c) as first:
            first.go_silent()
            async with FakePlugin(c) as second:
                # The bridge spends up to a second probing the incumbent before
                # handing the slot over, and anything sent during that window is
                # still addressed to the dying socket — it comes back as
                # "plugin reconnected mid-request" rather than being queued.
                # What's guaranteed is that the handover completes and the new
                # plugin serves; retry until it does.
                deadline = asyncio.get_running_loop().time() + 5
                r = None
                while asyncio.get_running_loop().time() < deadline:
                    r = await c.post("/exec", json={"code": "return 1", "timeout": 2})
                    if r.status == 200:
                        break
                    await asyncio.sleep(0.2)
                assert r is not None and r.status == 200
                assert second.seen_codes, "exec never reached the new plugin"
                assert first.seen_codes == [], "stale socket was still being used"
        await c.close()
    run(go())


# ─── the session registry ─────────────────────────────────────────────────

def test_two_files_are_two_sessions():
    async def go():
        c = await make_client()
        async with FakePlugin(c, sid="s1", file="Design System"):
            async with FakePlugin(c, sid="s2", file="Marketing Site"):
                r = await c.get("/sessions")
                rows = (await r.json())["sessions"]
                assert {row["file"] for row in rows} == {"Design System", "Marketing Site"}
                assert (await (await c.get("/status")).json())["sessions"] == 2
        await c.close()
    run(go())


def test_second_file_is_not_turned_away():
    """The old bridge answered a second document with 'Slot busy'."""
    async def go():
        c = await make_client()
        async with FakePlugin(c, sid="s1", file="A") as first:
            async with FakePlugin(c, sid="s2", file="B") as second:
                assert first.ws.closed is False
                assert second.ws.closed is False
                assert len(bridge.live_sessions()) == 2
        await c.close()
    run(go())


def test_same_file_reconnecting_still_owns_one_slot():
    """Two windows on the *same* document is still a conflict, and only there."""
    async def go():
        c = await make_client()
        async with FakePlugin(c, sid="s1", file="A"):
            twin = await c.ws_connect("/plugin", headers={"Origin": "null"})
            await twin.send_str(json.dumps(
                {"type": "hello", "version": "test", "sid": "s1", "file": "A"}))
            msg = await asyncio.wait_for(twin.receive(), timeout=5)
            assert "already connected" in json.loads(msg.data)["text"]
            await asyncio.wait_for(twin.receive(), timeout=5)
            assert twin.close_code == 1008
        await c.close()
    run(go())


def test_one_file_closing_leaves_the_other_running():
    """The bug the registry exists to fix: a shared PENDING failed everyone."""
    async def go():
        c = await make_client()
        async with FakePlugin(c, sid="keep", file="Keep") as keeper:
            doomed = FakePlugin(c, sid="drop", file="Drop",
                                reply=lambda code: {"type": "__drop__"})
            await doomed.__aenter__()

            slow = asyncio.create_task(
                c.post("/exec", json={"code": "slow", "timeout": 10, "session": "drop"}))
            await asyncio.sleep(0.2)
            await doomed.__aexit__()

            r = await asyncio.wait_for(slow, timeout=5)
            assert r.status == 500 and "disconnected" in (await r.json())["error"]

            # The surviving file must still work, and be the only session left.
            assert [s.sid for s in bridge.live_sessions()] == ["keep"]
            ok = await c.post("/exec", json={"code": "return 1"})
            assert ok.status == 200 and (await ok.json())["value"] == 42
            assert keeper.seen_codes[-1] == "return 1"
        await c.close()
    run(go())


def test_ambiguous_target_is_a_409_with_the_list():
    async def go():
        c = await make_client()
        async with FakePlugin(c, sid="s1", file="Design System"):
            async with FakePlugin(c, sid="s2", file="Marketing Site"):
                r = await c.post("/exec", json={"code": "return 1"})
                assert r.status == 409, "guessing a file is worse than failing"
                body = await r.json()
                assert {row["file"] for row in body["sessions"]} == {
                    "Design System", "Marketing Site"}
        await c.close()
    run(go())


def test_concurrent_execs_do_not_cross_files():
    """Two documents, two answers, each to the caller that asked for it."""
    async def go():
        c = await make_client()
        async with FakePlugin(c, sid="s1", file="A", reply=lambda code: {"value": "A"}):
            async with FakePlugin(c, sid="s2", file="B", reply=lambda code: {"value": "B"}):
                a, b = await asyncio.gather(
                    c.post("/exec", json={"code": "who", "session": "s1"}),
                    c.post("/exec", json={"code": "who", "session": "s2"}),
                )
                assert (await a.json())["value"] == "A"
                assert (await b.json())["value"] == "B"
        await c.close()
    run(go())


def test_unknown_target_is_refused_not_guessed():
    async def go():
        c = await make_client()
        async with FakePlugin(c, sid="s1", file="A"):
            r = await c.post("/exec", json={"code": "x", "session": "nope"})
            assert r.status == 409, "a single session must not absorb a wrong target"
            assert (await r.json())["sessions"][0]["sid"] == "s1"
        await c.close()
    run(go())


def test_target_can_come_from_a_header_or_query():
    async def go():
        c = await make_client()
        async with FakePlugin(c, sid="s1", file="A", reply=lambda code: {"value": "A"}):
            async with FakePlugin(c, sid="s2", file="B", reply=lambda code: {"value": "B"}):
                r = await c.post("/exec", json={"code": "x"},
                                 headers={"X-Figmosha-Session": "s2"})
                assert (await r.json())["value"] == "B"
                r = await c.post("/exec?session=s1", json={"code": "x"})
                assert (await r.json())["value"] == "A"
        await c.close()
    run(go())


def test_legacy_plugin_without_sid_keeps_the_old_behaviour():
    async def go():
        c = await make_client()
        async with FakePlugin(c) as legacy:          # no sid in hello
            assert [s.sid for s in bridge.live_sessions()] == [bridge.ANON_SID]
            r = await c.post("/exec", json={"code": "return 1"})
            assert r.status == 200 and legacy.seen_codes == ["return 1"]
        await c.close()
    run(go())


def test_a_silent_connection_is_assumed_legacy():
    """A socket that never introduces itself predates the registry."""
    async def go():
        c = await make_client()
        ws = await c.ws_connect("/plugin", headers={"Origin": "null"})
        await asyncio.sleep(bridge.HELLO_WAIT + 0.2)
        assert [s.sid for s in bridge.live_sessions()] == [bridge.ANON_SID]
        await ws.close()
        await c.close()
    run(go())


def test_hello_carries_file_and_page_into_the_listing():
    async def go():
        c = await make_client()
        async with FakePlugin(c, sid="s1", file="Design System") as p:
            await p.ws.send_str(json.dumps({"type": "page", "page": "Tokens"}))
            await asyncio.sleep(0.1)
            row = (await (await c.get("/sessions")).json())["sessions"][0]
            assert row["file"] == "Design System"
            assert row["page"] == "Tokens"
            assert row["sid"] == "s1"
        await c.close()
    run(go())


def test_page_change_does_not_disturb_in_flight_work():
    """The plugin reports page changes on its own; they are not requests."""
    async def go():
        c = await make_client()
        async with FakePlugin(c, sid="s1", file="A") as p:
            await p.ws.send_str(json.dumps({"type": "page", "page": "Two"}))
            r = await c.post("/exec", json={"code": "return 1"})
            assert r.status == 200 and (await r.json())["value"] == 42
            assert bridge.SESSIONS["s1"].page == "Two"
        await c.close()
    run(go())


def test_a_file_can_be_addressed_by_name():
    async def go():
        c = await make_client()
        async with FakePlugin(c, sid="s1", file="Northwind DS",
                              reply=lambda code: {"value": "DS"}):
            async with FakePlugin(c, sid="s2", file="Northwind Web",
                                  reply=lambda code: {"value": "Web"}):
                r = await c.post("/exec", json={"code": "x", "session": "ds"})
                assert (await r.json())["value"] == "DS"
                r = await c.post("/exec", json={"code": "x", "session": "Northwind Web"})
                assert (await r.json())["value"] == "Web"
        await c.close()
    run(go())


def test_an_ambiguous_name_lists_the_candidates():
    async def go():
        c = await make_client()
        async with FakePlugin(c, sid="s1", file="Northwind DS"):
            async with FakePlugin(c, sid="s2", file="Northwind Web"):
                r = await c.post("/exec", json={"code": "x", "session": "northwind"})
                assert r.status == 409
                body = await r.json()
                assert len(body["sessions"]) == 2
        await c.close()
    run(go())


# ─── one document at a time ───────────────────────────────────────────────

class OrderedPlugin(FakePlugin):
    """Answers on demand, so a test can hold a request open deliberately."""

    def __init__(self, client, **kw):
        super().__init__(client, **kw)
        self.received = asyncio.Queue()
        self.order = []

    async def _pump(self):
        async for msg in self.ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            m = json.loads(msg.data)
            if m.get("type") == "ping":
                await self.ws.send_str(json.dumps({"type": "pong"}))
            elif m.get("type") == "exec":
                self.order.append(m["code"])
                await self.received.put(m)

    async def answer(self, m, value):
        await self.ws.send_str(json.dumps(
            {"type": "result", "id": m["id"], "text": str(value), "value": value}))


def test_one_file_runs_scripts_one_at_a_time():
    """Two scripts interleaving in one document would see each other's edits."""
    async def go():
        c = await make_client()
        async with OrderedPlugin(c, sid="s1", file="A") as p:
            first = asyncio.create_task(
                c.post("/exec", json={"code": "first", "timeout": 10}))
            got_first = await asyncio.wait_for(p.received.get(), timeout=5)

            second = asyncio.create_task(
                c.post("/exec", json={"code": "second", "timeout": 10}))
            await asyncio.sleep(0.3)
            assert p.order == ["first"], "second script started before the first finished"

            row = (await (await c.get("/sessions")).json())["sessions"][0]
            assert row["queued"] == 1

            await p.answer(got_first, 1)
            got_second = await asyncio.wait_for(p.received.get(), timeout=5)
            await p.answer(got_second, 2)

            assert (await (await first).json())["value"] == 1
            assert (await (await second).json())["value"] == 2
        await c.close()
    run(go())


def test_two_files_are_not_serialised_against_each_other():
    async def go():
        c = await make_client()
        async with OrderedPlugin(c, sid="s1", file="A") as a:
            async with OrderedPlugin(c, sid="s2", file="B") as b:
                ta = asyncio.create_task(
                    c.post("/exec", json={"code": "a", "session": "s1", "timeout": 10}))
                tb = asyncio.create_task(
                    c.post("/exec", json={"code": "b", "session": "s2", "timeout": 10}))

                ma = await asyncio.wait_for(a.received.get(), timeout=5)
                mb = await asyncio.wait_for(b.received.get(), timeout=5)
                # Both reached their plugin without either finishing first.
                await b.answer(mb, "B")
                await a.answer(ma, "A")
                assert (await (await ta).json())["value"] == "A"
                assert (await (await tb).json())["value"] == "B"
        await c.close()
    run(go())


def test_timeout_in_the_queue_says_so():
    async def go():
        c = await make_client()
        async with OrderedPlugin(c, sid="s1", file="A") as p:
            blocker = asyncio.create_task(
                c.post("/exec", json={"code": "hold", "timeout": 10}))
            held = await asyncio.wait_for(p.received.get(), timeout=5)

            r = await c.post("/exec", json={"code": "queued", "timeout": 0.4})
            assert r.status == 504
            body = await r.json()
            assert body["ran_ms"] == 0, "it never ran; the wait was the whole story"
            assert body["waited_ms"] >= 300
            assert "finish earlier work" in body["error"]

            await p.answer(held, 1)
            await blocker
        await c.close()
    run(go())


# ─── what one call may cost ───────────────────────────────────────────────

def test_a_non_numeric_timeout_is_a_400_not_a_crash():
    async def go():
        c = await make_client()
        async with FakePlugin(c):
            r = await c.post("/exec", json={"code": "return 1", "timeout": "soon"})
            assert r.status == 400
            assert "timeout" in (await r.json())["error"]
        await c.close()
    run(go())


@pytest.mark.parametrize("asked,expected", [
    (99999, bridge.MAX_TIMEOUT),   # a typo must not hold the document's lock for a day
    (0, bridge.MIN_TIMEOUT),
    (-5, bridge.MIN_TIMEOUT),
    (30, 30.0),
    ("45", 45.0),                  # curl one-liners quote everything
])
def test_timeout_is_clamped_to_a_sane_range(asked, expected):
    assert bridge.clamp_timeout(asked) == expected


def test_timeout_rejects_garbage():
    for bad in ("soon", None, [], float("nan")):
        with pytest.raises((ValueError, TypeError)):
            bridge.clamp_timeout(bad)


def test_the_plugin_is_told_whether_the_value_is_wanted():
    """The CLI prints only the text, so it opts out of the second copy."""
    async def go():
        c = await make_client()
        async with FakePlugin(c) as plugin:
            await c.post("/exec", json={"code": "return 1", "want_value": False})
            assert plugin.seen_execs[-1]["want_value"] is False
            # Anything that does not ask keeps the documented response shape.
            await c.post("/exec", json={"code": "return 1"})
            assert plugin.seen_execs[-1]["want_value"] is True
        await c.close()
    run(go())


def test_sessions_says_which_row_a_target_matches():
    """So the `*` in `figmosha sessions` cannot disagree with the routing."""
    async def go():
        c = await make_client()
        async with FakePlugin(c, sid="s1", file="Kite folio"):
            async with FakePlugin(c, sid="s2", file="Kite site"):
                body = await (await c.get("/sessions?session=kite%20folio")).json()
                assert body["matched"] == ["s1"]
                body = await (await c.get("/sessions?session=kite")).json()
                assert sorted(body["matched"]) == ["s1", "s2"]
                body = await (await c.get("/sessions")).json()
                assert "matched" not in body
        await c.close()
    run(go())


def test_cyrillic_is_not_escaped_on_the_wire():
    """An escaped character is six on the wire, and six times the tokens."""
    async def go():
        c = await make_client()
        async with FakePlugin(c, reply=lambda code: {"text": "Привіт", "value": None}):
            r = await c.post("/exec", json={"code": "return 1"})
            raw = await r.read()
            # The UTF-8 bytes are only in there if nothing escaped them.
            assert "Привіт".encode("utf-8") in raw
        await c.close()
    run(go())


# ─── hints ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("error_text,expected", [
    ("Cannot assign to read only property 'fills'", "h.bF()"),
    ("in an unloaded font Inter Bold", "h.setText"),
    ("teamlibrary permission not specified in manifest", "manifest.json"),
    ("Unable to find a variant matching", "h.variantsOf"),
    ("something entirely unrecognised", None),
])
def test_find_hint(error_text, expected):
    hint = bridge.find_hint(error_text)
    if expected is None:
        assert hint is None
    else:
        assert expected in hint


# ─── the queue counter, and how the incumbent is asked ────────────────────

def test_queued_comes_back_down_when_a_caller_walks_away():
    """Ctrl-C mid-wait raises CancelledError inside /exec.

    The count it leaves behind is what `sessions` reports and what the 504 hint
    tells the next caller to look at, so a leak there misinforms every request
    that follows for as long as the bridge runs.
    """
    async def go():
        c = await make_client()
        async with FakePlugin(c, reply=lambda code: {"text": "slow"}) as p:
            # Hold the session lock so the second caller has to queue.
            session = next(iter(bridge.SESSIONS.values()))
            await session.lock.acquire()

            waiting = asyncio.create_task(
                c.post("/exec", json={"code": "return 1", "timeout": 30}))
            await asyncio.sleep(0.2)
            assert session.queued == 1

            waiting.cancel()
            try:
                await waiting
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(0.1)
            assert session.queued == 0

            session.lock.release()
        await c.close()
    run(go())


def test_incumbent_is_answered_by_the_pong_not_by_a_poll():
    """A reconnecting plugin waits on this, so it must not cost a poll tick."""
    async def go():
        c = await make_client()
        async with FakePlugin(c, sid="s1", file="One"):
            session = bridge.SESSIONS["s1"]
            start = time.monotonic()
            assert await bridge._incumbent_answers(session) is True
            elapsed = time.monotonic() - start
            # The old loop slept 50 ms before its first look; anything under
            # that shows the future is being resolved by the pong itself.
            assert elapsed < 0.04, elapsed
            assert session.pong_waiters == []
        await c.close()
    run(go())


def test_a_silent_plugin_still_times_out():
    """The fast path must not make a dead socket look alive."""
    async def go():
        c = await make_client()
        async with FakePlugin(c, sid="s1", answer_ping=False) as p:
            p.go_silent()
            session = bridge.SESSIONS["s1"]
            assert await bridge._incumbent_answers(session, timeout=0.2) is False
            assert session.pong_waiters == []
        await c.close()
    run(go())
