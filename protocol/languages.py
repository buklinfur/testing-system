"""The languages of the system: key and display name. The single source for the backend (the migration of
session 4a.2 inserts the missing rows of `languages`, dev_seed does the same) without importing the runner;
tests/test_layers_units.py checks that the keys and extensions equal those of runner/languages.py:PROFILES."""

SYSTEM_LANGUAGES = (
    ("python", "Python 3"),
    ("c", "C"),
    ("cpp", "C++"),
)

# source file extensions -> language key (solutions in a package have no `language` field)
EXTENSIONS = {".py": "python", ".c": "c", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp"}


def language_of(filename: str):
    """The language of a source file by its extension, or None."""
    lower = filename.lower()
    for ext, key in EXTENSIONS.items():
        if lower.endswith(ext):
            return key
    return None
