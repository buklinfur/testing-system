"""/api/admin groups and deadline extensions on a real PostgreSQL (delta 3.5): the endpoints, the codes of their
errors, participants with joined_via and access_now, and that no id in a path or body reaches what it should not."""
import unittest

from tests.pg.apibase import HAVE_API, ApiCase
from tests.pg.pgbase import HAVE_SA, URL, requires_pg

if URL and HAVE_SA and HAVE_API:
    from sqlalchemy import select

    from app import groups as g
    from app.contests import service
    from app.models import AuditLog, DeadlineExtension, User


def contest(slug="lesson", mode="groups", groups=("7Б",), due="2026-10-27 18:00:00", **extra):
    problem = {"label": "A", "problem": "sum"}
    if due:
        problem["due_at"] = due
    access = {"mode": mode, **({"groups": list(groups)} if groups else {})}
    return {"format_version": 1, "slug": slug, "title": "Урок", "starts_at": "2026-10-27 09:00",
            "ends_at": "2026-10-28 18:00", "access": access, "problems": [problem], **extra}


@requires_pg
@unittest.skipUnless(HAVE_API, "needs fastapi and httpx")
class AdminGroupsTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.boss, password, secret = self.make_admin()
        self.b = self.browser()
        self.h = self.csrf(self.admin_login(self.b, password, secret))
        self.ivanov, self.ivanov_password = self.make_student("Иванов", "Пётр")
        self.petrova, _ = self.make_student("Петрова", "Анна")

    def post(self, path, **kw):
        return self.b.post(f"/api/admin{path}", headers=self.h, **kw)

    def put(self, path, **kw):
        return self.b.put(f"/api/admin{path}", headers=self.h, **kw)

    def get(self, path, **kw):
        return self.b.get(f"/api/admin{path}", **kw)

    def ok(self, r, status=200):
        self.assertEqual(r.status_code, status, r.text)
        return r.json() if r.content else None

    def refused(self, r, status, code):
        self.assertEqual((r.status_code, r.json()["detail"]), (status, code), r.text)
        return r.json()

    def group(self, name="7Б"):
        return self.ok(self.post("/groups", json={"name": name}), 201)

    # ---------------------------------------------------------------- groups
    def test_groups(self):
        made = self.group(" 7  Б ")
        self.assertEqual((made["name"], made["members"], made["students"], made["contests"]), ("7 Б", 0, [], []))
        self.refused(self.post("/groups", json={"name": "7 б"}), 409, "group_name_taken")
        self.refused(self.post("/groups", json={"name": "   "}), 422, "group_name_invalid")
        self.refused(self.post("/groups", json={"name": "8А", "id": 5}), 422, "invalid_request")  # no extra fields
        other = self.group("8А")

        gid = made["id"]
        added = self.ok(self.post(f"/groups/{gid}/members", json={"user_ids": [self.ivanov.id, self.petrova.id]}))
        self.assertEqual(added, {"added": ["ivanov.p", "petrova.a"], "already": []})
        self.assertEqual(self.ok(self.post(f"/groups/{gid}/members", json={"user_ids": [self.ivanov.id]}))["already"],
                         ["ivanov.p"])
        body = self.refused(self.post(f"/groups/{gid}/members", json={"user_ids": [self.boss.id]}), 422,
                            "not_a_student")
        self.assertEqual(body["logins"], ["boss"])
        body = self.refused(self.post(f"/groups/{gid}/members", json={"user_ids": [9999, 9998]}), 404, "user_not_found")
        self.assertEqual(body["problems"], ["no user 9999", "no user 9998"])
        self.refused(self.post(f"/groups/{gid}/members", json={"user_ids": []}), 422, "invalid_request")
        removed = self.ok(self.post(f"/groups/{gid}/members/remove", json={"user_ids": [self.petrova.id, self.boss.id]}))
        self.assertEqual(removed, {"removed": ["petrova.a"], "not_members": ["boss"]})

        shown = self.ok(self.get(f"/groups/{gid}"))
        self.assertEqual([s["login"] for s in shown["students"]], ["ivanov.p"])
        history = self.ok(self.get(f"/groups/{gid}", params={"history": "true"}))["students"]
        self.assertEqual([(s["login"], s["removed_at"] is not None) for s in history],
                         [("ivanov.p", False), ("petrova.a", True)])
        self.assertTrue(history[1]["removed_at"].endswith("Z"))

        self.assertEqual(self.ok(self.put(f"/groups/{gid}", json={"name": "7Б"}))["name"], "7Б")
        self.refused(self.put(f"/groups/{other['id']}", json={"name": "7б"}), 409, "group_name_taken")
        listed = self.ok(self.get("/groups", params={"q": "7"}))["items"]
        self.assertEqual([i["name"] for i in listed], ["7Б"])

        self.ok(self.put("/contests/lesson", json=contest()), 201)  # attached to a contest
        archived = self.ok(self.post(f"/groups/{gid}/archive"))
        self.assertEqual(archived["contests"], ["lesson"])  # K6
        self.assertIsNotNone(archived["group"]["archived_at"])
        self.assertEqual([c["slug"] for c in archived["group"]["contests"]], ["lesson"])
        self.refused(self.post(f"/groups/{gid}/members", json={"user_ids": [self.petrova.id]}), 409, "group_archived")
        self.assertEqual([i["name"] for i in self.ok(self.get("/groups"))["items"]], ["8А"])
        self.assertEqual([i["name"] for i in self.ok(self.get("/groups", params={"archived": "true"}))["items"]], ["7Б"])
        self.assertEqual(len(self.ok(self.get("/groups", params={"archived": "all"}))["items"]), 2)
        self.group("7Б")  # the name of an archived group is free again
        self.refused(self.post(f"/groups/{gid}/unarchive"), 409, "group_name_taken")
        self.refused(self.get("/groups/999"), 404, "group_not_found")

        actions = self.s.execute(select(AuditLog.action, AuditLog.actor_user_id)
                                 .where(AuditLog.action.like("group.%")).order_by(AuditLog.id)).all()
        self.assertEqual({who for _a, who in actions}, {self.boss.id})  # the admin of the session, not the console
        self.assertEqual([a for a, _who in actions][:4], ["group.create", "group.create", "group.add_members",
                                                          "group.remove_members"])

    # ---------------------------------------------------------------- extensions
    def test_extensions(self):
        group = self.group()
        self.ok(self.put("/contests/lesson", json=contest()), 201)
        self.ok(self.put("/contests/other", json=contest("other", mode="code", groups=())), 201)
        path = "/contests/lesson/problems/A/extensions"

        first = self.post(path, json={"user_id": self.ivanov.id, "due_at": "2026-10-27 20:00", "reason": "болел"})
        made = self.ok(first, 201)
        self.assertEqual((made["replaced"], made["extension"]["user"], made["extension"]["due_at"],
                          made["extension"]["base_due_at"]), (None, "ivanov.p", "2026-10-27T20:00:00Z",
                                                              "2026-10-27T18:00:00Z"))  # JUDGE_TIMEZONE is UTC here
        again = self.ok(self.post(path, json={"user_id": self.ivanov.id, "due_at": "2026-10-27T21:00:00Z"}))  # 200
        self.assertEqual((again["extension"]["id"], again["replaced"]), (made["extension"]["id"], "2026-10-27T20:00:00Z"))
        by_group = self.ok(self.post(path, json={"group_id": group["id"], "due_at": "2026-10-27 22:00"}), 201)

        self.refused(self.post(path, json={"user_id": self.petrova.id, "due_at": "2026-10-27 18:00"}), 422,
                     "extension_not_later")
        self.refused(self.post(path, json={"user_id": self.petrova.id, "group_id": group["id"],
                                           "due_at": "2026-10-27 19:00"}), 422, "extension_subject")
        self.refused(self.post(path, json={"due_at": "2026-10-27 19:00"}), 422, "extension_subject")
        self.refused(self.post(path, json={"user_id": self.boss.id, "due_at": "2026-10-27 19:00"}), 422, "not_a_student")
        self.refused(self.post(path, json={"user_id": 999, "due_at": "2026-10-27 19:00"}), 404, "user_not_found")
        self.refused(self.post(path, json={"group_id": 999, "due_at": "2026-10-27 19:00"}), 404, "group_not_found")
        self.refused(self.post(path, json={"user_id": self.petrova.id, "due_at": "завтра"}), 422, "due_at_invalid")
        self.refused(self.post("/contests/lesson/problems/Z/extensions", json={"user_id": self.petrova.id,
                                                                               "due_at": "2026-10-27 19:00"}),
                     404, "contest_problem_not_found")
        self.ok(self.put("/contests/nodue", json=contest("nodue", mode="code", groups=(), due=None)), 201)
        self.refused(self.post("/contests/nodue/problems/A/extensions",
                               json={"user_id": self.petrova.id, "due_at": "2026-10-27 19:00"}), 409, "no_deadline")

        items = self.ok(self.get(path))["items"]
        self.assertEqual([(i["user"], i["group"]) for i in items], [("ivanov.p", None), (None, "7Б")])
        self.assertEqual(self.ok(self.get("/contests/lesson/extensions"))["items"], items)
        self.assertEqual(self.ok(self.get("/contests/lesson/extensions", params={"label": "A"}))["items"], items)

        # an extension of another contest cannot be deleted through this one, nor through a wrong label
        other = self.ok(self.post("/contests/other/problems/A/extensions",
                                  json={"user_id": self.petrova.id, "due_at": "2026-10-27 19:00"}), 201)
        other_id = other["extension"]["id"]
        self.refused(self.post(f"{path}/{other_id}/delete"), 404, "extension_not_found")
        self.refused(self.post(f"/contests/lesson/problems/Z/extensions/{other_id}/delete"), 404,
                     "contest_problem_not_found")
        self.assertIsNotNone(self.s.get(DeadlineExtension, other_id))
        r = self.post(f"{path}/{by_group['extension']['id']}/delete")
        self.assertEqual(r.status_code, 204, r.text)
        self.assertEqual([i["user"] for i in self.ok(self.get(path))["items"]], ["ivanov.p"])
        self.refused(self.post(f"{path}/{by_group['extension']['id']}/delete"), 404, "extension_not_found")
        entry = self.s.execute(select(AuditLog).where(AuditLog.action == "contest.extension_add")
                               .order_by(AuditLog.id.desc())).scalars().first()
        self.assertEqual(entry.actor_user_id, self.boss.id)

    # ---------------------------------------------------------------- participants
    def test_participants_show_how_they_came_in_and_whether_it_still_gives_access(self):
        group = self.group()
        self.ok(self.post(f"/groups/{group['id']}/members", json={"user_ids": [self.ivanov.id, self.petrova.id]}))
        self.ok(self.put("/contests/lesson", json=contest(mode="code_or_groups")), 201)
        self.ok(self.post("/contests/lesson/publish"))
        c = service.get_contest(self.s, "lesson")
        for student in (self.ivanov, self.petrova):
            service.enter(self.s, self.s.get(User, student.id), c, self.now)
        self.s.commit()
        self.ok(self.post(f"/groups/{group['id']}/members/remove", json={"user_ids": [self.petrova.id]}))
        items = self.ok(self.get("/contests/lesson/participants"))["items"]
        self.assertEqual(sorted((i["login"], i["joined_via"], i["access_now"]) for i in items),
                         [("ivanov.p", "group", True), ("petrova.a", "group", False)])
        self.assertEqual(g.contest_member_ids(self.s, c), {self.ivanov.id})

    # ---------------------------------------------------------------- who may call these
    def test_students_and_half_logged_in_admins_get_nothing(self):
        group = self.group()
        self.ok(self.put("/contests/lesson", json=contest()), 201)
        student = self.browser()
        sh = self.csrf(self.login(student, "ivanov.p", self.ivanov_password))
        _admin, password, _secret = self.make_admin("half")
        half = self.browser()
        hh = self.csrf(self.login(half, "half", password))  # password given, TOTP not yet
        calls = [("get", "/groups", None), ("post", "/groups", {"name": "x"}), ("get", f"/groups/{group['id']}", None),
                 ("put", f"/groups/{group['id']}", {"name": "x"}), ("post", f"/groups/{group['id']}/archive", None),
                 ("post", f"/groups/{group['id']}/unarchive", None),
                 ("post", f"/groups/{group['id']}/members", {"user_ids": [self.ivanov.id]}),
                 ("post", f"/groups/{group['id']}/members/remove", {"user_ids": [self.ivanov.id]}),
                 ("get", "/contests/lesson/extensions", None), ("get", "/contests/lesson/problems/A/extensions", None),
                 ("post", "/contests/lesson/problems/A/extensions", {"user_id": self.ivanov.id,
                                                                     "due_at": "2026-10-28 10:00"}),
                 ("post", "/contests/lesson/problems/A/extensions/1/delete", None),
                 ("get", "/contests/lesson/participants", None)]
        for method, path, body in calls:
            with self.subTest(path=path, method=method):
                kw = {"json": body} if body is not None else {}
                r = getattr(student, method)(f"/api/admin{path}", headers=sh, **kw)
                self.assertEqual((r.status_code, r.json()["detail"]), (403, "admin_required"))
                r = getattr(half, method)(f"/api/admin{path}", headers=hh, **kw)
                self.assertEqual((r.status_code, r.json()["detail"]), (401, "mfa_required"))
                if method != "get":
                    r = getattr(self.b, method)(f"/api/admin{path}", **kw)  # the admin, without the CSRF token
                    self.assertEqual((r.status_code, r.json()["detail"]), (403, "csrf_token_required"))
        self.assertEqual(self.s.execute(select(DeadlineExtension)).all(), [])
        self.assertEqual(self.ok(self.get(f"/groups/{group['id']}"))["students"], [])
