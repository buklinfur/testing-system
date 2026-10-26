"""Operator commands (until the admin UI exists).

    python -m app.admin_cli create-worker-token main [--caps cpu,gpu]
    python -m app.admin_cli revoke-worker-token main
    python -m app.admin_cli status
    python -m app.admin_cli reap
    python -m app.admin_cli rejudge --submission 17 | --contest-problem 3 [--verdicts WA,TL]
    python -m app.admin_cli judgements [--last 20]     (newest first: verdict, first failure, TL kind, timing)
    python -m app.admin_cli events [--last 30]         (queue_events, newest first: releases, requeues, restarts)
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import select, update

from . import queue as q
from .db import session_scope
from .models import Judgement, Problem, ProblemVersion, QueueEvent, Submission, TestResult, User, Worker, WorkerToken
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
    p = sub.add_parser("judgements")
    p.add_argument("--last", type=int, default=20)
    p = sub.add_parser("events")
    p.add_argument("--last", type=int, default=30)
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
        elif args.cmd == "judgements":
            print_judgements(session, args.last)
        elif args.cmd == "events":
            print_events(session, args.last)
    return 0


def print_events(session, last: int) -> None:
    """Newest queue events first (kept JUDGE_EVENT_RETENTION_DAYS, 14 by default)."""
    rows = session.execute(select(QueueEvent, Worker.name).outerjoin(Worker, Worker.id == QueueEvent.worker_id)
                           .order_by(QueueEvent.id.desc()).limit(last)).all()
    for e, worker in rows:
        details = json.dumps(e.details, ensure_ascii=False)
        print(f"{e.id:>6} {e.at:%Y-%m-%d %H:%M:%S} {e.kind:<18} judgement={e.judgement_id or '-':<6} "
              f"worker={worker or '-':<12} {details}")


def print_judgements(session, last: int) -> None:
    """Newest judgements first, one line each (teacher view: includes internal errors)."""
    rows = session.execute(
        select(Judgement, Submission, User.login, Problem.slug, ProblemVersion.version, Worker.name)
        .join(Submission, Submission.id == Judgement.submission_id)
        .join(User, User.id == Submission.user_id)
        .join(ProblemVersion, ProblemVersion.id == Judgement.problem_version_id)
        .join(Problem, Problem.id == ProblemVersion.problem_id)
        .outerjoin(Worker, Worker.id == Judgement.worker_id)
        .order_by(Judgement.id.desc()).limit(last)).all()
    print(f"{'id':>5} {'sub':>5} {'user':<14} {'problem':<18} {'lang':<6} {'status':<14} {'verdict':<8} "
          f"{'score':>6} {'att':>3} {'fail':>4} {'first':<6} {'tl':<5} {'judge_s':>7}  worker / error")
    for j, sub, login, slug, version, worker in rows:
        first = (j.summary or {}).get("first_fail") or ""
        kinds = session.execute(select(TestResult.tl_kind).where(TestResult.judgement_id == j.id,
                                                                 TestResult.tl_kind.is_not(None))).scalars().all()
        took = (j.finished_at - j.started_at).total_seconds() if j.finished_at and j.started_at else None
        lang = (sub.language_key or "-") + ("+zip" if sub.is_archive else "")
        tail = (j.error or "").replace("\n", " ")[:60] or (worker or "")
        print(f"{j.id:>5} {sub.id:>5} {login:<14} {slug + '@' + str(version):<18} {lang:<6} {j.status.value:<14} "
              f"{(j.verdict.value if j.verdict else '-'):<8} {j.score:>6} {j.attempt:>3} {j.failures:>4} {first:<6} "
              f"{','.join(sorted(set(kinds))) or '-':<5} {('-' if took is None else f'{took:.2f}'):>7}  {tail}")


if __name__ == "__main__":
    raise SystemExit(main())
