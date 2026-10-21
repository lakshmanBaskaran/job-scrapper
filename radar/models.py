from dataclasses import dataclass, field


@dataclass
class Job:
    """One posting, normalised across every ATS."""
    company: str
    ats: str
    job_id: str          # the ATS's own id for this posting
    title: str
    location: str
    url: str
    description: str = ""  # plain text; empty when the ATS list endpoint doesn't include it
    posted: str = ""

    @property
    def key(self) -> str:
        # Unique across companies and ATSs, so two boards can't collide.
        return f"{self.ats}:{self.company}:{self.job_id}"


@dataclass
class Verdict:
    keep: bool
    category: str = ""     # swe | ai | cloud
    reason: str = ""
    source: str = ""       # "rules" or "llm"
    extra: dict = field(default_factory=dict)
