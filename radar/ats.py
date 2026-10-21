"""One small adapter per applicant tracking system.

Each adapter takes a company entry from companies.yaml and returns a list of Job.
Only public, unauthenticated job-board endpoints are used.
"""
import html
import re
import time
import xml.etree.ElementTree as ET

import requests

from .models import Job

TIMEOUT = 20
HEADERS = {"User-Agent": "job-radar/1.0 (personal job alert script)"}


def _strip_html(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _request(method, url, headers=None, **kw):
    """HTTP with polite retries: on 429 (too many requests) or 503, wait and try again."""
    for attempt in range(5):
        r = requests.request(method, url, headers=headers or HEADERS, timeout=TIMEOUT, **kw)
        if r.status_code not in (429, 503) or attempt == 4:
            r.raise_for_status()
            return r
        wait = r.headers.get("Retry-After", "")
        time.sleep(min(int(wait), 60) if wait.isdigit() else 10 * (attempt + 1))


def _get(url, **kw):
    return _request("GET", url, **kw)


# ---------- Greenhouse ----------
def parse_greenhouse(company, data):
    out = []
    for j in data.get("jobs", []):
        out.append(Job(
            company=company, ats="greenhouse", job_id=str(j["id"]),
            title=j.get("title", "").strip(),
            location=(j.get("location") or {}).get("name", ""),
            url=j.get("absolute_url", ""),
            description=_strip_html(j.get("content", "")),
            posted=j.get("first_published") or j.get("updated_at", ""),
        ))
    return out


def fetch_greenhouse(c):
    # content=true includes the job description, which the filters and classifier use.
    url = f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs?content=true"
    return parse_greenhouse(c["name"], _get(url).json())


# ---------- Lever ----------
def parse_lever(company, data):
    out = []
    for j in data:
        cats = j.get("categories") or {}
        locs = cats.get("allLocations") or [cats.get("location", "")]
        out.append(Job(
            company=company, ats="lever", job_id=j["id"],
            title=j.get("text", "").strip(),
            location="; ".join(l for l in locs if l),
            url=j.get("hostedUrl", ""),
            description=j.get("descriptionPlain", "") + " " + " ".join(
                _strip_html(x.get("content", "")) for x in j.get("lists", [])),
            posted=str(j.get("createdAt", "")),
        ))
    return out


def fetch_lever(c):
    # EU-hosted Lever accounts live on api.eu.lever.co; set `region: eu` in the YAML.
    host = "api.eu.lever.co" if c.get("region") == "eu" else "api.lever.co"
    return parse_lever(c["name"], _get(f"https://{host}/v0/postings/{c['slug']}?mode=json").json())


# ---------- Ashby ----------
def parse_ashby(company, data):
    out = []
    for j in data.get("jobs", []):
        locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations", [])]
        loc = "; ".join(l for l in locs if l)
        if j.get("isRemote"):
            loc += " (Remote)"
        out.append(Job(
            company=company, ats="ashby", job_id=j["id"],
            title=j.get("title", "").strip(), location=loc,
            url=j.get("jobUrl", ""), description=j.get("descriptionPlain", ""),
            posted=j.get("publishedAt", ""),
        ))
    return out


def fetch_ashby(c):
    return parse_ashby(c["name"], _get(f"https://api.ashbyhq.com/posting-api/job-board/{c['slug']}").json())


# ---------- Workable ----------
def parse_workable(company, data):
    out = []
    for j in data.get("jobs", []):
        loc = ", ".join(x for x in [j.get("city", ""), j.get("country", "")] if x)
        if j.get("telecommuting"):
            loc += " (Remote)"
        out.append(Job(
            company=company, ats="workable", job_id=j.get("shortcode", ""),
            title=j.get("title", "").strip(), location=loc,
            url=j.get("url") or j.get("application_url", ""),
            description=_strip_html(j.get("description", "")),
            posted=j.get("published_on", ""),
        ))
    return out


def fetch_workable(c):
    url = f"https://apply.workable.com/api/v1/widget/accounts/{c['slug']}?details=true"
    return parse_workable(c["name"], _get(url).json())


# ---------- SmartRecruiters ----------
def parse_smartrecruiters(company, slug, data):
    out = []
    for j in data.get("content", []):
        l = j.get("location") or {}
        loc = ", ".join(x for x in [l.get("city", ""), (l.get("country") or "").upper()] if x)
        if l.get("remote"):
            loc += " (Remote)"
        out.append(Job(
            company=company, ats="smartrecruiters", job_id=str(j["id"]),
            title=j.get("name", "").strip(), location=loc,
            url=f"https://jobs.smartrecruiters.com/{slug}/{j['id']}",
            posted=j.get("releasedDate", ""),
        ))
    return out


def fetch_smartrecruiters(c):
    jobs, offset = [], 0
    while True:
        url = f"https://api.smartrecruiters.com/v1/companies/{c['slug']}/postings"
        data = _get(url, params={"country": "ie", "limit": 100, "offset": offset}).json()
        jobs += parse_smartrecruiters(c["name"], c["slug"], data)
        offset += 100
        if offset >= data.get("totalFound", 0):
            return jobs


# ---------- Personio (XML feed) ----------
def parse_personio(company, slug, xml_text):
    out = []
    root = ET.fromstring(xml_text)
    for p in root.iter("position"):
        jid = p.findtext("id", "")
        desc = " ".join(_strip_html(v.text or "") for v in p.iter("value"))
        out.append(Job(
            company=company, ats="personio", job_id=jid,
            title=(p.findtext("name") or "").strip(),
            location=p.findtext("office") or "",
            url=f"https://{slug}.jobs.personio.de/job/{jid}",
            description=desc, posted=p.findtext("createdAt") or "",
        ))
    return out


def fetch_personio(c):
    return parse_personio(c["name"], c["slug"], _get(f"https://{c['slug']}.jobs.personio.de/xml").text)


# ---------- Workday ----------
def parse_workday(company, base, data):
    out = []
    for j in data.get("jobPostings", []):
        path = j.get("externalPath", "")
        out.append(Job(
            company=company, ats="workday",
            job_id=(j.get("bulletFields") or [path])[0],
            title=j.get("title", "").strip(),
            location=j.get("locationsText", ""),
            url=base + path, posted=j.get("postedOn", ""),
        ))
    return out


def fetch_workday(c):
    """Workday has no GET feed: POST a search per tenant.

    From a careers URL like https://acme.wd3.myworkdayjobs.com/en-US/AcmeCareers
    take tenant=acme, host=wd3, site=AcmeCareers.
    """
    t, host, site = c["tenant"], c.get("host", "wd3"), c["site"]
    api = f"https://{t}.{host}.myworkdayjobs.com/wday/cxs/{t}/{site}/jobs"
    base = f"https://{t}.{host}.myworkdayjobs.com/en-US/{site}"
    jobs = []
    # Workday lists are large; search narrows server-side before we page.
    for term in c.get("search", ["software", "engineer", "graduate", "developer"]):
        offset = 0
        while True:
            data = _request("POST", api, json={"appliedFacets": {}, "limit": 20, "offset": offset,
                                               "searchText": term},
                            headers={**HEADERS, "Content-Type": "application/json"}).json()
            jobs += parse_workday(c["name"], base, data)
            offset += 20
            if offset >= data.get("total", 0) or offset >= 200:
                break
            time.sleep(1)  # big tenants (Salesforce) rate-limit rapid paging
    uniq = {j.job_id: j for j in jobs}
    return list(uniq.values())


# ---------- Recruitee ----------
def parse_recruitee(company, data):
    out = []
    for j in data.get("offers", []):
        loc = j.get("location") or ", ".join(x for x in [j.get("city", ""), j.get("country_code", "")] if x)
        if j.get("remote"):
            loc += " (Remote)"
        out.append(Job(
            company=company, ats="recruitee", job_id=str(j.get("id") or j.get("slug")),
            title=(j.get("title") or "").strip(), location=loc,
            url=j.get("careers_url", ""),
            description=_strip_html((j.get("description") or "") + " " + (j.get("requirements") or "")),
            posted=j.get("published_at", ""),
        ))
    return out


def fetch_recruitee(c):
    return parse_recruitee(c["name"], _get(f"https://{c['slug']}.recruitee.com/api/offers/").json())


# ---------- Breezy HR ----------
def _breezy_loc(l):
    l = l or {}
    country = (l.get("country") or {}).get("name", "") if isinstance(l.get("country"), dict) else (l.get("country") or "")
    text = l.get("name") or ", ".join(x for x in [l.get("city", ""), country] if x)
    if country and country not in text:
        text += f", {country}"
    return text + (" (Remote)" if l.get("is_remote") else "")


def parse_breezy(company, data):
    out = []
    for j in data or []:
        locs = j.get("locations") or [j.get("location")]
        out.append(Job(
            company=company, ats="breezy", job_id=str(j.get("id") or j.get("friendly_id")),
            title=(j.get("name") or "").strip(),
            location="; ".join(x for x in (_breezy_loc(l) for l in locs) if x),
            url=j.get("url", ""), posted=j.get("published_date", ""),
        ))
    return out


def fetch_breezy(c):
    return parse_breezy(c["name"], _get(f"https://{c['slug']}.breezy.hr/json").json())


# ---------- Pinpoint ----------
def parse_pinpoint(company, data):
    out = []
    for j in data.get("data", []):
        l = j.get("location") or {}
        loc = ", ".join(x for x in [l.get("city"), l.get("province"), l.get("name")] if x)
        if j.get("workplace_type") == "remote":
            loc += " (Remote)"
        desc = " ".join(j.get(k) or "" for k in ("description", "key_responsibilities", "skills_knowledge_expertise"))
        out.append(Job(
            company=company, ats="pinpoint", job_id=str(j.get("id")),
            title=(j.get("title") or "").strip(), location=loc,
            url=j.get("url", ""), description=_strip_html(desc),
        ))
    return out


def fetch_pinpoint(c):
    return parse_pinpoint(c["name"], _get(f"https://{c['slug']}.pinpointhq.com/postings.json").json())


# ---------- Teamtailor (public RSS feed) ----------
def _local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def parse_teamtailor(company, xml_text):
    """Tag names in Teamtailor's feed aren't documented, so read any element whose name
    mentions 'location' (namespaced or not) and join the text inside it."""
    out = []
    root = ET.fromstring(xml_text)
    for item in root.iter("item"):
        fields, locs = {}, []
        for el in item.iter():
            name = _local(el.tag)
            if "location" in name:
                text = " ".join(t.strip() for t in el.itertext() if t.strip())
                if text and text not in locs:
                    locs.append(text)
            elif name in ("title", "link", "guid", "description", "pubdate", "remotestatus", "remote"):
                fields.setdefault(name, (el.text or "").strip())
        loc = "; ".join(locs)
        if fields.get("remotestatus", fields.get("remote", "")).lower() in ("true", "fully", "remote", "hybrid"):
            loc += " (Remote)"
        link = fields.get("link", "")
        out.append(Job(
            company=company, ats="teamtailor", job_id=fields.get("guid") or link,
            title=fields.get("title", ""), location=loc, url=link,
            description=_strip_html(fields.get("description", "")), posted=fields.get("pubdate", ""),
        ))
    return out


def fetch_teamtailor(c):
    # slug "acme" -> acme.teamtailor.com; a slug with a dot is a custom domain, e.g. jobs.acme.com
    host = c["slug"] if "." in c["slug"] else f"{c['slug']}.teamtailor.com"
    jobs, offset = [], 0
    while offset < 1000:
        batch = parse_teamtailor(c["name"], _get(f"https://{host}/jobs.rss",
                                                  params={"offset": offset, "per_page": 100}).text)
        jobs += batch
        if len(batch) < 100:
            return jobs
        offset += 100
    return jobs


# ---------- BambooHR ----------
def parse_bamboohr(company, slug, data):
    out = []
    for j in data.get("result", []):
        l = j.get("atsLocation") or j.get("location") or {}
        if isinstance(l, str):
            loc = l
        else:
            loc = ", ".join(x for x in [l.get("city"), l.get("state") or l.get("province"), l.get("country")] if x)
        if j.get("isRemote") or j.get("locationType") == "1":
            loc += " (Remote)"
        out.append(Job(
            company=company, ats="bamboohr", job_id=str(j.get("id")),
            title=(j.get("jobOpeningName") or j.get("title") or "").strip(), location=loc,
            url=f"https://{slug}.bamboohr.com/careers/{j.get('id')}",
        ))
    return out


def fetch_bamboohr(c):
    return parse_bamboohr(c["name"], c["slug"], _get(f"https://{c['slug']}.bamboohr.com/careers/list").json())


def fetch_bamboohr_description(job):
    m = re.match(r"https://([\w-]+)\.bamboohr\.com/careers/(\w+)", job.url)
    if not m:
        return ""
    try:
        data = _get(f"https://{m.group(1)}.bamboohr.com/careers/{m.group(2)}/detail").json()
    except Exception:
        return ""
    opening = (data.get("result") or {}).get("jobOpening") or {}
    return _strip_html(opening.get("description", ""))


def fetch_description(job):
    """Fill in a missing description for ATSs whose list endpoint leaves it out.

    Called only for ambiguous jobs about to go to the LLM, so it's a few requests per run.
    Returns "" when there's nothing to fetch or the request fails.
    """
    if job.ats == "bamboohr":
        return fetch_bamboohr_description(job)
    if job.ats != "smartrecruiters":
        return ""
    m = re.match(r"https://jobs\.smartrecruiters\.com/([^/]+)/([^/?#]+)", job.url)
    if not m:
        return ""
    try:
        data = _get(f"https://api.smartrecruiters.com/v1/companies/{m.group(1)}/postings/{m.group(2)}").json()
    except Exception:
        return ""
    sections = (data.get("jobAd") or {}).get("sections") or {}
    parts = [sections.get(k, {}).get("text", "") for k in ("jobDescription", "qualifications", "additionalInformation")]
    return _strip_html(" ".join(p for p in parts if p))


WORKDAY_URL = re.compile(r"https://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/en-US/([\w-]+)(/job/.+)")


def fetch_workday_detail(job):
    """Return (description, location) for one Workday posting, or ("", "") on failure.

    The list endpoint gives "2 Locations" and no description; the detail endpoint has both.
    """
    m = WORKDAY_URL.match(job.url)
    if not m:
        return "", ""
    t, host, site, path = m.groups()
    try:
        info = _get(f"https://{t}.{host}.myworkdayjobs.com/wday/cxs/{t}/{site}{path}").json()
    except Exception:
        return "", ""
    info = info.get("jobPostingInfo") or {}
    locs = [info.get("location", "")] + list(info.get("additionalLocations") or [])
    return _strip_html(info.get("jobDescription", "")), "; ".join(l for l in locs if l)


def enrich(job):
    """Fill in description (and real location for Workday) when the list endpoint left them out.

    Returns True if anything changed. Only called for jobs that already passed the title checks.
    """
    if job.ats == "workday":
        desc, loc = fetch_workday_detail(job)
        changed = bool(desc or loc)
        job.description = desc or job.description
        job.location = loc or job.location
        return changed
    if not job.description:
        job.description = fetch_description(job)
        return bool(job.description)
    return False


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workable": fetch_workable,
    "smartrecruiters": fetch_smartrecruiters,
    "personio": fetch_personio,
    "workday": fetch_workday,
    "recruitee": fetch_recruitee,
    "breezy": fetch_breezy,
    "pinpoint": fetch_pinpoint,
    "teamtailor": fetch_teamtailor,
    "bamboohr": fetch_bamboohr,
}


def fetch(company: dict):
    return FETCHERS[company["ats"]](company)