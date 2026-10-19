"""Twilio, the little this plugin needs, without the Twilio library: request signatures, TwiML and two REST calls.

Signatures: X-Twilio-Signature is base64(HMAC-SHA1(Auth Token, URL + each POST parameter's name and value, sorted by
name)). Voice callbacks over HTTPS are signed with the port dropped, and the Media Streams handshake is signed over the
`wss://` URL exactly as our TwiML wrote it (sometimes with a trailing slash), with no parameters. So `valid` tries those
forms of a URL this service built itself, never one rebuilt from the request's Host header.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from xml.sax.saxutils import quoteattr

try:
    from . import dialing
except ImportError:  # the tests import the modules flat
    import dialing  # type: ignore[no-redef]

API = "https://api.twilio.com/2010-04-01"

#: (url, form fields as a list of pairs, basic-auth user, password, timeout) -> (status, parsed JSON body or None)
Poster = Callable[[str, List[Tuple[str, str]], str, str, float], Tuple[int, Any]]


def signature(token: str, url: str, params: Mapping[str, str]) -> str:
    payload = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()).decode()


def _without_port(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    return urllib.parse.urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment)) if parts.port else url


def valid(token: str, url: str, params: Mapping[str, str], header: Optional[str]) -> bool:
    """Whether `header` is Twilio's signature of this request, for the URL as we published it."""
    if not token or not header:
        return False

    forms = []
    for candidate in (url, _without_port(url)):
        for form in (candidate, candidate.rstrip("/") + "/"):
            if form not in forms:
                forms.append(form)

    return any(hmac.compare_digest(signature(token, form, params), header) for form in forms)


def stream_twiml(ws_url: str) -> str:
    """Connect the call to our WebSocket; when we close it, the call hangs up."""
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Connect><Stream url={quoteattr(ws_url)}/></Connect><Hangup/></Response>'


def reject_twiml() -> str:
    """Turn a call away before answering: the caller hears busy, and Twilio doesn't bill it."""
    return '<?xml version="1.0" encoding="UTF-8"?><Response><Reject/></Response>'


def post_form(url: str, fields: List[Tuple[str, str]], user: str, password: str, timeout: float) -> Tuple[int, Any]:
    data = urllib.parse.urlencode(fields).encode()
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    request = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read().decode() or "{}")
        except ValueError:
            return error.code, None


class TwilioError(Exception):
    def __init__(self, message: str, status: int = 0, code: Any = None):
        super().__init__(message)
        self.status = status
        self.code = code


class Rest:
    """Places and ends calls. An API key (SID + secret) is used when the server has one, otherwise the Auth Token."""

    def __init__(self, account_sid: str, auth_token: str, api_key: str = "", api_secret: str = "", post: Optional[Poster] = None):
        self.account = account_sid
        self.user, self.password = (api_key, api_secret) if api_key and api_secret else (account_sid, auth_token)
        self._post = post or post_form

    def _call(self, path: str, fields: List[Tuple[str, str]]) -> Dict[str, Any]:
        try:
            status, body = self._post(f"{API}/Accounts/{self.account}/{path}", fields, self.user, self.password, 20.0)
        except Exception as error:  # DNS, TLS, timeout
            raise TwilioError(f"Twilio could not be reached ({type(error).__name__}).") from None
        if status >= 400 or not isinstance(body, dict):
            body = body if isinstance(body, dict) else {}
            # Twilio's message names the problem (a Geo Permission, an unverified number); it never echoes credentials.
            raise TwilioError(str(body.get("message") or f"Twilio refused ({status})")[:300], status, body.get("code"))
        return body

    def create_call(self, to: str, from_: str, twiml: str, status_url: str, amd_url: str, time_limit_s: int, ring_s: int = 30) -> str:
        fields = [
            ("To", to), ("From", from_), ("Twiml", twiml),
            ("StatusCallback", status_url), ("StatusCallbackMethod", "POST"),
            *[("StatusCallbackEvent", event) for event in ("initiated", "ringing", "answered", "completed")],
            ("MachineDetection", "DetectMessageEnd"), ("AsyncAmd", "true"),
            ("AsyncAmdStatusCallback", amd_url), ("AsyncAmdStatusCallbackMethod", "POST"),
            ("Timeout", str(ring_s)),
            # Twilio's own cap, a minute past ours: a call can't outlive the service by accident.
            ("TimeLimit", str(time_limit_s)),
        ]
        return str(self._call("Calls.json", fields).get("sid") or "")

    def hang_up(self, sid: str) -> None:
        if sid:
            self._call(f"Calls/{sid}.json", [("Status", "completed")])


def read_env(path) -> Dict[str, str]:
    """KEY=value lines from a Hermes `.env` (quotes and `export ` allowed). Values are never logged."""
    out: Dict[str, str] = {}

    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key] = value

    return out


def credentials(env: Mapping[str, str]) -> Optional[Dict[str, str]]:
    """The Twilio settings this line needs, or None when the server doesn't have them yet."""
    sid, token, number = (env.get(k, "").strip() for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"))
    if not (sid and token and number):
        return None
    return {"account_sid": sid, "auth_token": token, "number": dialing.try_normalize(number) or number,
            "api_key": env.get("TWILIO_API_KEY", "").strip(), "api_secret": env.get("TWILIO_API_SECRET", "").strip()}
