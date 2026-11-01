"""M9: JWT verification, role hierarchy, collection mapping, media tokens, clip bounds."""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from newsintel.api.auth import TokenVerifier
from newsintel.api.media_tokens import MediaTokenError, MediaTokens
from newsintel.clips import ClipError, clip_bounds
from newsintel.storage import ObjectRef

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class StubJWKS:
    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": KEY.public_key()})()


def token(settings, key=KEY, **overrides):
    now = int(time.time())
    claims = {
        "sub": "user-1", "preferred_username": "alice", "iat": now, "exp": now + 300,
        "iss": settings.oidc_issuer, "aud": settings.oidc_audience,
        "realm_access": {"roles": ["analyst", "offline_access"]},
        "groups": ["/collection-general", "unrelated-group"],
    }  # fmt: skip
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256")


def test_valid_token_maps_roles_and_collections(settings):
    p = TokenVerifier(settings, StubJWKS()).verify(token(settings))
    assert p.username == "alice" and p.roles == {"analyst"} and p.collections == ("collection-general",)
    assert p.has_role("viewer") and p.has_role("analyst") and not p.has_role("admin")


@pytest.mark.parametrize(
    "override",
    [{"aud": "some-other-api"}, {"iss": "https://evil.example/realms/newsintel"}, {"exp": int(time.time()) - 120}],
    ids=["wrong-audience", "wrong-issuer", "expired"],
)
def test_invalid_claims_are_rejected(settings, override):
    with pytest.raises(HTTPException) as e:
        TokenVerifier(settings, StubJWKS()).verify(token(settings, **override))
    assert e.value.status_code == 401


def test_token_signed_by_another_key_is_rejected(settings):
    with pytest.raises(HTTPException) as e:
        TokenVerifier(settings, StubJWKS()).verify(token(settings, key=OTHER_KEY))
    assert e.value.status_code == 401


def test_user_without_platform_roles_has_no_access_level(settings):
    p = TokenVerifier(settings, StubJWKS()).verify(token(settings, realm_access={"roles": ["offline_access"]}))
    assert p.level == 0 and not p.has_role("viewer")


def test_media_token_roundtrip_tamper_and_expiry():
    mt = MediaTokens("x" * 40, ttl_s=60)
    ref = ObjectRef("bronze", "videos/abc/original.mp4")
    tok = mt.issue(ref, "user-1", "video/mp4")
    assert mt.verify(tok) == (ref, "user-1", "video/mp4")
    body, sig = tok.split(".")
    forged_body = MediaTokens("y" * 40).issue(ObjectRef("bronze", "videos/OTHER/original.mp4"), "u", "video/mp4")
    with pytest.raises(MediaTokenError, match="signature"):
        mt.verify(forged_body.split(".")[0] + "." + sig)
    with pytest.raises(MediaTokenError, match="expired"):
        mt.verify(mt.issue(ref, "user-1", "video/mp4", ttl_s=-1))
    with pytest.raises(ValueError):
        MediaTokens("short")


def test_clip_bounds_validation():
    assert clip_bounds(5, 50, duration_s=30, max_clip_s=300) == (5, 30)  # clamped to duration
    for bad in [(10, 5), (-1, 5), (40, 50)]:
        with pytest.raises(ClipError):
            clip_bounds(*bad, duration_s=30, max_clip_s=300)
    with pytest.raises(ClipError, match="longer"):
        clip_bounds(0, 400, duration_s=1000, max_clip_s=300)
