# Defense Industry Internship Tracker

Automated daily scan of internship postings at major U.S. defense contractors.
A GitHub Actions workflow runs `tracker.py` every 24 hours, queries each
company's public job board, filters for internship-level roles, and commits
the diff back to `internships.json`.

## Files

| File | Purpose |
| ---- | ------- |
| `tracker.py` | Scraper / diff engine |
| `companies.json` | Target list with platform-specific config |
| `internships.json` | Current set of open internship postings (auto-generated) |
| `.github/workflows/update.yml` | Daily scheduled run |
| `requirements.txt` | Python dependencies |

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python tracker.py
```

The script logs a per-company count and a final summary line:

```
summary: 47 total | +3 added | -1 removed | 10 companies scanned
```

## Adding a new company

Append an entry to `companies.json`. The required fields depend on the platform.

### Greenhouse

```json
{
  "name": "Anduril",
  "platform": "greenhouse",
  "config": { "board_token": "andurilindustries" }
}
```

`board_token` is the slug in the public board URL: `boards.greenhouse.io/<token>`.

### Lever

```json
{
  "name": "Palantir",
  "platform": "lever",
  "config": { "site": "palantir" }
}
```

`site` is the slug in `jobs.lever.co/<site>`.

### Workday

```json
{
  "name": "Lockheed Martin",
  "platform": "workday",
  "config": {
    "tenant": "lockheedmartin",
    "site": "Lockheed_Martin",
    "wd": "wd1",
    "external_url": "https://www.lockheedmartinjobs.com/en/job"
  }
}
```

To find these values, open the company's careers page and look at the URL:
`https://<tenant>.<wd>.myworkdayjobs.com/<site>`. The script POSTs to
`/wday/cxs/<tenant>/<site>/jobs` — the same endpoint the careers page itself
uses. `external_url` is just the prefix prepended to each posting's
`externalPath` so the stored URL points at the public job page rather than the
internal CXS path.

> ⚠️ Workday tenants are inconsistent. If a company returns 404, open the
> careers site in a browser, watch the Network tab for the `cxs/.../jobs` POST,
> and copy the values from there.

## How filtering works

A posting is kept if its title matches `intern`, `internship`, or `co-op` and
does **not** also match exclusion terms (`manager`, `senior`, `principal`,
etc., to avoid e.g. "Internship Program Manager"). Lever's `commitment` field
is also checked when present.

## How the diff works

Each posting is keyed by `company::url`. On every run:

- New keys → `+added`, recorded with today's `date_found`.
- Missing keys → `-removed`, dropped from the file.
- Existing keys → `date_found` is preserved; `title` / `location` are refreshed.

## Workflow

`.github/workflows/update.yml` runs daily at 12:00 UTC and on manual dispatch.
It commits any change to `internships.json` back to the repository.
