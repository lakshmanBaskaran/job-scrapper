"""Find each company's job system by reading its own website.

    python scripts/detect_ats.py                         # reads scripts/careers_sites.txt
    python scripts/detect_ats.py --render                # also use a real browser for JS-only pages
    python scripts/detect_ats.py --render --workers 2    # fewer browsers on a slow laptop
    python scripts/detect_ats.py --only "Fexco | https://www.fexco.com"

Input lines:  Name | https://company-website-or-careers-page
For each company it:
  1. fetches the page and looks for a known job-board link in the HTML
  2. if none, follows up to 4 careers-looking links on the page, and tries /careers and /jobs
  3. with --render, opens the remaining pages in headless Chromium (Playwright) and also
     watches the page's network requests, which catches job widgets loaded by JavaScript
Every detected board is then checked live, exactly like discover.py.

Writes detected.yaml: entries to paste into companies.yaml, plus a list of companies whose
site has no recognised job system (custom careers pages).
Companies already in companies.yaml are skipped.
"""
import argparse
import json
import os
import threading
import time
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import requests  # noqa: E402
import yaml  # noqa: E402

from discover import check_entry  # noqa: E402

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-IE,en;q=0.9",
}

# Every way a job board shows up in a page: links, embeds, scripts and API calls.
PATTERNS = [
    (r"boards-api\.greenhouse\.io/v1/boards/([\w-]+)", "greenhouse"),
    (r"greenhouse\.io/embed/job_board(?:/js)?\?for=([\w-]+)", "greenhouse"),
    (r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/([\w-]+)", "greenhouse"),
    (r"api\.eu\.lever\.co/v0/postings/([\w-]+)", "lever-eu"),
    (r"jobs\.eu\.lever\.co/([\w-]+)", "lever-eu"),
    (r"api\.lever\.co/v0/postings/([\w-]+)", "lever"),
    (r"jobs\.lever\.co/([\w-]+)", "lever"),
    (r"api\.ashbyhq\.com/posting-api/job-board/([\w.%-]+)", "ashby"),
    (r"jobs\.ashbyhq\.com/([\w.%-]+)", "ashby"),
    (r"apply\.workable\.com/api/v\d/(?:widget/)?accounts/([\w-]+)", "workable"),
    (r"apply\.workable\.com/([\w-]+)", "workable"),
    (r"([\w-]+)\.workable\.com", "workable"),
    (r"api\.smartrecruiters\.com/v1/companies/([\w-]+)", "smartrecruiters"),
    (r"(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)", "smartrecruiters"),
    (r"([\w-]+)\.jobs\.personio\.(?:de|com)", "personio"),
    (r"([\w-]+)\.recruitee\.com", "recruitee"),
    (r"([\w-]+)\.teamtailor\.com", "teamtailor"),
    (r"([\w-]+)\.bamboohr\.com", "bamboohr"),
    (r"([\w-]+)\.pinpointhq\.com", "pinpoint"),
    (r"([\w-]+)\.breezy\.hr", "breezy"),
]
WORKDAY = re.compile(r"([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:wday/cxs/[\w-]+/)?(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)")
# Path segments and subdomains that look like slugs but aren't.
NOT_SLUGS = {"embed", "js", "api", "www", "jobs", "job", "careers", "app", "apps", "cdn", "static",
             "assets", "v0", "v1", "widget", "accounts", "postings", "posting-api", "en", "en-us",
             "login", "signup", "help", "support", "blog", "company", "search", "wday", "cxs",
             "apply", "career", "boards", "job-boards", "resources", "marketing", "status", "docs"}
CAREER_LINK = re.compile(r"career|jobs?\b|join|work-with-us|work-for-us|vacanc|opportunit|hiring|open-roles",
                         re.I)
HREF = re.compile(r"""href\s*=\s*["']([^"'#]+)["']""", re.I)


def find_boards(text):
    """All job-board entries mentioned anywhere in text (HTML, scripts or a list of URLs)."""
    found = []
    for m in WORKDAY.finditer(text):
        if m.group(3).lower() not in NOT_SLUGS:
            found.append({"ats": "workday", "tenant": m.group(1), "host": m.group(2), "site": m.group(3)})
    for pattern, system in PATTERNS:
        for m in re.finditer(pattern, text, re.I):
            slug = m.group(1)
            if slug.lower() in NOT_SLUGS or len(slug) < 2:
                continue
            entry = {"ats": "lever", "slug": slug, "region": "eu"} if system == "lever-eu" else \
                {"ats": system, "slug": slug}
            found.append(entry)
    unique = []
    for e in found:
        if e not in unique:
            unique.append(e)
    return unique


def career_links(base_url, html):
    """Up to 4 links on the page that look like careers pages, plus the usual paths."""
    host = urlparse(base_url).netloc
    links = []
    for href in HREF.findall(html):
        url = urljoin(base_url, href.strip())
        if url.startswith("http") and CAREER_LINK.search(href) and url not in links:
            # stay on the company's own site; boards on ATS hosts are caught by find_boards
            if urlparse(url).netloc.endswith(host.replace("www.", "")):
                links.append(url)
    root = f"{urlparse(base_url).scheme}://{host}"
    guesses = [root + p for p in ("/careers", "/jobs", "/careers/", "/about/careers")]
    return links[:4] + [g for g in guesses if g not in links]


STATUS = {}  # url -> HTTP status from the plain fetch (0 = couldn't connect); decides what to render
BOT_BLOCKED = {401, 403, 406, 429, 503}  # a real browser often gets past these


def fetch_html(url):
    try:
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=20, allow_redirects=True)
        STATUS[url] = STATUS[r.url] = r.status_code
        if r.status_code >= 400:
            return r.url, ""
        return r.url, r.text
    except requests.RequestException:
        STATUS[url] = 0
        return url, ""


def render_targets(checked, limit=2):
    """Pages worth opening in a browser: careers pages that loaded, then the home page.

    Skips 404s and sites that couldn't be reached at all, which used to burn a full
    timeout each in the browser for nothing.
    """
    def good(u):
        code = STATUS.get(u)
        return code is None or 200 <= code < 400 or code in BOT_BLOCKED
    careers = [u for u in checked[1:] if good(u)]
    home = [checked[0]] if checked and good(checked[0]) else []
    out = []
    for u in careers + home:
        if u not in out:
            out.append(u)
    return out[:limit]


def detect_static(url):
    """Plain HTTP: the page itself, then its careers links. Returns (boards, pages_checked)."""
    final, html = fetch_html(url)
    checked = [final]
    boards = find_boards(final + " " + html)
    if boards:
        return boards, checked
    for link in career_links(final, html):
        if link in checked:
            continue
        final2, html2 = fetch_html(link)
        checked.append(final2)
        boards = find_boards(final2 + " " + html2)
        if boards:
            return boards, checked
    return [], checked


BLOCK_TYPES = {"image", "media", "font", "stylesheet"}
MAX_NAVS = 2          # page loads per company
NAV_TIMEOUT = 12000   # ms per page load
SETTLE_MS = 2500      # max wait for JS widgets to call their job API (stops early on a hit)
POLL_MS = 250


def detect_rendered(urls, browser):
    """Headless Chromium: rendered DOM plus every network request the page makes.

    Uses a fresh context per company (no leaked memory or cookies), skips images/fonts,
    and never waits for 'networkidle' (sites with analytics never reach it).
    """
    seen = []
    ctx = browser.new_context(user_agent=BROWSER_HEADERS["User-Agent"], locale="en-IE")
    ctx.route("**/*", lambda route: route.abort() if route.request.resource_type in BLOCK_TYPES
              else route.continue_())
    page = ctx.new_page()
    page.set_default_timeout(NAV_TIMEOUT)
    page.on("request", lambda req: seen.append(req.url))
    navs = 0

    def visit(url):
        nonlocal navs
        navs += 1
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            # stop waiting as soon as a job-board request shows up
            for _ in range(SETTLE_MS // POLL_MS):
                if find_boards(" ".join(seen)):
                    break
                page.wait_for_timeout(POLL_MS)
            html = page.content()
        except Exception:
            return None, ""
        return find_boards(page.url + " " + html + " " + " ".join(seen)), html

    try:
        queue = list(urls)
        while queue and navs < MAX_NAVS:
            url = queue.pop(0)
            boards, html = visit(url)
            if boards:
                return boards
            if html:
                # careers links that only exist after JS runs go to the front of the queue
                extra = [l for l in career_links(page.url, html)[:2] if l not in queue and l != url]
                queue = extra + queue
        return []
    finally:
        try:
            ctx.close()
        except Exception:
            pass


def render_worker(jobs, results, lock, counter, total):
    """One thread = one Playwright instance + one browser; pulls companies off the shared list."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            while True:
                with lock:
                    if not jobs:
                        return
                    name, urls = jobs.pop(0)
                try:
                    boards = detect_rendered(urls, browser)
                except Exception as e:  # one bad site must not kill the run
                    boards = []
                    print(f"  !! {name}: {type(e).__name__}", flush=True)
                with lock:
                    counter[0] += 1
                    results[name] = boards
                    tag = f"{boards[0]['ats']}" if boards else "-"
                    print(f"  [{counter[0]:>3}/{total}] {name:<30} {tag}", flush=True)
        finally:
            browser.close()


def load_cache(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_cache(path, cache):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=1)
    os.replace(tmp, path)


def verify(name, boards):
    """Check each detected board live; return the best (entry, total, irish), or the first unverified."""
    best = None
    for b in boards[:4]:
        hit = check_entry({"name": name, **b})
        if hit and (best is None or hit[2] > best[2]):
            best = hit
    return best


def load_lines(args):
    if args.only:
        return [args.only]
    with open(args.input, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.lstrip().startswith("#") and "|" in l]


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--input", default="scripts/careers_sites.txt")
    p.add_argument("--companies", default="companies.yaml")
    p.add_argument("--out", default="detected.yaml")
    p.add_argument("--only")
    p.add_argument("--render", action="store_true", help="use headless Chromium for pages plain HTTP can't read")
    p.add_argument("--workers", type=int, default=6, help="browsers in parallel for --render (default 6)")
    p.add_argument("--cache", default="detect_cache.json", help="render results kept between runs")
    p.add_argument("--fresh", action="store_true", help="ignore the render cache and redo every site")
    args = p.parse_args(argv)

    try:
        with open(args.companies, encoding="utf-8") as f:
            have = {c["name"].lower() for c in (yaml.safe_load(f) or {}).get("companies", [])}
    except FileNotFoundError:
        have = set()
    todo = []
    for line in load_lines(args):
        name, _, url = line.partition("|")
        name, url = name.strip(), url.strip()
        if name.lower() not in have:
            todo.append((name, url if "://" in url else "https://" + url))
    print(f"checking {len(todo)} company websites...")

    results = {}  # name -> (status, detail)
    with ThreadPoolExecutor(max_workers=8) as ex:
        statics = []
        for i, res in enumerate(ex.map(lambda t: detect_static(t[1]), todo), 1):
            statics.append(res)
            if i % 20 == 0 or i == len(todo):
                print(f"  plain fetch {i}/{len(todo)}", flush=True)
    unresolved, dead = [], set()
    for (name, url), (boards, checked) in zip(todo, statics):
        if boards:
            results[name] = ("board", boards)
            continue
        targets = render_targets(checked or [url])
        if targets:
            unresolved.append((name, targets))
        else:
            dead.add(name)  # site unreachable or every page 404: a browser won't do better
    if args.render and dead:
        print(f"skipping {len(dead)} sites that didn't load at all (wrong URL or site down)")

    pending = set()  # sites the render phase never reached (Ctrl+C)
    if args.render and unresolved:
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:
            print("Playwright isn't installed: pip install playwright && python -m playwright install chromium")
        else:
            cache = {} if args.fresh else load_cache(args.cache)
            done = {n: b for n, b in cache.items() if n in dict(unresolved)}
            for name, boards in done.items():
                if boards:
                    results[name] = ("board", boards)
            jobs = [(n, u) for n, u in unresolved if n not in done]
            if done:
                print(f"{len(done)} already rendered in an earlier run (from {args.cache}; --fresh to redo)")
            if jobs:
                n_workers = max(1, min(args.workers, len(jobs)))
                est = len(jobs) * 10 / n_workers / 60
                print(f"rendering {len(jobs)} sites in headless Chromium with {n_workers} browsers "
                      f"(about {est:.0f} min; Ctrl+C stops and keeps what's done)...", flush=True)
                rendered, lock, counter = {}, threading.Lock(), [0]
                threads = [threading.Thread(target=render_worker, daemon=True,
                                            args=(jobs, rendered, lock, counter, len(jobs)))
                           for _ in range(n_workers)]
                for t in threads:
                    t.start()
                saved = 0
                try:
                    while any(t.is_alive() for t in threads):
                        time.sleep(1)
                        with lock:
                            if len(rendered) - saved >= 5:  # checkpoint every 5 companies
                                cache.update(rendered)
                                save_cache(args.cache, cache)
                                saved = len(rendered)
                except KeyboardInterrupt:
                    print("\nstopped: writing results for the sites done so far...", flush=True)
                    with lock:
                        jobs.clear()  # workers finish their current site and exit
                with lock:
                    cache.update(rendered)
                    save_cache(args.cache, cache)
                    for name, boards in rendered.items():
                        if boards:
                            results[name] = ("board", boards)
                    pending = {n for n, _ in unresolved if n not in cache}

    found, no_jobs, custom = [], [], []
    to_verify = [n for n, _ in todo if n in results]
    print(f"checking {len(to_verify)} detected boards live...", flush=True)
    with ThreadPoolExecutor(max_workers=8) as ex:
        hits = dict(zip(to_verify, ex.map(lambda n: verify(n, results[n][1]), to_verify)))
    for name, url in todo:
        if name in pending:
            continue
        if name in dead and name not in results:
            continue
        if name not in results:
            custom.append((name, url))
            continue
        boards = results[name][1]
        hit = hits[name]
        if hit:
            entry, total, irish = hit
            found.append((irish, total, entry))
            ident = entry.get("slug") or f"{entry['tenant']}/{entry['site']}"
            region = " (eu)" if entry.get("region") else ""
            print(f"  ok   {name:<28} {entry['ats'] + region:<20} {ident:<26} {total:>4} jobs, {irish:>3} Ireland")
        else:
            no_jobs.append((name, boards[0]))
            print(f"  --   {name:<28} uses {boards[0]['ats']} but the board has no open jobs today")

    found.sort(key=lambda x: (-x[0], x[2]["name"]))
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("# Paste the entries you want into companies.yaml under `companies:`.\n")
        f.write("# Comments: open jobs today / in Ireland. 0 in Ireland = check before adding.\n")
        for irish, total, entry in found:
            f.write(f"\n  # {total} jobs / {irish} Ireland\n")
            f.write("  - " + yaml.safe_dump(entry, sort_keys=False, default_flow_style=True).strip() + "\n")
        if no_jobs:
            f.write("\n# Job system found, but no open jobs today (add now so new postings are caught):\n")
            for name, b in no_jobs:
                f.write("#  - " + yaml.safe_dump({"name": name, **b}, sort_keys=False,
                                                  default_flow_style=True).strip() + "\n")
        if custom:
            f.write("\n# No known job system on their site (custom careers page, or wrong URL):\n")
            for name, url in custom:
                f.write(f"#   {name} | {url}\n")
        if dead:
            f.write("\n# Site didn't load at all (wrong URL in careers_sites.txt, or site down):\n")
            for name, url in todo:
                if name in dead:
                    f.write(f"#   {name} | {url}\n")
        if pending:
            f.write("\n# Not checked yet (run stopped early; run again with --render to finish these):\n")
            for name, url in todo:
                if name in pending:
                    f.write(f"#   {name} | {url}\n")
    print(f"\n{len(found)} with jobs, {len(no_jobs)} found but empty, {len(custom)} custom/unknown"
          + (f", {len(dead)} didn't load" if dead else "")
          + (f", {len(pending)} not checked yet (run again to finish)" if pending else "") + f" -> {args.out}")


if __name__ == "__main__":
    main()