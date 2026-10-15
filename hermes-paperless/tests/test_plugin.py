"""hermes-paperless against a fake Paperless (an httpx mock transport): the routes, the settings, and what bots are told.

    python -m unittest discover -s hermes-paperless/tests
"""

import base64
import importlib.util
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "test-token-not-real"
PDF = b"%PDF-1.7\n" + bytes(range(256)) * 40


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)

    return module


settings_module = load("hermes_paperless_settings_test", ROOT / "paperless.py")


class FakePaperless:
    """Answers the few Paperless routes the plugin uses, and remembers what it was asked."""

    def __init__(self):
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)

        if request.headers.get("authorization") != f"Token {TOKEN}":
            return httpx.Response(401, json={"detail": "Invalid token."})

        path = request.url.path

        if path == "/api/documents/15/preview/":
            wanted = request.headers.get("range")

            if wanted:
                start, end = (int(part) for part in wanted.removeprefix("bytes=").split("-"))
                part = PDF[start : end + 1]

                return httpx.Response(206, content=part, headers={"content-type": "application/pdf", "content-range": f"bytes {start}-{end}/{len(PDF)}", "accept-ranges": "bytes", "content-length": str(len(part))})

            return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf", "content-length": str(len(PDF)), "set-cookie": "csrftoken=abc"})

        if path == "/api/documents/15/thumb/":
            return httpx.Response(200, content=b"RIFF....WEBP", headers={"content-type": "image/webp"})

        if path == "/api/documents/15/download/":
            return httpx.Response(200, content=b"original bytes", headers={"content-type": "image/jpeg"}) if request.url.params.get("original") == "true" else httpx.Response(400)

        if path == "/api/documents/15/":
            return httpx.Response(200, json={"id": 15, "title": "Dishwasher warranty", "created": "2025-01-15", "original_file_name": "warranty.pdf", "mime_type": "application/pdf", "page_count": 2, "content": "full text ..."})

        if path == "/api/documents/":
            hit = {"__search_hit__": {"highlights": '<span class="match">Dishwasher</span> warranty'}} if request.url.params.get("query") else {}
            return httpx.Response(200, json={"count": 2, "results": [{"id": 15, "title": "Dishwasher warranty", "created": "2025-01-15", "correspondent": 3, "content": "Warranty text", **hit}, {"id": 9, "title": "Acme receipt", "created": "2025-01-02", "correspondent": None}]})

        if path == "/api/correspondents/":
            return httpx.Response(200, json={"results": [{"id": 3, "name": "Acme Appliances"}]})

        return httpx.Response(404, json={"detail": "Not found."})


class Routes(unittest.TestCase):
    def setUp(self):
        self.api = load("hermes_dashboard_plugin_hermes-paperless-test", ROOT / "dashboard" / "plugin_api.py")
        self.paperless = FakePaperless()
        self.api.transport = httpx.MockTransport(self.paperless)
        self.env = mock.patch.dict(os.environ, {"PAPERLESS_URL": "paperless.lan:8000/api/", "PAPERLESS_TOKEN": TOKEN}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        app = FastAPI()
        app.include_router(self.api.router, prefix="/api/plugins/hermes-paperless")
        self.client = TestClient(app)
        self.base = "/api/plugins/hermes-paperless"

    def test_status_names_the_host_but_not_the_token(self):
        body = self.client.get(f"{self.base}/status").json()
        self.assertEqual(body, {"configured": True, "host": "paperless.lan:8000"})

    def test_preview_streams_the_pdf_inline_and_never_shows_the_token(self):
        response = self.client.get(f"{self.base}/documents/15/preview")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, PDF)
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertEqual(response.headers["content-disposition"], "inline")
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertNotIn("set-cookie", response.headers)
        self.assertNotIn(TOKEN, str(response.headers))
        sent = self.paperless.requests[-1]
        self.assertEqual(str(sent.url), "http://paperless.lan:8000/api/documents/15/preview/")

    def test_a_range_is_passed_on_and_answered_with_a_part(self):
        response = self.client.get(f"{self.base}/documents/15/preview", headers={"Range": "bytes=0-1023"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, PDF[:1024])
        self.assertEqual(response.headers["content-range"], f"bytes 0-1023/{len(PDF)}")
        self.assertEqual(self.paperless.requests[-1].headers["range"], "bytes=0-1023")

    def test_thumb_and_original(self):
        self.assertEqual(self.client.get(f"{self.base}/documents/15/thumb").headers["content-type"], "image/webp")
        self.assertEqual(self.client.get(f"{self.base}/documents/15/original").content, b"original bytes")

    def test_document_details_leave_out_the_text(self):
        body = self.client.get(f"{self.base}/documents/15").json()
        self.assertEqual(body, {"id": 15, "title": "Dishwasher warranty", "created": "2025-01-15", "original_file_name": "warranty.pdf", "mime_type": "application/pdf", "page_count": 2})

    def test_search_lists_each_hit_with_its_sender_and_where_it_matched(self):
        body = self.client.get(f"{self.base}/search", params={"q": "acme"}).json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["results"][0], {"id": 15, "title": "Dishwasher warranty", "created": "2025-01-15", "correspondent": "Acme Appliances", "snippet": "Dishwasher warranty"})
        self.assertEqual(body["results"][1]["correspondent"], None)
        asked = next(request for request in self.paperless.requests if request.url.path == "/api/documents/")
        self.assertEqual(asked.url.params["query"], "acme")
        self.assertEqual(asked.url.params["truncate_content"], "true")

    def test_recent_is_newest_first_with_the_start_of_the_text(self):
        body = self.client.get(f"{self.base}/recent", params={"limit": 5}).json()
        self.assertEqual([item["id"] for item in body["results"]], [15, 9])
        self.assertEqual(body["results"][0]["snippet"], "Warranty text")
        asked = next(request for request in self.paperless.requests if request.url.path == "/api/documents/")
        self.assertEqual((asked.url.params["ordering"], asked.url.params["page_size"]), ("-created", "5"))

    def test_preview_data_is_the_preview_as_a_data_url_and_refuses_a_huge_one(self):
        response = self.client.get(f"{self.base}/documents/15/preview-data")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["cache-control"])
        body = response.json()
        self.assertEqual((body["mime_type"], body["size"]), ("application/pdf", len(PDF)))
        self.assertEqual(base64.b64decode(body["data_url"].removeprefix("data:application/pdf;base64,")), PDF)

        with mock.patch.object(self.api, "DATA_URL_MAX_BYTES", 1000):
            self.assertEqual(self.client.get(f"{self.base}/documents/15/preview-data").status_code, 413)

        self.assertEqual(self.client.get(f"{self.base}/documents/99/preview-data").status_code, 404)

    def test_failures_read_plainly(self):
        self.assertEqual(self.client.get(f"{self.base}/documents/99/preview").status_code, 404)
        self.assertEqual(self.client.get(f"{self.base}/documents/abc/preview").status_code, 422)

        with mock.patch.dict(os.environ, {"PAPERLESS_TOKEN": "wrong"}):
            response = self.client.get(f"{self.base}/documents/15/preview")

        self.assertEqual(response.status_code, 502)
        self.assertIn("refused", response.json()["detail"])

    def test_unset_says_so(self):
        with mock.patch.object(self.api._settings_module(), "load_settings", lambda: None):
            self.assertEqual(self.client.get(f"{self.base}/documents/15/preview").status_code, 503)
            self.assertEqual(self.client.get(f"{self.base}/status").json(), {"configured": False, "host": None})


class Surfaces(unittest.TestCase):
    def test_the_tab_and_the_desktop_page_ship_and_name_this_plugin(self):
        import json

        manifest = json.loads((ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "hermes-paperless")
        self.assertFalse(manifest["tab"].get("hidden", False))
        self.assertEqual(manifest["tab"]["path"], "/paperless")
        self.assertIn(manifest["version"], (ROOT / "plugin.yaml").read_text(encoding="utf-8"))

        for key in ("entry", "css"):
            self.assertTrue((ROOT / "dashboard" / manifest[key]).is_file(), manifest[key])

        tab = (ROOT / "dashboard" / manifest["entry"]).read_text(encoding="utf-8")
        self.assertIn('register("hermes-paperless", PaperlessPage)', tab)
        self.assertNotIn("__HERMES_SESSION_TOKEN__", tab)  # the SDK's fetch handles sign-in

        desktop = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
        self.assertIn("id: 'hermes-paperless'", desktop)
        self.assertEqual(sorted(set(re.findall(r"from '([^']+)'", desktop))), ["@hermes/plugin-sdk", "react", "react/jsx-runtime"])


class Settings(unittest.TestCase):
    def test_reads_the_environment_first_then_the_env_file(self):
        with tempfile.TemporaryDirectory() as folder:
            env_file = Path(folder) / ".env"
            env_file.write_text('# keys\nexport PAPERLESS_API_URL="https://docs.example.net/"\nPAPERLESS_API_TOKEN=abc123 # mine\n')
            found = settings_module.load_settings({}, env_file)
            self.assertEqual((found.url, found.token), ("https://docs.example.net", "abc123"))

            from_env = settings_module.load_settings({"PAPERLESS_URL": "http://other:8000", "PAPERLESS_TOKEN": "t"}, env_file)
            self.assertEqual((from_env.url, from_env.token), ("http://other:8000", "t"))

    def test_reads_the_token_from_a_file_of_its_own(self):
        with tempfile.TemporaryDirectory() as folder:
            secret = Path(folder) / "paperless-token"
            secret.write_text("\n  file-token-xyz  \n")
            env_file = Path(folder) / ".env"
            env_file.write_text(f"PAPERLESS_URL=http://paperless.lan:8000\nPAPERLESS_TOKEN_FILE={secret}\n")
            found = settings_module.load_settings({}, env_file)
            self.assertEqual((found.url, found.token), ("http://paperless.lan:8000", "file-token-xyz"))

            self.assertIsNone(settings_module.load_settings({"PAPERLESS_URL": "http://p", "PAPERLESS_TOKEN_FILE": str(Path(folder) / "missing")}, Path(folder) / "none"))

    def test_missing_either_half_is_not_set_up(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(settings_module.load_settings({"PAPERLESS_URL": "http://p"}, Path(folder) / ".env"))


class Register(unittest.TestCase):
    def test_tells_new_sessions_how_to_link_when_set_up(self):
        sections = []

        class Ctx:
            def register_system_prompt_section(self, id, content, **options):
                sections.append((id, content, options))

        spec = importlib.util.spec_from_file_location("hermes_paperless_pkg_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
        package = importlib.util.module_from_spec(spec)
        sys.modules["hermes_paperless_pkg_test"] = package
        spec.loader.exec_module(package)

        with mock.patch.dict(os.environ, {"PAPERLESS_URL": "http://p", "PAPERLESS_TOKEN": TOKEN}):
            package.register(Ctx())

        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0][0], "hermes-paperless.links")
        self.assertIn("/api/plugins/hermes-paperless/documents/<id>/preview", sections[0][1])
        self.assertNotIn(TOKEN, sections[0][1])

        sections.clear()

        with mock.patch.dict(os.environ, {"PAPERLESS_URL": "", "PAPERLESS_TOKEN": ""}), mock.patch.object(sys.modules["hermes_paperless_pkg_test.paperless"], "hermes_home", lambda: Path("/nonexistent")):
            package.register(Ctx())

        self.assertEqual(sections, [])


if __name__ == "__main__":
    unittest.main()
