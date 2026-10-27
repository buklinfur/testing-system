"""The languages of the system: key and display name. The single source for the backend (the migration of
session 4a.2 inserts the missing rows of `languages`, dev_seed does the same) without importing the runner;
tests/test_layers_units.py checks that the keys equal those of runner/languages.py:PROFILES."""

SYSTEM_LANGUAGES = (
    ("python", "Python 3"),
    ("c", "C"),
    ("cpp", "C++"),
)
