"""Groups (D-020) on a real PostgreSQL: the service, contest.yaml access.groups, entry through a group, В6, traces
(В5), judgectl group, and that a student cannot reach another group's contest by any id."""
import inspect
import io
import json
from datetime import datetime, timedelta, timezone

from tests.pg.pgbase import HAVE_SA, URL, PgCase, requires_pg

T0 = datetime(2026, 11, 10, 3, 0, tzinfo=timezone.utc)

if URL and HAVE_SA:
    from sqlalchemy import select

    from app import groups as g
    from app.auth import audit, users
    from app.contests import access, service
    from app.contests.service import ContestError
    from app.contests.spec import parse
    from app.groups import GroupError
    from app.judgectl.cli import main
    from app.judgectl.core import Context
    from app.models import AuditLog, ContestGroup, ContestState, GroupMember, JoinedVia, Role, User
    from app.settings import Settings
    from app.timeutil import zone


def contest_yaml(slug="lesson", mode="groups", groups=("7Б",)):
    access_line = f"access: {{mode: {mode}" + (f", groups: [{', '.join(groups)}]" if groups else "") + "}\n"
    return (f"format_version: 1\nslug: {slug}\ntitle: Урок\nstarts_at: 2026-11-10 09:00\nends_at: 2026-11-10 18:00\n"
            + access_line + "problems:\n  - {label: A, problem: sum}\n")


@requires_pg
class GroupServiceTests(PgCase):
    def setUp(self):
        super().setUp()
        self.admin = self.seed["admin"]
        self.actor = audit.Actor(user_id=self.admin.id)  # as from the web: the admin is the actor
        self.a, self.b, self.c = self.student(1), self.student(2), self.student(3)
        self.s.commit()

    def apply(self, text):
        result = service.apply(self.s, self.actor, parse(text, zone("Asia/Novosibirsk")), T0)
        self.s.commit()
        return result

    def published(self, text):
        c = self.apply(text).contest
        service.set_state(self.s, self.actor, c, ContestState.published, T0)
        self.s.commit()
        return c

    def actions(self, prefix):
        return [a for a in self.s.execute(select(AuditLog.action).order_by(AuditLog.id)).scalars()
                if a.startswith(prefix)]

    def refused(self, code, func, *args):
        with self.assertRaises((GroupError, ContestError)) as ctx:
            func(*args)
        self.s.rollback()
        got = ctx.exception.code
        self.assertEqual(got, code, str(ctx.exception))
        return ctx.exception

    # ---------------------------------------------------------------- the service
    def test_create_rename_archive_unarchive(self):
        group = g.create(self.s, self.actor, "  7  Б ", T0)
        self.s.commit()
        self.assertEqual(group.name, "7 Б")
        self.refused("group_name_taken", g.create, self.s, self.actor, "7 б", T0)  # case does not matter
        self.refused("group_name_invalid", g.create, self.s, self.actor, "   ", T0)
        self.refused("group_name_invalid", g.create, self.s, self.actor, "x" * 65, T0)
        other = g.create(self.s, self.actor, "8А", T0)
        self.s.commit()
        self.refused("group_name_taken", g.rename, self.s, self.actor, other, "7 Б", T0)
        self.assertTrue(g.rename(self.s, self.actor, other, "8 А", T0))
        self.assertFalse(g.rename(self.s, self.actor, other, "8 А", T0))
        self.s.commit()

        self.published(contest_yaml(groups=("7 Б",)))
        self.assertEqual(g.archive(self.s, self.actor, group, T0), ["lesson"])  # K6: where it is attached
        self.s.commit()
        again = g.create(self.s, self.actor, "7 Б", T0)  # the name of an archived group is free again
        self.s.commit()
        self.refused("group_name_taken", g.unarchive, self.s, self.actor, group, T0)
        self.assertEqual(g.get_group(self.s, "7 б").id, again.id)  # by name: the group not archived
        self.assertEqual(g.get_group(self.s, f"#{group.id}").id, group.id)
        g.rename(self.s, self.actor, again, "7 Б (2025)", T0)
        self.assertTrue(g.unarchive(self.s, self.actor, group, T0))
        self.s.commit()
        g.archive(self.s, self.actor, group, T0)
        g.archive(self.s, self.actor, again, T0)
        g.rename(self.s, self.actor, again, "7 Б", T0)
        self.s.commit()
        self.refused("group_ambiguous", g.get_group, self.s, "7 Б")  # two archived groups of one name: by id
        self.refused("group_not_found", g.get_group, self.s, "9В")
        self.assertEqual(self.actions("group."), ["group.create", "group.create", "group.rename", "group.archive",
                                                  "group.create", "group.rename", "group.unarchive", "group.archive",
                                                  "group.archive", "group.rename"])

    def test_members(self):
        group = g.create(self.s, self.actor, "7Б", T0)
        self.assertEqual(g.add_members(self.s, self.actor, group, [self.a, self.b, self.a], T0),
                         {"added": ["student1", "student2"], "already": ["student1"]})  # twice in one call
        self.assertEqual(g.add_members(self.s, self.actor, group, [self.a], T0), {"added": [], "already": ["student1"]})
        self.s.commit()
        self.refused("not_a_student", g.add_members, self.s, self.actor, group, [self.admin], T0)
        group = g.get_group(self.s, "7Б")
        later = T0 + timedelta(days=1)
        self.assertEqual(g.remove_members(self.s, self.actor, group, [self.a, self.c], later),
                         {"removed": ["student1"], "not_members": ["student3"]})
        g.add_members(self.s, self.actor, group, [self.a], later)  # back again: a new row, the old one stays
        self.s.commit()
        rows = [(u.login, m.removed_at is not None) for m, u in g.members(self.s, group, with_history=True)]
        self.assertEqual(rows, [("student1", True), ("student1", False), ("student2", False)])
        self.assertEqual([u.login for _m, u in g.members(self.s, group)], ["student1", "student2"])
        removed = self.s.execute(select(GroupMember).where(GroupMember.removed_at.is_not(None))).scalar_one()
        self.assertEqual((removed.removed_at, removed.removed_by), (later, self.admin.id))
        g.archive(self.s, self.actor, group, later)
        self.s.commit()
        self.refused("group_archived", g.add_members, self.s, self.actor, group, [self.c], T0)

    # ---------------------------------------------------------------- contest.yaml access.groups
    def test_apply_attaches_and_detaches_groups(self):
        g.create(self.s, self.actor, "7Б", T0)
        g.create(self.s, self.actor, "7В", T0)
        self.s.commit()
        first = self.apply(contest_yaml(groups=("7б",)))  # case does not matter
        self.assertIn("group 7Б attached", first.changes)
        self.assertIsNone(first.contest.invite_code)  # groups only: no code
        self.assertEqual(self.apply(contest_yaml(groups=("7Б",))).changes, [])  # idempotent
        both = self.apply(contest_yaml(mode="code_or_groups", groups=("7Б", "7В")))
        self.assertEqual(sorted(both.changes), ["access_mode: groups -> code_or_groups", "group 7В attached",
                                                "invite code created"])
        detail = service.contest_detail(self.s, both.contest)
        self.assertEqual(detail["spec"]["access"], {"mode": "code_or_groups", "groups": ["7Б", "7В"]})
        self.assertEqual(len(detail["invite_code"]), 6)
        self.assertEqual(service.apply(self.s, self.actor, service_spec(detail["spec"]), T0).changes, [])  # round trip
        self.s.rollback()
        self.assertTrue(service.regenerate_code(self.s, self.actor, both.contest, T0))  # a code mode
        self.s.commit()
        fewer = self.apply(contest_yaml(mode="code_or_groups", groups=("7Б",)))
        self.assertEqual(fewer.changes, ["group 7В detached"])
        self.assertIn("unless they came in with the invite code", fewer.warnings[0])
        back = self.apply(contest_yaml(mode="groups"))
        self.assertIn("access_mode: code_or_groups -> groups", back.changes)
        self.refused("contest_is_open", service.regenerate_code, self.s, self.actor, back.contest, T0)

    def test_apply_refuses_unknown_and_archived_groups_but_keeps_one_already_attached(self):
        group = g.create(self.s, self.actor, "7Б", T0)
        old = g.create(self.s, self.actor, "6Б", T0)
        g.archive(self.s, self.actor, old, T0)
        self.s.commit()
        error = self.refused("contest_conflict", self.apply, contest_yaml(groups=("7Б", "6Б", "9Я")))
        self.assertEqual(len(error.problems), 2)  # every problem at once
        self.assertIn("group '6Б' is archived", error.problems[0])
        self.assertIn("no group '9Я' (judgectl group create '9Я')", error.problems[1])
        self.apply(contest_yaml(groups=("7Б",)))
        g.archive(self.s, self.actor, group, T0)  # archived after it was attached: the same file still applies
        self.s.commit()
        result = self.apply(contest_yaml(groups=("7Б",)))
        self.assertEqual(result.changes, [])
        self.assertEqual(result.warnings, ["group 7Б is archived: it gives no access to this contest"])

    # ---------------------------------------------------------------- entry, access through groups
    def test_entry_through_a_group_follows_the_current_membership(self):
        group = g.create(self.s, self.actor, "7Б", T0)
        g.add_members(self.s, self.actor, group, [self.a], T0)
        c = self.published(contest_yaml())
        cp = c.problems[0]
        part = service.enter(self.s, self.a, c, T0)
        self.s.commit()
        self.assertEqual(part.joined_via, JoinedVia.group)
        self.refused("no_access", service.enter, self.s, self.b, c, T0)  # not in the group
        self.assertFalse(access.can_view_contest(self.b, c, None, T0, g.in_contest_group(self.s, c, self.b)))

        g.add_members(self.s, self.actor, group, [self.b], T0)  # added later: access at once
        self.s.commit()
        part_b, in_group = service.access_inputs(self.s, c, self.b)
        self.assertEqual((part_b, in_group), (None, True))
        service.enter(self.s, self.b, c, T0)
        self.s.commit()
        part_b, in_group = service.access_inputs(self.s, c, self.b)
        self.assertTrue(access.can_submit(self.b, c, cp, part_b, T0, in_group=in_group))

        g.remove_members(self.s, self.actor, group, [self.b], T0)  # removed: no statements, no submitting
        self.s.commit()
        part_b, in_group = service.access_inputs(self.s, c, self.b)
        self.assertFalse(in_group)
        self.assertEqual(access.can_submit(self.b, c, cp, part_b, T0, in_group=in_group).reason, "no_access")
        self.assertFalse(access.can_view_problem(self.b, c, cp, part_b, T0, in_group))
        self.assertFalse(access.can_view_contest(self.b, c, part_b, T0, in_group))
        self.assertTrue(access.can_see_own_results(self.b, c, part_b))  # but their own results stay

        g.archive(self.s, self.actor, group, T0)  # an archived group gives no access either
        self.s.commit()
        self.assertFalse(g.in_contest_group(self.s, c, self.a))
        part_a = service.participation_of(self.s, c, self.a)
        self.assertFalse(access.has_access(self.a, c, part_a, False))

    def test_a_removed_student_comes_back_with_the_code(self):
        """В6: code_or_groups, taken out of the group, the right code turns the participation into a code one."""
        group = g.create(self.s, self.actor, "7Б", T0)
        g.add_members(self.s, self.actor, group, [self.a], T0)
        c = self.published(contest_yaml(mode="code_or_groups"))
        part = service.enter(self.s, self.a, c, T0, c.invite_code)  # a member with a code still comes in as group
        self.s.commit()
        self.assertEqual(part.joined_via, JoinedVia.group)
        self.refused("already_joined", service.enter, self.s, self.a, c, T0, c.invite_code)  # nothing to change
        g.remove_members(self.s, self.actor, group, [self.a], T0)
        self.s.commit()
        self.refused("already_joined", service.enter, self.s, self.a, c, T0, "WRONG1")
        part = service.enter(self.s, self.a, c, T0, c.invite_code)
        self.s.commit()
        self.assertEqual(part.joined_via, JoinedVia.code)
        self.assertTrue(access.has_access(self.a, c, part, False))
        entry = self.s.execute(select(AuditLog).where(AuditLog.action == "contest.rejoin_with_code")).scalar_one()
        self.assertEqual((entry.actor_user_id, entry.details["from"], entry.details["contest"]),
                         (self.a.id, "group", "lesson"))
        self.assertNotIn(c.invite_code, json.dumps(entry.details))  # the code itself is not logged

    def test_personal_start_needs_access(self):
        group = g.create(self.s, self.actor, "7Б", T0)
        g.add_members(self.s, self.actor, group, [self.a], T0)
        c = self.published(contest_yaml().replace("ends_at", "timing_mode: personal\npersonal_duration: 1h\nends_at"))
        service.enter(self.s, self.a, c, T0)
        g.remove_members(self.s, self.actor, group, [self.a], T0)
        self.s.commit()
        self.refused("no_access", service.start, self.s, self.a, c, T0)

    # ---------------------------------------------------------------- identity substitution
    def test_another_groups_contest_stays_closed_whatever_ids_are_used(self):
        mine, theirs = g.create(self.s, self.actor, "7Б", T0), g.create(self.s, self.actor, "7В", T0)
        g.add_members(self.s, self.actor, mine, [self.a], T0)
        g.add_members(self.s, self.actor, theirs, [self.b], T0)
        own = self.published(contest_yaml("own", groups=("7Б",)))
        other = self.published(contest_yaml("other", groups=("7В",)))
        service.enter(self.s, self.b, other, T0)
        self.s.commit()
        # the student of 7Б: in a group, and someone else is in the group of the other contest; still nothing
        self.assertTrue(g.in_contest_group(self.s, own, self.a))
        self.assertFalse(g.in_contest_group(self.s, other, self.a))
        self.refused("no_access", service.enter, self.s, self.a, other, T0)
        self.refused("no_access", service.enter, self.s, self.a, other, T0, "ANYCODE")
        self.assertFalse(access.can_view_contest(self.a, other, None, T0, g.in_contest_group(self.s, other, self.a)))
        # the student-facing functions take the student (a User of the session), never a user or group id
        for func in (service.enter, service.start, service.access_inputs, g.in_contest_group):
            params = inspect.signature(func).parameters
            with self.subTest(func.__name__):
                self.assertIn("user", params)
                self.assertFalse({"user_id", "group_id", "participation_id"} & set(params))

    # ---------------------------------------------------------------- traces (В5)
    def test_membership_is_not_a_trace_but_changing_groups_is(self):
        group = g.create(self.s, self.actor, "7Б", T0)
        g.add_members(self.s, self.actor, group, [self.c], T0)
        self.s.commit()
        details = users.delete_user(self.s, audit.Actor(cli="cli:judge"), self.c, T0)  # created by mistake
        self.s.commit()
        self.assertEqual(details["group_memberships_deleted"], 1)
        self.assertEqual(self.s.execute(select(GroupMember)).scalars().all(), [])
        teacher = User(login="teacher", password_hash="x", display_name="T", role=Role.admin)
        self.s.add(teacher)
        self.s.flush()
        g.add_members(self.s, audit.Actor(user_id=teacher.id), group, [self.a], T0)
        self.s.commit()
        self.assertEqual(users.traces(self.s, teacher)["students added to groups"], 1)
        self.assertEqual(users.traces(self.s, self.admin)["groups created"], 1)
        self.apply(contest_yaml())
        self.assertEqual(users.traces(self.s, self.admin)["groups attached to contests"], 1)

    # ---------------------------------------------------------------- class import (В4) and its rollback (K3)
    def import_csv(self, text):
        from app.auth import csvimport

        import_id, results = users.import_students(self.s, self.actor, csvimport.parse(text.encode()), T0, "class.csv")
        self.s.commit()
        return import_id, results

    def test_class_import_creates_or_fills_groups(self):
        existing = g.create(self.s, self.actor, "8А", T0)
        self.s.commit()
        _id, results = self.import_csv("Фамилия;Имя;Класс\nИванов;Пётр;7Б\nПетров;Иван;7б\nСидоров;Олег;8а\n"
                                       "Смирнов;Яков;\n")
        self.assertEqual([r["group"] for r in results], ["7Б", "7Б", "8А", None])
        made = g.get_group(self.s, "7Б")
        self.assertEqual([u.login for _m, u in g.members(self.s, made)], ["ivanov.p", "petrov.i"])
        self.assertEqual([u.login for _m, u in g.members(self.s, existing)], ["sidorov.o"])
        entry = self.s.execute(select(AuditLog).where(AuditLog.action == "user.import")).scalar_one()
        self.assertEqual((entry.details["groups_created"], entry.details["groups_joined"]), ([made.id], [existing.id]))
        self.assertNotIn("password", json.dumps(entry.details))

    def test_a_class_named_like_an_archived_group_is_a_line_error(self):
        old = g.create(self.s, self.actor, "6Б", T0)
        g.archive(self.s, self.actor, old, T0)
        self.s.commit()
        from app.auth.csvimport import CsvImportError

        with self.assertRaises(CsvImportError) as ctx:
            self.import_csv("Фамилия;Имя;Класс\nИванов;Пётр;7Б\nПетров;Иван;6б\nСидоров;Олег;6Б\n")
        self.s.rollback()
        self.assertEqual([p.split(":")[0] for p in ctx.exception.problems], ["line 3", "line 4"])
        self.assertIn("archived group", ctx.exception.problems[0])
        self.assertIsNone(g.live_named(self.s, "7Б"))  # all or nothing
        self.assertIsNone(users.find_by_login(self.s, "ivanov.p"))

    def test_rollback_deletes_the_groups_it_created_unless_something_uses_them(self):
        """K3, three cases: nothing else in the group -> deleted; another student added later -> kept; attached to a
        contest -> kept. A group that existed before only loses the imported students."""
        before = g.create(self.s, self.actor, "8А", T0)
        self.s.commit()
        import_id, _ = self.import_csv("Фамилия;Имя;Класс\nИванов;Пётр;7Б\nПетров;Иван;7В\nСидоров;Олег;7Г\n"
                                       "Смирнов;Яков;8А\n")
        g.add_members(self.s, self.actor, g.get_group(self.s, "7В"), [self.a], T0)  # someone else joined later
        self.s.commit()
        self.apply(contest_yaml(groups=("7Г",)))  # attached to a contest
        result = users.import_rollback(self.s, self.actor, import_id, T0)
        self.s.commit()
        self.assertEqual(result["deleted"], ["ivanov.p", "petrov.i", "sidorov.o", "smirnov.ya"])
        self.assertEqual(result["groups_deleted"], ["7Б"])
        self.assertEqual(result["groups_kept"], ["7В: it has other students", "7Г: it is attached to a contest"])
        self.assertRaises(GroupError, g.get_group, self.s, "7Б")
        self.assertEqual([u.login for _m, u in g.members(self.s, g.get_group(self.s, "7В"))], ["student1"])
        self.assertEqual(g.members(self.s, g.get_group(self.s, "7Г"), with_history=True), [])  # memberships gone
        self.assertEqual(g.members(self.s, before, with_history=True), [])  # existed before: kept, without them
        self.assertEqual(g.get_group(self.s, "8А").id, before.id)
        entry = self.s.execute(select(AuditLog).where(AuditLog.action == "user.import_rollback")).scalar_one()
        self.assertEqual(entry.details["groups_deleted"], ["7Б"])

    # ---------------------------------------------------------------- time zone (D-024)
    def test_time_zone(self):
        self.assertEqual(users.set_timezone(self.s, self.actor, self.a, " Asia/Novosibirsk ", T0), "Asia/Novosibirsk")
        self.assertEqual(users.set_timezone(self.s, self.actor, self.a, "Asia/Novosibirsk", T0), "Asia/Novosibirsk")
        self.s.commit()
        for bad in ("Mars/Olympus", "../etc/passwd", "/etc/localtime", "Asia/Novosibirsk\x00", "x" * 65):
            with self.subTest(bad=bad), self.assertRaises(users.UserError) as ctx:
                users.set_timezone(self.s, self.actor, self.a, bad, T0)
            self.assertEqual(ctx.exception.code, "timezone_invalid")
        self.assertEqual(self.a.timezone, "Asia/Novosibirsk")
        self.assertIsNone(users.set_timezone(self.s, self.actor, self.a, "", T0))
        self.s.commit()
        self.assertEqual(self.actions("user.set_timezone"), ["user.set_timezone"] * 2)  # unchanged: not audited
        self.assertIn("time zone Europe/Moscow", self.ok("user", "set-timezone", "student1", "Europe/Moscow")[0])
        self.assertIn("no time zone", self.ok("user", "set-timezone", "student1", "-")[0])
        code, _, err = self.run_cli("user", "set-timezone", "student1", "Nowhere/City")
        self.assertEqual((code, "not a time zone" in err), (1, True))
        code, _, err = self.run_cli("user", "set-timezone", "admin", "UTC")  # students only here
        self.assertEqual(code, 1)

    # ---------------------------------------------------------------- judgectl group
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        ctx = Context(session_factory=self.Session, settings=Settings(timezone="UTC"),
                      actor=audit.Actor(cli="cli:judge"), out=out, err=err, interactive=lambda: False,
                      clock=lambda: T0)
        code = main(list(argv), ctx)
        return code, out.getvalue(), err.getvalue()

    def ok(self, *argv):
        code, out, err = self.run_cli(*argv)
        self.assertEqual(code, 0, err)
        return out, err

    def test_judgectl_group(self):
        self.assertIn("group 7Б created", self.ok("group", "create", "7Б")[0])
        code, _, err = self.run_cli("group", "create", "7б")
        self.assertEqual((code, "already a group" in err), (1, True))
        out, err = self.ok("group", "add", "7Б", "student1", "student2", "student1")
        self.assertIn("added 2: student1, student2", out)
        self.assertIn("already in the group: student1", err)
        code, _, err = self.run_cli("group", "add", "7Б", "nobody")
        self.assertEqual((code, "no user 'nobody'" in err), (1, True))
        self.assertEqual(self.s.execute(select(GroupMember)).scalars().all().__len__(), 2)  # nothing half-done
        self.apply(contest_yaml())
        self.assertIn("removed 1: student2", self.ok("group", "remove", "7Б", "student2")[0])
        listing = json.loads(self.ok("group", "list", "--json")[0])
        self.assertEqual([(r["name"], r["members"], r["contests"]) for r in listing], [("7Б", 1, 1)])
        shown = json.loads(self.ok("group", "show", "7Б", "--history", "--json")[0])
        self.assertEqual(shown["contests"], ["lesson"])
        self.assertEqual([(s["login"], s["removed_at"] is not None) for s in shown["students"]],
                         [("student1", False), ("student2", True)])
        self.assertIn("contests: lesson", self.ok("group", "show", "7Б")[0])
        out, err = self.ok("group", "archive", "7Б")
        self.assertIn("archived", out)
        self.assertIn("gives no access to them any more: lesson", err)
        self.assertEqual(json.loads(self.ok("group", "list", "--json")[0]), [])
        self.assertEqual(len(json.loads(self.ok("group", "list", "--all", "--json")[0])), 1)
        code, _, err = self.run_cli("group", "add", "7Б", "student3")
        self.assertEqual((code, "is archived" in err), (1, True))
        self.assertIn("not archived any more", self.ok("group", "unarchive", "7Б")[0])
        self.assertIn("7Б -> 7 Б", self.ok("group", "rename", "7Б", "7 Б")[0])
        self.assertIn("groups 7 Б", self.ok("contest", "show", "lesson")[0])
        self.assertEqual(self.actions("group."), ["group.create", "group.add_members", "group.remove_members",
                                                  "group.archive", "group.unarchive", "group.rename"])
        entry = self.s.execute(select(AuditLog).where(AuditLog.action == "group.add_members")).scalar_one()
        self.assertEqual((entry.actor_cli, entry.details["logins"]), ("cli:judge", ["student1", "student2"]))


def service_spec(data):
    """contest_detail's spec back into apply, as PUT /api/admin/contests does (times are datetimes there)."""
    from app.contests.spec import from_data
    from app.jsonable import plain

    return from_data(plain(data), zone("Asia/Novosibirsk"))
