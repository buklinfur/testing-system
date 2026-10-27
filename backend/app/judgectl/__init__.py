"""judgectl: the console of the server owner (R2, R33-R36, docs/JUDGECTL.md).

    sudo -u judge ... python -m app.judgectl admin add boss --name "Иван Петрович"
    python -m app.judgectl user import class-7b.csv --out 7b-passwords.csv
    python -m app.judgectl queue status | worker list | worker token create main

It works through the service layer directly on the database (no HTTP); being able to run it as `judge`
with /etc/judge/backend.env is the authentication. Every change goes to audit_log as `cli:<unix user>`
(plus SUDO_USER). `python -m app.admin_cli ...` (module 3 syntax) stays as an alias until module 6.
"""
from .cli import main

__all__ = ["main"]
