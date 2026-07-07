# US-Only Filtering + Category Buckets — Design

Date: 2026-07-07
Status: approved

## Goal

1. Stop tracking internships located outside the United States.
2. Bucket every posting into one of three categories — `software`,
   `engineering`, `non-engineering` — and let the site visitor switch
   between them.

## Decisions (confirmed with user)

- **Ambiguous locations stay.** Only postings whose location *clearly*
  indicates a non-US country are dropped. Empty strings, "2 Locations",
  "Remote", bare US states, etc. are kept — these companies are US defense
  contractors, so US is the safe default.
- **Software wins over engineering.** "Software Engineering Intern" is
  `software`. The `software` bucket covers software/SWE/developer/IT/
  computer-science/data/cyber/AI-ML roles. `engineering` covers the
  remaining engineer/technical roles (mechanical, electrical, aerospace,
  systems, manufacturing, materials, …). Business-type roles (sales, HR,
  finance, supply chain, …) are `non-engineering`. Titles matching no
  keyword at all (bare "Intern", "Co-op (Fall Term)") default to
  `engineering` (amended 2026-07-08 at user request; originally they fell
  to `non-engineering`).
- **UI: tab bar** above the controls — All / Engineering / Software /
  Non-engineering, each with a live posting count. Search, company filter,
  and sort operate within the active tab.

## Where the logic lives

In `tracker.py`, because the GitHub Action rewrites `internships.json`
three times a day — client-side-only filtering would leave the published
JSON polluted and a one-off cleanup would be undone on the next scrape.

- `is_non_us(location: str) -> bool` — matches known non-US country
  names, foreign city/region names that appear on these boards without a
  country, and platform-specific patterns (e.g. `Australia-…`, trailing
  3-letter ISO country codes from dejobs slugs). Returns `False` for
  ambiguous/empty locations.
- `categorize(title: str, disciplines: Iterable[str]) -> str` — ordered
  keyword rules over the title (and discipline tags when present):
  software patterns first, then engineering patterns, else
  `non-engineering`.
- Scraped postings are filtered through `is_non_us` before the diff, and
  previously-stored entries are filtered on load, so foreign entries can
  never survive a run. Every merged entry gets its `category` recomputed
  each run, so future keyword tweaks propagate to old postings.

## Data shape

Each entry in `internships.json` gains one field:

```json
{ "company": "...", "title": "...", "location": "...", "url": "...",
  "date_found": "...", "disciplines": [], "category": "software" }
```

## One-time migration

A throwaway script (not committed) applies `is_non_us` + `categorize` to
the current `internships.json` and refreshes `total_postings` in
`meta.json`, so the site is clean immediately instead of after the next
scheduled scrape.

## Frontend (`index.html`)

- Tab bar rendered above the existing controls: All / Engineering /
  Software / Non-engineering with counts, one active at a time.
- Active tab filters `postings` by `category` before the existing
  search/company/sort pipeline. Missing `category` falls back to
  `non-engineering`.

## Testing

- `tests/test_classify.py` — unit cases for `is_non_us` (clear-foreign,
  clear-US, ambiguous) and `categorize` (software-wins boundary, plain
  engineering, non-engineering).
- Manual validation: run both functions over the full current dataset and
  eyeball the bucket assignments / dropped locations before committing.
