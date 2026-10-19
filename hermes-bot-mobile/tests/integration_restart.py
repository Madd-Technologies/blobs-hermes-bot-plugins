"""End-to-end check of "restart from the app" on a Linux server with systemd. Not part of the unit tests.

Run it on the server, as the user Hermes runs as, with a Python that has `cryptography` (the Hermes venv has it):

    ~/.hermes/hermes-agent/venv/bin/python hermes-bot-mobile/tests/integration_restart.py            # read only
    ~/.hermes/hermes-agent/venv/bin/python hermes-bot-mobile/tests/integration_restart.py --restart  # a throwaway unit

Neither touches Hermes. Each runs the plugin's restarter inside a throwaway transient unit of this user's systemd, with
its log in a temporary folder, and stops that unit when done:

1. Read only (default). Inside `hermes-bot-restart-check-<ts>`, the restarter must find that unit in its own cgroup, as
   a `--user` unit of this user, and reach it with `systemctl --user show`; it prints the `systemd-run` call it would
   make. Nothing is restarted.
2. `--restart`. `hermes-bot-restart-it-<ts>` asks the restarter to restart its own unit, exactly as the dashboard does
   (`systemd-run --user ... systemctl --user restart <unit>`). The check passes when the unit comes back as a new
   process within 40 s, and that new process reads the logged request as `done`. This is the part that cannot be faked
   on a computer without systemd: the transient timer fires outside the requesting process's cgroup, and the restart
   stops the process that asked for it without being cut off.

Then, on the real dashboard (it restarts Hermes's dashboard: pick a quiet moment). Authenticate the way you already do
for the dashboard API; the HermesBot app does it for you.

3. `GET /api/plugins/hermes-bot-mobile/restart`: `supported: true`, `unit` is the dashboard's unit (for example
   `hermes-dashboard.service`), `scope: "user"`, `busy` lists the chats running a turn, `gateway.route: true`.
4. `POST /api/plugins/hermes-bot-mobile/restart` with `{"when": "now"}`: 202. Within about 10 s the same GET answers
   with a later `started_at` and `last.outcome: "done"`; `journalctl --user -u hermes-bot-restart-<ts>` shows the
   restart. A second POST within a minute is a 429 with `retry_after`.
5. Start a long turn in a chat, then POST `{"when": "idle", "idle_timeout_s": 60}`: GET shows `last.outcome: "waiting"`
   and the chat in `busy`; the dashboard restarts after the turn ends, or `last.outcome` turns `timed_out` after 60 s.
   `DELETE` the same route while it waits: `{"cancelled": true}`.
6. POST `{"when": "now", "include_gateway": true}`: Hermes's own action log for `gateway-restart` gets a new run, the
   gateway's unit gets a new MainPID (`systemctl --user show -p MainPID <gateway unit>`), and only after that the
   dashboard restarts; GET's `last.gateway` says how it went.
"""

import argparse
import json
import os
import shutil
import subprocess
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


def inside(folder: str) -> None:
    """In the throwaway unit: what the restarter sees here."""
    import restart

    answer = restart.Restarter(Path(folder)).support()
    argv = None

    if answer["supported"]:
        argv = restart.schedule_argv(shutil.which("systemd-run"), shutil.which("systemctl"), answer["unit"], answer["scope"], int(time.time()))

    print(json.dumps({"support": answer, "cgroup": restart.System().cgroup(), "argv": argv}))


def child(folder: str) -> None:
    """In the throwaway unit, every time it starts: the first start asks for its own restart; the one after reports."""
    import restart

    folder_path = Path(folder)
    marker = folder_path / "starts.jsonl"
    earlier = marker.read_text().splitlines() if marker.exists() else []

    with open(marker, "a") as out:
        out.write(json.dumps({"pid": os.getpid(), "started_at": restart.System().started_at()}) + "\n")

    restarter = restart.Restarter(folder_path)

    if not earlier:
        try:
            answer = restarter.request("now")
        except restart.Refused as refusal:
            answer = {"refused": refusal.status, "detail": refusal.detail}

        (folder_path / "request.json").write_text(json.dumps(answer))
    else:
        (folder_path / "status.json").write_text(json.dumps(restarter.status()))

    time.sleep(120)  # the restart stops this process; the parent stops the unit after the second start


def check() -> None:
    unit = f"hermes-bot-restart-check-{int(time.time())}"

    with tempfile.TemporaryDirectory() as folder:
        result = subprocess.run(
            ["systemd-run", "--user", "--wait", "--pipe", "--collect", "--quiet", f"--unit={unit}", "--", sys.executable, str(THIS), "--inside", folder],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60,
        )

    expect(result.returncode == 0, "a throwaway unit ran under this user's systemd", result.stderr.strip())

    if result.returncode != 0:
        return

    seen = json.loads(result.stdout.strip().splitlines()[-1])
    support = seen["support"]
    print(f"      cgroup: {(seen['cgroup'] or '').strip()}")
    expect(support["unit"] == f"{unit}.service", "the restarter found its own unit in its cgroup", f"unit={support['unit']}")
    expect(support["scope"] == "user", "as a --user unit", f"scope={support['scope']}")
    expect(support["supported"], "and says it can restart it", support["why"] or "")

    if seen["argv"]:
        print(f"      would run: {' '.join(seen['argv'])}")


def restart_check(keep: bool) -> None:
    unit = f"hermes-bot-restart-it-{int(time.time())}"
    folder = Path(tempfile.mkdtemp(prefix="hermes-bot-restart-it-"))
    started = subprocess.run(
        ["systemd-run", "--user", "--collect", "--quiet", f"--unit={unit}", "--", sys.executable, str(THIS), "--child", str(folder)],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30,
    )
    expect(started.returncode == 0, "a throwaway unit started", started.stderr.strip())

    try:
        deadline = time.time() + 40

        while started.returncode == 0 and time.time() < deadline and not (folder / "status.json").exists():
            time.sleep(0.5)

        time.sleep(0.5)
    finally:
        subprocess.run(["systemctl", "--user", "stop", f"{unit}.service"], capture_output=True, stdin=subprocess.DEVNULL, timeout=90)

    def read(name):
        path = folder / name

        return json.loads(path.read_text()) if path.exists() else None

    request, status = read("request.json"), read("status.json")
    starts = [json.loads(line) for line in (folder / "starts.jsonl").read_text().splitlines()] if (folder / "starts.jsonl").exists() else []

    expect(bool(request and request.get("accepted")), "the unit asked for its own restart and it was accepted", json.dumps(request))
    expect(len(starts) >= 2 and starts[0]["pid"] != starts[1]["pid"], "the unit came back as a new process", f"{len(starts)} start(s)")

    if status:
        last = status.get("last") or {}
        expect(last.get("outcome") == "done", "the new process reads the request as done", f"outcome={last.get('outcome')}, detail={last.get('detail')}")
        expect(status.get("started_at") == starts[-1].get("started_at"), "and reports its own start time")
        print(f"      the restart ran as {last.get('transient_unit')}: journalctl --user -u {last.get('transient_unit')}")

    if keep:
        print(f"      kept {folder}")
    else:
        shutil.rmtree(folder, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--restart", action="store_true", help="also restart a throwaway unit end to end")
    parser.add_argument("--keep", action="store_true", help="keep the --restart run's temporary folder")
    parser.add_argument("--inside", help=argparse.SUPPRESS)
    parser.add_argument("--child", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.inside:
        inside(args.inside)
        return 0

    if args.child:
        child(args.child)
        return 0

    if not sys.platform.startswith("linux") or not shutil.which("systemd-run"):
        print("This needs a Linux server with systemd (systemd-run).")
        return 2

    check()

    if args.restart:
        restart_check(args.keep)

    print("OK" if not failures else f"{len(failures)} check(s) failed")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
