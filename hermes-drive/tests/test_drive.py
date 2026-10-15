"""hermes-drive against fakes: the token handling, the routes (an httpx mock of Drive) and the skill's script (a real local HTTP
fake of Google's token endpoint and Drive), and what bots are told.

    python -m unittest discover -s hermes-drive/tests
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
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import httpx
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
SECRET = "client-secret-not-real"
REFRESH = "refresh-token-not-real"
FRESH = "fresh-access-not-real"
DOC = "1DocAbCdEfGhIjKlMnOp"
PDF_ID = "1PdfAbCdEfGhIjKlMnOp"
FOLDER_ID = "1FolderAbCdEfGhIjKlM"
PDF = b"%PDF-1.7\n" + bytes(range(256)) * 20
APP_LINK = re.compile(r"\[([^\]\n]+)\]\((/api/plugins/[A-Za-z0-9_-]+/[^)\s]+)\)")


def load(name: str, path: Path, package_dir: Path | None = None):
    spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=[str(package_dir)] if package_dir else None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)

    return module


core = load("hermes_drive_core_test", ROOT / "drive.py")
find = load("hermes_drive_find_test", ROOT / "skills" / "drive" / "scripts" / "find.py")


def token_file(home: Path, token_uri: str = "https://oauth2.googleapis.com/token", scopes=("https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/gmail.send"), expiry: str = "2000-01-01T00:00:00.000000Z") -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "google_token.json"
    path.write_text(json.dumps({"account": "alex@example.com", "client_id": "client-id.apps", "client_secret": SECRET, "refresh_token": REFRESH, "token": "stored-access-not-real", "expiry": expiry, "token_uri": token_uri, "scopes": list(scopes), "type": "authorized_user", "universe_domain": "googleapis.com"}), encoding="utf-8")

    return path


class Core(unittest.TestCase):
    def test_reads_the_skill_token_and_finds_it_in_the_main_home_for_a_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / ".hermes"
            token_file(root)
            profile = root / "profiles" / "orion"
            profile.mkdir(parents=True)

            with mock.patch.dict(os.environ, {"HERMES_HOME": str(profile)}):
                self.assertIn(root, core.homes())
                credentials = core.load_credentials()

        self.assertIsNotNone(credentials)
        self.assertTrue(credentials.drive)
        self.assertEqual(credentials.account, "alex@example.com")
        self.assertEqual(credentials.expiry, 946684800.0)
        self.assertNotIn(SECRET, repr(credentials))
        self.assertNotIn(REFRESH, repr(credentials))

    def test_no_token_or_no_drive_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(core.load_credentials([Path(folder)]))
            token_file(Path(folder), scopes=("https://www.googleapis.com/auth/calendar", "https://www.googleapis.com/auth/drive.file"))
            self.assertFalse(core.load_credentials([Path(folder)]).drive)

    def test_expiry_formats(self):
        self.assertEqual(core.parse_expiry("2025-01-01T12:00:00Z"), core.parse_expiry("2025-01-01T12:00:00+00:00"))
        self.assertIsNotNone(core.parse_expiry("2025-01-01T12:00:03.519291Z"))
        self.assertIsNone(core.parse_expiry("soon"))
        self.assertIsNone(core.parse_expiry(None))

    def test_search_escapes_quotes(self):
        self.assertEqual(core.search_query("Alex's \\ plan"), "fullText contains 'Alex\\'s \\\\ plan' and trashed = false")


class Cache(unittest.TestCase):
    def credentials(self, **overrides):
        values = {"client_id": "id", "client_secret": SECRET, "refresh_token": REFRESH, "token": "stored", "expiry": 2000.0, "token_uri": "https://oauth2.googleapis.com/token", "scopes": ("https://www.googleapis.com/auth/drive",), "account": None}
        return core.Credentials(**{**values, **overrides})

    def test_uses_the_stored_token_while_fresh_then_refreshes_in_memory(self):
        asked = []

        def opener(request, timeout):
            asked.append(parse_qs(request.data.decode()))
            return io.BytesIO(json.dumps({"access_token": FRESH, "expires_in": 3600}).encode())

        clock = [1000.0]
        cache = core.TokenCache(loader=lambda: self.credentials(), opener=opener, clock=lambda: clock[0])
        self.assertEqual(cache.get(), "stored")
        self.assertEqual(asked, [])
        clock[0] = 1990.0
        self.assertEqual(cache.get(), FRESH)
        self.assertEqual(asked[0]["grant_type"], ["refresh_token"])
        self.assertEqual(asked[0]["refresh_token"], [REFRESH])
        self.assertEqual(cache.get(), FRESH)
        self.assertEqual(len(asked), 1)
        self.assertEqual(cache.get(force=True), FRESH)
        self.assertEqual(len(asked), 2)

    def test_never_writes_the_token_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = token_file(Path(folder))
            before = path.read_bytes()
            cache = core.TokenCache(loader=lambda: core.load_credentials([Path(folder)]), opener=lambda request, timeout: io.BytesIO(json.dumps({"access_token": FRESH}).encode()))
            self.assertEqual(cache.get(), FRESH)
            self.assertEqual(path.read_bytes(), before)

    def test_failures_read_plainly_and_hold_no_secret(self):
        def refused(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"error": "invalid_grant"}'))

        with self.assertRaises(core.Failure) as caught:
            core.TokenCache(loader=lambda: self.credentials(expiry=None), opener=refused).get()
        self.assertIn("refused this server's sign-in", str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception))

        with self.assertRaises(core.Failure) as caught:
            core.TokenCache(loader=lambda: None).get()
        self.assertEqual(caught.exception.status, 503)

        with self.assertRaises(core.Failure) as caught:
            core.TokenCache(loader=lambda: self.credentials(scopes=("https://www.googleapis.com/auth/calendar",))).get()
        self.assertIn("no Drive access", str(caught.exception))

        with self.assertRaises(core.Failure) as caught:
            core.TokenCache(loader=lambda: self.credentials(expiry=None, token_uri="http://evil.example/token"), opener=refused).get()
        self.assertIn("refusing to send the client secret", str(caught.exception))


class FakeDrive:
    """Answers the Drive calls the routes make, and remembers them."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.reject_first = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, params = request.url.path, request.url.params

        if self.reject_first:
            self.reject_first = False
            return httpx.Response(401, json={"error": {"code": 401, "status": "UNAUTHENTICATED"}})

        if path == "/drive/v3/files":
            files = [{"id": DOC, "name": "Budget [2026]", "mimeType": "application/vnd.google-apps.spreadsheet", "modifiedTime": "2026-10-01T10:00:00.000Z", "owners": [{"displayName": "Alex"}]}, {"id": PDF_ID, "name": "Lease.pdf", "mimeType": "application/pdf", "size": str(len(PDF)), "modifiedTime": "2026-09-01T10:00:00.000Z"}]
            return httpx.Response(200, json={"files": files})

        meta = {DOC: ("Budget [2026]", "application/vnd.google-apps.spreadsheet"), PDF_ID: ("Lease.pdf", "application/pdf"), FOLDER_ID: ("Projects", "application/vnd.google-apps.folder")}
        match = re.fullmatch(r"/drive/v3/files/([^/]+)(/export)?", path)

        if not match or match.group(1) not in meta:
            return httpx.Response(404, json={"error": {"code": 404, "errors": [{"reason": "notFound"}]}})

        name, mime = meta[match.group(1)]

        if match.group(2):
            if params.get("mimeType") == "application/pdf" and match.group(1) == DOC:
                return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})
            return httpx.Response(403, json={"error": {"code": 403, "errors": [{"reason": "exportSizeLimitExceeded"}]}})

        if params.get("alt") == "media":
            wanted = request.headers.get("range")
            if wanted:
                start, end = (int(part) for part in wanted.removeprefix("bytes=").split("-"))
                return httpx.Response(206, content=PDF[start : end + 1], headers={"content-type": "application/pdf", "content-range": f"bytes {start}-{end}/{len(PDF)}", "accept-ranges": "bytes"})
            return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf", "content-length": str(len(PDF))})

        return httpx.Response(200, json={"id": match.group(1), "name": name, "mimeType": mime, "webViewLink": "https://docs.google.com/x"})


class StubCache:
    def __init__(self):
        self.calls: list[bool] = []

    def get(self, force=False):
        self.calls.append(force)
        return "access-2" if force else "access-1"

    def invalidate(self):
        pass


class Routes(unittest.TestCase):
    def setUp(self):
        self.api = load("hermes_dashboard_plugin_hermes-drive-test", ROOT / "dashboard" / "plugin_api.py")
        self.drive = FakeDrive()
        self.api.transport = httpx.MockTransport(self.drive)
        self.cache = StubCache()
        self.api.cache = self.cache
        app = FastAPI()
        app.include_router(self.api.router, prefix="/api/plugins/hermes-drive")
        self.client = TestClient(app)
        self.base = "/api/plugins/hermes-drive"

    def test_status_from_the_token_file_without_its_secrets(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"HERMES_HOME": folder}), mock.patch.object(Path, "home", lambda: Path(folder)):
            self.assertEqual(self.client.get(f"{self.base}/status").json(), {"configured": False, "drive": False, "account": None})
            token_file(Path(folder))
            body = self.client.get(f"{self.base}/status").json()

        self.assertEqual(body, {"configured": True, "drive": True, "account": "alex@example.com"})

    def test_search_and_recent_ask_drive_read_only_and_list_kinds(self):
        body = self.client.get(f"{self.base}/search", params={"q": "budget's"}).json()
        self.assertEqual(body["results"][0], {"id": DOC, "name": "Budget [2026]", "mime_type": "application/vnd.google-apps.spreadsheet", "kind": "Google Sheet", "modified": "2026-10-01T10:00:00.000Z", "owner": "Alex", "size": None})
        self.assertEqual(self.drive.requests[-1].url.params["q"], "fullText contains 'budget\\'s' and trashed = false")
        self.assertEqual(self.drive.requests[-1].headers["authorization"], "Bearer access-1")
        self.client.get(f"{self.base}/recent", params={"limit": 5})
        self.assertEqual((self.drive.requests[-1].url.params["orderBy"], self.drive.requests[-1].url.params["pageSize"]), ("modifiedTime desc", "5"))
        self.assertTrue(all(request.method == "GET" for request in self.drive.requests))

    def test_a_real_file_streams_with_ranges_and_a_google_file_comes_as_pdf(self):
        whole = self.client.get(f"{self.base}/files/{PDF_ID}/preview")
        self.assertEqual((whole.status_code, whole.content, whole.headers["content-disposition"]), (200, PDF, "inline"))
        part = self.client.get(f"{self.base}/files/{PDF_ID}/preview", headers={"Range": "bytes=0-99"})
        self.assertEqual((part.status_code, part.content), (206, PDF[:100]))
        sheet = self.client.get(f"{self.base}/files/{DOC}/preview")
        self.assertEqual((sheet.status_code, sheet.headers["content-type"]), (200, "application/pdf"))
        self.assertEqual(self.drive.requests[-1].url.params["mimeType"], "application/pdf")
        self.assertNotIn("range", self.drive.requests[-1].headers)

    def test_failures_read_plainly(self):
        self.assertEqual(self.client.get(f"{self.base}/files/{DOC}/original").status_code, 413)
        self.assertEqual(self.client.get(f"{self.base}/files/{FOLDER_ID}/preview").status_code, 415)
        self.assertEqual(self.client.get(f"{self.base}/files/1MissingAbCdEfGhIjK/preview").status_code, 404)
        self.assertEqual(self.client.get(f"{self.base}/files/not%20an%20id/preview").status_code, 400)

    def test_a_rejected_token_is_refreshed_once(self):
        self.drive.reject_first = True
        self.assertEqual(self.client.get(f"{self.base}/files/{PDF_ID}").status_code, 200)
        self.assertEqual(self.cache.calls, [False, True])
        self.assertEqual(self.drive.requests[-1].headers["authorization"], "Bearer access-2")

    def test_not_set_up_says_so(self):
        failing = mock.Mock()
        failing.get.side_effect = self.api._core().Failure("Google Drive is not set up on this server.", 503)
        self.api.cache = failing
        response = self.client.get(f"{self.base}/recent")
        self.assertEqual(response.status_code, 503)
        self.assertIn("not set up", response.json()["detail"])


class FakeGoogle(BaseHTTPRequestHandler):
    """Google's token endpoint and the Drive calls the script makes, over real HTTP."""

    def log_message(self, *args):
        pass

    def answer(self, status, body, kind="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        form = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        ok = form.get("refresh_token") == [REFRESH] and form.get("client_secret") == [SECRET]
        self.answer(200 if ok else 400, {"access_token": FRESH, "expires_in": 3600} if ok else {"error": "invalid_grant"})

    def do_GET(self):
        if self.headers.get("Authorization") != f"Bearer {FRESH}":
            return self.answer(401, {"error": {"code": 401}})

        url = urlsplit(self.path)
        params = {key: values[0] for key, values in parse_qs(url.query).items()}

        if url.path == "/drive/v3/files":
            return self.answer(200, {"files": [{"id": DOC, "name": "Plan\n[draft]", "mimeType": "application/vnd.google-apps.document", "modifiedTime": "2026-10-02T09:00:00Z", "owners": [{"displayName": "Alex"}]}]})

        if url.path == f"/drive/v3/files/{DOC}" and "alt" not in params:
            return self.answer(200, {"id": DOC, "name": "Plan [draft]", "mimeType": "application/vnd.google-apps.document"})

        if url.path == f"/drive/v3/files/{DOC}/export" and params.get("mimeType") == "text/plain":
            return self.answer(200, ("Plan text. " * 300).encode(), "text/plain")

        if url.path == f"/drive/v3/files/{PDF_ID}":
            return self.answer(200, {"id": PDF_ID, "name": "Lease.pdf", "mimeType": "application/pdf"})

        self.answer(404, {"error": {"code": 404, "errors": [{"reason": "notFound"}]}})


class Script(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGoogle)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def run_find(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()

        with tempfile.TemporaryDirectory() as home:
            token_file(Path(home), token_uri=f"{self.url}/token")

            with mock.patch.dict(os.environ, {"HERMES_HOME": home, "HERMES_DRIVE_API_BASE": f"{self.url}/drive/v3"}), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = find.main(list(args))

        return code, out.getvalue(), err.getvalue()

    def test_search_refreshes_the_token_and_gives_links_the_app_opens(self):
        code, out, err = self.run_find("search", "plan")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(APP_LINK.findall(out), [("Plan (draft)", f"/api/plugins/hermes-drive/files/{DOC}/preview")])
        self.assertIn("Google Doc · 2026-10-02 · owner Alex", out)
        for secret in (SECRET, REFRESH, FRESH):
            self.assertNotIn(secret, out)

    def test_text_of_a_google_doc_is_capped_and_a_pdf_has_none(self):
        code, out, _ = self.run_find("text", DOC, "--max-chars", "500")
        self.assertEqual(code, 0)
        self.assertIn("--- text: first 500 of", out)
        code, out, _ = self.run_find("text", PDF_ID)
        self.assertEqual(code, 0)
        self.assertIn("no text here for a pdf", out)

    def test_not_set_up_and_bad_ids_say_so(self):
        err = io.StringIO()

        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HERMES_HOME": home}), mock.patch.object(Path, "home", lambda: Path(home)), contextlib.redirect_stderr(err):
            self.assertEqual(find.main(["recent"]), 2)

        self.assertIn("not set up", err.getvalue())
        self.assertEqual(self.run_find("text", "../etc")[0], 2)

    def test_runs_on_its_own_with_the_standard_library(self):
        with tempfile.TemporaryDirectory() as home:
            token_file(Path(home), token_uri=f"{self.url}/token")
            env = {**os.environ, "HERMES_HOME": home, "HERMES_DRIVE_API_BASE": f"{self.url}/drive/v3"}
            result = subprocess.run([sys.executable, "-I", str(ROOT / "skills" / "drive" / "scripts" / "find.py"), "recent"], capture_output=True, text=True, env=env, timeout=30)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"/api/plugins/hermes-drive/files/{DOC}/preview", result.stdout)

    def test_the_api_override_only_takes_loopback(self):
        with mock.patch.dict(os.environ, {"HERMES_DRIVE_API_BASE": "https://evil.example/drive/v3"}):
            self.assertEqual(core.drive_api(), core.DRIVE_API)


class SkillAndRegister(unittest.TestCase):
    def test_skill_frontmatter_matches_what_is_registered(self):
        content = (ROOT / "skills" / "drive" / "SKILL.md").read_text(encoding="utf-8")
        frontmatter = yaml.safe_load(content.split("---")[1])
        self.assertEqual((frontmatter["name"], frontmatter["description"]), (core.SKILL_NAME, core.SKILL_DESCRIPTION))
        self.assertIn('python3 "${HERMES_SKILL_DIR}/scripts/find.py"', content)

    def register(self, ctx, home: Path):
        package = load("hermes_drive_pkg_test", ROOT / "__init__.py", ROOT)

        with mock.patch.dict(os.environ, {"HERMES_HOME": str(home)}), mock.patch.object(Path, "home", lambda: home):
            package.register(ctx)

    def test_registers_the_skill_and_the_hint_only_with_drive_access(self):
        skills, sections = [], []

        class Ctx:
            def register_skill(self, name, path, description=""):
                skills.append((name, Path(path).is_file()))

            def register_system_prompt_section(self, id, content, **options):
                sections.append((id, content))

        with tempfile.TemporaryDirectory() as folder:
            self.register(Ctx(), Path(folder))
            self.assertEqual((skills, sections), ([], []))
            token_file(Path(folder))
            self.register(Ctx(), Path(folder))

        self.assertEqual(skills, [("drive", True)])
        self.assertEqual(sections[0][0], "hermes-drive.links")
        self.assertIn("hermes-drive:drive", sections[0][1])
        self.assertIn("/api/plugins/hermes-drive/files/<id>/preview", sections[0][1])
        self.assertNotIn(SECRET, sections[0][1])


if __name__ == "__main__":
    unittest.main()
