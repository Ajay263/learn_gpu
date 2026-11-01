"""Application settings.

Single source of configuration for API, worker and MCP server. Values come from environment
variables; for local development they may also come from the file named by NEWSINTEL_ENV_FILE
(default: ./.env). Nothing environment-specific (hosts, tunnel URLs, credentials, paths) is hardcoded:
the same code runs in Codespaces and on-premises with different environment values.
"""

import os
from functools import lru_cache
from typing import Literal
from urllib.parse import quote

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.environ.get("NEWSINTEL_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    # ── Object storage (MinIO / any S3-compatible store)
    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = Field(validation_alias=AliasChoices("S3_ACCESS_KEY", "MINIO_APP_ACCESS_KEY"))
    s3_secret_key: SecretStr = Field(validation_alias=AliasChoices("S3_SECRET_KEY", "MINIO_APP_SECRET_KEY"))
    s3_region: str = "us-east-1"
    bucket_landing: str = "landing"
    bucket_bronze: str = "bronze"
    bucket_derived: str = "derived"
    bucket_manifests: str = "manifests"
    bucket_evidence: str = "evidence"
    presign_ttl_s: int = Field(default=900, ge=60, le=86400)

    # ── Postgres
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    pixeltable_db_password: SecretStr
    app_db_password: SecretStr
    pixeltable_home: str = Field(default_factory=lambda: os.path.expanduser("~/.pixeltable"))
    pixeltable_dir: str = "news"

    # ── Queue
    rabbitmq_host: str = "localhost"
    rabbitmq_port: int = 5672
    rabbitmq_user: str
    rabbitmq_password: SecretStr
    rabbitmq_vhost: str = "/"

    # ── Inference (provider independent)
    ai_inference_base_url: str
    ai_inference_api_key: SecretStr
    ai_inference_timeout_s: float = 300.0
    ai_inference_connect_timeout_s: float = 10.0
    embed_text_dim: int = 1024
    embed_image_dim: int = 768

    # ── Auth
    oidc_issuer: str = "http://localhost:8081/realms/newsintel"
    oidc_jwks_url: str | None = None  # defaults to <issuer>/protocol/openid-connect/certs
    oidc_audience: str = "newsintel-api"
    ingest_service_token: SecretStr
    auth_disabled: bool = False  # tests only; refused when app_env=production
    media_token_secret: SecretStr = Field(default=SecretStr(""), description="HMAC key for media URLs (>=32 chars)")
    max_clip_s: float = Field(default=300.0, gt=0)
    # Access-control units. An upload at uploads/<user>/<collection>/<file> goes to <collection> if it is
    # listed here; otherwise to default_collection. Users get collections through Keycloak groups.
    collections: list[str] = ["collection-general", "collection-restricted"]
    default_collection: str = "collection-general"

    # ── Processing
    scratch_dir: str = Field(default_factory=lambda: os.path.join(os.path.expanduser("~"), ".cache", "newsintel"))
    max_upload_bytes: int = 1_073_741_824
    audio_chunk_s: int = Field(default=300, ge=30, le=1800)
    scene_threshold: float = 27.0
    min_scene_s: float = 2.0
    max_scenes_per_video: int = 400
    keyframe_max_px: int = 768
    caption_prompt: str = (
        "Describe this news video frame factually in one or two sentences: setting, people (by role, "
        "never by guessed name), on-screen graphics and visible actions. Do not speculate."
    )
    max_stage_attempts: int = Field(default=8, ge=1)

    @field_validator("ai_inference_base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    # ── Derived values
    @property
    def pixeltable_db_url(self) -> str:
        pw = quote(self.pixeltable_db_password.get_secret_value(), safe="")
        return f"postgresql://pixeltable:{pw}@{self.postgres_host}:{self.postgres_port}/pixeltable"

    @property
    def app_db_url(self) -> str:
        pw = quote(self.app_db_password.get_secret_value(), safe="")
        return f"postgresql://newsintel:{pw}@{self.postgres_host}:{self.postgres_port}/newsintel"

    @property
    def rabbitmq_url(self) -> str:
        pw = quote(self.rabbitmq_password.get_secret_value(), safe="")
        vhost = quote(self.rabbitmq_vhost, safe="")
        return f"amqp://{self.rabbitmq_user}:{pw}@{self.rabbitmq_host}:{self.rabbitmq_port}/{vhost}"

    @property
    def jwks_url(self) -> str:
        return self.oidc_jwks_url or f"{self.oidc_issuer}/protocol/openid-connect/certs"

    def assert_safe_for_environment(self) -> None:
        """Refuse dangerous combinations at startup rather than at request time."""
        if self.app_env == "production":
            if self.auth_disabled:
                raise RuntimeError("AUTH_DISABLED is not allowed in production")
            if self.ai_inference_base_url.endswith("trycloudflare.com"):
                raise RuntimeError("production must not depend on a Cloudflare quick tunnel")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
