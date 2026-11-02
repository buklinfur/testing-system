"""Command line of judgectl: the argument parser and the dispatch (docs/JUDGECTL.md)."""
from __future__ import annotations

import argparse
import sys
from typing import Optional

from ..auth.audit import cli_actor
from ..auth.logins import LoginError
from ..auth.totp import TotpConfigError
from ..auth.users import UserError
from ..contests.service import ContestError
from ..contests.spec import ContestSpecError
from ..settings import Settings
from ..timeutil import TimezoneError
from protocol.problem_package import PackageError
from ..groups import GroupError
from . import accounts, contests, groups as group_cmds, ops, problems, submissions
from .core import CliError, Context
from ..auth.csvimport import CsvImportError


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")
    parser = argparse.ArgumentParser(prog="judgectl", description="Console of the server owner: accounts, "
                                     "queue, workers. Run as the `judge` user with /etc/judge/backend.env loaded.")
    groups = parser.add_subparsers(dest="group", required=True, metavar="GROUP")

    def command(group, name, func, help_text, json=False):
        p = group.add_parser(name, help=help_text, description=help_text, parents=[common] if json else [])
        p.set_defaults(func=func)
        return p

    admin = groups.add_parser("admin", help="administrators (only here: R2)").add_subparsers(
        dest="cmd", required=True, metavar="COMMAND")
    p = command(admin, "add", accounts.admin_add, "create an admin: password typed twice, then 2FA with a QR code")
    p.add_argument("login")
    p.add_argument("--name", required=True, help="display name")
    p = command(admin, "list", accounts.admin_list, "active admins with their 2FA state, last login, failures and lockout",
                json=True)
    p.add_argument("--all", action="store_true", help="include disabled admins")
    p = command(admin, "delete", accounts.admin_delete, "delete an admin created by mistake (only without traces)",
                json=True)
    p.add_argument("login")
    p.add_argument("--yes", action="store_true", help="no confirmation (scripts)")
    p = command(admin, "failures", accounts.admin_failures, "failed logins of an admin (kind, time, IP)", json=True)
    p.add_argument("login")
    p.add_argument("--last", type=int, default=20)
    for name, func, text in (("disable", accounts.admin_disable, "disable an admin (ends all sessions)"),
                             ("enable", accounts.admin_enable, "enable an admin again"),
                             ("reset-password", accounts.admin_reset_password,
                              "set a new password (typed twice); ends sessions, lifts the lockout"),
                             ("reset-2fa", accounts.admin_reset_2fa,
                              "new 2FA secret with a QR code (also repeats an unconfirmed enrolment); ends sessions"),
                             ("unlock", accounts.admin_unlock, "lift the brute-force lockout of an admin")):
        command(admin, name, func, text).add_argument("login")

    user = groups.add_parser("user", help="students").add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    p = command(user, "add", accounts.user_add, "create a student; the generated password is shown once", json=True)
    p.add_argument("--surname", help="Russian surname: the login becomes surname.initial")
    p.add_argument("--first-name")
    p.add_argument("--login", help="explicit login instead of the generated one")
    p.add_argument("--name", help="display name (default: first name and the surname's initial)")
    p = command(user, "import", accounts.user_import,
                "create students from CSV: surname;first name[;login][;display name]; all or nothing", json=True)
    p.add_argument("file")
    p.add_argument("--out", required=True, help="new file for logins and passwords (created 0600), or - for stdout")
    p = command(user, "imports", accounts.user_imports, "past `user import` runs with their ids", json=True)
    p = command(user, "import-rollback", accounts.user_import_rollback,
                "delete every user of one import, if none of them has logged in", json=True)
    p.add_argument("import_id", type=int)
    p.add_argument("--yes", action="store_true", help="no confirmation (scripts)")
    p = command(user, "delete", accounts.user_delete, "delete a student created by mistake (only without traces)",
                json=True)
    p.add_argument("login")
    p.add_argument("--yes", action="store_true", help="no confirmation (scripts)")
    p = command(user, "failures", accounts.user_failures, "failed logins of a login, even an unknown one", json=True)
    p.add_argument("login")
    p.add_argument("--last", type=int, default=20)
    p = command(user, "list", accounts.user_list, "students (active ones unless --all)", json=True)
    p.add_argument("--all", action="store_true", help="include disabled students")
    command(user, "reset-password", accounts.user_reset_password,
            "new generated password; ends sessions and QR tokens, lifts the lockout", json=True).add_argument("login")
    command(user, "disable", accounts.user_disable, "disable a student (ends all sessions)").add_argument("login")
    command(user, "enable", accounts.user_enable, "enable a student again").add_argument("login")
    p = command(user, "unlock", accounts.user_unlock, "lift a brute-force lockout of a login or of an IP address")
    p.add_argument("login", nargs="?")
    p.add_argument("--ip")
    p = command(user, "set-login", accounts.user_set_login, "change the login of a student")
    p.add_argument("login")
    p.add_argument("new_login")
    p = command(user, "qr-token", accounts.user_qr_token, "one-time login link and QR code (7 days)", json=True)
    p.add_argument("login")
    p.add_argument("--no-qr", action="store_true", help="only the link, no QR code in the terminal")

    queue = groups.add_parser("queue", help="the judging queue").add_subparsers(dest="cmd", required=True,
                                                                               metavar="COMMAND")
    command(queue, "status", ops.queue_status, "load snapshot: queue length, workers, expected wait", json=True)
    command(queue, "reap", ops.queue_reap, "return jobs of lost workers to the queue now", json=True)
    p = command(queue, "judgements", ops.queue_judgements, "newest judgements first", json=True)
    p.add_argument("--last", type=int, default=20)
    p = command(queue, "events", ops.queue_events, "newest queue events first", json=True)
    p.add_argument("--last", type=int, default=30)
    p = command(queue, "rejudge", ops.queue_rejudge, "deprecated: judgectl submission rejudge", json=True)
    p.add_argument("--submission", type=int)
    p.add_argument("--contest-problem", type=int)
    p.add_argument("--verdicts", default="", help="with --contest-problem: only these current verdicts, e.g. WA,TL")

    worker = groups.add_parser("worker", help="judging workers").add_subparsers(dest="cmd", required=True,
                                                                               metavar="COMMAND")
    command(worker, "list", ops.worker_list, "workers with state and heartbeat", json=True)
    token = worker.add_parser("token", help="worker API tokens").add_subparsers(dest="token_cmd", required=True,
                                                                              metavar="COMMAND")
    p = command(token, "create", ops.worker_token_create, "new worker token (shown once, last line)", json=True)
    p.add_argument("name")
    p.add_argument("--caps", default="cpu")
    command(token, "revoke", ops.worker_token_revoke, "revoke a worker token").add_argument("name")

    problem = groups.add_parser("problem", help="problems and their versions").add_subparsers(
        dest="cmd", required=True, metavar="COMMAND")
    p = command(problem, "import", problems.problem_import,
                "import a package: a folder, a zip, or - for a zip on stdin (tools/pack-problem.sh <folder> | ...)",
                json=True)
    p.add_argument("source")
    p.add_argument("--dry-run", action="store_true", help="every check, nothing written")
    p = command(problem, "list", problems.problem_list, "problems with their latest version", json=True)
    p.add_argument("--all", action="store_true", help="include archived problems")
    command(problem, "versions", problems.problem_versions, "versions of a problem", json=True).add_argument("slug")
    command(problem, "show", problems.problem_show, "one version: groups, programs (<slug> or <slug>@<version>)",
            json=True).add_argument("ref")
    command(problem, "archive", problems.problem_archive, "archive: no more imports or pins").add_argument("slug")
    command(problem, "unarchive", problems.problem_unarchive, "undo archive").add_argument("slug")

    contest = groups.add_parser("contest", help="contests").add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    p = command(contest, "apply", contests.contest_apply, "make a contest what contest.yaml says (- for stdin)",
                json=True)
    p.add_argument("file")
    p.add_argument("--dry-run", action="store_true", help="show the changes, write nothing")
    p = command(contest, "list", contests.contest_list, "contests (not archived unless --all)", json=True)
    p.add_argument("--all", action="store_true")
    command(contest, "show", contests.contest_show, "settings, invite code, problems", json=True).add_argument("slug")
    command(contest, "publish", contests.contest_publish, "make it visible to students").add_argument("slug")
    command(contest, "archive", contests.contest_archive, "read-only for its participants").add_argument("slug")
    p = command(contest, "pin", contests.contest_pin, "pin another (ready) version of a problem", json=True)
    p.add_argument("contest")
    p.add_argument("label")
    p.add_argument("version", type=int)
    choice = p.add_mutually_exclusive_group(required=True)
    choice.add_argument("--rejudge", dest="rejudge", action="store_true", help="rejudge every submission of it")
    choice.add_argument("--no-rejudge", dest="rejudge", action="store_false", help="keep the old results")
    code = contest.add_parser("code", help="invite codes").add_subparsers(dest="code_cmd", required=True,
                                                                         metavar="COMMAND")
    command(code, "regenerate", contests.contest_code_regenerate, "a new invite code; the old one stops working",
            json=True).add_argument("slug")
    command(contest, "participants", contests.contest_participants, "who joined and started",
            json=True).add_argument("slug")

    group = groups.add_parser("group", help="groups (classes) that contests are addressed to").add_subparsers(
        dest="cmd", required=True, metavar="COMMAND")
    command(group, "create", group_cmds.group_create, "a new group, e.g. 7Б").add_argument("name")
    p = command(group, "rename", group_cmds.group_rename, "a new name (<group>: name or #id)")
    p.add_argument("group")
    p.add_argument("name")
    command(group, "archive", group_cmds.group_archive,
            "archive: gives no access any more, lists the contests it is attached to").add_argument("group")
    command(group, "unarchive", group_cmds.group_unarchive, "undo archive").add_argument("group")
    p = command(group, "list", group_cmds.group_list, "groups with their number of students and contests", json=True)
    p.add_argument("--all", action="store_true", help="include archived groups")
    p = command(group, "show", group_cmds.group_show, "students and contests of a group", json=True)
    p.add_argument("group")
    p.add_argument("--history", action="store_true", help="also students removed from it, with when")
    for name, func, text in (("add", group_cmds.group_add, "add students (by login)"),
                             ("remove", group_cmds.group_remove, "remove students; their history stays")):
        p = command(group, name, func, text, json=True)
        p.add_argument("group")
        p.add_argument("logins", nargs="+", metavar="login")

    submission = groups.add_parser("submission", help="submissions").add_subparsers(dest="cmd", required=True,
                                                                                    metavar="COMMAND")
    command(submission, "show", submissions.submission_show, "one submission with every judgement and test",
            json=True).add_argument("id", type=int)
    p = command(submission, "rejudge", submissions.submission_rejudge,
                "rejudge: --id N | --contest <slug> --problem <label> | --contest-problem-id N | "
                "--problem-version <slug>@<v>", json=True)
    p.add_argument("--id", type=int)
    p.add_argument("--contest")
    p.add_argument("--problem", help="the label in the contest")
    p.add_argument("--contest-problem-id", type=int)
    p.add_argument("--problem-version", help="<slug>@<version>: every contest problem pinned to it")
    p.add_argument("--verdicts", default="", help="only submissions whose current verdict is one of these, e.g. WA,TL")

    p = groups.add_parser("status", help="database, migrations, workers, queue, configuration, blob store",
                          parents=[common])
    p.set_defaults(func=ops.status)
    p.add_argument("--details", action="store_true",
                   help="for blob rows without a file: what refers to them (problem@version, submission, program)")
    return parser


def default_context() -> Context:
    from ..db import get_sessionmaker

    return Context(session_factory=get_sessionmaker(), settings=Settings.from_env(), actor=cli_actor())


def main(argv: Optional[list] = None, ctx: Optional[Context] = None) -> int:
    args = build_parser().parse_args(argv)
    ctx = ctx or default_context()
    ctx.json = getattr(args, "json", False)
    try:
        return args.func(ctx, args)
    except (CliError, UserError, LoginError, TotpConfigError, CsvImportError, PackageError, ContestError,
            ContestSpecError, TimezoneError, GroupError) as exc:
        print(f"judgectl: {exc}", file=ctx.err)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\njudgectl: interrupted", file=ctx.err)
        return 130


if __name__ == "__main__":
    sys.exit(main())
