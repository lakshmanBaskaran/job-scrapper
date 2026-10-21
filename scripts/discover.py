"""Find which ATS each company uses, from a name or from a careers URL.

    python scripts/discover.py                     # reads scripts/candidates.txt
    python scripts/discover.py --only "Wayflyer"   # test one name
    python scripts/discover.py --only "State Street | https://statestreet.wd1.myworkdayjobs.com/en-US/Global"

candidates.txt lines can be:
    Name                          try the name as a slug on every ATS
    Name | slug1, slug2           try these slugs on every ATS
    Name | https://careers-url    read the ATS straight from the URL (works for Workday too)

Writes discovered.yaml: entries ready to paste into companies.yaml, sorted by how many
Irish jobs each board has right now. Companies already in companies.yaml are skipped.
A company with zero open jobs on the day you run this won't be found; rerun later.
"""
import argparse
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import yaml  # noqa: E402

from radar import ats  # noqa: E402
from radar.filters import in_ireland  # noqa: E402

# Workday needs tenant/site names that can't be guessed from a name; give its URL instead.
PROBE_ORDER = ["greenhouse", "lever", "lever-eu", "ashby", "workable", "smartrecruiters", "personio",
               "recruitee", "teamtailor", "bamboohr", "pinpoint", "breezy"]


def slug_variants(name):
    base = re.sub(r"\(.*?\)", "", name).lower().strip()
    out = [re.sub(r"[^a-z0-9]", "", base), re.sub(r"[^a-z0-9]+", "-", base).strip("-")]
    return list(dict.fromkeys(s for s in out if s))  # unique, order kept


URL_PATTERNS = [
    (r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)", "greenhouse"),
    (r"jobs\.eu\.lever\.co/([\w-]+)", "lever-eu"),
    (r"jobs\.lever\.co/([\w-]+)", "lever"),
    (r"jobs\.ashbyhq\.com/([\w.%-]+)", "ashby"),  # % keeps encoded slugs like Protex%20AI
    (r"apply\.workable\.com/([\w-]+)", "workable"),
    (r"(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)", "smartrecruiters"),
    (r"([\w-]+)\.jobs\.personio\.(?:de|com)", "personio"),
    (r"([\w-]+)\.recruitee\.com", "recruitee"),
    (r"([\w-]+)\.teamtailor\.com", "teamtailor"),
    (r"([\w-]+)\.bamboohr\.com", "bamboohr"),
    (r"([\w-]+)\.pinpointhq\.com", "pinpoint"),
    (r"([\w-]+)\.breezy\.hr", "breezy"),
]
WORKDAY = re.compile(r"([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)")


def entry_from_url(name, url):
    """Turn a careers URL into a companies.yaml entry, or None if the ATS isn't recognised."""
    m = WORKDAY.search(url)
    if m:
        return {"name": name, "ats": "workday", "tenant": m.group(1), "host": m.group(2), "site": m.group(3)}
    for pattern, system in URL_PATTERNS:
        m = re.search(pattern, url)
        if m:
            if system == "lever-eu":
                return {"name": name, "ats": "lever", "slug": m.group(1), "region": "eu"}
            return {"name": name, "ats": system, "slug": m.group(1)}
    return None


def parse_line(line):
    """'Name' | 'Name | slug1, slug2' | 'Name | https://...' -> (name, [slugs] or entry dict or None)."""
    name, _, extra = line.partition("|")
    name, extra = name.strip(), extra.strip()
    if "://" in extra or "myworkdayjobs" in extra:
        return name, entry_from_url(name, extra)
    slugs = [s.strip() for s in extra.split(",") if s.strip()] if extra else []
    return name, slugs or slug_variants(name)


def check_entry(entry):
    """Fetch one entry; return (entry, total, irish) if it has jobs, else None."""
    try:
        jobs = ats.fetch(entry)
    except Exception:
        return None
    if not jobs:
        return None
    return entry, len(jobs), sum(in_ireland(j) for j in jobs)


def try_one(name, system, slug):
    entry = {"name": name, "ats": system, "slug": slug}
    if system == "lever-eu":
        entry = {"name": name, "ats": "lever", "slug": slug, "region": "eu"}
    return check_entry(entry)


def probe(name, target):
    """target is a list of slugs to try, an entry parsed from a URL, or None (unrecognised URL)."""
    if target is None:
        return None
    if isinstance(target, dict):
        return check_entry(target)
    slugs = target
    for system in PROBE_ORDER:
        for slug in slugs:
            hit = try_one(name, system, slug)
            if hit:
                return hit
    return None


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--candidates", default="scripts/candidates.txt")
    p.add_argument("--companies", default="companies.yaml")
    p.add_argument("--out", default="discovered.yaml")
    p.add_argument("--only")
    args = p.parse_args(argv)

    if args.only:
        lines = [args.only]
    else:
        with open(args.candidates, encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")]

    try:
        with open(args.companies, encoding="utf-8") as f:
            have = {c["name"].lower() for c in (yaml.safe_load(f) or {}).get("companies", [])}
    except FileNotFoundError:
        have = set()

    todo = [parse_line(l) for l in lines]
    todo = [(n, s) for n, s in todo if n.lower() not in have]
    print(f"probing {len(todo)} companies...")

    found, missing = [], []
    with ThreadPoolExecutor(max_workers=12) as ex:
        for (name, _), hit in zip(todo, ex.map(lambda t: probe(*t), todo)):
            if hit:
                entry, total, irish = hit
                found.append((irish, total, entry))
                ident = entry.get("slug") or f"{entry['tenant']}/{entry['site']}"
                print(f"  ok   {name:<28} {entry['ats']:<16} {ident:<22} {total:>4} jobs, {irish:>3} Ireland")
            else:
                missing.append(name)

    found.sort(key=lambda x: (-x[0], x[2]["name"]))
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("# Paste the entries you want into companies.yaml under `companies:`.\n")
        f.write("# Comments show open jobs today: total / in Ireland. 0 in Ireland = probably skip.\n")
        for irish, total, entry in found:
            f.write(f"\n  # {total} jobs / {irish} Ireland\n")
            f.write("  - " + yaml.safe_dump(entry, sort_keys=False, default_flow_style=True).strip() + "\n")
        if missing:
            f.write("\n# Not found (custom careers site, wrong name/URL, or no open jobs):\n")
            for n in missing:
                f.write(f"#   {n}\n")

    print(f"\n{len(found)} found, {len(missing)} not found -> {args.out}")


if __name__ == "__main__":
    main()