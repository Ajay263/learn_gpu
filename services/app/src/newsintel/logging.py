"""Structured logging (JSON lines by default) via structlog.

Every log line carries `service`, and any context bound with `bind_contextvars` (e.g. video_id,
job_id, request_id), so one `grep video_id=...` or Loki query follows an item across services.
"""

import logging
import sys

import structlog

_SECRET_KEYS = {"password", "secret", "token", "api_key", "authorization", "access_key", "secret_key"}


def _redact(_, __, event_dict: dict) -> dict:
    for k in list(event_dict):
        if any(s in k.lower() for s in _SECRET_KEYS):
            event_dict[k] = "***"
    return event_dict


def configure_logging(service: str, level: str = "INFO", fmt: str = "json") -> None:
    renderer = structlog.processors.JSONRenderer() if fmt == "json" else structlog.dev.ConsoleRenderer()
    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact,
    ]
    structlog.configure(
        processors=[*shared, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(service=service)
    # Route stdlib logging (uvicorn, pika, pixeltable) through the same JSON output.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(structlog.stdlib.ProcessorFormatter(processor=renderer, foreign_pre_chain=shared))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("pika", "botocore", "urllib3", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None):
    return structlog.get_logger(name)
