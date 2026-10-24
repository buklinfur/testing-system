from __future__ import annotations

import logging
import sys


def setup_logging(level: str = "INFO", prefix: str = "") -> None:
    """One line per event on stderr; journald adds timestamps itself, so keep ours short."""
    tag = f" [{prefix}]" if prefix else ""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(f"%(levelname)s{tag} %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
