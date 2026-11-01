"""Stage graph rules (M4/M6). The key business rule: an OCR failure never invalidates the transcript."""

from newsintel.pipeline.stages import Stage, StageStatus, VideoStatus, resolve, video_status

D, F, P, S = StageStatus.DONE, StageStatus.FAILED, StageStatus.PENDING, StageStatus.SKIPPED


def states(**kw) -> dict[Stage, StageStatus]:
    base = {s: P for s in Stage}
    base.update({Stage(k): v for k, v in kw.items()})
    return base


def test_new_video_starts_with_probe_only():
    to_run, _ = resolve(states())
    assert to_run == [Stage.PROBE]
    assert video_status(states()) == VideoStatus.REGISTERED


def test_after_probe_audio_and_scenes_run_in_parallel():
    to_run, _ = resolve(states(probe=D))
    assert to_run == [Stage.AUDIO, Stage.SCENES]


def test_embed_waits_until_all_content_stages_settle():
    to_run, _ = resolve(states(probe=D, audio=D, asr=D, scenes=D, captions=D))
    assert Stage.EMBED not in to_run  # ocr still pending
    to_run, _ = resolve(states(probe=D, audio=D, asr=D, scenes=D, captions=D, ocr=F))
    assert to_run == [Stage.EMBED]


def test_ocr_failure_yields_partial_not_failed():
    final = states(probe=D, audio=D, asr=D, scenes=D, captions=D, ocr=F, embed=D)
    assert video_status(final) == VideoStatus.PARTIAL


def test_scene_failure_cascades_skips_but_transcript_still_indexes():
    to_run, new = resolve(states(probe=D, audio=D, asr=D, scenes=F))
    assert new[Stage.CAPTIONS] == S and new[Stage.OCR] == S
    assert to_run == [Stage.EMBED]
    new[Stage.EMBED] = D
    assert video_status(new) == VideoStatus.PARTIAL


def test_probe_failure_fails_everything():
    to_run, new = resolve(states(probe=F))
    assert to_run == [Stage.EMBED] or to_run == []
    assert all(new[s] == S for s in (Stage.AUDIO, Stage.ASR, Stage.SCENES, Stage.CAPTIONS, Stage.OCR))
    new[Stage.EMBED] = D  # nothing to embed, but settles
    assert video_status(new) == VideoStatus.FAILED


def test_all_done_is_indexed():
    assert video_status({s: D for s in Stage}) == VideoStatus.INDEXED


def test_in_flight_is_processing():
    assert video_status(states(probe=D, audio=StageStatus.RETRYING)) == VideoStatus.PROCESSING
