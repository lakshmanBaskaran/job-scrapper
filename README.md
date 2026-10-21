# job-radar

Checks Irish company career pages once a day and emails a digest of new entry-level
SWE, AI and cloud roles.

```
companies.yaml ──► ats.py (one adapter per ATS) ──► filters.py (location, role, seniority, years)
                                                         │
                                       ambiguous title? ─┴─► classify.py (Claude Haiku)
                                                         │
                          store.py (SQLite: skip anything seen before) ──► notify.py (email)
```

## Files

| File | What it does |
|---|---|
| `companies.yaml` | The company list. Every entry names its ATS and slug. |
| `radar/ats.py` | Fetchers and parsers for Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Personio and Workday, all normalised to one `Job`. |
| `radar/filters.py` | Regex rules: Irish location, target role (SWE/AI/cloud), senior titles out, entry titles in, "N+ years of experience" check. Returns `None` when unsure. |
| `radar/classify.py` | Sends the ambiguous jobs to an LLM (local Ollama or Claude Haiku) and parses a JSON verdict. |
| `radar/store.py` | SQLite tables `seen` (every job ever processed, with its verdict) and `runs` (a log of each run). |
| `radar/notify.py` | Builds the HTML/plain-text digest and sends it over SMTP. |
| `radar/main.py` | Wires it together. `--dry-run`, `--bootstrap`, `--interns`. |
| `scripts/check_seeds.py` | Hits every feed once and reports ok/empty/failed, with an Ireland count. |
| `run_daily.ps1` | One daily run on Windows with Ollama; appends output to `radar.log`. |
| `scripts/install_task.ps1` | Registers `run_daily.ps1` as a daily Windows scheduled task. |
| `.github/workflows/radar.yml` | Hourly 08:00-19:00 (Irish time) on GitHub Actions; Claude by default, Ollama switchable. |

## Run locally

```bash
pip install -r requirements.txt
pytest -q                               # 16 offline tests
python scripts/check_seeds.py           # are the slugs right?
python -m radar.main --dry-run          # writes digest.html, sends nothing
```

## LLM backend

Set `RADAR_LLM`:

| Value | Uses | Needs |
|---|---|---|
| `ollama` | local model at `OLLAMA_HOST` (default `http://localhost:11434`) | Ollama running; `ollama pull $OLLAMA_MODEL` (default `qwen2.5:7b`) |
| `anthropic` | Claude Haiku | `ANTHROPIC_API_KEY` |
| `none` | nothing; ambiguous titles emailed unchecked | — |

Unset means `anthropic` if a key exists, else `none`. If the chosen backend can't start
(Ollama closed, model not pulled), the run continues without it and the email says so.

## Deploy on Windows (Ollama)

```powershell
ollama pull qwen2.5:7b
copy .env.example .env          # then fill in SMTP_USER, SMTP_PASSWORD (Gmail app password), MAIL_TO
.\run_daily.ps1                 # one real run; check radar.log and your inbox
.\scripts\install_task.ps1      # schedule it daily at 08:00
```

The laptop needs to be on, and Ollama running (the tray app starts at login), at some point each day.

## Deploy on GitHub Actions

Runs hourly 08:00-19:00 Irish time whether or not your laptop is on. State (`radar.db`) is
committed back to the repo after each run.

1. Push to a **private** GitHub repo, **including your current `radar.db`** so it carries on from
   where the laptop left off (otherwise the first run treats every open job as new).
2. Repo → Settings → Secrets and variables → Actions → **Secrets**:
   - `SMTP_USER`, `SMTP_PASSWORD` (Gmail app password), `MAIL_TO`
   - `ANTHROPIC_API_KEY`
3. Same page → **Variables** (optional):
   - `RADAR_LLM`: `anthropic` (default if unset), `ollama`, or `none`
   - `OLLAMA_MODEL`: default `qwen2.5:7b`; `qwen2.5:3b` is faster on GitHub's CPUs
4. Actions tab → *job radar* → **Run workflow**. You can pick the LLM for that one run.
5. Turn the laptop task off so you don't get two emails per hour:
   `Disable-ScheduledTask -TaskName job-radar`

With `RADAR_LLM=ollama` the runner installs Ollama and loads the model from the Actions cache
each run, so runs take a few minutes longer and use more of the free Actions minutes.

## Behaviour notes

- A feed that errors doesn't stop the run; failures are listed at the bottom of the email.
- If sending the email fails, nothing is committed, so the same jobs are retried on the next run.
- Workday is searched with a few terms (`software`, `engineer`, `graduate`, `developer` by default)
  and capped at 200 results per term.
- Locations like "3 Locations" with no names pass the location filter, so the title filter decides.
- Haiku cost is small: only titles the rules can't decide reach it, and only once per job.