"""A real call to your own phone through your own Twilio number, before any bot makes one: the call service exactly as the
systemd unit runs it, on a throwaway HERMES_HOME, dialing one test errand (a septic pump-out quote) to the phone you name.
Answer it and play the company; push it for more than the brief holds, put it on hold with music, ask whether it's a
person. Or let it ring out to your voicemail. Nothing here reads or writes the real `~/.hermes`.

    <python with starlette, uvicorn and websockets> hermes-phone/tests/twilio_check.py --env FILE --public URL

- `--env FILE`: `KEY=value` lines (kept private, never printed):
  - `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER` (optionally `TWILIO_API_KEY` / `TWILIO_API_SECRET`), each
    plain or with the plugin's `HERMES_PHONE_` prefix (this file isn't Hermes's `.env`)
  - `CHECK_CALL`: the phone to call (yours; on a trial account it must be a verified number)
  - `XAI_API_KEY`, unless `--mint-cmd` gives Grok sessions instead (see `tests/live.py`)
- `--public URL`: the `https://` address that reaches `127.0.0.1:<--port>`, for example
  `tailscale funnel --bg --https=8443 8796` and `https://<machine>.<tailnet>.ts.net:8443`.
- `--out FILE`: the call's record (status, outcome, transcript; numbers masked) as JSON.

Questions the call asks you (`ask_owner`) can be answered by writing a line to `<home>/answer.txt` within a minute (the
path is printed); unanswered, the caller says it will get back to them. Ctrl-C hangs up.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import SETTINGS, septic  # noqa: E402  (also puts the plugin's folder on the path)

import brief as B  # noqa: E402
import deliver as D  # noqa: E402
import dialing  # noqa: E402
import grok as G  # noqa: E402
import persona as P  # noqa: E402
import phone_tools  # noqa: E402
import service as V  # noqa: E402
import store as S  # noqa: E402
import twilio_api as T  # noqa: E402

DEADLINE_S = 25 * 60


class Told:
    """Stands in for deliver.py: prints the push and the bot's line instead of sending them."""

    errand_line = staticmethod(D.errand_line)
    delivery_id = staticmethod(D.delivery_id)

    def __init__(self) -> None:
        self.pushes: List[str] = []
        self.bot_lines: List[str] = []

    def push(self, root, kind, *, profile, bot, body, key, url=None):
        self.pushes.append(f"{kind}: {body}")
        print(f"  push ({kind}): {body}", flush=True)

    def tell_bot(self, root, profile, line, key):
        self.bot_lines.append(line)
        print(f"  to the bot: {line}", flush=True)
        return "delivered"


class Minted:
    """Grok sessions from a mint command (one JSON line per line written), or the env file's XAI_API_KEY."""

    def __init__(self, mint_cmd: str, key: str):
        self.key = key
        self.minter = subprocess.Popen(shlex.split(mint_cmd), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1) if mint_cmd else None

    def bearer(self, root: Path, profile: str):
        return (self.key, "key" if self.key else "minted", "eve")

    def _mint(self) -> Dict[str, Any]:
        assert self.minter and self.minter.stdin and self.minter.stdout
        self.minter.stdin.write("mint\n")
        self.minter.stdin.flush()
        return json.loads(self.minter.stdout.readline() or '{"error": "the mint command ended"}')

    async def connect(self, token: str = ""):
        if not self.minter:
            return await G.connect(token=token)
        minted = await asyncio.to_thread(self._mint)
        if minted.get("error"):
            raise G.GrokError("mint", str(minted["error"]))
        return await G.connect(secret=minted["client_secret"], url=minted.get("url") or P.REALTIME_URL)

    def check(self) -> str:
        """Mints one session and drops it, so a broken minter shows before anyone's phone rings. '' when it works."""
        if not self.minter:
            return ""
        try:
            minted = self._mint()
        except (OSError, ValueError) as error:
            return type(error).__name__
        return str(minted.get("error") or "")

    def close(self) -> None:
        if self.minter and self.minter.stdin:
            try:
                self.minter.stdin.close()
                self.minter.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                self.minter.kill()


def account_kind(creds: Dict[str, str]) -> str:
    """'Trial' or 'Full' (a trial account plays Twilio's notice and waits for a key press before the call starts)."""
    auth = base64.b64encode(f"{creds['account_sid']}:{creds['auth_token']}".encode()).decode()
    request = urllib.request.Request(f"{T.API}/Accounts/{creds['account_sid']}.json", headers={"Authorization": f"Basic {auth}"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = json.loads(response.read().decode())
        return f"{body.get('type', '?')}, {body.get('status', '?')}"
    except Exception as error:
        return f"unknown ({getattr(error, 'code', type(error).__name__)})"


def answer_waiting(store: S.Store, errand_id: str, answer_file: Path) -> None:
    """A line written to answer.txt answers the question the call is waiting on, screened like the app's answer route."""
    if not answer_file.exists():
        return
    text = answer_file.read_text(encoding="utf-8").strip()
    answer_file.unlink()
    errand = store.errand(errand_id) or {}
    for call in errand.get("calls", []):
        if (call.get("question") or {}).get("answer") is None and call.get("question"):
            refused = B.screen(text, store.settings()["never_share"])
            if refused:
                print(f"  answer refused: {'; '.join(refused)}", flush=True)
                return
            store.update_call(errand_id, call["id"], lambda c: c["question"].update(answer=text))
            print(f"  answered: {text}", flush=True)


async def watch(store: S.Store, errand_id: str, answer_file: Path, stop: asyncio.Event) -> Dict[str, Any]:
    seen: Dict[str, Any] = {"status": None, "lines": 0, "question": None, "answered_by": "", "outcome": None, "hold": None}
    started = time.time()
    errand: Dict[str, Any] = {}
    while not stop.is_set() and time.time() - started < DEADLINE_S:
        await asyncio.sleep(0.5)
        answer_waiting(store, errand_id, answer_file)
        errand = store.errand(errand_id) or {}
        call = (errand.get("calls") or [{}])[0]
        if call.get("status") != seen["status"]:
            seen["status"] = call.get("status")
            print(f"[{time.strftime('%H:%M:%S')}] call: {seen['status']}{' · ' + call['error'] if call.get('error') else ''}", flush=True)
        if call.get("answered_by") and call["answered_by"] != seen["answered_by"]:
            seen["answered_by"] = call["answered_by"]
            print(f"  answered by: {seen['answered_by']}", flush=True)
        if call.get("hold") != seen["hold"]:
            seen["hold"] = call.get("hold")
            print(f"  hold: {seen['hold']}", flush=True)
        lines = call.get("transcript") or []
        for line in lines[seen["lines"]:]:
            print(f"  so far {line.get('who', '?'):>5}: {line.get('text', '')}", flush=True)
        seen["lines"] = len(lines)
        question = (call.get("question") or {}).get("text")
        if question and question != seen["question"]:
            seen["question"] = question
            print(f"  QUESTION for you: {question}\n  (answer within a minute: echo '...' > {answer_file})", flush=True)
        if call.get("outcome") != seen["outcome"]:
            seen["outcome"] = call.get("outcome")
            print(f"  outcome: {json.dumps(seen['outcome'])}", flush=True)
        if errand.get("status") in ("done", "cancelled") and errand.get("notified"):
            break
    return errand


def hang_up_active(store: S.Store, errand_id: str, creds: Dict[str, str]) -> bool:
    calls = [c for c in (store.errand(errand_id) or {}).get("calls", []) if c.get("sid") and c.get("status") in S.CALL_ACTIVE]
    for call in calls:
        try:
            T.Rest(creds["account_sid"], creds["auth_token"], creds["api_key"], creds["api_secret"]).hang_up(call["sid"])
        except Exception:
            pass  # already over
    return bool(calls)


def masked_record(errand: Dict[str, Any], told: Told) -> Dict[str, Any]:
    calls = []
    for call in errand.get("calls", []):
        calls.append({k: v for k, v in call.items() if k not in ("token", "sid", "number")} | {"number": dialing.masked(call["number"])})
    return {"status": errand.get("status"), "problem": errand.get("problem"), "calls": calls, "pushes": told.pushes, "bot_lines": told.bot_lines}


async def main_async(args: argparse.Namespace) -> int:
    env = T.read_env(Path(args.env).expanduser())
    keys = {T.PREFIX + name: env.get(T.PREFIX + name) or env.get(name, "") for name in T.NAMES}
    keys = {k: v for k, v in keys.items() if v}
    creds = T.credentials(keys)
    to = dialing.try_normalize(env.get("CHECK_CALL", ""))
    if not creds:
        print("The env file needs TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_PHONE_NUMBER.")
        return 2
    if not to:
        print("CHECK_CALL must be a US or Canadian number that may be dialed (see dialing.py).")
        return 2
    if not (args.mint_cmd or env.get("XAI_API_KEY")):
        print("Give --mint-cmd or put XAI_API_KEY in the env file.")
        return 2
    if not args.public.startswith("https://"):
        print("--public must be the https:// address that reaches this port.")
        return 2
    print(f"line {dialing.masked(creds['number'])} · calling {dialing.masked(to)} · account {account_kind(creds)}")

    grok = Minted(args.mint_cmd, env.get("XAI_API_KEY", "").strip())
    broken = grok.check()
    if broken:
        grok.close()
        print(f"The mint command doesn't work ({broken}); nothing was dialed.")
        return 2
    home = Path(tempfile.mkdtemp(prefix="hermes-phone-check-"))
    os.chmod(home, 0o700)
    told = Told()
    errand: Dict[str, Any] = {}
    service: Optional[V.Service] = None
    try:
        env_file = home / ".env"
        env_file.write_text("".join(f"{k}={v}\n" for k, v in keys.items()), encoding="utf-8")
        os.chmod(env_file, 0o600)
        store = S.Store(home)
        store.update_settings({"name": args.name, "test_numbers": [to], "stage": "test", "public_url": args.public, "port": args.port,
                               "hours": [0, 24], "daily_cap": 3, "max_minutes": args.minutes, "hold_minutes": min(args.minutes, 10),
                               "never_share": SETTINGS["never_share"]})

        tools = phone_tools.PhoneTools(root=home, profile=lambda: "default", pusher=told.push)
        made = json.loads(tools.phone_call(septic(calls=[{"name": "Pine Valley Septic", "number": to}]), session_id="twilio-check"))
        if made.get("error"):
            print("phone_call refused the brief:", made["error"])
            return 1
        errand_id = made["errand_id"]
        pending = store.errand(errand_id)
        brief, flags = B.check(pending["brief"], store.settings(), creds["number"])
        store.update_errand(errand_id, lambda e: e.update(brief=brief, flags=flags, ticked=sorted({f["fact"] for f in flags}), status="approved",
                                                          decided={"at": time.time(), "by": "twilio_check", "hash": S.brief_hash(brief)}))
        print(f"errand {errand_id} approved: {brief['title']} · flagged {len(flags)}")

        service = V.Service(root=home, grok_connect=grok.connect, bearer=grok.bearer, deliverer=told)
        import uvicorn

        server = uvicorn.Server(uvicorn.Config(service.app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning",
                                               proxy_headers=False, lifespan="on"))
        server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        serving = asyncio.ensure_future(server.serve())
        answer_file = home / "answer.txt"
        print(f"service on 127.0.0.1:{args.port}; answers go to {answer_file}")
        errand = await watch(store, errand_id, answer_file, stop)
        if hang_up_active(store, errand_id, creds):
            print("hanging up", flush=True)
            await asyncio.sleep(5)  # Twilio closes the stream, and the service writes the call down
            errand = store.errand(errand_id) or errand
        server.should_exit = True
        try:
            await asyncio.wait_for(serving, 15)
        except asyncio.TimeoutError:
            print("the service didn't stop in time", flush=True)
    finally:
        if errand.get("id"):
            hang_up_active(S.Store(home), errand["id"], creds)
        grok.close()
        shutil.rmtree(home, ignore_errors=True)

    record = masked_record(errand, told)
    if args.out:
        Path(args.out).write_text(json.dumps(record, indent=2), encoding="utf-8")
    call = (record["calls"] or [{}])[0]
    print("\nThe transcript:")
    for line in call.get("transcript") or []:
        print(f"  {line.get('t', 0):>6}s {line.get('who', '?'):>5}: {line.get('text', '')}")
    print(f"\nerrand {record['status']} · call {call.get('status')} · {json.dumps(call.get('outcome'))}")
    return 0 if call.get("answered_by") else 1  # answered by a person or a machine


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--env", required=True)
    parser.add_argument("--public", required=True)
    parser.add_argument("--port", type=int, default=8796)
    parser.add_argument("--mint-cmd", default="")
    parser.add_argument("--name", default="Alex")
    parser.add_argument("--minutes", type=int, default=8, help="the call's time limit (Twilio's a minute past it)")
    parser.add_argument("--out", default="")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
