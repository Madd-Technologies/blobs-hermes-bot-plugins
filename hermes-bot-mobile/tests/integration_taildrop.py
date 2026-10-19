"""End-to-end check of "Taildrop from the server" on a Linux server with Tailscale. Not part of the unit tests.

Run it on the server, as the user Hermes runs as, with the Hermes venv (it has `cryptography`, and Hermes 0.21.5's own
download check, which every send goes through):

    ~/.hermes/hermes-agent/venv/bin/python hermes-bot-mobile/tests/integration_taildrop.py                  # read only
    ~/.hermes/hermes-agent/venv/bin/python hermes-bot-mobile/tests/integration_taildrop.py --send <device>  # sends a file

`--hermes <checkout>` picks another Hermes checkout (default `~/.hermes/hermes-agent`). HERMES_HOME is pointed at a
throwaway folder, so the real `~/.hermes` is never read or written, and the plugin's log goes to a temporary folder.

1. Read only (default). Tailscale is installed and connected, `tailscale file cp --targets` lists devices and parses,
   and `tailscale whois --json` parses for this server's own address. Nothing is sent.
2. `--send <device>` (a name or address from that list: pick one of your own). Sends one small text file, made in a
   temporary folder and checked by Hermes's own download check, and follows it to `done`. A user that is not
   Tailscale's operator gets `failed` with the one-time setup command to run. `--diff` also sends a short patch
   (`hermes-bot-taildrop-check.patch`) and checks its temporary folder is gone afterwards. The device gets one or two
   small files named `hermes-bot-taildrop-check.*`.

Then, from the app on a phone on the tailnet (the dashboard must run hermes-bot-mobile 0.1.6):

3. `GET /api/plugins/hermes-bot-mobile/taildrop`: `supported: true`, the targets, and `you` naming the phone.
4. Taildrop a file from the viewer: 202 with an id; `GET /taildrop/<id>` goes `running` (with `sent_bytes` rising for
   a large file) to `done`. A path the dashboard would not download (for example `~/.hermes/.env`) is refused with the
   download route's own 403.
5. Start a large file and `DELETE /taildrop/<id>`: the state turns `cancelled`.
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

THIS = Path(__file__).resolve()
sys.path.insert(0, str(THIS.parent.parent))

failures = []


def expect(ok, what, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {what}{f'  ({detail})' if detail and not ok else ''}")

    if not ok:
        failures.append(what)

    return ok


def follow(sender, send_id, seconds=120):
    deadline = time.time() + seconds
    last = None

    while time.time() < deadline:
        view = sender.progress(send_id)

        if (view["state"], view["sent_bytes"]) != last:
            print(f"      {view['state']}: {view['sent_bytes']} of {view['total_bytes']} bytes")
            last = (view["state"], view["sent_bytes"])

        if view["state"] != "running":
            return view

        time.sleep(0.5)

    return sender.progress(send_id)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--send", metavar="DEVICE", help="send a small file to this device (one of the listed targets)")
    parser.add_argument("--diff", action="store_true", help="with --send, also send a short patch")
    parser.add_argument("--hermes", default=str(Path.home() / ".hermes" / "hermes-agent"), help="the Hermes checkout whose download check is used")
    args = parser.parse_args()

    if not sys.platform.startswith("linux"):
        print("This needs a Linux server with Tailscale.")
        return 2

    work = Path(tempfile.mkdtemp(prefix="hermes-bot-taildrop-it-"))
    os.environ["HERMES_HOME"] = str(work / "home")
    (work / "home").mkdir()

    import taildrop

    sender = taildrop.Taildrop(work / "data")
    status = sender.status()

    if not expect(status["supported"], "Tailscale is installed, connected and offers Taildrop", status["why"] or ""):
        return 1

    targets = status["targets"]
    expect(bool(targets), "tailscale file cp --targets lists devices and they parse")

    for target in targets:
        print(f"      {target['name']:24} {target['ip']:28} {target['detail'] or 'online'}")

    own = json.loads(sender.system.run([sender.system.which("tailscale"), "status", "--json"]).stdout).get("Self", {}).get("TailscaleIPs") or []

    if own:
        me = sender.whois(own[0])
        expect(bool(me and me["name"]), "tailscale whois --json parses for this server's own address", json.dumps(me))

    if args.send:
        sys.path.insert(0, args.hermes)

        try:
            from hermes_cli.web_routers import files
        except Exception as failure:
            expect(False, "Hermes 0.21.5's download check can be loaded", str(failure))
            return 1

        sender = taildrop.Taildrop(work / "data", files=lambda: files)
        source = work / "outbox" / "hermes-bot-taildrop-check.txt"
        source.parent.mkdir()
        source.write_text(f"A small file Taildropped by hermes-bot-mobile's integration check at {time.ctime()}.\n")

        started = sender.send(args.send, source=asyncio.run(sender.check(str(source))))
        view = follow(sender, started["id"])

        if view.get("setup"):
            print(f"      this user is not Tailscale's operator; run once: {view['setup']['command']}")

        expect(view["state"] == "done", f"the file reached {args.send}", view.get("error") or "")

        if args.diff:
            started = sender.send(args.send, diff="--- a/check.txt\n+++ b/check.txt\n@@ -1 +1 @@\n-before\n+after\n", name="hermes-bot-taildrop-check")
            folder = sender._sends[started["id"]].cleanup
            view = follow(sender, started["id"])
            expect(view["state"] == "done" and view["name"] == "hermes-bot-taildrop-check.patch", "the patch reached it as hermes-bot-taildrop-check.patch", view.get("error") or "")
            expect(not folder.exists(), "and its temporary folder is gone")

        rows = json.loads((work / "data" / "taildrop.json").read_text())
        expect(all(str(work) not in json.dumps(row) for row in rows), "the log names files, never their paths")

    print("OK" if not failures else f"{len(failures)} check(s) failed")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
