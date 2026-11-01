# One image for API, worker, MCP server and the (fake) inference gateway. Build context: services/
# The command decides the role: newsintel-api | newsintel-worker | newsintel-mcp | newsintel-inference

FROM python:3.12.16-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv

WORKDIR /src
# Dependency layer first (cached unless the lockfile changes)
COPY inference/pyproject.toml inference/pyproject.toml
COPY inference/src inference/src
COPY app/pyproject.toml app/uv.lock app/
RUN cd app && uv sync --frozen --no-dev --no-install-project
COPY app/src app/src
RUN cd app && uv sync --frozen --no-dev

RUN useradd --create-home --uid 10001 newsintel \
 && mkdir -p /var/cache/newsintel /var/lib/pixeltable \
 && chown -R newsintel /var/cache/newsintel /var/lib/pixeltable
USER newsintel
ENV PATH=/opt/venv/bin:$PATH \
    SCRATCH_DIR=/var/cache/newsintel \
    PIXELTABLE_HOME=/var/lib/pixeltable \
    NEWSINTEL_ENV_FILE=/nonexistent
WORKDIR /home/newsintel
CMD ["newsintel-api", "--host", "0.0.0.0", "--port", "8000"]
