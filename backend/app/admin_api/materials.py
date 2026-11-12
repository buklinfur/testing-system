"""/api/admin the library of materials and «Справочник» (4v.4; docs/ADMIN_API.md §5.11, docs/CLASSROOM.md §11,
D-083..D-087). The same services as `judgectl material ...` / `section ...` (materials/service.py, library.py).

Materials: a material is addressed by its id (int4, DbId); its slug never changes (PATCH with another slug: 422
slug_immutable). Every change of the content - a new text, a file or a link added, one removed, a new order - makes a
version and answers it (`version`, `created`: false when nothing changed); links [[памятка:slug]] that are malformed,
unknown or archived never refuse a text: they come back as `warnings`. Ids of attachments are those of the latest
version; GET ...?version=N reads an older one.

The upload of a file of a version follows §5.10 exactly: the raw body (application/octet-stream), its name in
?name=, the path exempt from the 1 MB JSON limit by routes.UPLOAD_PATTERNS, and every cheap refusal before a byte of
the body is read, in this order: the Origin (the public app), the admin session and its CSRF token (admin_session),
the Content-Type (415), the name (422), the material (404) and what can be decided without the body
(materials.precheck_file: archived, the number of attachments, the type by the name, a repeated name; no lock, the
transaction rolled back before the body). The row of the material is locked (FOR UPDATE) only inside the services,
after the body; a refused or broken upload leaves nothing in the store or its tmp.

The library: sections two levels deep, materials in them in the teacher's order, GET /library as an admin sees it
(every section, archived materials marked) or the search ?q= (ILIKE, %, _ and \\ literal). Changes of the library
take locks.LIBRARY inside the services. Titles, texts, names and URLs are untrusted text (§4.1), returned as they
are. A student has no endpoint here (4b.2); /materials/resolve answers the admin's view: ok, archived or unknown.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field, StrictInt, StrictStr
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..api_errors import ApiError
from ..auth import audit
from ..auth_api import Current, get_db, now
from ..classroom import attachments as att
from ..classroom.items import FeedError
from ..content_files import file_response
from ..http_body import require_type, save_body
from ..jsonable import plain
from ..materials import library, refs
from ..materials import service as materials
from ..models import Material
from .attachments import UPLOAD_DOC, UPLOAD_TYPE, DbId, LinkBody, OrderBody, upload_name
from .deps import Page, admin_actor, admin_session, page, paginate
from .feed import Body, DeleteBody, blobs, get_group, refused

router = APIRouter()

BODY_MAX = 200_000  # the schema's bound above the rules' 100 000 characters: a longer text is body_too_long


class MaterialCreate(Body):
    slug: StrictStr = Field(max_length=200, description="a-z, 0-9 and -, 2..63 characters; never changes")
    title: StrictStr = Field(max_length=1000)
    body_md: StrictStr = Field("", max_length=BODY_MAX, description="Markdown; links [[памятка:slug]]")
    library: StrictStr = Field("none", max_length=20, description="none | all_students | groups")
    group_ids: list[StrictInt] = Field(default_factory=list, max_length=200,
                                       description="the library groups (with library groups)")


class MaterialPatch(Body):
    """Only the keys sent change. slug: only the current one is accepted (another: 422 slug_immutable)."""
    title: Optional[StrictStr] = Field(None, max_length=1000)
    library: Optional[StrictStr] = Field(None, max_length=20, description="none | all_students | groups")
    slug: Optional[StrictStr] = Field(None, max_length=200, description="never changes")


class VersionBody(Body):
    body_md: StrictStr = Field(max_length=BODY_MAX)


class GroupsBody(Body):
    group_ids: list[StrictInt] = Field(max_length=200)


class ResolveBody(Body):
    text: StrictStr = Field(max_length=BODY_MAX)


class SectionCreate(Body):
    title: StrictStr = Field(max_length=1000)
    parent_id: Optional[StrictInt] = Field(None, description="a top-level section: this one becomes its subsection")


class SectionPatch(Body):
    title: StrictStr = Field(max_length=1000)


class SectionMove(Body):
    parent_id: Optional[StrictInt] = Field(description="a top-level section, or null for the top level")


class SectionOrder(Body):
    parent_id: Optional[StrictInt] = Field(None, description="the subsections of this section; null: the top level")
    ids: list[StrictInt] = Field(max_length=500)


class PlaceBody(Body):
    material_id: StrictInt


# ----------------------------------------------------------------------------- helpers
def _material(db: Session, material_id: int) -> Material:
    try:
        return materials.get_material(db, material_id)
    except FeedError as exc:
        raise refused(exc) from exc


def _section(db: Session, section_id: Optional[int]):
    if section_id is None:
        return None
    try:
        return library.get_section(db, section_id)
    except FeedError as exc:
        raise refused(exc) from exc


def _change(db: Session, func):
    """Run a change; a refusal rolls back and answers its code, success commits."""
    try:
        out = func()
    except FeedError as exc:
        db.rollback()
        raise refused(exc) from exc
    db.commit()
    return out


def _view(db: Session, viewer, material: Material, request: Request) -> dict:
    out = materials.material_view(db, viewer, material, now(request))
    out["link_warnings"] = refs.warnings(db, {"body": out["body_md"]})
    return plain(out)


def _version_out(db: Session, material: Material, version, created: bool = True, warnings=()) -> dict:
    return plain({"material_id": material.id, "slug": material.slug, "version": version.version,
                  "created": created, "warnings": list(warnings),
                  "attachments": [att.describe(r) for r in materials.version_attachments(db, version)]})


# ----------------------------------------------------------------------------- materials
@router.get("/materials")
def list_materials(archived: bool = False, pg: Page = Depends(page), db: Session = Depends(get_db)):
    """Newest first by id: slug, title, library audience and groups, sections, number of versions. archived=true:
    archived ones too."""
    query = select(Material)
    if not archived:
        query = query.where(Material.archived_at.is_(None))
    return paginate(db, query, Material.id, pg, lambda row: materials.material_summary(db, row[0]))


@router.post("/materials", status_code=201)
def create_material(body: MaterialCreate, request: Request, db: Session = Depends(get_db),
                    cur: Current = Depends(admin_session), actor: audit.Actor = Depends(admin_actor)):
    """A material and its version 1 (with its library groups in one transaction). 409 slug_taken (archived ones
    too); 422 slug_invalid, title_invalid, body_invalid, body_too_long, library_invalid. The material as GET answers
    it, plus `warnings` about the links of the text."""
    at = now(request)
    groups = [get_group(db, i) for i in dict.fromkeys(body.group_ids)]

    def work():
        result = materials.create_material(db, actor, at, slug=body.slug, title=body.title, body_md=body.body_md,
                                           library=body.library)
        material = materials.get_material(db, result.version.material_id)
        if groups:
            materials.set_library_groups(db, actor, material, at, groups)
        return material, result.warnings
    material, warnings = _change(db, work)
    return {**_view(db, cur.user, material, request), "warnings": warnings}


@router.post("/materials/resolve")
def resolve_links(body: ResolveBody, request: Request, db: Session = Depends(get_db),
                  cur: Current = Depends(admin_session)):
    """The links [[памятка:slug]] of a text as they would be saved: `links` - one per distinct slug, status ok,
    archived or unknown (with id and title when the material exists); `warnings` - what judgectl prints. Writes
    nothing."""
    return plain({"links": materials.resolve(db, cur.user, [body.text], now(request)),
                  "warnings": refs.warnings(db, {"text": body.text})})


@router.get("/materials/{material_id}")
def get_material(material_id: DbId, request: Request, db: Session = Depends(get_db),
                 cur: Current = Depends(admin_session)):
    """The latest version: text, attachments, its links resolved (ok / archived / unknown) and `link_warnings`; the
    library audience, groups, sections, number of versions, archived_at."""
    return _view(db, cur.user, _material(db, material_id), request)


@router.patch("/materials/{material_id}")
def patch_material(material_id: DbId, body: MaterialPatch, request: Request, db: Session = Depends(get_db),
                   actor: audit.Actor = Depends(admin_actor)):
    """The title and the library audience, in place (no version). A slug other than the current one: 422
    slug_immutable (create a new material and archive this one). 409 material_archived."""
    material = _material(db, material_id)
    values = {name: getattr(body, name) for name in body.model_fields_set}
    if any(v is None for v in values.values()):
        raise ApiError(422, "invalid_request", problems=[f"{n}: null" for n, v in values.items() if v is None])
    changed = _change(db, lambda: materials.update_material(db, actor, material, now(request), **values))
    return {"material": plain(materials.material_summary(db, material)), "changed": changed}


@router.post("/materials/{material_id}/versions")
def new_version(material_id: DbId, body: VersionBody, request: Request, db: Session = Depends(get_db),
                actor: audit.Actor = Depends(admin_actor)):
    """A new text: a new version with the attachments of the latest one (`created`: false and the latest version
    when the text is the same). `warnings` about its links. 409 material_archived; 422 body_invalid, body_too_long."""
    material = _material(db, material_id)
    result = _change(db, lambda: materials.new_version(db, actor, material, now(request), body.body_md))
    return _version_out(db, material, result.version, result.created, result.warnings)


@router.get("/materials/{material_id}/versions")
def list_versions(material_id: DbId, db: Session = Depends(get_db)):
    """Every version, newest first: number, when, who, length of the text, number of attachments."""
    return plain({"items": materials.versions(db, _material(db, material_id))})


@router.get("/materials/{material_id}/versions/{number}")
def get_version(material_id: DbId, number: DbId, request: Request, db: Session = Depends(get_db),
                cur: Current = Depends(admin_session)):
    """One version: its text, attachments and links resolved now. 404 version_not_found."""
    material = _material(db, material_id)
    try:
        out = materials.version_detail(db, material, number)
    except FeedError as exc:
        raise refused(exc) from exc
    out["links"] = materials.resolve(db, cur.user, [out["body_md"]], now(request))
    return plain(out)


@router.put("/materials/{material_id}/groups")
def set_groups(material_id: DbId, body: GroupsBody, request: Request, db: Session = Depends(get_db),
               actor: audit.Actor = Depends(admin_actor)):
    """The library groups: exactly these (they count with library groups). A group added must not be archived (409
    group_archived); one already there stays. 409 material_archived."""
    material = _material(db, material_id)
    groups = [get_group(db, i) for i in dict.fromkeys(body.group_ids)]
    _change(db, lambda: materials.set_library_groups(db, actor, material, now(request), groups))
    return plain(materials.material_summary(db, material))


def _archive(db: Session, material_id: int, func, request: Request, actor: audit.Actor) -> dict:
    material = _material(db, material_id)
    changed = _change(db, lambda: func(db, actor, material, now(request)))
    return {"material": plain(materials.material_summary(db, material)), "changed": changed}


@router.post("/materials/{material_id}/archive")
def archive_material(material_id: DbId, request: Request, db: Session = Depends(get_db),
                     actor: audit.Actor = Depends(admin_actor)):
    """Read only and out of the library; the feed and links still open it. Again: changed false."""
    return _archive(db, material_id, materials.archive, request, actor)


@router.post("/materials/{material_id}/unarchive")
def unarchive_material(material_id: DbId, request: Request, db: Session = Depends(get_db),
                       actor: audit.Actor = Depends(admin_actor)):
    """Back from the archive (its sections were kept). Again: changed false."""
    return _archive(db, material_id, materials.unarchive, request, actor)


@router.get("/materials/{material_id}/where")
def where_used(material_id: DbId, db: Session = Depends(get_db)):
    """Material items of it, feed items linking it, problem versions linking it (slug@version), materials whose
    latest version links it."""
    return plain(materials.where_used(db, _material(db, material_id)))


# ----------------------------------------------------------------------------- attachments of a version
@router.post("/materials/{material_id}/attachments", status_code=201, openapi_extra=UPLOAD_DOC)
async def upload_material_file(material_id: DbId, request: Request, db: Session = Depends(get_db),
                               actor: audit.Actor = Depends(admin_actor)):
    """A new version with this file appended: the raw body (application/octet-stream), the name in ?name=. 201 with
    the version and its attachments. 415 another Content-Type; 422 invalid_request (no name, twice, broken UTF-8),
    file_type_not_allowed, file_content_mismatch, file_empty, duplicate_name; 404 material_not_found; 409
    material_archived, too_many_attachments; 413 too_large (more than JUDGE_ATTACHMENT_MAX_MB, before the service)."""
    require_type(request, UPLOAD_TYPE)
    name = upload_name(request)
    settings = request.app.state.settings
    limits = att.teacher_limits(settings.files)

    def precheck():
        try:
            material = materials.get_material(db, material_id)
            materials.precheck_file(db, material, name, limits)
            return material
        except FeedError as exc:
            raise refused(exc) from exc
        finally:
            db.rollback()  # no transaction (and no lock) is held while the body is received

    material = await run_in_threadpool(precheck)
    with blobs(request).incoming(name) as upload:
        await save_body(request, upload, settings.files.attachment_max_bytes)

        def work():
            version = _change(db, lambda: materials.add_file(db, actor, material, upload, now(request),
                                                             limits=limits))
            return _version_out(db, material, version)

        return await run_in_threadpool(work)


@router.post("/materials/{material_id}/links", status_code=201)
def add_material_link(material_id: DbId, body: LinkBody, request: Request, db: Session = Depends(get_db),
                      actor: audit.Actor = Depends(admin_actor)):
    """A new version with this link appended (stored after strip; the server never opens it)."""
    material = _material(db, material_id)
    version = _change(db, lambda: materials.add_link(db, actor, material, body.url, now(request), title=body.title,
                                                     limits=att.teacher_limits(request.app.state.settings.files)))
    return _version_out(db, material, version)


@router.put("/materials/{material_id}/attachments/order")
def reorder_material_attachments(material_id: DbId, body: OrderBody, request: Request,
                                 db: Session = Depends(get_db), actor: audit.Actor = Depends(admin_actor)):
    """{ids}: every attachment of the latest version exactly once (attachment_order_invalid otherwise, with missing,
    extra and repeated). A new version in this order (`created`: false for the same order)."""
    material = _material(db, material_id)
    result = _change(db, lambda: materials.reorder(db, actor, material, body.ids, now(request)))
    return _version_out(db, material, result.version, result.created)


@router.get("/materials/{material_id}/attachments/{attachment_id}")
def download_material_file(material_id: DbId, attachment_id: DbId, request: Request,
                           version: Optional[int] = Query(None, ge=1, le=2_147_483_647),
                           db: Session = Depends(get_db), cur: Current = Depends(admin_session)):
    """The file of a file attachment of the latest version, or of ?version=N (docs/FILES.md: the type by name and
    content, ETag, 304). 404 attachment_not_found (not a file of that version: another version's, a link, no such
    id), version_not_found."""
    material = _material(db, material_id)
    try:
        row = materials.material_file(db, cur.user, material, attachment_id, now(request), version=version)
    except FeedError as exc:
        raise refused(exc) from exc
    return file_response(blobs(request), row.blob_sha256, row.name, request=request)


@router.post("/materials/{material_id}/attachments/{attachment_id}/delete")
def delete_material_attachment(material_id: DbId, attachment_id: DbId, request: Request,
                               db: Session = Depends(get_db), actor: audit.Actor = Depends(admin_actor)):
    """A new version without this attachment of the latest one (the blob stays with the old versions). 404
    attachment_not_found: not one of the latest version."""
    material = _material(db, material_id)
    version = _change(db, lambda: materials.remove(db, actor, material, attachment_id, now(request)))
    return _version_out(db, material, version)


# ----------------------------------------------------------------------------- the library
@router.get("/library")
def get_library(request: Request, q: Optional[str] = Query(None, max_length=200), db: Session = Depends(get_db),
                cur: Current = Depends(admin_session)):
    """The library as an admin sees it: {sections: [{id, title, materials, sections}], unsorted} - every section and
    material, archived ones marked. ?q=: {query, materials} - the search by title, any case (Cyrillic too), with %,
    _ and \\ taken literally."""
    return plain(library.library_view(db, cur.user, now(request), q=q))


@router.post("/library/sections", status_code=201)
def create_section(body: SectionCreate, request: Request, db: Session = Depends(get_db),
                   actor: audit.Actor = Depends(admin_actor)):
    """A section at the end of the top level, or with parent_id a subsection at the end of a top-level section (409
    section_too_deep under a subsection). 422 section_title_invalid."""
    parent = _section(db, body.parent_id)
    section = _change(db, lambda: library.create_section(db, actor, now(request), body.title, parent))
    return {"id": section.id, "title": section.title, "parent_id": section.parent_id}


@router.put("/library/sections/order")
def order_sections(body: SectionOrder, request: Request, db: Session = Depends(get_db),
                   actor: audit.Actor = Depends(admin_actor)):
    """The order of the top level (parent_id null) or of the subsections of a section: every one exactly once (422
    order_invalid with missing, extra and repeated)."""
    parent = _section(db, body.parent_id)
    ids = _change(db, lambda: library.reorder_sections(db, actor, now(request), body.ids, parent))
    return {"parent_id": body.parent_id, "ids": ids}


@router.patch("/library/sections/{section_id}")
def rename_section(section_id: DbId, body: SectionPatch, request: Request, db: Session = Depends(get_db),
                   actor: audit.Actor = Depends(admin_actor)):
    """A new title. changed false: the same one."""
    section = _section(db, section_id)
    changed = _change(db, lambda: library.rename_section(db, actor, section, now(request), body.title))
    return {"id": section.id, "title": section.title, "parent_id": section.parent_id, "changed": changed}


@router.post("/library/sections/{section_id}/move")
def move_section(section_id: DbId, body: SectionMove, request: Request, db: Session = Depends(get_db),
                 actor: audit.Actor = Depends(admin_actor)):
    """To the end of a top-level section (parent_id) or of the top level (null). 422 section_invalid (under itself);
    409 section_too_deep (under a subsection, or a section with subsections under another)."""
    section = _section(db, section_id)
    parent = _section(db, body.parent_id)
    changed = _change(db, lambda: library.move_section(db, actor, section, now(request), parent))
    return {"id": section.id, "title": section.title, "parent_id": section.parent_id, "changed": changed}


@router.post("/library/sections/{section_id}/delete")
def delete_section(section_id: DbId, body: DeleteBody, request: Request, db: Session = Depends(get_db),
                   actor: audit.Actor = Depends(admin_actor)):
    """Delete a section without subsections (409 section_not_empty); its materials only leave it. confirm_id: the
    id again (422 confirmation_mismatch). What was deleted, with the slugs that were in it."""
    if body.confirm_id != section_id:
        raise ApiError(422, "confirmation_mismatch")
    section = _section(db, section_id)
    return _change(db, lambda: library.delete_section(db, actor, section, now(request)))


@router.post("/library/sections/{section_id}/materials")
def place_material(section_id: DbId, body: PlaceBody, request: Request, db: Session = Depends(get_db),
                   actor: audit.Actor = Depends(admin_actor)):
    """Put a material at the end of the section (changed false: it is there). 409 material_archived."""
    section = _section(db, section_id)
    material = _material(db, body.material_id)
    changed = _change(db, lambda: library.place(db, actor, material, section, now(request)))
    return {"section_id": section_id, "material_id": material.id, "changed": changed}


@router.put("/library/sections/{section_id}/materials/order")
def order_section_materials(section_id: DbId, body: OrderBody, request: Request, db: Session = Depends(get_db),
                            actor: audit.Actor = Depends(admin_actor)):
    """{ids}: the material ids of the section, every one exactly once (422 order_invalid)."""
    section = _section(db, section_id)
    ids = _change(db, lambda: library.reorder_materials(db, actor, section, now(request), body.ids))
    return {"section_id": section_id, "ids": ids}


@router.post("/library/sections/{section_id}/materials/{material_id}/delete")
def unplace_material(section_id: DbId, material_id: DbId, request: Request, db: Session = Depends(get_db),
                     actor: audit.Actor = Depends(admin_actor)):
    """Take a material out of the section (it stays in the library). changed false: it was not there."""
    section = _section(db, section_id)
    material = _material(db, material_id)
    changed = _change(db, lambda: library.unplace(db, actor, material, section, now(request)))
    return {"section_id": section_id, "material_id": material.id, "changed": changed}
