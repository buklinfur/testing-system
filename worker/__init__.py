"""Judge worker: claims jobs from the backend's internal API and runs them with runner.Judge.

The worker never touches the database. It only needs the API URL, a token and `requests`.
"""
