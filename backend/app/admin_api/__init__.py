"""The admin HTTP API under /api/admin (session 4a.3; docs/ADMIN_API.md, MODULE_4A 5.7)."""
from .routes import UPLOADS, router

__all__ = ["UPLOADS", "router"]
