"""LLM fallback for titles the rules can't decide, e.g. a plain "Software Engineer".

Pick the backend with RADAR_LLM:
    ollama     local model via Ollama (default model: qwen2.5:7b)
    anthropic  Claude Haiku via the API (needs ANTHROPIC_API_KEY)
    none       no LLM; ambiguous jobs are emailed unchecked
If RADAR_LLM is unset: anthropic when ANTHROPIC_API_KEY is set, otherwise none.
"""
import json
import os

import requests

from .models import Job, Verdict

PROMPT = """Extract facts from this job posting. Do not judge the candidate; just report what the posting says.

Title: {title}
Company: {company}
Location: {location}
Description:
{description}

Reply with JSON only:
{{"min_years": <smallest number of years of professional experience the posting requires, or null if it states none>,
 "level": "entry" | "mid" | "senior",
 "category": "swe" | "ai" | "cloud" | "other",
 "reason": "<10 words max>"}}

level: "entry" = graduate/junior/new grad or clearly for early-career people; "senior" = leads, mentors, \
owns architecture; otherwise "mid". category "other" = not a software, AI/ML or cloud engineering role \
(e.g. sales, support, data analyst, hardware)."""


def build_prompt(job: Job) -> str:
    return PROMPT.format(title=job.title, company=job.company, location=job.location,
                         description=(job.description or "(no description available)")[:6000])


def decide(data: dict, source: str) -> Verdict:
    """Turn the model's extracted facts into a keep/drop decision. The code decides, not the model."""
    cat = data.get("category", "other")
    level = data.get("level", "mid")
    yrs = data.get("min_years")
    try:
        yrs = int(yrs) if yrs is not None else None
    except (TypeError, ValueError):
        yrs = None
    note = data.get("reason", "")

    if cat == "other":
        return Verdict(False, cat, f"not a target role: {note}", source)
    if level == "senior":
        return Verdict(False, cat, f"senior: {note}", source)
    if yrs is not None and yrs >= 3:
        return Verdict(False, cat, f"asks for {yrs}+ years", source)
    if yrs == 2:
        return Verdict(True, cat, f"stretch: asks for 2 years. {note}", source)
    if yrs is not None:
        return Verdict(True, cat, f"asks for {yrs} years. {note}", source)
    if level == "entry":
        return Verdict(True, cat, f"entry level, no years stated. {note}", source)
    return Verdict(False, cat, f"mid level, no years stated: {note}", source)


def parse_reply(text: str, source: str) -> Verdict:
    """Pull the JSON object out of a model reply, even if it added chatter around it."""
    start, end = text.find("{"), text.rfind("}")
    return decide(json.loads(text[start:end + 1]), source)


class OllamaClassifier:
    def __init__(self, model=None, host=None, timeout=180):
        self.model = model or os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
        self.host = (host or os.getenv("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")
        self.timeout = timeout

    def check(self):
        """Fail fast with a clear message if Ollama isn't running or the model isn't pulled."""
        try:
            tags = requests.get(f"{self.host}/api/tags", timeout=5).json()
        except requests.RequestException:
            raise RuntimeError(f"Ollama not reachable at {self.host}. Is it running?")
        names = {m["name"] for m in tags.get("models", [])}
        if self.model not in names and f"{self.model}:latest" not in names:
            raise RuntimeError(f"Model {self.model} not pulled. Run: ollama pull {self.model}")

    def __call__(self, job: Job) -> Verdict:
        r = requests.post(f"{self.host}/api/chat", timeout=self.timeout, json={
            "model": self.model,
            "messages": [{"role": "user", "content": build_prompt(job)}],
            "format": "json",            # constrains output to valid JSON
            "stream": False,
            "options": {"temperature": 0},
        })
        r.raise_for_status()
        return parse_reply(r.json()["message"]["content"], f"ollama:{self.model}")


class AnthropicClassifier:
    def __init__(self, client=None, model=None):
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        self.client = client
        self.model = model or os.getenv("RADAR_MODEL", "claude-haiku-5-5")

    def check(self):
        pass

    def __call__(self, job: Job) -> Verdict:
        msg = self.client.messages.create(
            model=self.model, max_tokens=150,
            messages=[{"role": "user", "content": build_prompt(job)}])
        return parse_reply(msg.content[0].text.strip(), "claude")


def make_classifier():
    """Return a callable Job -> Verdict, or None when no LLM is configured."""
    backend = os.getenv("RADAR_LLM", "").lower()
    if not backend:
        backend = "anthropic" if os.getenv("ANTHROPIC_API_KEY") else "none"
    if backend == "none":
        return None
    clf = OllamaClassifier() if backend == "ollama" else AnthropicClassifier()
    clf.check()
    return clf


def llm_verdict(job: Job, client=None) -> Verdict:
    """Kept for backwards compatibility with the tests: Claude with an injected client."""
    return AnthropicClassifier(client=client)(job)