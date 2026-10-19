"""Asks Hermes's real `GET /api/fs/download` and the plugin's Taildrop check the same questions and prints both answers
as JSON, for tests/test_taildrop.py (HermesParity). Not a test on its own.

    python hermes-bot-mobile/tests/parity_taildrop.py <Hermes 0.21.5+ checkout>

HERMES_HOME is a throwaway folder, so the real ~/.hermes is never read or written.
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

tree = Path(sys.argv[1]).resolve()
home = Path(tempfile.mkdtemp(prefix="hermes-bot-taildrop-parity-home-"))
os.environ["HERMES_HOME"] = str(home)
sys.path.insert(0, str(tree))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import taildrop  # noqa: E402
from hermes_cli.web_routers import files  # noqa: E402

folder = Path(tempfile.mkdtemp(prefix="hermes-bot-taildrop-parity-")).resolve()
outside = Path(tempfile.mkdtemp(prefix="hermes-bot-taildrop-parity-out-")).resolve()
(folder / "ok.txt").write_text("fine\n")
(folder / "sub").mkdir()
(folder / ".env").write_text("SECRET=1\n")
(folder / "config.yaml").write_text("model: x\n")
(folder / "auth.json").write_text("{}\n")
(folder / "mcp-tokens").mkdir()
(folder / "mcp-tokens" / "server.json").write_text("{}\n")
(outside / "elsewhere.txt").write_text("outside the folder\n")
os.symlink(folder / ".env", folder / "notes.txt")
os.symlink(folder / "mcp-tokens" / "server.json", folder / "tokens.json")
os.symlink(outside / "elsewhere.txt", folder / "out-link.txt")
os.mkfifo(folder / "pipe")

CASES = [
    ("a plain file", str(folder / "ok.txt"), None, None),
    ("traversal back to a plain file", str(folder / "sub" / ".." / "ok.txt"), None, None),
    ("traversal to .env", str(folder / "sub" / ".." / ".env"), None, None),
    (".env", str(folder / ".env"), None, None),
    ("a link to .env", str(folder / "notes.txt"), None, None),
    ("a link into mcp-tokens", str(folder / "tokens.json"), None, None),
    ("a file in mcp-tokens", str(folder / "mcp-tokens" / "server.json"), None, None),
    ("config.yaml", str(folder / "config.yaml"), None, None),
    ("auth.json", str(folder / "auth.json"), None, None),
    ("a link out of the folder", str(folder / "out-link.txt"), None, None),
    ("a folder", str(folder / "sub"), None, None),
    ("a fifo", str(folder / "pipe"), None, None),
    ("missing", str(folder / "missing.txt"), None, None),
    ("empty", "", None, None),
    ("a NUL byte", f"{folder}/ok\0.txt", None, None),
    ("a file URL", (folder / "ok.txt").as_uri(), None, None),
    ("a file URL naming another host", "file://elsewhere.example/etc/hostname", None, None),
    # `profile` only has to exist (Hermes's route does not resolve the path against it); `default` always does.
    ("a known profile", str(folder / "ok.txt"), "default", None),
    ("an unknown profile", str(folder / "ok.txt"), "zz-nobody", None),
    ("a profile name that is a path", str(folder / "ok.txt"), "../zz-scout", None),
    ("an unknown chat", "ok.txt", None, "20990101_000000_nope"),
]


def short(path):
    return Path(path).name


async def hermes(path, profile, session_id):
    try:
        response = await files.fs_download(path, profile, session_id)
    except Exception as failure:  # HTTPException, or anything else the route lets out
        return {"status": getattr(failure, "status_code", 500), "detail": str(getattr(failure, "detail", failure))}

    return {"ok": short(response.path)}


async def plugin(path, profile, session_id):
    try:
        target, _ = await taildrop.check_file(files, path, profile, session_id)
    except taildrop.Refused as refusal:
        return {"status": refusal.status, "detail": refusal.detail}

    return {"ok": short(target)}


async def main():
    results = []

    for case, path, profile, session_id in CASES:
        results.append({"case": case, "hermes": await hermes(path, profile, session_id), "plugin": await plugin(path, profile, session_id)})

    print(json.dumps(results))


try:
    asyncio.run(main())
finally:
    for made in (folder, outside, home):
        shutil.rmtree(made, ignore_errors=True)
