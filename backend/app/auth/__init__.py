"""Accounts, sessions, 2FA and brute-force limits (docs/AUTH.md).

Service layer only: no HTTP here. The public app (auth_api) and judgectl call these functions,
and every function that depends on time takes `now`, so tests do not need to sleep.
"""
