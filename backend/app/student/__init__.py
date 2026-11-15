"""The services of the student API (4b.1, docs/STUDENT_API.md): contests, statements, submissions, the leaderboard.

The student is always the User of the session: no function here takes a user, participation or group id from a
request (a group id only as a filter of the leaderboard, checked against the contest's groups). Functions take a
Session and never commit: the caller owns the transaction (an attempt to enter is recorded even when refused, so
enter() returns its outcome instead of raising). `now` is the moment the request was received, taken once.
"""
