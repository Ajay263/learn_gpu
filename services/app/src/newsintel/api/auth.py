"""OIDC bearer-token authentication (Keycloak) and role/collection authorisation."""

import time
from dataclasses import dataclass, field

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from newsintel.config import Settings

ROLE_ORDER = {"viewer": 1, "analyst": 2, "admin": 3}
_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    subject: str
    username: str
    roles: frozenset[str]
    collections: tuple[str, ...]
    actor_type: str = "user"
    claims: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def level(self) -> int:
        return max((ROLE_ORDER.get(r, 0) for r in self.roles), default=0)

    def has_role(self, role: str) -> bool:
        return self.level >= ROLE_ORDER[role]


class TokenVerifier:
    def __init__(self, settings: Settings, jwks_client: jwt.PyJWKClient | None = None):
        self.settings = settings
        self._jwks = jwks_client or jwt.PyJWKClient(settings.jwks_url, cache_keys=True, lifespan=3600, timeout=5)

    def verify(self, token: str) -> Principal:
        try:
            key = self._jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.settings.oidc_audience,
                issuer=self.settings.oidc_issuer,
                options={"require": ["exp", "iat", "sub"]},
                leeway=30,
            )
        except jwt.PyJWKClientError as e:
            raise HTTPException(503, "identity provider unavailable") from e
        except jwt.InvalidTokenError as e:
            raise HTTPException(
                401, f"invalid token: {type(e).__name__}", headers={"WWW-Authenticate": "Bearer"}
            ) from e
        roles = frozenset(r for r in claims.get("realm_access", {}).get("roles", []) if r in ROLE_ORDER)
        groups = [g.lstrip("/") for g in claims.get("groups", [])]
        collections = tuple(g for g in groups if g in self.settings.collections)
        return Principal(
            subject=claims["sub"],
            username=claims.get("preferred_username", claims["sub"]),
            roles=roles,
            collections=collections,
            claims=claims,
        )


def current_principal(request: Request, creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Principal:
    svc = request.app.state.services
    if svc.settings.auth_disabled:  # test mode only; refused in production by Settings
        return Principal("test", "test", frozenset({"admin"}), tuple(svc.settings.collections))
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    principal = request.app.state.verifier.verify(creds.credentials)
    request.state.principal = principal
    return principal


def require(role: str):
    def dep(request: Request, p: Principal = Depends(current_principal)) -> Principal:
        if not p.has_role(role):
            request.app.state.services.audit.record(
                actor=p.username, actor_type=p.actor_type, action=f"authz.{role}", outcome="denied",
                request_id=getattr(request.state, "request_id", None), detail={"path": request.url.path},
            )  # fmt: skip
            raise HTTPException(403, f"requires role '{role}'")
        return p

    return dep


def now() -> int:
    return int(time.time())
