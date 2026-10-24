"""Operator commands (until the admin UI exists).

    python -m app.admin_cli create-worker-token main [--caps cpu,gpu]
    python -m app.admin_cli revoke-worker-token main
    python -m app.admin_cli status
    python -m app.admin_cli reap
    python -m app.admin_cli rejudge --submission 17 | --contest-problem 3 [--verdicts WA,TL]
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import update

from . import queue as q
from .db import session_scope
from .models import WorkerToken
from .settings import Settings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="admin_cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("create-worker-token")
    p.add_argument("name")
    p.add_argument("--caps", default="cpu")
    p = sub.add_parser("revoke-worker-token")
    p.add_argument("name")
    sub.add_parser("status")
    sub.add_parser("reap")
    p = sub.add_parser("rejudge")
    p.add_argument("--submission", type=int)
    p.add_argument("--contest-problem", type=int)
    p.add_argument("--verdicts", default="")
    args = parser.parse_args(argv)
    settings = Settings.from_env().queue

    with session_scope() as session:
        if args.cmd == "create-worker-token":
            token = q.create_worker_token(session, args.name, [c.strip() for c in args.caps.split(",") if c.strip()])
            print("Token (shown once, put it into /etc/judge/worker.env as JUDGE_WORKER_TOKEN):")
            print(token)
        elif args.cmd == "revoke-worker-token":
            n = session.execute(update(WorkerToken).where(WorkerToken.name == args.name).values(
                revoked_at=datetime.now(timezone.utc))).rowcount
            print("revoked" if n else "no such token")
        elif args.cmd == "status":
            snap = q.load_snapshot(session, settings)
            print(json.dumps(snap.to_dict(), ensure_ascii=False))
            for w in q.worker_table(session, settings):
                age = "-" if w["heartbeat_age_s"] is None else f"{w['heartbeat_age_s']:.0f}s"
                print(f"{w['name']:<20} {w['state']:<9} hb={age:<6} judgement={w['judgement_id'] or '-':<6} "
                      f"cpus={w['info'].get('cpus', '?')} jobs={w['stats'].get('jobs_done', 0)}")
        elif args.cmd == "reap":
            print(q.reap(session, settings))
        elif args.cmd == "rejudge":
            if args.submission:
                print(q.request_rejudge(session, args.submission).kind)
            elif args.contest_problem:
                verdicts = [v for v in args.verdicts.split(",") if v] or None
                print(q.rejudge_contest_problem(session, args.contest_problem, only_current_verdicts=verdicts))
            else:
                parser.error("give --submission or --contest-problem")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
