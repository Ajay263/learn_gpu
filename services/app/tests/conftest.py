"""Unit-test defaults: a complete, fake configuration so Settings() never reads a developer's .env."""

import os
import socket
import threading
import time

import pytest

TEST_ENV = {
    "NEWSINTEL_ENV_FILE": "/nonexistent",
    "APP_ENV": "test",
    "S3_ACCESS_KEY": "test-access",
    "S3_SECRET_KEY": "test-secret",
    "PIXELTABLE_DB_PASSWORD": "x",
    "APP_DB_PASSWORD": "x",
    "RABBITMQ_USER": "x",
    "RABBITMQ_PASSWORD": "x",
    "AI_INFERENCE_BASE_URL": "http://127.0.0.1:1",
    "AI_INFERENCE_API_KEY": "test-key-0123456789abcdef",
    "INGEST_SERVICE_TOKEN": "test-ingest-token",
    "EMBED_TEXT_DIM": "256",
    "EMBED_IMAGE_DIM": "64",
}


@pytest.fixture
def settings(monkeypatch):
    for k, v in TEST_ENV.items():
        monkeypatch.setenv(k, v)
    from newsintel.config import Settings, get_settings

    get_settings.cache_clear()
    return Settings()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def fake_gateway():
    """Real inference gateway (fake backend) served over HTTP on a random port."""
    import uvicorn
    from newsintel_inference.backends.fake import FakeBackend
    from newsintel_inference.server import create_app

    port = _free_port()
    key = TEST_ENV["AI_INFERENCE_API_KEY"]
    app = create_app(backend=FakeBackend(embed_text_dim=256, embed_image_dim=64), api_key=key)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("fake gateway did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", key
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def gateway_settings(settings, fake_gateway, monkeypatch):
    url, _ = fake_gateway
    monkeypatch.setenv("AI_INFERENCE_BASE_URL", url)
    from newsintel.config import Settings

    return Settings()


os.environ.setdefault("NEWSINTEL_ENV_FILE", "/nonexistent")
