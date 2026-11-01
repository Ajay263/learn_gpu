"""Durable job queue on RabbitMQ.

Topology (declared idempotently by `declare_topology`):

    exchange newsintel (direct)
      ├─ rk "stage"        → queue stage.jobs        (quorum, delivery-limit → DLX on poison messages)
      ├─ rk "retry.30s"    → queue stage.retry.30s   (TTL 30 s, dead-letters back to rk "stage")
      ├─ rk "retry.5m"     → queue stage.retry.5m    (TTL 5 min)
      └─ rk "retry.30m"    → queue stage.retry.30m   (TTL 30 min)
    exchange newsintel.dlx (direct)
      └─ rk "dead"         → queue stage.dead        (inspect / replay manually)

Delayed retry without plugins or polling: a message parked in a TTL queue expires and RabbitMQ
dead-letters it back onto the work queue. This is how jobs wait out a Kaggle session restart.
"""

import threading
from datetime import UTC, datetime

import pika
from pika.adapters.blocking_connection import BlockingChannel
from pydantic import BaseModel, Field

from newsintel.config import Settings
from newsintel.pipeline.stages import Stage

EXCHANGE = "newsintel"
DLX = "newsintel.dlx"
WORK_QUEUE = "stage.jobs"
DEAD_QUEUE = "stage.dead"
RETRY_TIERS: list[tuple[str, int]] = [("retry.30s", 30_000), ("retry.5m", 300_000), ("retry.30m", 1_800_000)]


class StageJob(BaseModel):
    video_id: str = Field(pattern=r"^vid_[0-9a-f]{24}$")
    stage: Stage
    attempt: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    reason: str | None = None


def retry_tier(attempt: int) -> str:
    """attempt 1-2 → 30 s, 3-4 → 5 min, ≥5 → 30 min. Total patience with 8 attempts ≈ 2 h."""
    if attempt <= 2:
        return RETRY_TIERS[0][0]
    if attempt <= 4:
        return RETRY_TIERS[1][0]
    return RETRY_TIERS[2][0]


def connect(settings: Settings) -> pika.BlockingConnection:
    params = pika.URLParameters(settings.rabbitmq_url)
    params.heartbeat = 60
    params.blocked_connection_timeout = 30
    params.connection_attempts = 5
    params.retry_delay = 2
    return pika.BlockingConnection(params)


def declare_topology(ch: BlockingChannel) -> None:
    ch.exchange_declare(EXCHANGE, exchange_type="direct", durable=True)
    ch.exchange_declare(DLX, exchange_type="direct", durable=True)
    ch.queue_declare(
        WORK_QUEUE,
        durable=True,
        arguments={
            "x-queue-type": "quorum",
            "x-delivery-limit": 5,  # a message that crashes the worker 5 times is dead-lettered
            "x-dead-letter-exchange": DLX,
            "x-dead-letter-routing-key": "dead",
        },
    )
    ch.queue_bind(WORK_QUEUE, EXCHANGE, routing_key="stage")
    for name, ttl in RETRY_TIERS:
        q = f"stage.{name}"
        ch.queue_declare(
            q,
            durable=True,
            arguments={"x-message-ttl": ttl, "x-dead-letter-exchange": EXCHANGE, "x-dead-letter-routing-key": "stage"},
        )
        ch.queue_bind(q, EXCHANGE, routing_key=name)
    ch.queue_declare(DEAD_QUEUE, durable=True, arguments={"x-queue-type": "quorum"})
    ch.queue_bind(DEAD_QUEUE, DLX, routing_key="dead")


_PROPS = pika.BasicProperties(content_type="application/json", delivery_mode=pika.DeliveryMode.Persistent)


class JobPublisher:
    """Publishes with publisher confirms: `publish` returns only after RabbitMQ has persisted the message.

    pika's BlockingConnection is not thread-safe; a lock makes one publisher safe to share across the
    API's thread pool.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.Lock()
        self._conn: pika.BlockingConnection | None = None
        self._ch: BlockingChannel | None = None

    def _channel(self) -> BlockingChannel:
        if self._conn is None or self._conn.is_closed or self._ch is None or self._ch.is_closed:
            self._conn = connect(self.settings)
            self._ch = self._conn.channel()
            declare_topology(self._ch)
            self._ch.confirm_delivery()
        return self._ch

    def _send(self, exchange: str, routing_key: str, job: StageJob) -> None:
        body = job.model_dump_json().encode()
        with self._lock:
            try:
                self._channel().basic_publish(exchange, routing_key, body, properties=_PROPS, mandatory=True)
            except (pika.exceptions.AMQPConnectionError, pika.exceptions.ChannelClosed):
                self._close()  # idle connections get dropped after missed heartbeats: reconnect once
                self._channel().basic_publish(exchange, routing_key, body, properties=_PROPS, mandatory=True)

    def publish(self, job: StageJob) -> None:
        self._send(EXCHANGE, "stage", job)

    def publish_retry(self, job: StageJob) -> str:
        tier = retry_tier(job.attempt - 1)
        self._send(EXCHANGE, tier, job)
        return tier

    def publish_dead(self, job: StageJob) -> None:
        self._send(DLX, "dead", job)

    def ping(self) -> bool:
        with self._lock:
            try:
                self._channel()
                return True
            except Exception:
                return False

    def close(self) -> None:
        with self._lock:
            self._close()

    def _close(self) -> None:
        try:
            if self._conn is not None and self._conn.is_open:
                self._conn.close()
        finally:
            self._conn, self._ch = None, None
