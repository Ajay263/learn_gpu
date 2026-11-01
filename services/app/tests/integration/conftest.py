"""Shared fixtures for tests that run against the docker compose stack (`make up-core`)."""

import os
from pathlib import Path

import boto3
import pytest
from botocore.config import Config

REPO_ROOT = Path(__file__).resolve().parents[4]


def _load_env() -> dict[str, str]:
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        pytest.skip("no .env — run `make env && make up-core`")
    values: dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


@pytest.fixture(scope="session")
def env() -> dict[str, str]:
    return _load_env()


@pytest.fixture(scope="session")
def s3_factory(env):
    def make(access_key: str, secret_key: str):
        return boto3.client(
            "s3",
            endpoint_url=env.get("S3_ENDPOINT_URL", "http://localhost:9000"),
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="us-east-1",
            config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1}),
        )

    return make


@pytest.fixture(scope="module")
def root_s3(env, s3_factory):
    return s3_factory(env["MINIO_ROOT_USER"], env["MINIO_ROOT_PASSWORD"])


@pytest.fixture(scope="module")
def repo():
    """Repository on the same Pixeltable directory the running services use."""
    os.environ["NEWSINTEL_ENV_FILE"] = str(REPO_ROOT / ".env")
    os.environ.pop("PIXELTABLE_DIR", None)
    from newsintel.config import Settings
    from newsintel.index.repository import Repository

    return Repository(Settings())
