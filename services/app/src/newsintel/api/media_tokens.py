"""Short-lived, HMAC-signed media URLs.

Browsers cannot attach an Authorization header to <video src> or <img src>. After the API has
authorised a user for a video, it issues a token binding (object, user, expiry). The media endpoint
verifies the signature and expiry only — so the token must never be issued without an authz check.
"""

import base64
import hashlib
import hmac
import json
import time

from newsintel.storage import ObjectRef


class MediaTokenError(ValueError):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class MediaTokens:
    def __init__(self, secret: str, ttl_s: int = 900):
        if len(secret) < 32:
            raise ValueError("media token secret must be >= 32 chars")
        self._key = hashlib.sha256(b"newsintel-media:" + secret.encode()).digest()
        self.ttl_s = ttl_s

    def issue(self, ref: ObjectRef, subject: str, content_type: str, ttl_s: int | None = None) -> str:
        payload = {
            "b": ref.bucket,
            "k": ref.key,
            "s": subject,
            "t": content_type,
            "e": int(time.time()) + (ttl_s or self.ttl_s),
        }
        body = _b64(json.dumps(payload, separators=(",", ":")).encode())
        sig = _b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest())
        return f"{body}.{sig}"

    def verify(self, token: str) -> tuple[ObjectRef, str, str]:
        try:
            body, sig = token.split(".", 1)
        except ValueError as e:
            raise MediaTokenError("malformed token") from e
        expected = _b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            raise MediaTokenError("bad signature")
        payload = json.loads(_unb64(body))
        if payload["e"] < time.time():
            raise MediaTokenError("expired")
        return ObjectRef(payload["b"], payload["k"]), payload["s"], payload["t"]
