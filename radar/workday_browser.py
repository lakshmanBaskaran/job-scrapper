"""Read Workday job lists through a real browser.

Since mid-Nov 2026 Workday answers plain scripts with a bot-check page. A careers site
opened in a real browser still works, so this opens each company's careers page in
Chromium (via Playwright), the way a visitor would, and asks for the job list from inside
that page using the same request the page itself makes.

It does nothing to hide that it's an automated browser: no stealth plugins, no faked
fingerprints, no CAPTCHA solving. If Workday blocks the browser too, the company is
reported as failing and that's that. It's also gentle: one company at a time, one browser
tab, a pause between pages.

Turn it off with RADAR_WORKDAY=api (back to plain requests). RADAR_BROWSER_HEADED=1 opens
a visible window instead of a headless one (useful on your laptop to see what happens).
"""
import os
import time

from .ats import _strip_html, parse_workday

PAGE_TIMEOUT = 30_000  # ms to load a careers page
PAGE_SIZE = 20
MAX_RESULTS = 200       # per search term, same as the plain fetcher

# Runs inside the careers page: the same call the page's own JavaScript makes.
_FETCH_JS = """async ({url, method, body}) => {
  const r = await fetch(url, {
    method,
    headers: {"Accept": "application/json", "Content-Type": "application/json"},
    body: body ? JSON.stringify(body) : undefined,
    credentials: "include",
  });
  return {status: r.status, ctype: r.headers.get("content-type") || "", text: await r.text()};
}"""


class Blocked(RuntimeError):
    pass


class WorkdayBrowser:
    def __init__(self, headed=None, launch_args=None):
        self.headed = os.getenv("RADAR_BROWSER_HEADED") == "1" if headed is None else headed
        self.launch_args = launch_args or []  # tests point *.myworkdayjobs.com at a local server
        self._pw = self._browser = self.page = None
        self._origin = None  # careers site the tab is currently on

    def start(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=not self.headed, args=self.launch_args)
        self.page = self._browser.new_context(locale="en-IE").new_page()
        self.page.set_default_timeout(PAGE_TIMEOUT)
        return self

    def close(self):
        for thing in (self._browser, self._pw):
            try:
                thing and (thing.close() if thing is self._browser else thing.stop())
            except Exception:
                pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    # ---------- helpers ----------
    @staticmethod
    def _urls(c):
        t, host, site = c["tenant"], c.get("host", "wd3"), c["site"]
        origin = c.get("_origin") or f"https://{t}.{host}.myworkdayjobs.com"  # _origin: tests only
        return origin, f"{origin}/en-US/{site}", f"{origin}/wday/cxs/{t}/{site}"

    def _open(self, origin, page_url):
        """Visit the careers page (once per company), as a person would."""
        if self._origin == origin:
            return
        self.page.goto(page_url, wait_until="domcontentloaded")
        self.page.wait_for_timeout(2000)  # let the page settle and run its own scripts
        self._origin = origin

    def _call(self, url, method="GET", body=None):
        import json
        r = self.page.evaluate(_FETCH_JS, {"url": url, "method": method, "body": body})
        if "json" not in r["ctype"]:
            raise Blocked(f"Workday sent a web page instead of job data even in a browser "
                          f"(HTTP {r['status']}): blocked")
        if r["status"] >= 400:
            raise RuntimeError(f"HTTP {r['status']} from {url}")
        return json.loads(r["text"])

    # ---------- public ----------
    def fetch(self, c):
        """All matching postings for one Workday company (same results as ats.fetch_workday)."""
        origin, page_url, api = self._urls(c)
        self._origin = None  # always reload the careers page for a new company
        self._open(origin, page_url)
        jobs = []
        for term in c.get("search", ["software", "engineer", "graduate", "developer"]):
            offset = 0
            while True:
                data = self._call(f"{api}/jobs", "POST", {"appliedFacets": {}, "limit": PAGE_SIZE,
                                                          "offset": offset, "searchText": term})
                jobs += parse_workday(c["name"], page_url, data)
                offset += PAGE_SIZE
                if offset >= data.get("total", 0) or offset >= MAX_RESULTS:
                    break
                time.sleep(1)
        return list({j.job_id: j for j in jobs}.values())

    def detail(self, job):
        """(description, location) for one posting, like ats.fetch_workday_detail."""
        from .ats import WORKDAY_URL
        m = WORKDAY_URL.match(job.url)
        if not m:
            return "", ""
        origin = job.url.split("/en-US/")[0]
        _, _, site, path = m.groups()
        tenant = m.group(1)
        try:
            self._open(origin, job.url.split("/job/")[0])
            info = self._call(f"{origin}/wday/cxs/{tenant}/{site}{path}").get("jobPostingInfo") or {}
        except Exception:
            return "", ""
        locs = [info.get("location", "")] + list(info.get("additionalLocations") or [])
        return _strip_html(info.get("jobDescription", "")), "; ".join(l for l in locs if l)
