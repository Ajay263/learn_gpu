"""Milestone 2: configuration, object keys, inference client ↔ gateway contract, error taxonomy."""

import io
import wave

import httpx
import pytest
from newsintel_inference.contract import ChatMessage
from PIL import Image
from tenacity import wait_none

from newsintel.inference import (
    InferenceClient,
    InferenceRejected,
    InferenceUnavailable,
)
from newsintel.storage import Keys, ObjectRef, sha256_stream


@pytest.fixture(autouse=True)
def _no_retry_wait():
    InferenceClient._request.retry.wait = wait_none()  # type: ignore[attr-defined]


# ── configuration ────────────────────────────────────────────────────────────
def test_settings_build_urls_without_leaking_raw_password(settings, monkeypatch):
    monkeypatch.setenv("RABBITMQ_PASSWORD", "p@ss/word")
    from newsintel.config import Settings

    s = Settings()
    assert s.rabbitmq_url == "amqp://x:p%40ss%2Fword@localhost:5672/%2F"
    assert "test-secret" not in repr(s)  # SecretStr hides values in logs/reprs


def test_production_refuses_auth_disabled_and_quick_tunnels(settings, monkeypatch):
    from newsintel.config import Settings

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_DISABLED", "true")
    with pytest.raises(RuntimeError, match="AUTH_DISABLED"):
        Settings().assert_safe_for_environment()
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("AI_INFERENCE_BASE_URL", "https://abc.trycloudflare.com/")
    with pytest.raises(RuntimeError, match="Cloudflare"):
        Settings().assert_safe_for_environment()


# ── storage helpers ──────────────────────────────────────────────────────────
def test_object_ref_roundtrip_and_validation():
    ref = ObjectRef.parse("s3://bronze/videos/abc/original.mp4")
    assert (ref.bucket, ref.key, ref.uri) == (
        "bronze",
        "videos/abc/original.mp4",
        "s3://bronze/videos/abc/original.mp4",
    )
    for bad in ("http://x/y", "s3://bucketonly", "s3:///key"):
        with pytest.raises(ValueError):
            ObjectRef.parse(bad)


def test_keys_are_content_addressed_and_stable():
    assert Keys.original("ab" * 32, ".MP4") == f"videos/{'ab' * 32}/original.mp4"
    assert Keys.clip("v1", 1500, 9000) == "clips/v1/000001500-000009000.mp4"


def test_sha256_stream_matches_known_digest():
    assert sha256_stream(io.BytesIO(b"abc")) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


# ── client ↔ real gateway over HTTP ──────────────────────────────────────────
def _wav(seconds: float) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * seconds))
    return buf.getvalue()


def test_contract_roundtrip_against_gateway(gateway_settings):
    c = InferenceClient(gateway_settings)
    h = c.health()
    assert h.status == "ok" and h.embed_text_dim == 256 and h.capabilities["asr"].available

    asr = c.transcribe(_wav(25))
    assert [(s.start, s.end) for s in asr.segments] == [(0, 10), (10, 20), (20, 25)]
    assert asr.model.name == "fake/asr"

    emb = c.embed_texts(["payment issue", "payment problem", "weather forecast"])
    v = emb.vectors
    dot = lambda a, b: sum(x * y for x, y in zip(a, b, strict=True))  # noqa: E731
    assert emb.dim == 256 and dot(v[0], v[1]) > dot(v[0], v[2])

    red = Image.new("RGB", (64, 64), (220, 30, 30))
    assert "red" in c.caption([red], prompt="describe").captions[0]
    img_vec = c.embed_images([red]).vectors[0]
    q_red, q_blue = c.embed_image_queries(["a red studio", "blue sky"]).vectors
    assert dot(img_vec, q_red) > dot(img_vec, q_blue)

    rr = c.rerank("minister policy", ["sports results", "the minister announced a policy"])
    assert rr.results[0].index == 1

    chat = c.chat([ChatMessage(role="user", content="Evidence: [E3] something")], json_mode=True)
    assert '"E3"' in chat.content


def test_gateway_rejects_wrong_api_key(gateway_settings, monkeypatch):
    monkeypatch.setenv("AI_INFERENCE_API_KEY", "wrong-key-wrong-key")
    from newsintel.config import Settings

    with pytest.raises(InferenceUnavailable, match="401"):
        InferenceClient(Settings()).health()


def test_gateway_rejects_invalid_image(gateway_settings, fake_gateway):
    url, key = fake_gateway
    r = httpx.post(f"{url}/v1/embed/image", headers={"X-API-Key": key}, json={"images": ["bm90IGFuIGltYWdl"]})
    assert r.status_code == 422


# ── error taxonomy with a mock transport ─────────────────────────────────────
def _client_with(settings, handler) -> tuple[InferenceClient, list]:
    calls: list = []

    def wrapped(request):
        calls.append(request)
        return handler(request)

    return InferenceClient(settings, transport=httpx.MockTransport(wrapped)), calls


@pytest.mark.parametrize("status", [503, 530, 429])
def test_transient_statuses_retry_then_raise_unavailable(settings, status):
    c, calls = _client_with(settings, lambda r: httpx.Response(status, text="down"))
    with pytest.raises(InferenceUnavailable):
        c.health()
    assert len(calls) == 3


def test_validation_error_is_permanent_and_not_retried(settings):
    c, calls = _client_with(settings, lambda r: httpx.Response(422, json={"detail": "bad"}))
    with pytest.raises(InferenceRejected):
        c.rerank("q", ["d"])
    assert len(calls) == 1


def test_connection_error_is_unavailable(settings):
    def boom(request):
        raise httpx.ConnectError("refused")

    c, _ = _client_with(settings, boom)
    with pytest.raises(InferenceUnavailable, match="ConnectError"):
        c.health()


def test_circuit_breaker_opens_after_repeated_failures(settings):
    c, calls = _client_with(settings, lambda r: httpx.Response(503))
    for _ in range(2):
        with pytest.raises(InferenceUnavailable):
            c.health()
    n = len(calls)
    with pytest.raises(InferenceUnavailable, match="circuit open"):
        c.health()
    assert len(calls) == n  # no network call while open
