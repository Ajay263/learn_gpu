"""Processing worker: consumes StageJob messages and runs one stage per message.

Reliability mechanics:
* prefetch=1 and manual ack: a message is acknowledged only after its outcome (done / retry scheduled /
  failed) is durably recorded. A crash before that → RabbitMQ redelivers (quorum delivery-limit guards
  against poison messages).
* The stage runs in a thread while this thread keeps servicing the AMQP connection, so heartbeats keep
  flowing during multi-minute ASR calls.
* Exceptions are classified: transient → re-publish to a TTL retry tier; permanent → FAILED (dependents
  get SKIPPED by the orchestrator); attempts exhausted → FAILED + copy to the dead-letter queue.
"""

import signal
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor

import pika
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from newsintel.config import Settings, get_settings
from newsintel.index.repository import Repository
from newsintel.inference import InferenceClient, InferenceRejected, InferenceUnavailable
from newsintel.logging import configure_logging, get_logger
from newsintel.media import MediaError
from newsintel.metrics import STAGE_DURATION, STAGE_OUTCOMES, start_metrics_server
from newsintel.pipeline.handlers import HANDLERS, Context, PermanentStageError
from newsintel.pipeline.orchestrator import advance
from newsintel.pipeline.stages import StageStatus, VideoStatus
from newsintel.queue import WORK_QUEUE, JobPublisher, StageJob, connect, declare_topology

log = get_logger(__name__)

TRANSIENT = (InferenceUnavailable, BotoCoreError, ClientError, TimeoutError, ConnectionError, OSError)
PERMANENT = (InferenceRejected, MediaError, PermanentStageError)
FINAL = {VideoStatus.INDEXED, VideoStatus.PARTIAL, VideoStatus.FAILED}


def classify(exc: BaseException) -> str:
    if isinstance(exc, PERMANENT):
        return "permanent"
    if isinstance(exc, TRANSIENT):
        return "transient"
    return "unknown"  # likely a bug: retried a few times, then dead-lettered for a human


class Worker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.ctx = Context(settings, Repository(settings), _store(settings), InferenceClient(settings))
        self.publisher = JobPublisher(settings)
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stage")
        self._stop = threading.Event()

    # ── single job ───────────────────────────────────────────────────────────
    def process(self, job: StageJob, wait) -> None:
        """Run one job to a recorded outcome. `wait(future)` services the connection until it completes."""
        repo = self.ctx.repo
        current = repo.stage_states(job.video_id).get(job.stage)
        if current in (StageStatus.DONE, StageStatus.SKIPPED):
            log.info("stage_already_settled", video_id=job.video_id, stage=job.stage, status=current)
            return
        repo.set_stage(job.video_id, job.stage, StageStatus.RUNNING, attempts=job.attempt)
        t0 = time.perf_counter()
        future = self.pool.submit(HANDLERS[job.stage], self.ctx, job.video_id)
        try:
            outcome = wait(future)
        except Exception as exc:  # noqa: BLE001 — classified below
            self._on_failure(job, exc, time.perf_counter() - t0)
            return
        elapsed = time.perf_counter() - t0
        STAGE_DURATION.labels(job.stage.value).observe(elapsed)
        STAGE_OUTCOMES.labels(job.stage.value, "done").inc()
        repo.set_stage(
            job.video_id,
            job.stage,
            StageStatus.DONE,
            model=outcome.model,
            detail={**outcome.detail, "elapsed_s": round(elapsed, 2)},
        )
        log.info("stage_done", video_id=job.video_id, stage=job.stage, elapsed_s=round(elapsed, 2), **outcome.detail)
        self._advance(job.video_id)

    def _on_failure(self, job: StageJob, exc: BaseException, elapsed: float) -> None:
        kind = classify(exc)
        err = f"{type(exc).__name__}: {exc}"
        limit = self.settings.max_stage_attempts if kind == "transient" else 3
        repo = self.ctx.repo
        if kind != "permanent" and job.attempt < limit:
            nxt = job.model_copy(update={"attempt": job.attempt + 1, "reason": err[:300]})
            repo.set_stage(job.video_id, job.stage, StageStatus.RETRYING, attempts=job.attempt, error=err)
            tier = self.publisher.publish_retry(nxt)
            STAGE_OUTCOMES.labels(job.stage.value, "retry").inc()
            log.warning(
                "stage_retry_scheduled",
                video_id=job.video_id,
                stage=job.stage,
                attempt=job.attempt,
                tier=tier,
                kind=kind,
                error=err[:300],
            )
            return
        repo.set_stage(job.video_id, job.stage, StageStatus.FAILED, attempts=job.attempt, error=err)
        STAGE_OUTCOMES.labels(job.stage.value, "failed").inc()
        if kind != "permanent":
            self.publisher.publish_dead(job.model_copy(update={"reason": err[:300]}))
        trace = "".join(traceback.format_exception(exc))[-2000:] if kind == "unknown" else None
        log.error(
            "stage_failed",
            video_id=job.video_id,
            stage=job.stage,
            attempt=job.attempt,
            kind=kind,
            error=err[:500],
            trace=trace,
        )
        self._advance(job.video_id)

    def _advance(self, video_id: str) -> None:
        advance(self.ctx.repo, self.publisher, video_id)
        status = VideoStatus(self.ctx.repo.get_video(video_id)["status"])
        if status in FINAL:
            self.ctx.cleanup(video_id)
            log.info("video_settled", video_id=video_id, status=status)

    # ── consume loop ─────────────────────────────────────────────────────────
    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self._consume()
            except pika.exceptions.AMQPConnectionError as e:
                log.warning("amqp_connection_lost", error=str(e))
                time.sleep(5)

    def _consume(self) -> None:
        conn = connect(self.settings)
        ch = conn.channel()
        declare_topology(ch)
        ch.basic_qos(prefetch_count=1)

        def wait(future: Future):
            while not future.done():
                conn.process_data_events(time_limit=1)  # keeps heartbeats alive during long stages
            return future.result()

        log.info("worker_consuming", queue=WORK_QUEUE)
        for method, _props, body in ch.consume(WORK_QUEUE, inactivity_timeout=1):
            if self._stop.is_set():
                break
            if method is None:
                continue
            try:
                job = StageJob.model_validate_json(body)
            except ValidationError as e:
                log.error("invalid_job_message", error=str(e)[:300])
                ch.basic_nack(method.delivery_tag, requeue=False)  # → DLX
                continue
            try:
                self.process(job, wait)
            except Exception as e:  # noqa: BLE001 — recording the outcome failed; let RabbitMQ redeliver
                log.error("job_outcome_not_recorded", video_id=job.video_id, stage=job.stage, error=str(e)[:300])
                ch.basic_nack(method.delivery_tag, requeue=True)
                time.sleep(5)
                continue
            ch.basic_ack(method.delivery_tag)
        ch.cancel()
        conn.close()

    def stop(self, *_):
        log.info("worker_stopping")
        self._stop.set()


def _store(settings: Settings):
    from newsintel.storage import ObjectStore

    return ObjectStore(settings)


def run() -> None:
    settings = get_settings()
    configure_logging("worker", settings.log_level, settings.log_format)
    settings.assert_safe_for_environment()
    start_metrics_server(9101)
    worker = Worker(settings)
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    worker.run_forever()
