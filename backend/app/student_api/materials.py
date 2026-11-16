"""/api/library and /api/materials of a student: «Справочник» and the materials (docs/STUDENT_API.md §6, D-103; the
rules: docs/CLASSROOM.md §11, D-083). The services are app/student/classroom.py over materials/service.py and
library.py.

A student opens only the latest version of a material, and only on a basis (materials.rules.open_basis): the
library for them (all students, or a current group of the library), a material item in their feed, a link
[[памятка:…]] in an item or a problem statement they see. A link from another material is no basis. Without one -
an archived material outside a basis, a draft of the library, a missing id - the answer is 404 material_not_found,
one answer. A file is one of the latest version only (another version's, another material's, a link: 404
attachment_not_found) and goes out through content_files.file_response (docs/FILES.md). No sha256 and no author of a
file reaches the student.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from ..auth_api import Current, get_db, now
from ..classroom.items import FeedError
from ..content_files import file_response
from ..ids import Id
from ..jsonable import plain
from ..student import classroom as sc
from .deps import blobs, refused, student_session

router = APIRouter()


@router.get("/library")
def get_library(request: Request, q: Optional[str] = Query(None, max_length=200), db: Session = Depends(get_db),
                cur: Current = Depends(student_session)):
    """«Справочник» as the student sees it: the sections (two levels) with their materials in the teacher's order,
    only the sections with something for them, and `unsorted`; no archived material. With q: {query, materials},
    the titles containing q, ignoring case (%, _ and \\ taken literally)."""
    at = now(request)
    return plain({**sc.library(db, cur.user, at, q), "server_time": at})


@router.get("/materials/{material_id}")
def get_material(material_id: Id, request: Request, db: Session = Depends(get_db),
                 cur: Current = Depends(student_session)):
    """The latest version: the text (Markdown), the files and links, the links [[памятка:…]] of the text with
    `available` for this student. 404 material_not_found."""
    try:
        return plain(sc.material(db, cur.user, material_id, now(request)))
    except FeedError as exc:
        raise refused(exc) from exc


@router.get("/materials/{material_id}/attachments/{attachment_id}")
def get_material_file(material_id: Id, attachment_id: Id, request: Request, db: Session = Depends(get_db),
                      cur: Current = Depends(student_session)):
    """A file of the latest version. 404 material_not_found, attachment_not_found."""
    try:
        row = sc.material_file(db, cur.user, material_id, attachment_id, now(request))
    except FeedError as exc:
        raise refused(exc) from exc
    return file_response(blobs(request), row.blob_sha256, row.name, request=request)
