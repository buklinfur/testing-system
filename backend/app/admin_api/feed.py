"""/api/admin class feed: items, their audience, the feed of a group, deadline extensions of assignments (D-059,
docs/ADMIN_API.md §5.8, docs/CLASSROOM.md). The same services as `judgectl item` (app/classroom/items.py).

Items are addressed by id, groups and students by id. Times come as text like contest.yaml (without an offset in
JUDGE_TIMEZONE) and go out in UTC with Z. Titles, statements, names and reasons are untrusted text (ADMIN_API §4.1):
they go out as they are. STATUS is the one table "refusal code -> HTTP status" of the feed, works and comments
endpoints; a unit test checks it against every code the services can raise.
"""
from __future__ import annotations

from typing import Optional, Union

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr
from sqlalchemy.orm import Session

from ..api_errors import ApiError
from ..auth import audit
from ..auth_api import get_db, now
from ..blobstore import BlobStore
from ..classroom import items
from ..classroom.items import FeedError
from ..contests import service as contests
from ..contests.service import ContestError
from ..jsonable import plain
from ..models import FeedItem, Group, User
from ..timeutil import TimezoneError, parse_local, zone
from .deps import Page, admin_actor, page, paginate, users_by_id

router = APIRouter()

# every refusal code of the class feed (FeedError, the Decision codes of classroom/access.py, grading.py and
# comment_rules.py, the ExtensionError codes) and of these endpoints -> HTTP status: 404 no such thing, 409 the state
# forbids it now, 422 the input is never valid, 403 not yours, 429 too often. tests/test_admin_feed_units.py keeps it
# complete.
STATUS = {
    # 404
    "item_not_found": 404, "user_not_found": 404, "group_not_found": 404, "contest_not_found": 404,
    "extension_not_found": 404, "work_not_found": 404, "thread_not_found": 404, "comment_not_found": 404,
    "file_not_found": 404, "attachment_not_found": 404,
    # 422: the input
    "title_invalid": 422, "kind_not_available": 422, "contest_required": 422, "item_invalid": 422,
    "grading_invalid": 422, "max_points_invalid": 422, "answer_kind_invalid": 422, "deadline_invalid": 422,
    "score_invalid": 422, "score_required": 422, "score_out_of_range": 422, "grade_kind_mismatch": 422,
    "passed_required": 422, "grade_not_expected": 422, "comment_too_long": 422, "comment_required": 422,
    "not_a_student": 422, "extension_subject": 422, "extension_not_later": 422, "extension_before_available": 422,
    "missing_needs_item": 422,
    "confirmation_mismatch": 422, "due_at_invalid": 422, "available_from_invalid": 422,
    "comment_invalid": 422, "reason_too_long": 422,
    # files and links (4v.3): the file, its name and the link are input; the order must list the attachments
    "file_type_not_allowed": 422, "file_content_mismatch": 422, "file_name_invalid": 422, "file_empty": 422,
    "file_too_large": 422, "duplicate_name": 422, "link_invalid": 422, "link_title_invalid": 422,
    "attachment_order_invalid": 422,
    # 409: the state
    "grading_locked": 409, "no_audience": 409, "item_archived": 409, "item_not_published": 409,
    "item_not_draft": 409, "item_has_works": 409, "item_has_extensions": 409, "group_archived": 409,
    "not_published": 409, "not_addressed": 409, "not_an_assignment": 409, "no_deadline": 409,
    "nothing_to_return": 409, "already_returned": 409, "release_not_needed": 409, "already_deleted": 409,
    "too_many_attachments": 409,
    # the results of a bulk release per student (in a 200 answer), never a refusal of an admin request
    "nothing_to_release": 409, "already_released": 409,
    # a student's hand-in only: no admin endpoint hands work in (the student endpoints of 4b.2 use these)
    "already_graded": 409, "deadline_passed": 409, "not_available_yet": 409, "no_access": 403,
    "answer_required": 422, "text_not_expected": 422, "text_too_long": 422, "files_required": 422,
    "files_not_expected": 422, "quota_exceeded": 409, "too_many_files": 422, "submission_too_large": 422,
    # a student's comment only (4b.2; D-077): teachers write with comments off and in contest items, delete any
    # comment and have no rate
    "comments_disabled": 409, "comments_teacher_only": 409, "not_your_comment": 403, "rate_limited": 429,
}
# defensive codes no request can reach today: an extension of an item is later than due_at, which a CHECK keeps
# later than available_from; a draft cannot have works (a hand-in and a grade need a published item). Of the files
# (4v.3): an upload over JUDGE_ATTACHMENT_MAX_MB is 413 too_large from save_body before the service could say
# file_too_large (judgectl reaches it); the name is cleaned first (content_files.clean_name: never empty, no
# separators or control characters, cut to 150), so file_name_invalid stays the rules' guard.
DEFENSIVE = frozenset({"extension_before_available", "item_has_works", "file_too_large", "file_name_invalid"})
# codes that come only as per-student results of POST /items/{id}/release
RESULT_ONLY = frozenset({"nothing_to_release", "already_released"})
# codes no admin request can produce: only a student's hand-in (D-063; the test of 4b.2, ROADMAP)
HAND_IN_ONLY = frozenset({"already_graded", "deadline_passed", "not_available_yet", "no_access", "answer_required",
                          "text_not_expected", "text_too_long", "files_required", "files_not_expected",
                          "quota_exceeded", "too_many_files", "submission_too_large"})
# codes of services whose admin routes come in a later stage: none now (file_not_found has its route since 4v.3
# stage 5); a test keeps it empty unless a stage says why
NO_ROUTE_YET: frozenset = frozenset()
# codes no admin request can produce: only a student's comment (D-077; the test of 4b.2, ROADMAP)
COMMENT_STUDENT_ONLY = frozenset({"comments_disabled", "comments_teacher_only", "not_your_comment", "rate_limited"})

POINTS = Union[StrictStr, StrictInt, StrictFloat]  # a JSON number or a string: grading.parse_points decides


def refused(exc: FeedError) -> ApiError:
    return ApiError(STATUS[exc.code], exc.code, str(exc), problems=exc.problems if len(exc.problems) > 1 else None,
                    **exc.data)


def blobs(request: Request) -> BlobStore:
    return BlobStore(request.app.state.settings.blob_dir)


def get_item(db: Session, item_id: int, lock: bool = False) -> FeedItem:
    try:
        return items.get_item(db, item_id, lock=lock)
    except FeedError as exc:
        raise refused(exc) from exc


def get_group(db: Session, group_id: int) -> Group:
    group = db.get(Group, group_id)
    if group is None:
        raise ApiError(404, "group_not_found")
    return group


def get_user(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise ApiError(404, "user_not_found")
    return user


def parse_time(request: Request, value: Optional[str], field: str):
    """A time like contest.yaml: without an offset in JUDGE_TIMEZONE; None stays None (clears the field)."""
    if value is None:
        return None
    try:
        return parse_local(value, zone(request.app.state.settings.timezone))
    except TimezoneError as exc:
        raise ApiError(500, "server_misconfigured", str(exc)) from exc
    except ValueError as exc:
        raise ApiError(422, f"{field}_invalid", f"{field}: a time like 2026-11-17 23:59:59 or with Z / +07:00") \
            from exc


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ItemFields(Body):
    """The editable fields; in PUT only the keys sent change and null clears (due_at: null)."""
    title: Optional[str] = Field(None, max_length=1000)
    body_md: Optional[str] = Field(None, max_length=200_000)
    available_from: Optional[str] = Field(None, max_length=40, description="without an offset: JUDGE_TIMEZONE")
    due_at: Optional[str] = Field(None, max_length=40, description="without an offset: JUDGE_TIMEZONE")
    after_due_statement: Optional[str] = Field(None, max_length=20, description="visible | hidden")
    after_due_submit: Optional[str] = Field(None, max_length=20, description="no | late | yes")
    grading: Optional[str] = Field(None, max_length=20, description="points | pass_fail | none")
    max_points: Optional[POINTS] = Field(None, description="with grading points: like 10 or 4.5 (two decimals)")
    answer_kind: Optional[str] = Field(None, max_length=20, description="mark | text | files | text_or_files")
    grades_release: Optional[str] = Field(None, max_length=20, description="manual | immediate")
    comments_enabled: Optional[StrictBool] = Field(None, description="students write in the shared thread "
                                                                     "(assignments only)")

    def values(self, request: Request) -> dict:
        out = {}
        for name in self.model_fields_set & set(items.EDITABLE):
            value = getattr(self, name)
            if name in ("available_from", "due_at"):
                value = parse_time(request, value, name)
            out[name] = value
        return out


class CreateBody(ItemFields):
    kind: str = Field(max_length=20, description="assignment | contest (material: from 4v.4)")
    title: str = Field(max_length=1000)
    contest: Optional[str] = Field(None, max_length=64, description="slug, for kind contest")
    group_ids: list[int] = Field(default_factory=list, max_length=200)
    user_ids: list[int] = Field(default_factory=list, max_length=200)


class AudienceBody(Body):
    group_ids: list[int] = Field(default_factory=list, max_length=200)
    user_ids: list[int] = Field(default_factory=list, max_length=200)


class DeleteBody(Body):
    confirm_id: int


class ItemExtensionBody(Body):
    user_id: Optional[int] = None  # exactly one of user_id / group_id
    group_id: Optional[int] = None
    due_at: str = Field(max_length=40, description="without an offset: JUDGE_TIMEZONE")
    reason: Optional[str] = Field(None, max_length=200)


def _groups(db: Session, ids: list[int]) -> list[Group]:
    return [get_group(db, i) for i in dict.fromkeys(ids)]


def detail(db: Session, item: FeedItem) -> dict:
    return plain(items.item_detail(db, item))


# ----------------------------------------------------------------------------- items
@router.get("/items")
def list_items(group_id: Optional[int] = None, kind: Optional[str] = None, state: Optional[str] = None,
               pg: Page = Depends(page), db: Session = Depends(get_db)):
    """Newest first by id. group_id: addressed to the group; kind: assignment | contest; state: draft | published |
    archived."""
    if kind not in (None, "assignment", "contest", "material"):
        raise ApiError(422, "item_invalid", "kind is assignment or contest")
    if state not in (None, "draft", "published", "archived"):
        raise ApiError(422, "item_invalid", "state is draft, published or archived")
    group = get_group(db, group_id) if group_id is not None else None
    query = items.items_query(group=group, kind=kind, states=[state] if state else None)
    return paginate(db, query, FeedItem.id, pg, lambda row: items.item_summary(db, row[0]))


@router.post("/items", status_code=201)
def create_item(body: CreateBody, request: Request, db: Session = Depends(get_db),
                actor: audit.Actor = Depends(admin_actor)):
    """A draft with its audience in one transaction (a refused addressee leaves no item)."""
    at = now(request)
    fields = body.values(request)
    fields.pop("title", None)
    body_md = fields.pop("body_md", None) or ""
    groups = _groups(db, body.group_ids)
    users = users_by_id(db, body.user_ids) if body.user_ids else []
    contest = None
    if body.contest is not None:
        try:
            contest = contests.get_contest(db, body.contest)
        except ContestError as exc:
            raise ApiError(404, "contest_not_found") from exc
    try:
        item = items.create_item(db, actor, at, kind=body.kind, title=body.title, body_md=body_md, contest=contest,
                                 **fields)
        if groups or users:
            items.set_audience(db, actor, item, at, groups=groups, users=users)
    except FeedError as exc:
        db.rollback()
        raise refused(exc) from exc
    db.commit()
    return detail(db, item)


@router.get("/items/{item_id}")
def get_item_detail(item_id: int, db: Session = Depends(get_db)):
    """Every setting, the contest, the audience by name, the number of extensions, works by status, `warning` (a
    contest item whose addressees may not see the contest)."""
    return detail(db, get_item(db, item_id))


@router.put("/items/{item_id}")
def update_item(item_id: int, body: ItemFields, request: Request, db: Session = Depends(get_db),
                actor: audit.Actor = Depends(admin_actor)):
    """Only the keys sent change; null clears. grading pass_fail / none without max_points clears max_points."""
    item = get_item(db, item_id, lock=True)
    try:
        changed = items.update_item(db, actor, item, now(request), **body.values(request))
    except FeedError as exc:
        db.rollback()
        raise refused(exc) from exc
    db.commit()
    return {"item": detail(db, item), "changed": changed}


def _state(db: Session, item_id: int, func, request: Request, actor: audit.Actor) -> dict:
    item = get_item(db, item_id, lock=True)
    try:
        changed = func(db, actor, item, now(request))
    except FeedError as exc:
        db.rollback()
        raise refused(exc) from exc
    db.commit()
    return {"item": detail(db, item), "changed": changed}


@router.post("/items/{item_id}/publish")
def publish_item(item_id: int, request: Request, db: Session = Depends(get_db),
                 actor: audit.Actor = Depends(admin_actor)):
    """Needs an addressee; with a future available_from the students see it from then (no scheduler)."""
    return _state(db, item_id, items.publish, request, actor)


@router.post("/items/{item_id}/archive")
def archive_item(item_id: int, request: Request, db: Session = Depends(get_db),
                 actor: audit.Actor = Depends(admin_actor)):
    """Gone from the feed, refuses hand-ins; students keep their work and released grades."""
    return _state(db, item_id, items.archive, request, actor)


@router.post("/items/{item_id}/unarchive")
def unarchive_item(item_id: int, request: Request, db: Session = Depends(get_db),
                   actor: audit.Actor = Depends(admin_actor)):
    return _state(db, item_id, items.unarchive, request, actor)


@router.post("/items/{item_id}/delete", status_code=204)
def delete_item(item_id: int, body: DeleteBody, request: Request, db: Session = Depends(get_db),
                actor: audit.Actor = Depends(admin_actor)):
    """Only a draft without works and extensions; {confirm_id} must repeat the id."""
    item = get_item(db, item_id, lock=True)
    if body.confirm_id != item.id:
        raise ApiError(422, "confirmation_mismatch", "confirm_id must be the id of the item")
    try:
        items.delete_item(db, actor, item, now(request))
    except FeedError as exc:
        db.rollback()
        raise refused(exc) from exc
    db.commit()
    return Response(status_code=204)


@router.put("/items/{item_id}/audience")
def set_audience(item_id: int, body: AudienceBody, request: Request, db: Session = Depends(get_db),
                 actor: audit.Actor = Depends(admin_actor)):
    """The audience becomes exactly these groups and students: {item, added, removed}."""
    item = get_item(db, item_id, lock=True)
    groups = _groups(db, body.group_ids)
    users = users_by_id(db, body.user_ids) if body.user_ids else []
    try:
        result = items.set_audience(db, actor, item, now(request), groups=groups, users=users)
    except FeedError as exc:
        db.rollback()
        raise refused(exc) from exc
    db.commit()
    return {"item": detail(db, item), **result}


@router.get("/groups/{group_id}/feed")
def group_feed(group_id: int, drafts: bool = True, db: Session = Depends(get_db)):
    """The items addressed to the group, in the order of the feed (available_from, else published_at; newest
    first). No cursor: one group's feed."""
    group = get_group(db, group_id)
    return plain({"items": [items.item_summary(db, i) for i in items.group_feed(db, group, include_drafts=drafts)]})


# ----------------------------------------------------------------------------- extensions
@router.get("/items/{item_id}/extensions")
def list_extensions(item_id: int, db: Session = Depends(get_db)):
    return plain({"items": items.list_extensions(db, get_item(db, item_id))})


@router.post("/items/{item_id}/extensions")
def add_extension(item_id: int, body: ItemExtensionBody, request: Request, db: Session = Depends(get_db),
                  actor: audit.Actor = Depends(admin_actor)):
    """201 for a new extension; 200 when it replaced one of the same student or group (`replaced`: the old due)."""
    item = get_item(db, item_id)
    due = parse_time(request, body.due_at, "due_at")
    user = get_user(db, body.user_id) if body.user_id is not None else None
    group = get_group(db, body.group_id) if body.group_id is not None else None
    try:
        row, replaced = items.add_extension(db, actor, item, due, now(request), user=user, group=group,
                                            reason=body.reason)
    except FeedError as exc:
        db.rollback()
        raise refused(exc) from exc
    db.commit()
    found = next(e for e in items.list_extensions(db, item) if e["id"] == row.id)
    return JSONResponse(plain({"extension": found, "replaced": replaced}), status_code=200 if replaced else 201)


@router.post("/items/{item_id}/extensions/{extension_id}/delete", status_code=204)
def delete_extension(item_id: int, extension_id: int, request: Request, db: Session = Depends(get_db),
                     actor: audit.Actor = Depends(admin_actor)):
    """Only an extension of this item: the id of another one (of a contest problem too) is 404, like a missing one."""
    item = get_item(db, item_id)
    try:
        items.remove_extension(db, actor, item, extension_id, now(request))
    except FeedError as exc:
        db.rollback()
        raise refused(exc) from exc
    db.commit()
    return Response(status_code=204)

