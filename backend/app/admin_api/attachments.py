"""/api/admin a teacher's files and links of a feed item (D-078..D-082; docs/ADMIN_API.md §5.10, docs/CLASSROOM.md
§10). The same services as `judgectl item attach | link | attachments | detach | attachment-order`.

The upload is the raw body (Content-Type application/octet-stream), one file per request, its name in ?name= (UTF-8,
percent-encoded). The route is exempt from the 1 MB JSON limit by its path pattern (routes.UPLOAD_PATTERNS) and
reads the body itself (http_body.save_body, limit JUDGE_ATTACHMENT_MAX_MB: 413 too_large before the service). Every
cheap refusal comes before a byte of the body is read, in this order: the Origin (the public app), the admin session
and its CSRF token (admin_session), the Content-Type (415), the name (422), the item (404) and what can be decided
without the body (attachments.precheck_file: archived, the number of attachments, the type by the name, a repeated
name). Only then is the body written into <blob root>/tmp (blobstore.Incoming); the row of the item is locked
(FOR UPDATE) only inside attachments.add_file, after the body, never while it is being received; a refused or broken
upload leaves nothing in the store or its tmp.

Files go out through content_files.file_response(..., request=request), only after attachments.item_file found
the file of this item (another item's id, a link or a missing one: 404 attachment_not_found). Names, URLs and titles
are untrusted text (ADMIN_API §4.1) and go out as they are. Ids in these paths are int4 (Path le=DB_ID_MAX): a
larger number is 422, never a database error.
"""
from __future__ import annotations

from typing import Annotated, Optional
from urllib.parse import unquote_to_bytes

from fastapi import APIRouter, Depends, Path, Request, Response
from pydantic import Field, StrictInt, StrictStr
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..api_errors import ApiError
from ..auth import audit
from ..auth_api import Current, get_db, now
from ..classroom import attachments
from ..classroom.items import FeedError
from ..content_files import file_response
from ..http_body import require_type, save_body
from ..jsonable import plain
from .deps import admin_actor, admin_session
from .feed import Body, blobs, get_item, refused

router = APIRouter()

DB_ID_MAX = 2_147_483_647  # the ids are int4: a larger one in a path is 422, not a DataError (500)
DbId = Annotated[int, Path(ge=1, le=DB_ID_MAX)]
UPLOAD_TYPE = "application/octet-stream"

UPLOAD_DOC = {
    "parameters": [{"name": "name", "in": "query", "required": True, "schema": {"type": "string"},
                    "description": "the file name with its extension, UTF-8 percent-encoded (encodeURIComponent)"}],
    "requestBody": {"required": True, "content": {UPLOAD_TYPE: {"schema": {"type": "string", "format": "binary"}}}},
}


class LinkBody(Body):
    """Limits of the schema above those of the rules (2000 and 200 characters): a longer value is link_invalid /
    link_title_invalid from the rules, not invalid_request."""
    url: StrictStr = Field(max_length=10_000)
    title: Optional[StrictStr] = Field(None, max_length=1_000)


class OrderBody(Body):
    ids: list[StrictInt] = Field(max_length=200)


def upload_name(request: Request) -> str:
    """?name= decoded strictly from the raw query string. Starlette's query_params replaces bytes that are not
    UTF-8 with U+FFFD; a name changed that way would be stored silently, so a broken encoding is 422 instead. The name
    must come exactly once and not be blank; the service then cleans it (content_files.clean_name) and checks it."""
    found = []
    for pair in request.scope.get("query_string", b"").split(b"&"):
        key, _sep, value = pair.partition(b"=")
        if pair and unquote_to_bytes(key.replace(b"+", b" ")) == b"name":
            found.append(value)
    if len(found) != 1:
        raise ApiError(422, "invalid_request", problems=["name: give the file name once, as ?name=<percent-encoded "
                                                         "UTF-8>"])
    try:
        name = unquote_to_bytes(found[0].replace(b"+", b" ")).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ApiError(422, "invalid_request", problems=["name: not UTF-8 after percent-decoding"]) from exc
    if not name.strip():
        raise ApiError(422, "invalid_request", problems=["name: empty"])
    return name


def _item(db: Session, item_id: int):
    return get_item(db, item_id)


@router.get("/items/{item_id}/attachments")
def list_item_attachments(item_id: DbId, db: Session = Depends(get_db)):
    """The files (id, name, size, sha256) and links (id, url, title) of the item, in their order."""
    item = _item(db, item_id)
    return plain({"items": [attachments.describe(r) for r in attachments.list_attachments(db, item)]})


@router.post("/items/{item_id}/attachments", status_code=201, openapi_extra=UPLOAD_DOC)
async def upload_attachment(item_id: DbId, request: Request, db: Session = Depends(get_db),
                            actor: audit.Actor = Depends(admin_actor)):
    """A file attachment, appended: the raw body (application/octet-stream), the name in ?name=. 201 with the
    attachment. 415 another Content-Type; 422 invalid_request (no name, twice, broken UTF-8), file_type_not_allowed,
    file_content_mismatch, file_empty, duplicate_name; 409 item_archived, too_many_attachments; 413 too_large (more
    than JUDGE_ATTACHMENT_MAX_MB, before the service)."""
    require_type(request, UPLOAD_TYPE)
    name = upload_name(request)
    settings = request.app.state.settings
    limits = attachments.teacher_limits(settings.files)

    def precheck():
        try:
            item = _item(db, item_id)
            attachments.precheck_file(db, item, name, limits)
            return item
        except FeedError as exc:
            raise refused(exc) from exc
        finally:
            db.rollback()  # no transaction (and no lock) is held while the body is received

    item = await run_in_threadpool(precheck)
    with blobs(request).incoming(name) as upload:
        await save_body(request, upload, settings.files.attachment_max_bytes)

        def work():
            try:
                row = attachments.add_file(db, actor, item, upload, now(request), limits=limits)
            except FeedError as exc:
                db.rollback()
                raise refused(exc) from exc
            db.commit()
            return plain(attachments.describe(row))

        return await run_in_threadpool(work)


@router.post("/items/{item_id}/links", status_code=201)
def add_item_link(item_id: DbId, body: LinkBody, request: Request, db: Session = Depends(get_db),
             actor: audit.Actor = Depends(admin_actor)):
    """A link attachment, appended: stored as sent after strip (a blank title: none); the server never opens it."""
    item = _item(db, item_id)
    try:
        row = attachments.add_link(db, actor, item, body.url, now(request), title=body.title,
                                   limits=attachments.teacher_limits(request.app.state.settings.files))
    except FeedError as exc:
        db.rollback()
        raise refused(exc) from exc
    db.commit()
    return plain(attachments.describe(row))


@router.put("/items/{item_id}/attachments/order")
def reorder_attachments(item_id: DbId, body: OrderBody, request: Request, db: Session = Depends(get_db),
            actor: audit.Actor = Depends(admin_actor)):
    """{ids}: every attachment of the item exactly once (attachment_order_invalid otherwise, with missing, extra and
    repeated). The attachments in the new order."""
    item = _item(db, item_id)
    try:
        rows = attachments.reorder(db, actor, item, body.ids, now(request))
    except FeedError as exc:
        db.rollback()
        raise refused(exc) from exc
    db.commit()
    return plain({"items": [attachments.describe(r) for r in rows]})


@router.get("/items/{item_id}/attachments/{attachment_id}")
def download_attachment(item_id: DbId, attachment_id: DbId, request: Request, db: Session = Depends(get_db),
             cur: Current = Depends(admin_session)):
    """The file of a file attachment of this item (docs/FILES.md: the type by name and content, ETag, 304). 404
    attachment_not_found: no such attachment of this item (another item's included) or a link."""
    item = _item(db, item_id)
    try:
        row = attachments.item_file(db, cur.user, item, attachment_id, now(request))
    except FeedError as exc:
        raise refused(exc) from exc
    return file_response(blobs(request), row.blob_sha256, row.name, request=request)


@router.post("/items/{item_id}/attachments/{attachment_id}/delete", status_code=204)
def delete_attachment(item_id: DbId, attachment_id: DbId, request: Request, db: Session = Depends(get_db),
           actor: audit.Actor = Depends(admin_actor)):
    """Remove an attachment of this item (the blob stays; the audit keeps name, sha256 and size, or url and title).
    Another item's id: 404 attachment_not_found."""
    item = _item(db, item_id)
    try:
        attachments.remove(db, actor, item, attachment_id, now(request))
    except FeedError as exc:
        db.rollback()
        raise refused(exc) from exc
    db.commit()
    return Response(status_code=204)
