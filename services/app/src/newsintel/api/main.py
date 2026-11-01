"""FastAPI application (Environment 4)."""

import argparse
import time
import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from newsintel.api import routes_internal, routes_user
from newsintel.api.auth import TokenVerifier
from newsintel.api.media_tokens import MediaTokens
from newsintel.api.services import Services
from newsintel.config import Settings, get_settings
from newsintel.logging import configure_logging, get_logger
from newsintel.metrics import HTTP_LATENCY

log = get_logger(__name__)


def create_app(settings: Settings | None = None, services: Services | None = None,
               verifier: TokenVerifier | None = None) -> FastAPI:  # fmt: skip
    settings = settings or get_settings()
    settings.assert_safe_for_environment()
    secret = settings.media_token_secret.get_secret_value()
    if len(secret) < 32:
        raise RuntimeError("MEDIA_TOKEN_SECRET must be set (>= 32 chars)")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.services = services or await run_in_threadpool(Services.build, settings)
        app.state.verifier = verifier or TokenVerifier(settings)
        app.state.media_tokens = MediaTokens(secret, ttl_s=settings.presign_ttl_s)
        log.info("api_started", env=settings.app_env)
        yield
        app.state.services.close()

    app = FastAPI(
        title="News Intelligence API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs" if settings.app_env != "production" else None,
        openapi_url="/api/openapi.json" if settings.app_env != "production" else None,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(request_id=rid)
        request.state.request_id = rid
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
        response.headers["X-Request-ID"] = rid
        return response

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict:
        """Liveness: the process is up."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["ops"])
    def readyz(request: Request):
        """Readiness: dependencies needed to serve requests. Inference is reported, not required."""
        svc: Services = request.app.state.services
        checks: dict[str, str] = {}
        try:
            svc.repo.list_videos(limit=1)
            checks["pixeltable"] = "ok"
        except Exception as e:  # noqa: BLE001
            checks["pixeltable"] = f"error: {type(e).__name__}"
        checks["rabbitmq"] = "ok" if svc.publisher.ping() else "error"
        try:
            svc.store.client.head_bucket(Bucket=svc.settings.bucket_bronze)
            checks["object_store"] = "ok"
        except Exception as e:  # noqa: BLE001
            checks["object_store"] = f"error: {type(e).__name__}"
        ok = all(v == "ok" for v in checks.values())
        return JSONResponse({"status": "ok" if ok else "degraded", "checks": checks}, status_code=200 if ok else 503)

    @app.middleware("http")
    async def metrics_mw(request: Request, call_next):
        t0 = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        HTTP_LATENCY.labels(request.method, getattr(route, "path", "unmatched"), str(response.status_code)).observe(
            time.perf_counter() - t0
        )
        return response

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    app.include_router(routes_internal.router)
    app.include_router(routes_user.router)
    app.include_router(routes_user.media_router)
    return app


def run() -> None:
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    settings = get_settings()
    configure_logging("api", settings.log_level, settings.log_format)
    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_config=None, proxy_headers=True)
