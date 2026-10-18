"""End to end through a real Hermes dashboard: a teammate's message runs in the recipient's "Bot Inbox", not its main chat.

Not part of the unit tests (it starts a Hermes dashboard from a checkout at the version being fixed). Run it with that
Hermes's venv, which has `websockets`:

    ~/.hermes/hermes-agent/venv/bin/python hermes-bot-fixes/tests/integration_inbox.py [path to a Hermes checkout]

Everything runs in a throwaway HERMES_HOME on loopback ports with a scripted local model, so the real `~/.hermes` is
never read or written. Three bots (Orion, Juniper, Harbor) each have their main chat ("Bot Chat") open live in the
dashboard, the case where Hermes hands a teammate's message to that chat's mailbox. The same home runs three times:

1. without the plugin: Hermes as it is (the message runs in Juniper's main chat);
2. with the plugin: the message runs in Juniper's "Bot Inbox"; Juniper's main chat gets no rows and never runs; the
   reply wakes the sender's main chat; two senders at once queue on Juniper's turn lock; a dangerous command is denied
   and clarify cannot hold the turn;
3. with the plugin switched off (HERMES_BOT_FIXES_BOT_INBOX=off in the home's .env): Hermes as it is again.
"""

import asyncio
import json
import os
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import websockets

PLUGIN = Path(__file__).resolve().parent.parent
CHECKOUT = Path(sys.argv[1] if len(sys.argv) > 1 else Path.home() / ".hermes" / "hermes-agent").resolve()
HERMES = CHECKOUT / "venv" / "bin" / "hermes"

BOTS = {"orion": "Orion", "juniper": "Juniper", "harbor": "Harbor"}
WAIT = 150

ROOT = Path(tempfile.mkdtemp(prefix="hbf-inbox-"))
HOME = ROOT / "home"
WORK = ROOT / "work"
LOGS = ROOT / "logs"
VICTIM = WORK / "victim"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))

        return sock.getsockname()[1]


# ── the scripted model ──────────────────────────────────────────────────────────────────────────────────────────────


class Model:
    """Answers each turn from its first user message; records every request with its time."""

    def __init__(self):
        self.calls = []
        self.slow = 0.0
        self.lock = threading.Lock()


MODEL = Model()


def text_of(content) -> str:
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))

    return content or ""


def decide(body: dict) -> dict:
    messages = body.get("messages") or []
    system = text_of(messages[0].get("content")) if messages and messages[0].get("role") == "system" else ""
    starts = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    turn = messages[starts[-1]:] if starts else messages
    user = text_of(turn[0].get("content")) if turn else ""
    results = [text_of(m.get("content")) for m in turn if m.get("role") == "tool"]
    tools = {t.get("function", {}).get("name") for t in body.get("tools") or []}

    asked = re.match(r"Ask (\w+): (.*)", user, re.S)

    if asked:
        if results:
            return {"text": "Sent it; the answer comes back here."}

        if "message_agent" not in tools:
            return {"text": "I have no message_agent here."}

        return {"tool": "message_agent", "args": {"target": asked[1], "message": asked[2]}}

    dm = re.match(r"Message from 🤖 (\w+) \(@(\w+)\): (.*)", user, re.S)

    if dm:
        ask = dm[3]

        if "rm -rf" in ask:
            if not results:
                return {"tool": "terminal", "args": {"command": f"rm -rf {VICTIM}"}}

            return {"text": "terminal said: " + results[-1][:400]}

        if "clarify" in ask:
            if not results and "clarify" in tools:
                return {"tool": "clarify", "args": {"questions": [{"question": "Which file do you mean?", "choices": ["notes.txt", "plan.txt"]}]}}

            return {"text": "clarify said: " + (results[-1][:300] if results else "(no clarify tool in this chat)")}

        time.sleep(MODEL.slow)

        return {"text": f"`true` ran and exited 0 (asked by @{dm[2]})."}

    for marker in ("ran and exited 0", "terminal said", "clarify said"):
        if marker in user:
            return {"text": "Relayed: " + user[user.index(marker):][:300]}

    return {"text": "ok"}


def chunk(model: str, delta: dict, finish=None) -> str:
    return "data: " + json.dumps({"id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": int(time.time()),
                                  "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            return self.send_json(200, {"object": "list", "data": [{"id": "fake-model", "object": "model", "context_length": 131072}]})

        self.send_json(404, {"error": {"message": "no route"}})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self.send_json(404, {"error": {"message": "no route"}})

        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        started = time.time()
        decision = decide(body)
        messages = body.get("messages") or []
        starts = [i for i, m in enumerate(messages) if m.get("role") == "user"]
        system = text_of(messages[0].get("content")) if messages and messages[0].get("role") == "system" else ""

        with MODEL.lock:
            MODEL.calls.append({
                "start": started, "end": time.time(),
                "user": text_of(messages[starts[-1]].get("content"))[:200] if starts else "",
                "tools": sorted(n for n in (t.get("function", {}).get("name") for t in body.get("tools") or []) if n in ("message_agent", "clarify", "terminal")),
                "protocol": "## Messaging other agents" in system,
                "decision": decision,
            })

        model = body.get("model") or "fake-model"
        call = {"id": f"call_{secrets.token_hex(4)}", "type": "function",
                "function": {"name": decision.get("tool"), "arguments": json.dumps(decision.get("args"))}}

        if not body.get("stream"):
            message = ({"role": "assistant", "content": None, "tool_calls": [call]} if "tool" in decision
                       else {"role": "assistant", "content": decision["text"]})

            return self.send_json(200, {
                "id": "chatcmpl-fake", "object": "chat.completion", "created": int(time.time()), "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if "tool" in decision else "stop"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
            })

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        out = [chunk(model, {"role": "assistant", "content": ""})]

        if "tool" in decision:
            out += [chunk(model, {"tool_calls": [{"index": 0, **call}]}), chunk(model, {}, "tool_calls")]
        else:
            out += [chunk(model, {"content": decision["text"]}), chunk(model, {}, "stop")]

        out.append("data: [DONE]\n\n")
        self.wfile.write("".join(out).encode())


# ── the throwaway home ──────────────────────────────────────────────────────────────────────────────────────────────


def config(model_port: int, plugin: bool) -> str:
    text = f"""\
_config_version: 50
model:
  default: fake-model
  provider: custom
  base_url: http://127.0.0.1:{model_port}/v1
  api_key: fake-local
  context_length: 131072
agent:
  coding_context: "off"
terminal:
  cwd: {WORK}
memory:
  memory_enabled: false
  user_profile_enabled: false
  nudge_interval: 0
auxiliary:
  title_generation:
    enabled: false
  background_review:
    enabled: false
curator:
  enabled: false
"""

    return text + ("plugins:\n  enabled:\n    - hermes-bot-fixes\n" if plugin else "")


def set_up(model_port: int, *, plugin: bool, switch_off: bool) -> None:
    homes = [HOME, *(HOME / "profiles" / name for name in BOTS)]

    for home in homes:
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text(config(model_port, plugin), encoding="utf-8")
        copy = home / "plugins" / "hermes-bot-fixes"
        shutil.rmtree(copy, ignore_errors=True)

        if plugin:
            shutil.copytree(PLUGIN, copy, ignore=shutil.ignore_patterns("tests", "__pycache__"))

    for name, title in BOTS.items():
        (HOME / "profiles" / name / "profile.yaml").write_text(
            f"description: throwaway teammate\ndisplay_name: {title}\nui_meta:\n  hermes-bots:\n    shape: cloud\n", encoding="utf-8")

    (HOME / ".env").write_text("HERMES_BOT_FIXES_BOT_INBOX=off\n" if switch_off else "", encoding="utf-8")


def borrow_dependencies() -> None:
    """A checkout whose `hermes` launcher runs a Python from another home's store (a home that is neither the folder the
    checkout sits in nor ~/.hermes, where Hermes looks for one itself) has no dependencies from a throwaway home, and
    message deliveries run that launcher. Give this home a clone of that store, pointed at itself."""
    launcher = CHECKOUT / ".hermes" / "bin" / "hermes"
    first = re.search(r"^exec (\S+)", launcher.read_text(errors="replace"), re.M) if launcher.is_file() else None
    owner = next((parent.parent for parent in Path(first[1]).parents if parent.name == "tools"), None) if first else None

    if owner is None or not (owner / "installs").is_dir() or owner.resolve() in (CHECKOUT.parent.resolve(), (Path.home() / ".hermes").resolve()):
        return

    for state in (owner / "installs").iterdir():
        target = HOME / "installs" / state.name

        if target.exists():
            continue

        target.parent.mkdir(parents=True, exist_ok=True)

        if subprocess.run(["cp", "-cR", str(state), str(target)], check=False).returncode != 0:
            shutil.copytree(state, target, symlinks=True)

        facts = target / "facts.json"

        if facts.is_file():
            facts.write_text(facts.read_text(encoding="utf-8").replace(str(owner), str(HOME)), encoding="utf-8")


def start_dashboard(port: int, token: str, tag: str) -> subprocess.Popen:
    env = {key: value for key, value in os.environ.items() if not key.startswith("HERMES_")}
    env.update(HERMES_HOME=str(HOME), HERMES_DASHBOARD_SESSION_TOKEN=token, HERMES_TUI_WS_ORPHAN_REAP_GRACE_S="0")
    log = open(LOGS / f"dashboard-{tag}.log", "w")
    proc = subprocess.Popen([str(HERMES), "dashboard", "--host", "127.0.0.1", "--port", str(port), "--no-open", "--skip-build"],
                            cwd=str(WORK), env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.time() + 120

    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"the dashboard exited; see {log.name}")

        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2):
                return proc
        except OSError:
            time.sleep(0.5)

    raise SystemExit(f"the dashboard did not answer; see {log.name}")


def stop(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=30)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    # Delivery runners started for this home, and the turns under them (in their process group), only. A runner that
    # was being spawned as the dashboard stopped shows up a moment later.
    for _ in range(3):
        time.sleep(1)
        listed = subprocess.run(["ps", "-axo", "pgid=,command="], capture_output=True, text=True, check=False).stdout
        groups = {int(line.split(None, 1)[0]) for line in listed.splitlines() if ROOT.name in line} - {os.getpgrp()}

        for group in groups:
            try:
                os.killpg(group, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def chats(profile: str) -> dict:
    """Each "Bot ..." session of a bot: its id, source and rows (role, display kind, start of the text)."""
    db = HOME / "profiles" / profile / "state.db"

    if not db.exists():
        return {}

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)

    try:
        found = {}

        for sid, title, source in con.execute("SELECT id, title, source FROM sessions WHERE title LIKE 'Bot %' ORDER BY started_at"):
            rows = con.execute("SELECT role, display_kind, content FROM messages WHERE session_id = ? ORDER BY id", (sid,)).fetchall()
            found.setdefault(title, []).append({"id": sid, "source": source,
                                                "rows": [(role, kind, (content or "")[:90]) for role, kind, content in rows]})

        return found
    finally:
        con.close()


def rows(profile: str, title: str) -> list:
    return [row for chat in chats(profile).get(title, []) for row in chat["rows"]]


# ── the dashboard client ────────────────────────────────────────────────────────────────────────────────────────────


class Gateway:
    def __init__(self, url: str):
        self.url, self.events, self.requests, self.pending, self.next = url, [], [], {}, 0

    async def __aenter__(self):
        self.ws = await websockets.connect(self.url, max_size=None)
        self.reader = asyncio.create_task(self.read())

        return self

    async def __aexit__(self, *exc):
        self.reader.cancel()
        await self.ws.close()

    async def read(self):
        async for raw in self.ws:
            frame = json.loads(raw)

            if frame.get("method") == "event":
                self.events.append((time.time(), frame.get("params") or {}))
            elif frame.get("method"):
                # A prompt for a person (approval, clarify, ...): none is expected; answer it so nothing waits.
                self.requests.append(frame)
                await self.ws.send(json.dumps({"jsonrpc": "2.0", "id": frame.get("id"), "error": {"code": -32601, "message": "test client"}}))
            elif frame.get("id") in self.pending:
                self.pending.pop(frame["id"]).set_result(frame)

    async def call(self, method: str, params: dict):
        self.next += 1
        rid = f"t{self.next}"
        self.pending[rid] = asyncio.get_running_loop().create_future()
        await self.ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))
        frame = await asyncio.wait_for(self.pending[rid], 60)

        if "error" in frame:
            raise RuntimeError(f"{method}: {frame['error']}")

        return frame.get("result") or {}

    async def open_bot_chat(self, profile: str) -> str:
        listed = await self.call("session.list", {"profile": profile, "title": "Bot Chat", "include_hidden": True})
        known = [row for row in listed.get("sessions", []) if (row.get("title") or "") == "Bot Chat"]

        if known:
            opened = await self.call("session.resume", {"session_id": known[0].get("resolved_id") or known[0]["id"], "profile": profile, "omit_messages": True})
        else:
            opened = await self.call("session.create", {"profile": profile, "title": "Bot Chat", "hidden": True, "follow_profile_config": True})

        return opened["session_id"]

    def of(self, sid: str, kind: str, since: float = 0.0) -> list:
        return [params for at, params in self.events if at >= since and params.get("session_id") == sid and params.get("type") == kind]

    async def until(self, check, what: str, timeout: float = WAIT):
        deadline = time.time() + timeout

        while time.time() < deadline:
            if check():
                return

            await asyncio.sleep(0.25)

        raise AssertionError(f"timed out waiting for {what}")

    async def answer(self, sid: str, since: float, marker: str, timeout: float = WAIT) -> str:
        """The sender's main chat runs a turn whose reply carries ``marker`` (the wake-up with the answer)."""
        def done():
            return any(marker in (event.get("payload") or {}).get("text", "") for event in self.of(sid, "message.complete", since))

        await self.until(done, f"a reply with {marker!r}", timeout)

        return next(event["payload"]["text"] for event in self.of(sid, "message.complete", since) if marker in event["payload"].get("text", ""))


# ── the runs ────────────────────────────────────────────────────────────────────────────────────────────────────────


def check(ok: bool, what: str, detail="") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {what}" + (f": {detail}" if detail else ""))

    if not ok:
        raise SystemExit(f"failed: {what} (home kept at {ROOT})")


async def as_hermes_does(gw: Gateway, live: dict, label: str) -> None:
    """Orion asks Juniper; the message runs in Juniper's main chat, as Hermes does it."""
    before_main, before_inbox = len(rows("juniper", "Bot Chat")), len(rows("juniper", "Bot Inbox"))
    since = time.time()
    await gw.call("prompt.submit", {"session_id": live["orion"], "text": "Ask juniper: run `true` and tell me the exit code."})
    reply = await gw.answer(live["orion"], since, "ran and exited 0")
    main = rows("juniper", "Bot Chat")[before_main:]

    check(any("Message from 🤖 Orion" in text for _role, _kind, text in main), f"{label}: Orion's message ran in Juniper's main chat",
          f"{len(main)} new rows there")
    check(bool(gw.of(live["juniper"], "message.complete", since)), f"{label}: Juniper's open main chat ran the turn")
    check(len(rows("juniper", "Bot Inbox")) == before_inbox, f"{label}: nothing in Juniper's inbox")
    check(True, f"{label}: the reply woke Orion's main chat", reply)


async def inbox_run(gw: Gateway, live: dict) -> None:
    main_before = rows("juniper", "Bot Chat")
    since = time.time()

    # 1. One message.
    await gw.call("prompt.submit", {"session_id": live["orion"], "text": "Ask juniper: run `true` and tell me the exit code."})
    reply = await gw.answer(live["orion"], since, "ran and exited 0")
    inbox = chats("juniper").get("Bot Inbox", [])
    inbox_rows = rows("juniper", "Bot Inbox")
    dm_calls = [call for call in MODEL.calls if call["start"] >= since and call["user"].startswith("Message from 🤖 Orion")]

    check(len(inbox) == 1, 'inbox: Juniper has one session titled "Bot Inbox"', f"id {inbox[0]['id'] if inbox else None}, source {inbox[0]['source'] if inbox else None!r}")
    check(any("Message from 🤖 Orion" in text for _role, _kind, text in inbox_rows), "inbox: Orion's message ran there")
    check(any(role == "assistant" and "ran and exited 0" in text for role, _kind, text in inbox_rows), "inbox: Juniper's answer is there")
    check(rows("juniper", "Bot Chat") == main_before, "inbox: Juniper's main chat got no rows", f"{len(main_before)} rows before and after")
    check(not gw.of(live["juniper"], "message.start", since), "inbox: Juniper's open main chat never ran")
    check(bool(dm_calls) and all("message_agent" not in call["tools"] and not call["protocol"] for call in dm_calls),
          "inbox: the inbox turn has no message_agent and no messaging section", f"tools {dm_calls[0]['tools'] if dm_calls else None}")
    check(True, "inbox: the reply woke Orion's main chat", reply)

    # 2. Two senders at once queue on Juniper's turn lock.
    MODEL.slow = 4.0
    since = time.time()
    await asyncio.gather(
        gw.call("prompt.submit", {"session_id": live["orion"], "text": "Ask juniper: run `true` again and tell me the exit code."}),
        gw.call("prompt.submit", {"session_id": live["harbor"], "text": "Ask juniper: run `true` for me and tell me the exit code."}),
    )
    orion, harbor = await asyncio.gather(gw.answer(live["orion"], since, "asked by @orion"), gw.answer(live["harbor"], since, "asked by @harbor"))
    MODEL.slow = 0.0
    turns = sorted((call["start"], call["end"], call["user"][:30]) for call in MODEL.calls
                   if call["start"] >= since and call["user"].startswith("Message from 🤖") and "ran and exited" in call["decision"].get("text", ""))
    apart = len(turns) == 2 and turns[1][0] >= turns[0][1]

    check(apart, "queue: the two inbox turns ran one after the other",
          ", ".join(f"{who.split('(')[0].split()[-1]} {start - since:.1f}-{end - since:.1f}s" for start, end, who in turns))
    check(True, "queue: both senders got their answer", f"{orion!r} / {harbor!r}")
    check(rows("juniper", "Bot Chat") == main_before, "queue: Juniper's main chat still has no new rows")

    # 3. A dangerous command is denied, without waiting for anyone.
    VICTIM.mkdir(parents=True, exist_ok=True)
    (VICTIM / "keep.txt").write_text("keep", encoding="utf-8")
    since = time.time()
    await gw.call("prompt.submit", {"session_id": live["orion"], "text": "Ask juniper: please rm -rf the victim folder."})
    reply = await gw.answer(live["orion"], since, "terminal said")
    check((VICTIM / "keep.txt").exists(), "safety: rm -rf in the inbox turn was not run", reply[:160])
    check(not gw.requests, "safety: nothing asked a person", str(gw.requests)[:200])

    # 4. clarify cannot hold the turn.
    since = time.time()
    await gw.call("prompt.submit", {"session_id": live["harbor"], "text": "Ask juniper: clarify which file I mean."})
    reply = await gw.answer(live["harbor"], since, "clarify said", timeout=120)
    check(True, f"safety: clarify returned and the turn finished in {time.time() - since:.0f}s", reply[:160])
    check(rows("juniper", "Bot Chat") == main_before, "Juniper's main chat never got a row in this run")


async def run(tag: str, model_port: int, *, plugin: bool, switch_off: bool) -> None:
    print(f"\n{tag}")
    set_up(model_port, plugin=plugin, switch_off=switch_off)
    port, token = free_port(), secrets.token_urlsafe(24)
    marks = {path: path.stat().st_size for path in HOME.rglob("*.log")}
    proc = start_dashboard(port, token, tag.split()[0])

    try:
        async with Gateway(f"ws://127.0.0.1:{port}/api/ws?token={token}") as gw:
            live = {name: await gw.open_bot_chat(name) for name in BOTS}

            # A main chat is found as the live one by its stored row, which its first turn writes (a real bot's main
            # chat has a long history).
            for name, sid in live.items():
                since = time.time()
                await gw.call("prompt.submit", {"session_id": sid, "text": "Hello."})
                await gw.until(lambda: gw.of(sid, "message.complete", since), f"{name}'s first turn")

            if plugin and not switch_off:
                await inbox_run(gw, live)
            else:
                await as_hermes_does(gw, live, tag.split()[0])

        # This run's lines only: the home's logs carry the earlier runs too.
        logged = "\n".join(path.read_bytes()[marks.get(path, 0):].decode(errors="replace") for path in HOME.rglob("*.log"))
        lines = sorted({line.split("hermes-bot-fixes: ", 1)[1][:120] for line in logged.splitlines() if "hermes-bot-fixes: panels" in line})

        if plugin:
            check(bool(lines) and all(("bot inbox off" if switch_off else "bot inbox on") in line for line in lines),
                  "the plugin loaded with the inbox " + ("off" if switch_off else "on"), lines[0] if lines else "no log line")
    finally:
        stop(proc)


def main() -> None:
    for folder in (HOME, WORK, LOGS):
        folder.mkdir(parents=True, exist_ok=True)

    borrow_dependencies()
    model_port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", model_port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Hermes {CHECKOUT} in {HOME}")

    asyncio.run(run("baseline (no plugin): Hermes as it is", model_port, plugin=False, switch_off=False))
    asyncio.run(run("inbox (plugin on)", model_port, plugin=True, switch_off=False))
    asyncio.run(run("off (plugin on, HERMES_BOT_FIXES_BOT_INBOX=off)", model_port, plugin=True, switch_off=True))

    inbox = chats("juniper").get("Bot Inbox", [{}])[0]
    print(f'\nJuniper\'s inbox: title "Bot Inbox", source {inbox.get("source")!r}')
    print("OK")

    if not os.environ.get("KEEP"):
        shutil.rmtree(ROOT, ignore_errors=True)


if __name__ == "__main__":
    main()
