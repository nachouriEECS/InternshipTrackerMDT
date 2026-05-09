#!/usr/bin/env python3
"""Defense industry internship tracker.

Reads target companies from companies.json, queries each company's job board
for internship postings, and updates internships.json with the diff.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections import Counter
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
COMPANIES_FILE = ROOT / "companies.json"
DATA_FILE = ROOT / "internships.json"
META_FILE = ROOT / "meta.json"

REQUEST_TIMEOUT = 30
USER_AGENT = "defense-internship-tracker/1.0 (+https://github.com)"

INTERN_PATTERN = re.compile(r"\b(intern(ship)?|co[\s\-]?op)\b", re.IGNORECASE)
NEGATIVE_PATTERN = re.compile(
    r"\b(manager|director|senior|principal|staff|lead|head\s+of)\b",
    re.IGNORECASE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("tracker")


@dataclass(frozen=True)
class Posting:
    company: str
    title: str
    location: str
    url: str
    date_found: str
    disciplines: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.company}::{self.url}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["disciplines"] = list(self.disciplines)
        return d


def is_internship(title: str, employment_type: str | None = None) -> bool:
    if employment_type and "intern" in employment_type.lower():
        return True
    if not INTERN_PATTERN.search(title):
        return False
    if NEGATIVE_PATTERN.search(title):
        return False
    return True


def http_get(url: str, **kwargs: Any) -> requests.Response:
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", USER_AGENT)
    headers.setdefault("Accept", "application/json")
    return requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, **kwargs)


def http_post(url: str, **kwargs: Any) -> requests.Response:
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", USER_AGENT)
    headers.setdefault("Accept", "application/json")
    headers.setdefault("Content-Type", "application/json")
    return requests.post(url, headers=headers, timeout=REQUEST_TIMEOUT, **kwargs)


def fetch_greenhouse(company: str, cfg: dict[str, Any], today: str) -> list[Posting]:
    token = cfg["board_token"]
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    resp = http_get(url)
    resp.raise_for_status()
    payload = resp.json()
    postings: list[Posting] = []
    for job in payload.get("jobs", []):
        title = job.get("title", "")
        if not is_internship(title):
            continue
        location = (job.get("location") or {}).get("name", "")
        job_url = job.get("absolute_url", "").replace(
            "https://boards.greenhouse.io/", "https://job-boards.greenhouse.io/", 1
        )
        disciplines = tuple(
            d.get("name", "") for d in job.get("departments", []) if d.get("name")
        )
        postings.append(
            Posting(company, title, location, job_url, today, disciplines)
        )
    return postings


def fetch_lever(company: str, cfg: dict[str, Any], today: str) -> list[Posting]:
    site = cfg["site"]
    url = f"https://api.lever.co/v0/postings/{site}?mode=json"
    resp = http_get(url)
    resp.raise_for_status()
    payload = resp.json()
    postings: list[Posting] = []
    for job in payload:
        title = job.get("text", "")
        categories = job.get("categories") or {}
        commitment = categories.get("commitment", "")
        if not is_internship(title, commitment):
            continue
        location = categories.get("location", "")
        team = categories.get("team", "")
        job_url = job.get("hostedUrl", "")
        disciplines = (team,) if team else ()
        postings.append(
            Posting(company, title, location, job_url, today, disciplines)
        )
    return postings


def fetch_workday(company: str, cfg: dict[str, Any], today: str) -> list[Posting]:
    """Query a Workday CXS jobs endpoint.

    cfg requires:
      - tenant: subdomain (e.g. "lockheedmartin")
      - host:   "myworkdayjobs.com" or company-specific careers domain
      - site:   site/section identifier (e.g. "Lockheed_Martin")
    Optional:
      - wd:     Workday cluster (default "wd1")
      - external_url: external careers URL prefix used to build job links
    """
    tenant = cfg["tenant"]
    site = cfg["site"]
    wd = cfg.get("wd", "wd1")
    api_host = cfg.get("api_host", f"{tenant}.{wd}.myworkdayjobs.com")
    external_prefix = cfg.get(
        "external_url",
        f"https://{api_host}/en-US/{site}",
    )
    endpoint = f"https://{api_host}/wday/cxs/{tenant}/{site}/jobs"

    postings: list[Posting] = []
    seen_urls: set[str] = set()
    limit = 20
    offset = 0
    # Workday's full-text search is the most reliable filter we have here.
    for search_text in ("intern", "internship", "co-op"):
        offset = 0
        while True:
            body = {
                "appliedFacets": {},
                "limit": limit,
                "offset": offset,
                "searchText": search_text,
            }
            resp = http_post(endpoint, json=body)
            if resp.status_code == 404:
                log.warning("%s: workday endpoint 404 (%s)", company, endpoint)
                return postings
            resp.raise_for_status()
            payload = resp.json()
            jobs = payload.get("jobPostings", [])
            if not jobs:
                break
            for job in jobs:
                title = job.get("title", "")
                if not is_internship(title):
                    continue
                external_path = job.get("externalPath", "")
                job_url = f"{external_prefix}{external_path}"
                if job_url in seen_urls:
                    continue
                seen_urls.add(job_url)
                location = job.get("locationsText") or job.get("location") or ""
                postings.append(
                    Posting(company, title, location, job_url, today)
                )
            total = payload.get("total", 0)
            offset += limit
            if offset >= total:
                break
    return postings


def fetch_talentbrew(company: str, cfg: dict[str, Any], today: str) -> list[Posting]:
    """Scrape a TalentBrew careers site (e.g., lockheedmartinjobs.com).

    Pagination on these sites is JS-driven and not easily reproduced from a
    plain HTTP request, so this fetches only the first results page (~15 jobs).
    Filtering still goes through ``is_internship`` because the path-keyword
    search matches anywhere in the description and lets unrelated roles leak in.

    cfg requires:
      - base_url: e.g., "https://www.lockheedmartinjobs.com"
    """
    base = cfg["base_url"].rstrip("/")
    resp = http_get(f"{base}/search-jobs/intern", headers={"Accept": "text/html"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    container = soup.find(id="search-results-list")
    if container is None:
        log.warning("%s: search-results-list container missing", company)
        return []
    postings: list[Posting] = []
    # TalentBrew themes vary: Lockheed wraps title in <span class="job-title">,
    # L3Harris uses <h2>. Location class also differs ("job-location" vs
    # "results-facet job-location"). Try the known variants and fall back to
    # the link text.
    for a in container.find_all("a", href=re.compile(r"^(/[a-z]{2})?/job/")):
        title_el = a.find(class_="job-title") or a.find("h2")
        title = title_el.get_text(strip=True) if title_el else a.get_text(" ", strip=True)
        if not is_internship(title):
            continue
        loc_el = a.find(class_="job-location") or a.find(
            "span", class_=re.compile(r"\bjob-location\b")
        )
        location = loc_el.get_text(strip=True) if loc_el else ""
        postings.append(Posting(company, title, location, base + a["href"], today))
    return postings


def fetch_hii(company: str, cfg: dict[str, Any], today: str) -> list[Posting]:
    """Scrape an HII-style table-based careers site (jobs.hii-tsd.com,
    careers.huntingtoningalls.com).

    The site renders a results table with rows like
    ``<tr class="data-row">`` containing a ``colTitle`` cell with
    ``<a class="jobTitle-link">`` and a ``colLocation`` cell with
    ``<span class="jobLocation">``. The path-based ``/search-jobs/intern``
    URL doesn't actually filter, so we rely on ``is_internship`` against the
    title to drop unrelated rows.

    cfg requires:
      - base_url: e.g., "https://jobs.hii-tsd.com"
    """
    base = cfg["base_url"].rstrip("/")
    resp = http_get(f"{base}/search-jobs/intern", headers={"Accept": "text/html"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    postings: list[Posting] = []
    seen: set[str] = set()
    for row in soup.select("tr.data-row"):
        link = row.select_one("a.jobTitle-link")
        if not link:
            continue
        title = link.get_text(strip=True)
        if not is_internship(title):
            continue
        # SkillBridge is a DoD program for transitioning service members, not a
        # college internship. HII's intern listings are dominated by these, so
        # drop them to keep the data focused on student internships.
        if re.search(r"\bskill[\s\-]?bridge\b", title, re.IGNORECASE):
            continue
        href = link.get("href", "")
        if not href.startswith("/job/"):
            continue
        url = base + href
        if url in seen:
            continue
        seen.add(url)
        loc_el = row.select_one("span.jobLocation")
        location = loc_el.get_text(strip=True) if loc_el else ""
        postings.append(Posting(company, title, location, url, today))
    return postings


_US_STATES = sorted(
    [
        "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
        "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
        "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
        "maine", "maryland", "massachusetts", "michigan", "minnesota",
        "mississippi", "missouri", "montana", "nebraska", "nevada",
        "new-hampshire", "new-jersey", "new-mexico", "new-york",
        "north-carolina", "north-dakota", "ohio", "oklahoma", "oregon",
        "pennsylvania", "rhode-island", "south-carolina", "south-dakota",
        "tennessee", "texas", "utah", "vermont", "virginia", "washington",
        "west-virginia", "wisconsin", "wyoming", "district-of-columbia",
    ],
    key=len,
    reverse=True,
)


def _parse_slug_location(slug: str) -> tuple[str, str]:
    """Strip trailing ``-united-states`` and a US-state suffix from a slug,
    returning ``(remaining_slug, location)``. Falls back to empty location.
    """
    if slug.endswith("-united-states"):
        slug = slug[: -len("-united-states")]
    location = ""
    for state in _US_STATES:
        suffix = f"-{state}"
        if slug.endswith(suffix):
            slug = slug[: -len(suffix)]
            location = state.replace("-", " ").title()
            break
    return slug, location


def fetch_clinch_sitemap(company: str, cfg: dict[str, Any], today: str) -> list[Posting]:
    """Discover postings via a public sitemap.xml when the careers site itself
    is behind a bot challenge (e.g., Amentum's Clinch site is fronted by AWS
    WAF and individual job pages return 202 to non-browser clients).

    Title and location are derived from the URL slug because the job pages
    can't be fetched cleanly. Slug shape is assumed to be
    ``<title>-<city>-<state>-<country>[-<uuid>]``.

    cfg requires:
      - sitemap_url: e.g. "https://www.amentumcareers.com/sitemap.xml"
      - jobs_path_segment: e.g. "/jobs/" — segment that introduces a job slug
    """
    seg = cfg["jobs_path_segment"]
    resp = http_get(cfg["sitemap_url"], headers={"Accept": "application/xml"})
    resp.raise_for_status()
    job_urls = re.findall(
        r"<loc>([^<]+" + re.escape(seg) + r"[^<]+)</loc>", resp.text
    )
    uuid_suffix = re.compile(
        r"-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.IGNORECASE
    )
    postings: list[Posting] = []
    seen: set[str] = set()
    for url in job_urls:
        slug = url.rsplit(seg, 1)[-1]
        slug = uuid_suffix.sub("", slug)
        title_slug, location = _parse_slug_location(slug)
        title = title_slug.replace("-", " ").title()
        if not is_internship(title):
            continue
        if url in seen:
            continue
        seen.add(url)
        postings.append(Posting(company, title, location, url, today))
    return postings


def fetch_phenom_sitemap(company: str, cfg: dict[str, Any], today: str) -> list[Posting]:
    """Phenom careers sites (GE Aerospace, KBR, ...) lock down their JSON
    search API behind a tenant token, but the sitemap is public. Each
    individual job page embeds its location in JSON we can regex out;
    title comes from the URL slug.

    cfg requires:
      - sitemap_url: e.g., "https://careers.geaerospace.com/sitemap.xml"
        (a sitemap index that points at one or more nested sitemaps)
    """
    resp = http_get(cfg["sitemap_url"], headers={"Accept": "application/xml"})
    resp.raise_for_status()
    nested = re.findall(r"<loc>([^<]+\.xml)</loc>", resp.text)
    if not nested:
        nested = [cfg["sitemap_url"]]
    job_urls: list[str] = []
    for sm in nested:
        r = http_get(sm, headers={"Accept": "application/xml"})
        if not r.ok:
            continue
        job_urls.extend(re.findall(r"<loc>([^<]+/job/[^<]+)</loc>", r.text))

    postings: list[Posting] = []
    seen: set[str] = set()
    for url in job_urls:
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        title = slug.replace("-", " ")
        if not is_internship(title):
            continue
        if url in seen:
            continue
        seen.add(url)
        location = ""
        try:
            page = http_get(url, headers={"Accept": "text/html"})
            if page.ok:
                m = re.search(r'"location"\s*:\s*"([^"]+)"', page.text)
                if m:
                    location = m.group(1).encode("utf-8").decode("unicode_escape")
        except requests.RequestException:
            pass
        postings.append(Posting(company, title, location, url, today))
    return postings


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "workday": fetch_workday,
    "talentbrew": fetch_talentbrew,
    "hii": fetch_hii,
    "clinch_sitemap": fetch_clinch_sitemap,
    "phenom_sitemap": fetch_phenom_sitemap,
}


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def collect(companies: Iterable[dict[str, Any]], today: str) -> list[Posting]:
    results: list[Posting] = []
    for entry in companies:
        name = entry["name"]
        platform = entry["platform"].lower()
        fetcher = FETCHERS.get(platform)
        if fetcher is None:
            log.warning("%s: unknown platform %r — skipping", name, platform)
            continue
        try:
            found = fetcher(name, entry.get("config", {}), today)
        except requests.HTTPError as e:
            log.error("%s: HTTP error %s", name, e)
            continue
        except requests.RequestException as e:
            log.error("%s: request failed: %s", name, e)
            continue
        except Exception as e:
            log.error("%s: unexpected error (%s): %s", name, type(e).__name__, e)
            continue
        log.info("%s (%s): %d internship postings", name, platform, len(found))
        results.extend(found)
    return results


def diff_and_merge(
    existing: list[dict[str, Any]], current: list[Posting]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    existing_by_key = {f"{e['company']}::{e['url']}": e for e in existing}
    current_by_key = {p.key: p for p in current}

    # If a company previously had postings but returned zero this run, treat it
    # as a likely scrape failure (rate limit, IP block, transient error) and
    # carry over its existing entries instead of dropping them. Otherwise a
    # single bad run wipes that company from the data.
    existing_counts = Counter(e["company"] for e in existing)
    current_counts = Counter(p.company for p in current)
    preserve_companies = {
        c for c, n in existing_counts.items() if n > 0 and current_counts[c] == 0
    }
    for c in preserve_companies:
        log.warning(
            "%s: 0 postings this run (had %d) — preserving existing entries",
            c,
            existing_counts[c],
        )

    merged: list[dict[str, Any]] = []
    added: list[dict[str, Any]] = []
    for key, posting in current_by_key.items():
        if key in existing_by_key:
            preserved = existing_by_key[key].copy()
            preserved["title"] = posting.title
            preserved["location"] = posting.location
            if posting.disciplines:
                preserved["disciplines"] = list(posting.disciplines)
            merged.append(preserved)
        else:
            d = posting.to_dict()
            merged.append(d)
            added.append(d)

    removed_count = 0
    for key, entry in existing_by_key.items():
        if key in current_by_key:
            continue
        if entry["company"] in preserve_companies:
            merged.append(entry)
        else:
            removed_count += 1

    merged.sort(key=lambda p: (p["company"], p["title"]))
    added.sort(key=lambda p: (p["company"], p["title"]))
    return merged, added, removed_count


def main() -> int:
    companies = load_json(COMPANIES_FILE, default=None)
    if not companies:
        log.error("companies.json missing or empty at %s", COMPANIES_FILE)
        return 1

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    existing = load_json(DATA_FILE, default=[])

    current = collect(companies, today)
    merged, added, removed = diff_and_merge(existing, current)
    save_json(DATA_FILE, merged)
    save_json(META_FILE, {"last_scanned": today, "total_postings": len(merged)})

    log.info(
        "summary: %d total | +%d added | -%d removed | %d companies scanned",
        len(merged),
        len(added),
        removed,
        len(companies),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
