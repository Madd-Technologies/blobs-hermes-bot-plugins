"""The `paperless` skill hermes-paperless ships: how it is registered, its SKILL.md, and its script against a fake Paperless over
real HTTP (the script uses only the standard library, so any python3 on the server runs it).

    python -m unittest discover -s hermes-paperless/tests
"""

import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import yaml

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "paperless"
TOKEN = "test-token-not-real"
#: The app's pattern for a plugin link in a reply (its ROUTE_LINK pattern).
APP_LINK = re.compile(r"\[([^\]\n]+)\]\((/api/plugins/[A-Za-z0-9_-]+/[^)\s]+)\)")

DOCUMENTS = {
    15: {"id": 15, "title": "Dishwasher warranty", "created": "2025-01-15T00:00:00+00:00", "correspondent": 3, "page_count": 1, "content": "Dishwasher warranty. Valid until 2030-12-31. " * 400},
    16: {"id": 16, "title": "Lease [signed]\ncopy", "created": "2023-08-01", "correspondent": None, "page_count": 4, "content": ""},
}


def load(name: str, path: Path, package_dir: Path | None = None):
    spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=[str(package_dir)] if package_dir else None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)

    return module


find = load("hermes_paperless_find_test", SKILL / "scripts" / "find.py")
settings_module = load("hermes_paperless_settings_skill_test", ROOT / "paperless.py")


class FakePaperless(BaseHTTPRequestHandler):
    asked: list[str] = []

    def log_message(self, *args):
        pass

    def answer(self, status: int, body: object):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlsplit(self.path)
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        FakePaperless.asked.append(self.path)

        if self.headers.get("Authorization") != f"Token {TOKEN}":
            return self.answer(401, {"detail": "Invalid token."})

        if url.path == "/api/documents/" and query.get("query"):
            hits = [dict(DOCUMENTS[15], content="Dishwasher warranty…", __search_hit__={"score": 1.0, "highlights": 'Dishwasher <span class="match">warranty</span> valid until 2030-12-31'})]
            return self.answer(200, {"count": 3, "results": hits})

        if url.path == "/api/documents/" and query.get("ordering") == "-created":
            return self.answer(200, {"count": 2, "results": [dict(DOCUMENTS[15], content="Dishwasher…"), DOCUMENTS[16]]})

        if url.path == "/api/correspondents/":
            return self.answer(200, {"results": [{"id": 3, "name": "Acme Appliances"}]})

        match = re.fullmatch(r"/api/documents/(\d+)/", url.path)

        if match and int(match.group(1)) in DOCUMENTS:
            return self.answer(200, DOCUMENTS[int(match.group(1))])

        return self.answer(404, {"detail": "Not found."})


class Script(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakePaperless)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def run_find(self, *args: str, token: str = TOKEN, url: str | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()

        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"PAPERLESS_URL": url or self.url, "PAPERLESS_TOKEN": token, "HERMES_HOME": home}), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = find.main(list(args))

        return code, out.getvalue(), err.getvalue()

    def test_search_gives_links_the_app_opens_with_sender_and_where_it_matched(self):
        code, out, err = self.run_find("search", "dishwasher", "warranty")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("3 documents for “dishwasher warranty”, best match first (showing 1)", out)
        self.assertEqual(APP_LINK.findall(out), [("Dishwasher warranty", "/api/plugins/hermes-paperless/documents/15/preview")])
        self.assertIn("id 15 · 2025-01-15 · from Acme Appliances · 1 page", out)
        self.assertIn("“Dishwasher warranty valid until 2030-12-31”", out)
        self.assertNotIn(TOKEN, out)

    def test_recent_and_a_title_the_app_could_not_read_as_a_link_label(self):
        code, out, _ = self.run_find("recent", "--limit", "5")
        self.assertEqual(code, 0)
        self.assertEqual([label for label, _href in APP_LINK.findall(out)], ["Dishwasher warranty", "Lease (signed) copy"])
        self.assertIn("id 16 · 2023-08-01 · 4 pages", out)

    def test_text_is_capped_and_says_so(self):
        code, out, _ = self.run_find("text", "15", "--max-chars", "500")
        self.assertEqual(code, 0)
        self.assertIn("[Dishwasher warranty](/api/plugins/hermes-paperless/documents/15/preview)", out)
        self.assertIn(f"--- text: first 500 of {len(DOCUMENTS[15]['content'].strip())} characters ---", out)

        code, out, _ = self.run_find("text", "16")
        self.assertEqual(code, 0)
        self.assertIn("--- no text (Paperless found none) ---", out)

    def test_failures_read_plainly_and_never_show_the_token(self):
        code, out, err = self.run_find("search", "x", token="wrong-token")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("refused this server's token (HTTP 401)", err)
        self.assertNotIn("wrong-token", err)

        code, _, err = self.run_find("text", "99")
        self.assertEqual(code, 1)
        self.assertIn("no such document", err)

        code, _, err = self.run_find("recent", url="http://127.0.0.1:9")
        self.assertEqual(code, 1)
        self.assertIn("Can't reach Paperless at 127.0.0.1:9", err)

    def test_not_set_up_says_so(self):
        err = io.StringIO()

        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HERMES_HOME": home, **{name: "" for name in settings_module.URL_NAMES + settings_module.TOKEN_NAMES + settings_module.TOKEN_FILE_NAMES}}), contextlib.redirect_stderr(err):
            code = find.main(["recent"])

        self.assertEqual(code, 2)
        self.assertIn("Paperless is not set up on this server", err.getvalue())

    def test_runs_on_its_own_with_the_standard_library(self):
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "PAPERLESS_URL": self.url, "PAPERLESS_TOKEN": TOKEN, "HERMES_HOME": home}
            result = subprocess.run([sys.executable, "-I", str(SKILL / "scripts" / "find.py"), "search", "warranty"], capture_output=True, text=True, env=env, timeout=30)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("/api/plugins/hermes-paperless/documents/15/preview", result.stdout)


class SkillFile(unittest.TestCase):
    def test_frontmatter_matches_what_the_plugin_registers_and_names_its_script(self):
        content = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        frontmatter = yaml.safe_load(content.split("---")[1])
        self.assertEqual(frontmatter["name"], settings_module.SKILL_NAME)
        self.assertEqual(frontmatter["description"], settings_module.SKILL_DESCRIPTION)
        self.assertIn('python3 "${HERMES_SKILL_DIR}/scripts/find.py"', content)
        self.assertTrue((SKILL / "scripts" / "find.py").is_file())
        self.assertIn("(/api/plugins/hermes-paperless/documents/15/preview)", content)


class Register(unittest.TestCase):
    def register(self, ctx):
        package = load("hermes_paperless_pkg_skill_test", ROOT / "__init__.py", ROOT)

        with mock.patch.dict(os.environ, {"PAPERLESS_URL": "http://p", "PAPERLESS_TOKEN": TOKEN}):
            package.register(ctx)

    def test_registers_the_skill_and_points_new_sessions_at_it(self):
        skills, sections = [], []

        class Ctx:
            def register_skill(self, name, path, description=""):
                skills.append((name, Path(path), description))

            def register_system_prompt_section(self, id, content, **options):
                sections.append((id, content, options))

        self.register(Ctx())
        self.assertEqual(len(skills), 1)
        name, path, description = skills[0]
        self.assertEqual((name, description), (settings_module.SKILL_NAME, settings_module.SKILL_DESCRIPTION))
        self.assertTrue(path.is_file())
        self.assertIn("hermes-paperless:paperless", sections[0][1])
        self.assertIn("/api/plugins/hermes-paperless/documents/<id>/preview", sections[0][1])
        self.assertGreaterEqual(sections[0][2]["max_chars"], len(sections[0][1]))

    def test_a_skill_that_fails_to_register_leaves_the_link_hint_without_pointing_at_it(self):
        sections = []

        class Ctx:
            def register_skill(self, name, path, description=""):
                raise ValueError("no")

            def register_system_prompt_section(self, id, content, **options):
                sections.append(content)

        with self.assertLogs("hermes-paperless", level="WARNING"):
            self.register(Ctx())

        self.assertEqual(len(sections), 1)
        self.assertNotIn("hermes-paperless:paperless", sections[0])


if __name__ == "__main__":
    unittest.main()
