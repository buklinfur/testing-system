"""The HTTP API of a student (4b.1, docs/STUDENT_API.md §6) over the services of app/student/."""
from .routes import PREFIXES, UPLOAD_PATTERNS, router

__all__ = ["PREFIXES", "UPLOAD_PATTERNS", "router"]
