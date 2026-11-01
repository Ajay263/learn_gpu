"""Processing stage graph and status rules. Pure functions — no I/O — so they are unit-tested exhaustively.

    probe ──► audio ──► asr ─────────────┐
      │                                   ├──► embed
      └────► scenes ──► captions ─────────┤
                 │                        │
                 └────► ocr ──────────────┘

Hard dependency: if it fails, the dependent stage is SKIPPED (cannot run without its input).
Soft dependency: must be *settled* (done/failed/skipped) before the stage runs, but failure is fine —
`embed` indexes whatever content exists. Hence an OCR failure never invalidates the transcript.
"""

from enum import StrEnum


class Stage(StrEnum):
    PROBE = "probe"
    AUDIO = "audio"
    ASR = "asr"
    SCENES = "scenes"
    CAPTIONS = "captions"
    OCR = "ocr"
    EMBED = "embed"


class StageStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    DONE = "done"
    FAILED = "failed"  # permanent: attempts exhausted or input rejected
    SKIPPED = "skipped"  # a hard dependency failed


class VideoStatus(StrEnum):
    REGISTERED = "registered"
    PROCESSING = "processing"
    INDEXED = "indexed"
    PARTIAL = "partial"
    FAILED = "failed"


HARD_DEPS: dict[Stage, set[Stage]] = {
    Stage.PROBE: set(),
    Stage.AUDIO: {Stage.PROBE},
    Stage.ASR: {Stage.AUDIO},
    Stage.SCENES: {Stage.PROBE},
    Stage.CAPTIONS: {Stage.SCENES},
    Stage.OCR: {Stage.SCENES},
    Stage.EMBED: set(),
}
SOFT_DEPS: dict[Stage, set[Stage]] = {s: set() for s in Stage}
SOFT_DEPS[Stage.EMBED] = {Stage.ASR, Stage.SCENES, Stage.CAPTIONS, Stage.OCR}

# Stages whose output depends on the GPU host. Their failures while inference is down are retried.
INFERENCE_STAGES = {Stage.ASR, Stage.CAPTIONS, Stage.OCR, Stage.EMBED}

SETTLED = {StageStatus.DONE, StageStatus.FAILED, StageStatus.SKIPPED}
ACTIVE = {StageStatus.QUEUED, StageStatus.RUNNING, StageStatus.RETRYING}


def runnable(states: dict[Stage, StageStatus]) -> tuple[list[Stage], list[Stage]]:
    """Given current states, return (stages to enqueue, stages to mark skipped). Order is deterministic."""
    to_run, to_skip = [], []
    for stage in Stage:
        if states.get(stage, StageStatus.PENDING) != StageStatus.PENDING:
            continue
        hard = HARD_DEPS[stage]
        if any(states.get(d) in (StageStatus.FAILED, StageStatus.SKIPPED) for d in hard):
            to_skip.append(stage)
        elif all(states.get(d) == StageStatus.DONE for d in hard) and all(
            states.get(d) in SETTLED for d in SOFT_DEPS[stage]
        ):
            to_run.append(stage)
    return to_run, to_skip


def resolve(states: dict[Stage, StageStatus]) -> tuple[list[Stage], dict[Stage, StageStatus]]:
    """Apply skip cascades until stable. Returns (stages to enqueue, full new state map)."""
    states = dict(states)
    while True:
        to_run, to_skip = runnable(states)
        if not to_skip:
            return to_run, states
        for s in to_skip:
            states[s] = StageStatus.SKIPPED


def video_status(states: dict[Stage, StageStatus]) -> VideoStatus:
    get = lambda s: states.get(s, StageStatus.PENDING)  # noqa: E731
    if all(get(s) == StageStatus.PENDING for s in Stage):
        return VideoStatus.REGISTERED
    if not all(get(s) in SETTLED for s in Stage):
        return VideoStatus.PROCESSING
    if all(get(s) == StageStatus.DONE for s in Stage):
        return VideoStatus.INDEXED
    # Settled with some failures: useful only if something searchable was produced and indexed.
    searchable = get(Stage.EMBED) == StageStatus.DONE and (
        get(Stage.ASR) == StageStatus.DONE or get(Stage.SCENES) == StageStatus.DONE
    )
    return VideoStatus.PARTIAL if searchable else VideoStatus.FAILED
