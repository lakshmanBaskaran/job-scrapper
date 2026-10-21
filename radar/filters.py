"""Cheap rule-based filters. These run on every job, before any LLM call."""
from __future__ import annotations  # lets `int | None` hints work on Python 3.8/3.9

import re

from .models import Job, Verdict

IRELAND = re.compile(
    r"\b(ireland|eire|"
    # 26 counties
    r"dublin|cork|galway|limerick|waterford|kerry|clare|mayo|donegal|kildare|meath|louth|wicklow|"
    r"wexford|kilkenny|tipperary|offaly|westmeath|laois|carlow|longford|roscommon|sligo|leitrim|"
    r"cavan|monaghan|"
    # towns and business parks that appear without a county
    r"athlone|letterkenny|shannon|dundalk|drogheda|tralee|killarney|killorglin|ennis|castlebar|"
    r"maynooth|leixlip|celbridge|naas|newbridge|navan|bray|swords|dun laoghaire|portlaoise|"
    r"mullingar|nenagh|mallow|cobh|carrigtwohill|ringaskiddy|ballina|tullamore|"
    r"leopardstown|sandyford|blanchardstown|mulhuddart|citywest|cherrywood|"
    r"\bIE\b|EMEA|europe|EU remote|remote.*\b(eu|europe|emea)\b)",
    re.I,
)
# Workday often says "3 Locations" with no names; let those through to the title filter.
VAGUE_LOCATION = re.compile(r"^\s*\d+\s+locations?\s*$|^\s*$", re.I)

ROLE = {
    "ai": re.compile(r"\b(ai|a\.i\.|ml|machine learning|llm|genai|gen ai|applied scientist|"
                     r"deep learning|nlp|computer vision|mlops)\b", re.I),
    "cloud": re.compile(r"\b(cloud|devops|platform|sre|site reliability|infrastructure|"
                        r"kubernetes|aws|azure|gcp)\b", re.I),
    "swe": re.compile(r"\b(software|developer|programmer|swe|backend|back-end|frontend|"
                      r"front-end|full[- ]?stack|engineer)\b", re.I),
}
# Titles containing "engineer" that aren't software jobs.
NON_SOFTWARE = re.compile(
    r"\b(sales|solutions? engineer|support engineer|field|mechanical|electrical|civil|"
    r"chemical|process engineer|quality engineer|manufacturing|validation|hvac|"
    r"maintenance|commissioning|structural|recruit|account|marketing|designer|"
    r"technician|security engineer|network engineer|it systems|data scientist|"
    r"mfg|product engineering|equipment engineering|test engineering|hardware|silicon|"
    r"analog|rf|layout|asic|fpga)\b",
    re.I,
)
# Customer-facing support roles stay out even when the title says "developer" or "cloud".
SUPPORT = re.compile(r"\b(support|technical services|helpdesk|help desk|service desk)\b", re.I)
SENIOR = re.compile(
    r"\b(senior|sr\.?|lead|staff|principal|head|director|manager|architect|"
    r"vp|chief|expert|distinguished|iii|iv|level\s*[3-9]|l[4-9])\b",
    re.I,
)
# Numbered levels: "Software Engineer 3", "Developer 4". Level 2/II is often 0-2 years, so it stays.
HIGH_LEVEL = re.compile(r"\b(?:engineer|developer|scientist)\s*[3-9]\b", re.I)
ENTRY = re.compile(
    r"\b(graduate|grad|junior|jr\.?|entry[- ]level|new grad|associate|intern(ship)?|"
    r"trainee|apprentice|early careers?|placement|engineer i\b|developer i\b|"
    r"level\s*1|l1|0[-–]2 years)\b",
    re.I,
)
# "5+ years", "3-5 years", "minimum of 4 years".
YEARS = re.compile(r"(?:minimum of |at least )?(\d{1,2})\s*\+?\s*(?:[-–to]+\s*\d{1,2}\s*)?years?", re.I)


def in_ireland(job: Job) -> bool:
    return bool(IRELAND.search(job.location)) or bool(VAGUE_LOCATION.match(job.location))


def role_category(title: str) -> str:
    """Return ai/cloud/swe, or '' when the title isn't a target role."""
    if SUPPORT.search(title):
        return ""
    # Word boundaries matter: without \b, "ai" matches inside "Maintenance".
    if NON_SOFTWARE.search(title) and not re.search(r"\b(software|developer|ai|ml)\b", title, re.I):
        return ""
    for cat in ("ai", "cloud", "swe"):
        if ROLE[cat].search(title):
            return cat
    return ""


def min_years_required(text: str) -> int | None:
    """Smallest 'N years' figure that reads like an experience requirement."""
    found = []
    for m in YEARS.finditer(text):
        window = text[m.start(): m.end() + 60].lower()
        if "experience" in window or "exp" in window:
            found.append(int(m.group(1)))
    return min(found) if found else None


def rule_verdict(job: Job, include_interns: bool = False) -> Verdict | None:
    """Return a Verdict when rules can decide; None means 'ambiguous, ask the LLM'."""
    if not in_ireland(job):
        return Verdict(False, reason="location", source="rules")
    cat = role_category(job.title)
    if not cat:
        return Verdict(False, reason="not a target role", source="rules")
    if SENIOR.search(job.title) or HIGH_LEVEL.search(job.title):
        return Verdict(False, cat, "senior title", "rules")
    if re.search(r"\bintern(ship)?\b", job.title, re.I) and not include_interns:
        return Verdict(False, cat, "internship", "rules")
    if ENTRY.search(job.title):
        return Verdict(True, cat, "entry-level title", "rules")
    yrs = min_years_required(job.description) if job.description else None
    if yrs is not None and yrs >= 3:
        return Verdict(False, cat, f"asks for {yrs}+ years", "rules")
    if yrs is not None and yrs <= 1:
        return Verdict(True, cat, f"asks for {yrs} years", "rules")
    if yrs == 2:
        return Verdict(True, cat, "stretch: asks for 2 years", "rules")
    return None