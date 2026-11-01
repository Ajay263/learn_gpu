# Authentication and authorization

Code: `services/app/src/newsintel/api/auth.py`, `media_tokens.py`, `routes_user.py`.
Identity provider: Keycloak, realm `newsintel` (`infra/keycloak/realm-newsintel.json`).

## Model

| Concept | Where it lives | Meaning |
|---|---|---|
| **Role** (realm role) | `realm_access.roles` claim | *What* you may do. Hierarchical: `viewer` < `analyst` < `admin` |
| **Collection** (group) | `groups` claim | *Which videos* you may see. A video belongs to exactly one collection |
| **Audience** | `aud` claim (`api-audience` mapper) | Token was issued for this API, not for some other client |

| Role | Can |
|---|---|
| viewer | list/view videos, transcript, scenes, search, ask, play video |
| analyst | + generate clips, see processing status (`/jobs`), retry failed stages |
| admin | + read the audit log |

Collections are configured in `COLLECTIONS` (`Settings.collections`); a Keycloak group whose name is not
listed there is ignored, so a typo in Keycloak cannot accidentally grant access.

## Request flow

```
Browser/CLI ──(OIDC login, Keycloak)──► access token (RS256 JWT, 15 min)
     │
     ▼  Authorization: Bearer <jwt>
FastAPI  current_principal()
     ├─ fetch Keycloak signing key by `kid` (JWKS, cached 1 h, internal URL keycloak:8080)
     ├─ verify signature, exp/iat (30 s leeway), iss == OIDC_ISSUER, aud == OIDC_AUDIENCE
     └─ Principal(username, roles ∩ {viewer,analyst,admin}, groups ∩ COLLECTIONS)
     ▼
require("analyst")      → 403 + audit "denied" if role too low
visible_video(video_id) → 404 + audit "denied" if video not in principal.collections
search/ask              → collections passed into retrieval; filter applied inside every query
```

Design decisions:

- **404, not 403, for other collections' videos.** A 403 would confirm that the id exists.
- **Permission filter inside retrieval, not after it.** Rows from other collections never become
  candidates, so they cannot influence ranking or leak into LLM text (see `docs/ai/embeddings-and-search.md`).
- **Issuer vs JWKS URL.** Tokens carry the issuer the *browser* saw (`http://localhost:8081/...`,
  `KC_HOSTNAME`), but the API fetches keys over the internal network (`OIDC_JWKS_URL=http://keycloak:8080/...`).
  The issuer is still checked exactly.
- **Keycloak down ⇒ 503, not 401.** A JWKS fetch failure is an infrastructure fault, not a bad token.
  Keys are cached, so already-known keys keep working through a short Keycloak outage.

## Media URLs (video, keyframes, clips)

`<video src>` and `<img src>` cannot send an `Authorization` header. After the normal authz checks, the API
issues `/api/v1/media/<token>`, where the token is an HMAC-SHA256-signed `{bucket, key, subject, content_type,
expiry}` (`MEDIA_TOKEN_SECRET`, TTL = `PRESIGN_TTL_S`). The media endpoint checks only signature + expiry and
streams from MinIO with HTTP Range support (seeking works).

Why not MinIO presigned URLs? They would expose MinIO to browsers and embed its internal hostname. With
this design MinIO stays on the backend network; the API is the only public entry point.

Trade-off: a media URL works for anyone who has it until it expires (like a presigned URL). Keep the TTL short.

## Clients in the realm

| Client | Type | Flow | Use |
|---|---|---|---|
| `newsintel-web` | public, PKCE | authorization code | frontend (M10) |
| `newsintel-cli` | public | **password grant (direct access)** | **development/tests only** — disable in production |

## Development users (DEV ONLY)

`make dev-users` creates `analyst1` (analyst, collection-general), `viewer2` (viewer, collection-restricted)
and `admin1` (admin, both), all with password `DEV_USER_PASSWORD` from `.env`. Never run against production.

## Internal service authentication

| Caller → callee | Mechanism |
|---|---|
| NiFi → `/internal/ingestions` | `INGEST_SERVICE_TOKEN` (shared secret, constant-time compare) |
| app → inference gateway | `AI_INFERENCE_API_KEY` |
| operator → `newsintel-admin` CLI | `docker exec` access (host-level), `--operator NAME` recorded in audit |

## Audit

Every user action (`search`, `ask`, `video.play`, `clip.create`, `processing.retry`) and every denial
(`authz.<role>`, `video.access`) is written to `audit_events` (Postgres) with actor, request id and detail.
CLI actions are recorded as `actor=admin-cli:<name>`, `actor_type=system`. Read with `GET /api/v1/audit` (admin).

## Production checklist

- Disable direct access grants on `newsintel-cli` (or delete the client); delete dev users.
- Serve Keycloak over HTTPS; set `KC_HOSTNAME` and `OIDC_ISSUER` to the real public URL.
- Generate `MEDIA_TOKEN_SECRET` with `openssl rand -hex 32`; rotating it invalidates outstanding media links only.
- `AUTH_DISABLED` is refused when `APP_ENV=production` (`Settings.assert_safe_for_environment`).
- Consider shorter media-token TTL and binding tokens to a session if links must not be shareable.
