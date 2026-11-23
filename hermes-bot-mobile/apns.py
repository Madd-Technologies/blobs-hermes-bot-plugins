"""Direct APNs HTTP/2 delivery and ES256 provider JWT helper for hermes-bot-mobile (stdlib-importable)."""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from pathlib import Path

try:
    from ._storage import JsonFile, default_folder
except ImportError:  # tests import modules flat
    from _storage import JsonFile, default_folder  # type: ignore[no-redef]

HEX_TOKEN = re.compile(r"^[0-9a-fA-F]{32,256}$")
APNS_ENVS = ("production", "sandbox")
TTL_DEFAULTS = {"approval": 300, "question": 3600, "blocked": 3600, "routine": 3600, "reply": 3600, "test": 60, "call": 3600, "call_question": 180}

_JWT_CACHE_LOCK = threading.Lock()
_JWT_CACHE: dict[tuple[str, str], tuple[int, str]] = {}


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def validate_apns_device(token: str, environment: str = "production") -> None:
    clean = str(token or "").strip()
    if not HEX_TOKEN.match(clean):
        raise ValueError("apns_token must be 32..256 hex characters")
    if environment not in APNS_ENVS:
        raise ValueError("apns_env must be production or sandbox")


def load_config(folder: Path | None = None) -> dict | None:
    """Loads APNs provider settings from `<folder>/apns.json` or `HERMES_APNS_*` environment variables."""
    base = Path(folder) if folder is not None else default_folder()
    stored = JsonFile(base / "apns.json", dict).read()

    key_id = str(os.environ.get("HERMES_APNS_KEY_ID") or stored.get("key_id") or "").strip()
    team_id = str(os.environ.get("HERMES_APNS_TEAM_ID") or stored.get("team_id") or "").strip()
    bundle_id = str(os.environ.get("HERMES_APNS_BUNDLE_ID") or stored.get("bundle_id") or "").strip()
    private_key = str(os.environ.get("HERMES_APNS_KEY_PEM") or stored.get("private_key") or "").strip()
    key_path = str(os.environ.get("HERMES_APNS_KEY_PATH") or stored.get("key_path") or "").strip()
    sandbox_raw = os.environ.get("HERMES_APNS_SANDBOX")
    sandbox = str(sandbox_raw).lower() in ("1", "true", "yes") if sandbox_raw is not None else bool(stored.get("sandbox", False))

    if not private_key and key_path:
        try:
            private_key = Path(key_path).expanduser().read_text(encoding="utf-8").strip()
        except OSError:
            private_key = ""

    if not (key_id and team_id and bundle_id and private_key):
        return None

    return {
        "key_id": key_id,
        "team_id": team_id,
        "bundle_id": bundle_id,
        "private_key": private_key,
        "sandbox": sandbox,
    }


def is_configured(folder: Path | None = None) -> bool:
    return load_config(folder) is not None


def make_jwt(team_id: str, key_id: str, private_key_pem: str, issued_at: int | None = None) -> str:
    """Creates (and caches for 50 min) an ES256 provider JWT with IEEE P1363 raw 64-byte `(r, s)` signature."""
    iat = int(issued_at if issued_at is not None else time.time())
    cache_key = (team_id, key_id)

    if issued_at is None:
        with _JWT_CACHE_LOCK:
            cached = _JWT_CACHE.get(cache_key)
            if cached and iat - cached[0] < 3000:
                return cached[1]

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    header = _b64u(json.dumps({"alg": "ES256", "kid": key_id}, separators=(",", ":")).encode("utf-8"))
    claims = _b64u(json.dumps({"iss": team_id, "iat": iat}, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header}.{claims}".encode("ascii")

    der_sig = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_sig)
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    token = f"{header}.{claims}.{_b64u(raw_sig)}"

    if issued_at is None:
        with _JWT_CACHE_LOCK:
            _JWT_CACHE[cache_key] = (iat, token)

    return token


def build_aps_payload(kind: str, payload: dict) -> dict:
    interruption = "time-sensitive" if kind in ("approval", "question", "call_question") else "active"
    aps: dict = {
        "alert": {
            "title": str(payload.get("title") or "Hermes"),
            "body": str(payload.get("body") or ""),
        },
        "sound": "default",
        "mutable-content": 1,
        "interruption-level": interruption,
    }
    if kind == "approval" or payload.get("approval"):
        aps["category"] = "APPROVAL"

    out = {
        "aps": aps,
        "kind": kind,
        "notification_id": payload.get("id"),
        "url": payload.get("url"),
    }
    if payload.get("actions"):
        out["actions"] = payload["actions"]
    if payload.get("approval"):
        out["approval"] = payload["approval"]

    return out


def _http2_post(url: str, headers: dict, content: bytes, timeout: float) -> tuple[int, str]:
    import httpx

    with httpx.Client(http2=True, timeout=timeout, follow_redirects=False) as client:
        response = client.post(url, headers=headers, content=content)
        return response.status_code, response.text


def send_apns(
    token: str,
    kind: str,
    payload: dict,
    config: dict,
    *,
    environment: str | None = None,
    poster=None,
    signer=None,
) -> tuple[int, str | None]:
    env = environment if environment in APNS_ENVS else ("sandbox" if config.get("sandbox") else "production")
    host = "api.sandbox.push.apple.com" if env == "sandbox" else "api.push.apple.com"
    url = f"https://{host}/3/device/{token}"

    jwt_token = (signer or make_jwt)(config["team_id"], config["key_id"], config["private_key"])
    ttl = TTL_DEFAULTS.get(kind, 3600)
    priority = "10" if kind in ("approval", "question", "call_question", "call") else "5"
    headers = {
        "authorization": f"bearer {jwt_token}",
        "apns-topic": str(config["bundle_id"]),
        "apns-push-type": "alert",
        "apns-priority": priority,
        "apns-expiration": str(int(time.time()) + ttl),
        "content-type": "application/json",
    }
    body = json.dumps(build_aps_payload(kind, payload), separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    result = (poster or _http2_post)(url, headers, body, 10.0)
    status = int(result[0]) if isinstance(result, tuple) else int(result)
    raw_text = result[1] if isinstance(result, tuple) and len(result) > 1 else ""

    if 200 <= status < 300:
        return status, None

    reason = ""
    if raw_text:
        try:
            reason = str(json.loads(raw_text).get("reason") or "")
        except ValueError:
            reason = raw_text[:120]

    if status == 410 or reason in ("BadDeviceToken", "Unregistered"):
        return 410, reason or "gone"

    return status, reason or f"http {status}"
