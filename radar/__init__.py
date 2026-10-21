"""Load settings from a .env file in the project folder, if there is one.

Lines look like KEY=value. Blank lines and # comments are ignored.
The file wins over environment variables, so a stale Windows `setx` value can't
silently override it. GitHub Actions has no .env file, so its secrets still apply.
"""
import os
from pathlib import Path


def load_env(path):
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.strip().strip('"').strip("'")
        os.environ[key.strip()] = val


load_env(Path(__file__).resolve().parent.parent / ".env")