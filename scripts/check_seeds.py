"""Hit every feed in companies.yaml once and report which ones work.

    python scripts/check_seeds.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from radar import ats  # noqa: E402
from radar.filters import in_ireland  # noqa: E402
from radar.main import load_companies  # noqa: E402

bad = 0
for c in load_companies("companies.yaml"):
    try:
        jobs = ats.fetch(c)
        ie = sum(in_ireland(j) for j in jobs)
        flag = "ok " if jobs else "EMPTY"
        print(f"{flag} {c['name']:<30} {c['ats']:<16} {len(jobs):>4} jobs, {ie:>3} in Ireland")
    except Exception as e:
        bad += 1
        print(f"FAIL {c['name']:<29} {c['ats']:<16} {type(e).__name__}: {str(e)[:80]}")
sys.exit(1 if bad else 0)
