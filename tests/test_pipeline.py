import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from radar import ats
from radar.filters import min_years_required, role_category, rule_verdict
from radar.models import Job
from radar.notify import build_digest
from radar.store import Store

# Trimmed from the live Intercom Greenhouse feed (12 Oct 2026).
GREENHOUSE = {"jobs": [
    {"id": 8018140, "title": "Application Security Engineer", "location": {"name": "Dublin, Ireland"},
     "absolute_url": "https://job-boards.greenhouse.io/intercom/jobs/8018140", "first_published": "2026-06-23"},
    {"id": 5082494, "title": "Senior Software Engineer", "location": {"name": "Dublin, Ireland"},
     "absolute_url": "https://job-boards.greenhouse.io/intercom/jobs/5082494"},
    {"id": 7820671, "title": "AI Infrastructure Engineer", "location": {"name": "Dublin, Ireland"},
     "absolute_url": "https://job-boards.greenhouse.io/intercom/jobs/7820671",
     "content": "&lt;p&gt;You have 5+ years of experience building infrastructure&lt;/p&gt;"},
    {"id": 7824137, "title": "AI Infrastructure Engineer", "location": {"name": "London, England"},
     "absolute_url": "x"},
]}


def job(title, loc="Dublin, Ireland", desc=""):
    return Job("Acme", "greenhouse", title, title, loc, "https://x", desc)


def test_greenhouse_parse_and_html_strip():
    jobs = ats.parse_greenhouse("Intercom", GREENHOUSE)
    assert len(jobs) == 4
    assert jobs[2].description == "You have 5+ years of experience building infrastructure"
    assert jobs[0].key == "greenhouse:Intercom:8018140"


def test_rules_on_real_feed():
    v = {j.job_id + j.location: rule_verdict(j) for j in ats.parse_greenhouse("Intercom", GREENHOUSE)}
    assert v["8018140Dublin, Ireland"].keep is False          # security engineer: not a target role
    assert v["5082494Dublin, Ireland"].reason == "senior title"
    assert v["7820671Dublin, Ireland"].reason == "asks for 5+ years"
    assert v["7824137London, England"].reason == "location"


def test_entry_titles_kept():
    for t in ["Graduate Software Engineer", "Junior Java Developer", "Associate Cloud Engineer",
              "Software Engineer - New Grad 2027", "AI Engineer, Early Careers"]:
        v = rule_verdict(job(t))
        assert v and v.keep, t


def test_senior_and_non_software_dropped():
    for t in ["Staff Product Engineer, AI", "Engineering Manager", "Sales Engineer",
              "Mechanical Design Engineer", "Software Engineer III", "Principal Engineer"]:
        v = rule_verdict(job(t))
        assert v and not v.keep, t


def test_ambiguous_goes_to_llm():
    assert rule_verdict(job("Software Engineer")) is None
    assert rule_verdict(job("Backend Engineer", desc="Python, Postgres, AWS.")) is None


def test_years_parsing():
    assert min_years_required("3-5 years of professional experience") == 3
    assert min_years_required("at least 1 year of experience with Python") == 1
    assert min_years_required("founded 12 years ago") is None
    assert rule_verdict(job("Software Engineer", desc="0-1 years experience")).keep


def test_categories():
    assert role_category("Machine Learning Engineer") == "ai"
    assert role_category("Graduate DevOps Engineer") == "cloud"
    assert role_category("Full Stack Developer") == "swe"
    assert role_category("Account Executive") == ""
    # Regressions from the first live run.
    assert role_category("Cloud Security Engineer") == ""
    assert role_category("Maintenance Engineer") == ""
    assert role_category("AI Security Engineer") == "ai"


def test_interns_opt_in():
    assert not rule_verdict(job("Software Engineering Intern")).keep
    assert rule_verdict(job("Software Engineering Intern"), include_interns=True).keep


def test_other_parsers():
    lever = [{"id": "a1", "text": "Junior Developer", "hostedUrl": "u",
              "categories": {"location": "Cork, Ireland"}, "descriptionPlain": "d"}]
    assert ats.parse_lever("L", lever)[0].location == "Cork, Ireland"
    ashby = {"jobs": [{"id": "b1", "title": "AI Engineer", "location": "Dublin", "isRemote": True,
                       "jobUrl": "u", "descriptionPlain": "d"}]}
    assert ats.parse_ashby("A", ashby)[0].location == "Dublin (Remote)"
    wk = {"jobs": [{"shortcode": "C1", "title": "Graduate QA", "city": "Dublin", "country": "Ireland",
                    "url": "u"}]}
    assert ats.parse_workable("W", wk)[0].job_id == "C1"
    sr = {"content": [{"id": "9", "name": "Cloud Engineer", "location": {"city": "Galway", "country": "ie"}}]}
    assert ats.parse_smartrecruiters("S", "s", sr)[0].url == "https://jobs.smartrecruiters.com/s/9"
    xml = ("<workzag-jobs><position><id>42</id><name>Junior Developer</name><office>Dublin</office>"
           "<jobDescriptions><jobDescription><value>&lt;p&gt;Hi&lt;/p&gt;</value></jobDescription>"
           "</jobDescriptions></position></workzag-jobs>")
    p = ats.parse_personio("P", "p", xml)[0]
    assert (p.job_id, p.description) == ("42", "Hi")
    wd = {"jobPostings": [{"title": "Graduate Software Engineer", "externalPath": "/job/Dublin/x_R1",
                           "locationsText": "Dublin", "bulletFields": ["R1"]}]}
    assert ats.parse_workday("D", "https://b", wd)[0].url == "https://b/job/Dublin/x_R1"


def test_store_dedup(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    a, b = job("Graduate Software Engineer"), job("Junior Developer")
    assert len(s.unseen([a, b])) == 2
    s.mark(a, rule_verdict(a))
    s.commit()
    assert [j.title for j in Store(str(tmp_path / "t.db")).unseen([a, b])] == ["Junior Developer"]


def test_digest():
    j = job("Graduate Software Engineer")
    subj, h, t = build_digest([(j, rule_verdict(j))], ["Foo (lever): HTTPError"])
    assert "1 new entry-level role" in subj and "Graduate Software Engineer" in h and "Foo" in t


def test_end_to_end_with_mocked_feed(tmp_path, monkeypatch):
    from radar import main as m
    (tmp_path / "c.yaml").write_text("companies:\n  - {name: Acme, ats: greenhouse, slug: acme}\n"
                                     "  - {name: Broken, ats: lever, slug: nope}\n")
    feed = {"jobs": [{"id": 1, "title": "Graduate Software Engineer", "location": {"name": "Dublin"},
                      "absolute_url": "u1"},
                     {"id": 2, "title": "Senior Software Engineer", "location": {"name": "Dublin"},
                      "absolute_url": "u2"}]}

    def fake_fetch(c):
        if c["name"] == "Broken":
            raise RuntimeError("404")
        return ats.parse_greenhouse(c["name"], json.loads(json.dumps(feed)))

    monkeypatch.setattr(m.ats, "fetch", fake_fetch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    sent = []
    monkeypatch.setattr(m, "send_email", lambda s, h, t: sent.append(s))
    base = ["--companies", "c.yaml", "--db", "r.db"]
    digest = tmp_path / "digest.html"

    m.main(base + ["--dry-run"])
    assert "Graduate Software Engineer" in digest.read_text()
    m.main(base + ["--dry-run"])  # dry runs don't save, so the match is still new
    assert "Graduate Software Engineer" in digest.read_text()

    m.main(base)  # real run: emails and saves
    assert len(sent) == 1 and "1 new" in sent[0]
    m.main(base + ["--dry-run"])  # nothing new now; broken feed still reported
    assert "Graduate" not in digest.read_text() and "Broken" in digest.read_text()


def test_llm_verdict_parses_reply():
    from types import SimpleNamespace
    from radar.classify import llm_verdict

    class Fake:
        def __init__(self, reply):
            self.messages = SimpleNamespace(create=lambda **kw: SimpleNamespace(
                content=[SimpleNamespace(text=reply)]))

    yes = llm_verdict(job("Software Engineer"), Fake('Sure: {"min_years": 1, "level": "mid", "category": "swe", "reason": "x"}'))
    no = llm_verdict(job("Software Engineer"), Fake('{"min_years": 4, "level": "mid", "category": "swe", "reason": "x"}'))
    other = llm_verdict(job("Engineer"), Fake('{"min_years": 0, "level": "entry", "category": "other", "reason": "hardware"}'))
    assert (yes.keep, no.keep, other.keep) == (True, False, False) and yes.source == "claude"


def test_ollama_classifier(monkeypatch):
    from types import SimpleNamespace
    from radar import classify as c
    calls = {}

    def fake_post(url, json, timeout):
        calls["url"], calls["body"] = url, json
        reply = '{"min_years": 1, "level": "entry", "category": "ai", "reason": "1 year required"}'
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"message": {"content": reply}})

    monkeypatch.setattr(c.requests, "post", fake_post)
    v = c.OllamaClassifier(model="qwen2.5:7b", host="http://localhost:11434")(job("Software Engineer"))
    assert v.keep and v.category == "ai" and v.source == "ollama:qwen2.5:7b"
    assert calls["url"] == "http://localhost:11434/api/chat"
    assert calls["body"]["format"] == "json" and calls["body"]["stream"] is False


def test_ollama_check_errors(monkeypatch):
    from types import SimpleNamespace
    import pytest
    import requests
    from radar import classify as c

    monkeypatch.setattr(c.requests, "get", lambda *a, **k: SimpleNamespace(
        json=lambda: {"models": [{"name": "llama3.1:8b"}]}))
    with pytest.raises(RuntimeError, match="ollama pull qwen2.5:7b"):
        c.OllamaClassifier(model="qwen2.5:7b").check()

    def down(*a, **k):
        raise requests.ConnectionError()
    monkeypatch.setattr(c.requests, "get", down)
    with pytest.raises(RuntimeError, match="not reachable"):
        c.OllamaClassifier().check()


def test_backend_selection(monkeypatch):
    from radar import classify as c
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("RADAR_LLM", raising=False)
    assert c.make_classifier() is None
    monkeypatch.setenv("RADAR_LLM", "ollama")
    monkeypatch.setattr(c.OllamaClassifier, "check", lambda self: None)
    assert isinstance(c.make_classifier(), c.OllamaClassifier)


def test_env_file_loading(tmp_path, monkeypatch):
    from radar import load_env
    (tmp_path / ".env").write_text('# comment\nSMTP_USER="a@b.com"\nMAIL_TO=c@d.com\nBROKEN LINE\n')
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.setenv("MAIL_TO", "already@set.com")
    load_env(tmp_path / ".env")
    assert os.environ["SMTP_USER"] == "a@b.com"
    assert os.environ["MAIL_TO"] == "c@d.com"           # .env wins over a stale env var
    load_env(tmp_path / "missing.env")                   # no file: no error


def test_decide_on_cases_from_first_live_run():
    from radar.classify import decide
    # Toast Android: model said keep while its reason said "2-4 years". Code now decides.
    v = decide({"min_years": 2, "level": "mid", "category": "swe", "reason": "2-4 years"}, "m")
    assert v.keep and v.reason.startswith("stretch")
    assert not decide({"min_years": 4, "level": "mid", "category": "swe"}, "m").keep
    assert not decide({"min_years": 1, "level": "senior", "category": "swe"}, "m").keep
    assert not decide({"min_years": None, "level": "mid", "category": "swe"}, "m").keep
    assert decide({"min_years": None, "level": "entry", "category": "cloud"}, "m").keep
    assert decide({"min_years": "0", "level": "mid", "category": "ai"}, "m").keep   # string from model
    assert not decide({"min_years": 0, "level": "entry", "category": "other"}, "m").keep


def test_numbered_levels_and_two_years():
    assert rule_verdict(job("Software Engineer 3")).reason == "senior title"
    assert rule_verdict(job("Software Engineer 2")) is None           # level 2 stays open
    v = rule_verdict(job("Software Engineer", desc="2+ years of professional experience"))
    assert v.keep and v.reason.startswith("stretch")


def test_duplicate_postings_shown_once(tmp_path, monkeypatch):
    from radar import main as m
    (tmp_path / "c.yaml").write_text("companies:\n  - {name: Acme, ats: greenhouse, slug: acme}\n")
    feed = {"jobs": [{"id": i, "title": "Graduate Software Engineer", "location": {"name": "Dublin"},
                      "absolute_url": f"u{i}"} for i in (1, 2)]}
    monkeypatch.setattr(m.ats, "fetch", lambda c: ats.parse_greenhouse("Acme", feed))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("RADAR_LLM", raising=False)
    monkeypatch.chdir(tmp_path)
    m.main(["--companies", "c.yaml", "--db", "r.db", "--dry-run"])
    assert (tmp_path / "digest.html").read_text().count("Graduate Software Engineer") == 1


def test_smartrecruiters_description_fetch(monkeypatch):
    from types import SimpleNamespace
    payload = {"jobAd": {"sections": {
        "jobDescription": {"text": "<p>Build cloud platforms.</p>"},
        "qualifications": {"text": "<ul><li>3+ years of experience with AWS</li></ul>"}}}}
    seen = {}

    def fake_get(url, **kw):
        seen["url"] = url
        return SimpleNamespace(json=lambda: payload)

    monkeypatch.setattr(ats, "_get", fake_get)
    j = Job("Version 1", "smartrecruiters", "123", "Cloud Platform Consultant", "Dublin, IE",
            "https://jobs.smartrecruiters.com/version1/123")
    desc = ats.fetch_description(j)
    assert seen["url"] == "https://api.smartrecruiters.com/v1/companies/version1/postings/123"
    assert desc == "Build cloud platforms. 3+ years of experience with AWS"
    j.description = desc
    assert rule_verdict(j).reason == "asks for 3+ years"      # now decided without the LLM
    assert ats.fetch_description(job("x")) == ""               # other ATSs: nothing to fetch


def test_workday_enrich_resolves_location_and_description(monkeypatch):
    from types import SimpleNamespace
    from radar import main as m
    detail = {"jobPostingInfo": {
        "location": "Dublin, Ireland", "additionalLocations": ["London, UK"],
        "jobDescription": "<p>Graduate role. 0-1 years of experience.</p>"}}
    us_detail = {"jobPostingInfo": {"location": "Charlotte, NC", "additionalLocations": ["New York, NY"],
                                    "jobDescription": "<p>Build things.</p>"}}
    seen = []

    def fake_get(url, **kw):
        seen.append(url)
        return SimpleNamespace(json=lambda: us_detail if "R2" in url else detail)

    monkeypatch.setattr(ats, "_get", fake_get)
    base = "https://acme.wd3.myworkdayjobs.com/en-US/AcmeCareers"
    ie = Job("Acme", "workday", "R1", "Software Engineer", "2 Locations", base + "/job/Dublin/SWE_R1")
    us = Job("Acme", "workday", "R2", "Graduate Software Engineer", "2 Locations", base + "/job/x/SWE_R2")
    snr = Job("Acme", "workday", "R3", "Senior Software Engineer", "2 Locations", base + "/job/x/SWE_R3")
    args = SimpleNamespace(interns=False)

    v = m.classify(ie, args, None)
    assert seen[0] == "https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/AcmeCareers/job/Dublin/SWE_R1"
    assert ie.location == "Dublin, Ireland; London, UK" and v.keep and v.reason == "asks for 0 years"
    assert m.classify(us, args, None).reason == "location"     # "2 Locations" turned out to be US
    assert not m.classify(snr, args, None).keep and len(seen) == 2   # senior: no detail fetch


def test_new_ats_parsers():
    # Recruitee: fields from its documented /api/offers/ response.
    rc = {"offers": [{"id": 7, "slug": "jr-dev", "title": "Junior Developer", "location": "Dublin, Ireland",
                      "city": "Dublin", "country_code": "IE", "careers_url": "https://acme.com/o/jr-dev",
                      "description": "<p>Python</p>", "requirements": "<p>0-1 years experience</p>",
                      "remote": False, "published_at": "2026-10-01 08:00:00 UTC"}]}
    r = ats.parse_recruitee("Acme", rc)[0]
    assert (r.job_id, r.location, r.url) == ("7", "Dublin, Ireland", "https://acme.com/o/jr-dev")
    assert "0-1 years experience" in r.description

    # Breezy: shape copied from a live /json response.
    bz = [{"id": "abc", "friendly_id": "x", "name": "Graduate Engineer", "url": "https://acme.breezy.hr/p/abc",
           "published_date": "2026-10-01", "location": {"country": {"name": "Ireland", "id": "IE"},
           "city": "Cork", "is_remote": False, "name": "Cork"}, "locations": []}]
    b = ats.parse_breezy("Acme", bz)[0]
    assert b.location == "Cork, Ireland" and b.title == "Graduate Engineer"

    # Pinpoint: shape copied from a live postings.json.
    pp = {"data": [{"id": "559663", "title": "Software Engineer", "url": "https://acme.pinpointhq.com/en/postings/u",
                    "description": "<div>Build</div>", "skills_knowledge_expertise": "<ul><li>2+ years</li></ul>",
                    "workplace_type": "hybrid", "location": {"city": "Dublin", "name": "Dublin Office", "province": None}}]}
    p = ats.parse_pinpoint("Acme", pp)[0]
    assert p.location == "Dublin, Dublin Office" and "2+ years" in p.description

    # Teamtailor RSS: tag names undocumented, so test a namespaced location block.
    tt = """<rss xmlns:tt="https://teamtailor.com/locations"><channel><item>
        <title>Junior Backend Engineer</title><link>https://acme.teamtailor.com/jobs/1-jr</link>
        <guid>tt-1</guid><description>&lt;p&gt;Go and AWS&lt;/p&gt;</description><pubDate>Mon, 12 Oct 2026</pubDate>
        <remoteStatus>hybrid</remoteStatus>
        <tt:locations><tt:location><tt:city>Dublin</tt:city><tt:country>Ireland</tt:country></tt:location></tt:locations>
        </item></channel></rss>"""
    t = ats.parse_teamtailor("Acme", tt)[0]
    assert t.title == "Junior Backend Engineer" and "Dublin" in t.location and "Ireland" in t.location
    assert t.location.endswith("(Remote)") and t.description == "Go and AWS" and t.job_id == "tt-1"

    # BambooHR: list has no description; location may be an object or string.
    bh = {"result": [{"id": "58", "jobOpeningName": "Cloud Engineer", "atsLocation": {"city": "Galway", "country": "Ireland"}},
                     {"id": "59", "jobOpeningName": "SRE", "location": "Remote", "isRemote": True}]}
    a, c = ats.parse_bamboohr("Acme", "acme", bh)
    assert a.location == "Galway, Ireland" and a.url == "https://acme.bamboohr.com/careers/58"
    assert c.location == "Remote (Remote)"


def test_discover_reads_new_ats_urls():
    import discover as d
    want = {"https://acme.recruitee.com/o/jr": "recruitee", "https://acme.teamtailor.com/jobs/1": "teamtailor",
            "https://acme.bamboohr.com/careers/58": "bamboohr", "https://acme.pinpointhq.com/en/postings/x": "pinpoint",
            "https://acme.breezy.hr/p/abc": "breezy"}
    for url, system in want.items():
        e = d.entry_from_url("Acme", url)
        assert (e["ats"], e["slug"]) == (system, "acme"), url


def test_irish_towns_without_country():
    from radar.filters import in_ireland
    for loc in ["Killorglin", "Killorglin, Kerry", "Leixlip", "Ennis", "Co. Mayo", "Citywest"]:
        assert in_ireland(job("x", loc=loc)), loc
    for loc in ["London", "Charlotte, NC", "Kildare Street, Melbourne"][:2]:
        assert not in_ireland(job("x", loc=loc)), loc


def test_concurrent_runs_skip_and_stale_lock_is_ignored(tmp_path, monkeypatch, capsys):
    import time as _t
    from radar import main as m
    (tmp_path / "c.yaml").write_text("companies:\n  - {name: Acme, ats: greenhouse, slug: acme}\n")
    feed = {"jobs": [{"id": 1, "title": "Graduate Software Engineer", "location": {"name": "Dublin"}, "absolute_url": "u"}]}
    monkeypatch.setattr(m.ats, "fetch", lambda c: ats.parse_greenhouse("Acme", feed))
    monkeypatch.setattr(m, "send_email", lambda *a: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("RADAR_LLM", raising=False)
    monkeypatch.chdir(tmp_path)
    base = ["--companies", "c.yaml", "--db", "r.db"]

    (tmp_path / "r.db.lock").write_text("123")                 # a run is in progress
    m.main(base)
    assert "another radar run is in progress" in capsys.readouterr().out
    m.main(base + ["--dry-run"])                               # dry runs ignore the lock
    assert "1 matches" in capsys.readouterr().out

    old = _t.time() - 2 * 60 * 60                              # lock left by a crash 2h ago
    os.utime(tmp_path / "r.db.lock", (old, old))
    m.main(base)
    out = capsys.readouterr().out
    assert "email sent" in out and not (tmp_path / "r.db.lock").exists()
    m.main(base)                                               # saved: nothing new now
    assert "0 new" in capsys.readouterr().out


def test_titles_from_first_full_run():
    # Wrongly kept on 14 Oct: hardware and support roles.
    for t in ["Engineer, Product Engineering (Mfg.)", "Associate Engineer, Equipment Engineering",
              "Developer Support Specialist", "Associate Technical Services Engineer II (Cloud)"]:
        assert role_category(t) == "", t
    # Correctly kept: must still pass.
    for t in ["FY27 Software Engineering Graduate Opportunities", "Junior .NET Developer",
              "Software Engineer Graduate 2026/2027", "Systems Engineer, Linux", "Platform Engineer II",
              "Golang Engineer - SRE Engineering Productivity", "Front End Developer"]:
        assert role_category(t) != "", t


def test_get_retries_on_429(monkeypatch):
    from types import SimpleNamespace
    calls = []

    def fake(method, url, **kw):
        calls.append(url)
        code = 429 if len(calls) < 3 else 200
        return SimpleNamespace(status_code=code, headers={"Retry-After": "0"}, raise_for_status=lambda: None)

    monkeypatch.setattr(ats.requests, "request", fake)
    monkeypatch.setattr(ats.time, "sleep", lambda s: None)
    assert ats._get("https://x").status_code == 200 and len(calls) == 3


def test_workday_retries_and_pages(monkeypatch):
    """Workday POSTs get the same 429 retry as GETs (Salesforce rate-limits fast paging)."""
    from types import SimpleNamespace
    calls = []

    def fake(method, url, json=None, **kw):
        calls.append((method, json["offset"]))
        if len(calls) == 1:
            return SimpleNamespace(status_code=429, headers={}, raise_for_status=lambda: None)
        posting = {"title": "Software Engineer", "externalPath": f"/job/Dublin/SE_{json['offset']}",
                   "locationsText": "Dublin, Ireland", "postedOn": "Posted Today"}
        return SimpleNamespace(status_code=200, headers={}, raise_for_status=lambda: None,
                               json=lambda: {"total": 30, "jobPostings": [posting]})

    monkeypatch.setattr(ats.requests, "request", fake)
    monkeypatch.setattr(ats.time, "sleep", lambda s: None)
    jobs = ats.fetch_workday({"name": "Acme", "tenant": "acme", "host": "wd3", "site": "Ext",
                              "search": ["software"]})
    assert calls == [("POST", 0), ("POST", 0), ("POST", 20)]
    assert len(jobs) == 2


def test_detect_ats_finds_boards_in_pages(monkeypatch):
    import detect_ats as d
    pages = {
        # home page has no board, but links to /careers which embeds Greenhouse
        "https://acme.ie": '<a href="/about">About</a> <a href="/careers">Careers</a>',
        "https://acme.ie/careers": '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acmeireland"></script>',
        # Workday link straight on the page
        "https://bank.ie": '<a href="https://bankie.wd3.myworkdayjobs.com/en-US/BankCareers">Jobs</a>',
        # API call only visible as a URL string (what the rendered network log looks like)
        "https://widget.ie": 'fetch("https://api.lever.co/v0/postings/widgetco?mode=json")',
        "https://plain.ie": "<p>Email cv@plain.ie</p>",
    }
    monkeypatch.setattr(d, "fetch_html", lambda url: (url, pages.get(url.rstrip("/"), "")))

    boards, checked = d.detect_static("https://acme.ie")
    assert boards == [{"ats": "greenhouse", "slug": "acmeireland"}] and checked[-1] == "https://acme.ie/careers"
    assert d.detect_static("https://bank.ie")[0] == [{"ats": "workday", "tenant": "bankie", "host": "wd3", "site": "BankCareers"}]
    assert d.detect_static("https://widget.ie")[0] == [{"ats": "lever", "slug": "widgetco"}]
    assert d.detect_static("https://plain.ie")[0] == []


def test_detect_ats_ignores_non_slugs():
    import detect_ats as d
    text = ("https://apply.workable.com/fenergocareers/ https://apply.workable.com/api/v1/widget/accounts/fenergocareers "
            "https://jobs.eu.lever.co/everseen/123 https://www.bamboohr.com/pricing https://deciphex.bamboohr.com/careers")
    found = d.find_boards(text)
    assert {"ats": "workable", "slug": "fenergocareers"} in found
    assert {"ats": "lever", "slug": "everseen", "region": "eu"} in found
    assert {"ats": "bamboohr", "slug": "deciphex"} in found
    assert all(b.get("slug") not in ("apply", "www", "api") for b in found)


def test_detect_ats_render_targets_skip_dead_pages(monkeypatch):
    import detect_ats as d
    monkeypatch.setattr(d, "STATUS", {"https://a.ie": 200, "https://a.ie/careers": 200, "https://a.ie/jobs": 404,
                                      "https://b.ie": 0, "https://b.ie/careers": 0,
                                      "https://c.ie": 403})
    # careers page first, 404s dropped
    assert d.render_targets(["https://a.ie", "https://a.ie/jobs", "https://a.ie/careers"]) == \
        ["https://a.ie/careers", "https://a.ie"]
    # unreachable site: nothing to render
    assert d.render_targets(["https://b.ie", "https://b.ie/careers"]) == []
    # bot-blocked home page: a real browser may get through
    assert d.render_targets(["https://c.ie"]) == ["https://c.ie"]