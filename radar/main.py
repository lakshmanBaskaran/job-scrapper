"""Daily run: fetch every feed, keep new entry-level roles in Ireland, email a digest.

Usage:
    python -m radar.main                 # normal run, sends email if there are matches
    python -m radar.main --dry-run       # no email; writes digest.html and prints matches
    python -m radar.main --bootstrap     # first run: mark everything seen, send nothing
"""
import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml

from . import ats
from .classify import make_classifier
from .filters import rule_verdict
from .models import Verdict
from .notify import build_digest, send_email
from .store import Store


def load_companies(path):
    with open(path) as f:
        cfg = yaml.safe_load(f)
    return [c for c in cfg["companies"] if c.get("enabled", True)]


def fetch_all(companies, workers=8):
    jobs, errors = [], []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(ats.fetch, c): c for c in companies}
        for f in as_completed(futs):
            c = futs[f]
            try:
                jobs += f.result()
            except Exception as e:  # one broken feed must not kill the run
                errors.append(f"{c['name']} ({c['ats']}): {type(e).__name__}: {str(e)[:120]}")
    return jobs, errors


def classify(job, args, clf):
    v = rule_verdict(job, include_interns=args.interns)
    # Workday, SmartRecruiters and BambooHR list endpoints leave out descriptions (and Workday often says
    # "2 Locations"). For jobs whose title survived the checks, fetch the detail and re-check.
    if (v is None or v.keep) and job.ats in ("workday", "smartrecruiters", "bamboohr"):
        if ats.enrich(job):
            v = rule_verdict(job, include_interns=args.interns)
    if v is not None:
        return v
    if clf is None:
        # No LLM: surface ambiguous roles rather than silently dropping them.
        return Verdict(True, "swe", "ambiguous title, not checked by LLM", "rules")
    try:
        return clf(job)
    except Exception as e:
        return Verdict(True, "swe", f"LLM failed ({type(e).__name__}), check manually", "rules")


LOCK_MAX_AGE = 60 * 60  # a lock older than an hour is from a crashed run; ignore it


def acquire_lock(path):
    """Return True if this run may proceed. Creates the lock file atomically."""
    try:
        if time.time() - os.path.getmtime(path) > LOCK_MAX_AGE:
            os.remove(path)
    except OSError:
        pass
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return True


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--companies", default="companies.yaml")
    p.add_argument("--db", default="radar.db")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--bootstrap", action="store_true")
    p.add_argument("--interns", action="store_true", help="include internships")
    args = p.parse_args(argv)

    if args.dry_run:  # dry runs never write, so they don't need the lock
        run(args)
        return
    lock = args.db + ".lock"
    if not acquire_lock(lock):
        print("another radar run is in progress; skipping this one")
        return
    try:
        run(args)
    finally:
        os.remove(lock)


def run(args):
    t0 = time.time()
    companies = load_companies(args.companies)
    jobs, errors = fetch_all(companies)
    store = Store(args.db)
    new = store.unseen(jobs)

    clf = None
    if not args.bootstrap:
        try:
            clf = make_classifier()
        except Exception as e:  # e.g. Ollama not running: carry on without the LLM
            errors.append(f"LLM disabled: {e}")

    matches, shown, verdicts = [], set(), []
    for job in new:
        v = Verdict(False, reason="bootstrap", source="rules") if args.bootstrap else classify(job, args, clf)
        verdicts.append((job, v))
        # Some boards post the same role twice under different ids; show it once.
        dup = (job.company, job.title.strip().lower(), job.location.strip().lower())
        if v.keep and dup not in shown:
            shown.add(dup)
            matches.append((job, v))

    print(f"{len(companies)} companies, {len(jobs)} jobs, {len(new)} new, "
          f"{len(matches)} matches, {len(errors)} errors, {time.time() - t0:.1f}s")
    for job, v in matches:
        print(f"  + {job.company}: {job.title} [{job.location}] ({v.reason})")
    for e in errors:
        print(f"  ! {e}", file=sys.stderr)

    if matches or (errors and not args.bootstrap):
        subject, html_body, text_body = build_digest(matches, errors)
        if args.dry_run:
            with open("digest.html", "w") as f:
                f.write(html_body)
            print("wrote digest.html")
        elif matches:
            send_email(subject, html_body, text_body)
            print("email sent")

    # Commit only after a successful send, so a failed email retries tomorrow.
    # Dry runs never commit, so you can rerun them while tuning filters.
    if args.dry_run:
        store.discard()
        print("dry run: radar.db not updated")
        return
    # One short write at the end, so the database is never locked during the slow LLM part.
    for job, v in verdicts:
        store.mark(job, v)
    store.log_run(len(jobs), len(new), len(matches), errors)
    store.commit()


if __name__ == "__main__":
    main()