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


@pytest.fixture(scope="module")
def root_s3(env, s3_factory):
    return s3_factory(env["MINIO_ROOT_USER"], env["MINIO_ROOT_PASSWORD"])


def integration_settings(**overrides):
    """Settings from the repo's .env (host view of the stack), with explicit overrides.

    The env file is passed explicitly: Settings reads NEWSINTEL_ENV_FILE only when newsintel.config is
    first imported, which in a full-tree run happens while collecting unit tests. Overrides are passed as
    arguments rather than via os.environ so they cannot leak into other test modules.
    """
    _load_env()  # skips when there is no .env
    from newsintel.config import Settings

    return Settings(_env_file=REPO_ROOT / ".env", **overrides)


@pytest.fixture(scope="module")
def repo():
    """Repository on the same Pixeltable directory the running services use."""
    from newsintel.index.repository import Repository

    return Repository(integration_settings())
