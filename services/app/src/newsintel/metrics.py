"""Prometheus metrics shared by API and worker."""

from prometheus_client import Counter, Histogram, start_http_server

STAGE_DURATION = Histogram(
    "newsintel_stage_duration_seconds", "Processing stage duration", ["stage"],
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1200, 3600),
)  # fmt: skip
STAGE_OUTCOMES = Counter("newsintel_stage_outcomes_total", "Stage outcomes", ["stage", "outcome"])
INFERENCE_CALLS = Counter("newsintel_inference_calls_total", "Inference calls", ["endpoint", "outcome"])
INFERENCE_LATENCY = Histogram("newsintel_inference_latency_seconds", "Inference call latency", ["endpoint"])
HTTP_LATENCY = Histogram("newsintel_http_request_duration_seconds", "API latency", ["method", "route", "status"])
SEARCH_REQUESTS = Counter("newsintel_search_requests_total", "Search/ask requests", ["kind", "outcome"])


def start_metrics_server(port: int) -> None:
    start_http_server(port)
