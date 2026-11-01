"""Operator CLI: inspect processing state, retry failed videos, requeue stuck stages.

Runs inside the app container (it uses the same Settings/.env as the API and worker):
    docker exec newsintel-api-1 newsintel-admin status [VIDEO_ID]
    docker exec newsintel-api-1 newsintel-admin retry VIDEO_ID --operator NAME [--stage ocr ...]
    docker exec newsintel-api-1 newsintel-admin requeue-stuck --operator NAME [--minutes 45] [--include-running]
Mutating commands require --operator (the container user says nothing about who ran them) and are
recorded in the audit log as actor "admin-cli:NAME", actor_type "system".
"""

import argparse
import json
import sys
from datetime import timedelta

from newsintel.audit import AuditLog
from newsintel.config import get_settings
from newsintel.index.repository import Repository
from newsintel.logging import configure_logging
from newsintel.pipeline.ops import requeue_stuck, retry_video
from newsintel.pipeline.stages import Stage
from newsintel.queue import JobPublisher


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="newsintel-admin", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("status", help="show stage status for one video, or a summary of all videos")
    st.add_argument("video_id", nargs="?")
    rt = sub.add_parser("retry", help="reset failed/skipped stages of a video and re-run them")
    rt.add_argument("video_id")
    rt.add_argument("--operator", required=True, help="your name, for the audit log")
    rt.add_argument("--stage", action="append", choices=[s.value for s in Stage], help="limit to these stages")
    rq = sub.add_parser("requeue-stuck", help="re-publish stages stuck in queued/retrying (lost messages)")
    rq.add_argument("--operator", required=True, help="your name, for the audit log")
    rq.add_argument("--minutes", type=int, default=45, help="only stages unchanged for this long (default 45)")
    rq.add_argument("--include-running", action="store_true", help="also requeue RUNNING stages (rarely needed)")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging("admin", settings.log_level)
    repo = Repository(settings)

    if args.cmd == "status":
        if args.video_id:
            if repo.get_video(args.video_id) is None:
                print(f"no such video: {args.video_id}", file=sys.stderr)
                return 1
            rows = sorted(repo.stages(args.video_id), key=lambda r: r["stage"])
            for r in rows:
                print(f"{r['stage']:<12} {r['status']:<10} attempts={r['attempts'] or 0}  {r['last_error'] or ''}")
        else:
            for v in repo.list_videos():
                print(f"{v['video_id']}  {v['status']:<10} {v['collection']:<22} {v['title'] or v['filename']}")
        return 0

    operator = f"admin-cli:{args.operator}"
    audit = AuditLog(settings)
    publisher = JobPublisher(settings)
    try:
        if args.cmd == "retry":
            if repo.get_video(args.video_id) is None:
                print(f"no such video: {args.video_id}", file=sys.stderr)
                return 1
            stages = [Stage(s) for s in args.stage] if args.stage else None
            reset = [s.value for s in retry_video(repo, publisher, args.video_id, stages)]
            audit.record(actor=operator, actor_type="system", action="processing.retry", resource_type="video",
                         resource_id=args.video_id, detail={"stages": reset})  # fmt: skip
            print(json.dumps({"video_id": args.video_id, "reset_stages": reset}))
        else:
            n = requeue_stuck(repo, publisher, timedelta(minutes=args.minutes), args.include_running)
            audit.record(actor=operator, actor_type="system", action="processing.requeue_stuck",
                         detail={"requeued": n, "older_than_min": args.minutes})  # fmt: skip
            print(json.dumps({"requeued": n}))
    finally:
        publisher.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
