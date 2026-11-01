"""Shared fixtures for tests that run against the docker compose stack (`make up-core`)."""

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
