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
    r"\b(manager|director|senior|principal|staff|lead|head\s+of"
    r"|skill[\s\-]?bridge)\b",
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


def _impersonated_get(url: str, **kwargs: Any) -> Any:
    """GET ``url`` with a real browser's TLS/JA3 fingerprint via curl_cffi.

    Some careers sites (SAIC's ``jobs.saic.com``) sit behind Akamai Bot
    Manager, which blocks plain ``requests`` at the TLS handshake regardless of
    headers — every path returns 403. Chrome impersonation clears the
    challenge. Imported lazily so the dependency is only required by sites that
    actually need it.
    """
    try:
        from curl_cffi import requests as cffi_requests
    except ModuleNotFoundError as e:  # pragma: no cover - dependency guard
        raise RuntimeError(
            "curl_cffi is required for bot-protected sites "
            "(pip install curl_cffi)"
        ) from e
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", USER_AGENT)
    return cffi_requests.get(
        url,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
        impersonate=kwargs.pop("impersonate", "chrome"),
        **kwargs,
    )


def fetch_talemetry(company: str, cfg: dict[str, Any], today: str) -> list[Posting]:
    """Scrape a Talemetry/Radancy careersite (SAIC's ``jobs.saic.com``).

    The keyword search at ``{base}/search/jobs/?q=intern`` server-renders every
    matching role into ``<div class="jobs-section__item">`` rows: an
    ``<h5><a>`` with the title/URL and a sibling column holding the location
    after a hidden ``Location:`` label. The default response already contains
    the full intern result set (no usable pagination param), matching the
    first-page-only behaviour of the other HTML scrapers here.

    The site is fronted by Akamai Bot Manager, so requests must use TLS
    impersonation (see ``_impersonated_get``).

    cfg requires:
      - base_url: e.g. "https://jobs.saic.com"
    cfg optional:
      - query: search keyword (default "intern")
    """
    base = cfg["base_url"].rstrip("/")
    query = cfg.get("query", "intern")
    resp = _impersonated_get(
        f"{base}/search/jobs/?q={query}",
        headers={"Accept": "text/html"},
    )
    if not resp.ok:
        log.warning("%s: talemetry search HTTP %s", company, resp.status_code)
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    postings: list[Posting] = []
    seen: set[str] = set()
    for item in soup.select(".jobs-section__item"):
        link = item.select_one("h5 a[href]")
        if not link:
            continue
        title = link.get_text(strip=True)
        if not is_internship(title):
            continue
        url = link["href"]
        if url.startswith("/"):
            url = base + url
        if url in seen:
            continue
        seen.add(url)
        # Location lives in the column that is neither the title nor the
        # date; it's prefixed by a visually-hidden "Location:" label.
        location = ""
        for col in item.select("div.columns"):
            if col.find("h5"):
                continue
            text = col.get_text(" ", strip=True)
            if text.lower().startswith("location:"):
                location = text[len("location:"):].strip()
                break
        location = re.sub(r"\s*,\s*", ", ", re.sub(r"\s+", " ", location)).strip()
        postings.append(Posting(company, title, location, url, today))
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


def _fetch_clinch_details(url: str) -> tuple[str, str]:
    """Pull the clean title and location from a Clinch/iCIMS job page.

    These pages server-render ``var jobTitle``/``addressLocality``/
    ``addressRegion`` string assignments (the JSON-LD block itself is a
    client-side template with unquoted placeholders, so it's not usable).
    Returns ``("", "")`` when the page is unavailable — some sitemap entries
    are stale and 404 — so the caller can fall back to slug-derived values.
    """
    try:
        resp = http_get(url, headers={"Accept": "text/html"})
    except requests.RequestException:
        return "", ""
    if not resp.ok:
        return "", ""

    def _var(name: str) -> str:
        m = re.search(rf'var\s+{name}\s*=\s*"([^"]*)"', resp.text)
        v = m.group(1).strip() if m else ""
        return "" if v.lower() in ("", "null", "undefined") else v

    title = _var("jobTitle")
    location = ", ".join(
        p for p in (_var("addressLocality"), _var("addressRegion")) if p
    )
    return title, location


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
    cfg optional:
      - fetch_details: when true, fetch each internship's job page and use
        its server-rendered title/location instead of the slug. Needed for
        Parsons, whose slug omits the state and tacks an "-r-<id>" suffix
        onto the title. Left off for Amentum (WAF-blocked) and Peraton
        (slug already yields a usable state).
    """
    seg = cfg["jobs_path_segment"]
    resp = http_get(cfg["sitemap_url"], headers={"Accept": "application/xml"})
    resp.raise_for_status()
    job_urls = re.findall(
        r"<loc>([^<]+" + re.escape(seg) + r"[^<]+)</loc>", resp.text
    )
    # Strip known trailing slug suffixes before location parsing:
    # - Amentum (Clinch): optional UUID
    # - Peraton/Parsons (iCIMS portal): "-[<letter>-]<id>-jobs--<category>--".
    #   Parsons requisitions carry a single-letter prefix ("-r-180587-"); the
    #   optional [a-z]- group consumes it without eating Peraton's trailing
    #   "-<state>" (which has no single-letter-then-dash shape before the id).
    suffix_patterns = [
        re.compile(r"-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.IGNORECASE),
        re.compile(r"-(?:[a-z]-)?\d+-jobs--[a-z0-9-]+--$", re.IGNORECASE),
    ]
    enrich = cfg.get("fetch_details", False)
    postings: list[Posting] = []
    seen: set[str] = set()
    for url in job_urls:
        slug = url.rsplit(seg, 1)[-1]
        for pat in suffix_patterns:
            slug = pat.sub("", slug)
        title_slug, location = _parse_slug_location(slug)
        title = title_slug.replace("-", " ").title()
        if not is_internship(title):
            continue
        if url in seen:
            continue
        seen.add(url)
        if enrich:
            page_title, page_location = _fetch_clinch_details(url)
            title = page_title or title
            location = page_location or location
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


def fetch_dejobs_sitemap(
    company: str, cfg: dict[str, Any], today: str
) -> list[Posting]:
    """DirectEmployers / "dejobs"-style careers site (Bechtel's
    ``bechtel.dejobs.org``, Textron's ``careers.textron.com``).

    The job *detail* pages are client-rendered SPAs (the server only returns a
    generic ``<title>Jobs | …</title>`` shell, no JSON-LD), but the public
    sitemap encodes the title and location straight into the URL path::

        https://<host>/<city-state-slug>/<title-slug>/<HEXID>/job/

    so both are derivable without fetching a single job page — fast and far
    less fragile than scraping the SPA.

    cfg requires:
      - sitemap_url: the sitemap index, e.g.
        "https://bechtel.dejobs.org/sitemaps/index.xml"
        (``/sitemap.xml`` 301-redirects here; requests follows that, but the
        canonical URL avoids the extra hop).
    """
    resp = http_get(cfg["sitemap_url"], headers={"Accept": "application/xml"})
    resp.raise_for_status()
    subs = re.findall(r"<loc>([^<]+\.xml)</loc>", resp.text) or [
        cfg["sitemap_url"]
    ]
    job_urls: list[str] = []
    for sm in subs:
        r = http_get(sm, headers={"Accept": "application/xml"})
        if not r.ok:
            continue
        job_urls.extend(re.findall(r"<loc>([^<]+/job/)</loc>", r.text))

    postings: list[Posting] = []
    seen: set[str] = set()
    for url in job_urls:
        if url in seen:
            continue
        seen.add(url)
        path = re.sub(r"^https?://[^/]+", "", url).strip("/")
        parts = path.split("/")
        # [<city-state>, <title>, <HEXID>, "job"]
        if len(parts) < 4 or parts[-1] != "job":
            continue
        title = parts[-3].replace("-", " ")
        # Some slugs (Textron's Québec roles) carry UTF-8 bytes that were
        # decoded as Latin-1 — "Ã©" should be "é". Repair best-effort.
        if "Ã" in title or "Â" in title:
            try:
                title = title.encode("latin-1").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
        title = title.title()
        if not is_internship(title):
            continue
        loc_slug = parts[-4]
        bits = loc_slug.split("-")
        # Trailing token is a 2-letter state or 3-letter country code
        # ("hunt-valley-md", "mirabel-can"); the rest is the city.
        if len(bits) >= 2 and 2 <= len(bits[-1]) <= 3 and bits[-1].isalpha():
            city = " ".join(bits[:-1]).title()
            region = bits[-1].upper()
            location = f"{city}, {region}" if city else region
        else:
            location = loc_slug.replace("-", " ").title()
        postings.append(Posting(company, title, location, url, today))
    return postings


def fetch_rss_feed(company: str, cfg: dict[str, Any], today: str) -> list[Posting]:
    """Google-for-Jobs RSS/XML feed (BWXT's SuccessFactors "jobs2web" site
    publishes one at ``careers.bwxt.com/sitemap.xml``).

    The feed is a flat list of ``<item>`` blocks; each ``<title>`` is
    ``"<Job Title> (City, ST, Country)"`` and ``<link>`` is the public job
    page. Location is taken from the trailing parenthetical. One request, no
    per-job fetches.

    cfg requires:
      - feed_url: e.g. "https://careers.bwxt.com/sitemap.xml"
    """
    resp = http_get(cfg["feed_url"], headers={"Accept": "application/xml"})
    resp.raise_for_status()

    def _tag(block: str, name: str) -> str:
        m = re.search(
            rf"<{name}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{name}>",
            block,
            re.S,
        )
        import html as _html

        return _html.unescape(m.group(1).strip()) if m else ""

    postings: list[Posting] = []
    seen: set[str] = set()
    for block in re.findall(r"<item>(.*?)</item>", resp.text, re.S):
        raw_title = _tag(block, "title")
        link = _tag(block, "link")
        if not raw_title or not link or link in seen:
            continue
        m = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", raw_title)
        title = m.group(1).strip() if m else raw_title
        location = m.group(2).strip() if m else ""
        if not is_internship(title):
            continue
        seen.add(link)
        postings.append(Posting(company, title, location, link, today))
    return postings


def fetch_ttcportals(company: str, cfg: dict[str, Any], today: str) -> list[Posting]:
    """Radancy "ttcportals" careersite (Parker Hannifin's
    ``parkercareers.ttcportals.com``).

    The search UI is a client-only SPA fronted by bot protection, but it is
    backed by a clean paginated JSON API: ``/jobs/search.json?q=<kw>&page=N``
    returns ``{total_entries, per_page, entries:[{id, permalink, title,
    location}]}``. Requires TLS impersonation (see ``_impersonated_get``).
    Job pages live at ``{base}/jobs/<id>-<permalink>``.

    cfg requires:
      - base_url: e.g. "https://parkercareers.ttcportals.com"
    cfg optional:
      - query: search keyword (default "intern")
    """
    base = cfg["base_url"].rstrip("/")
    query = cfg.get("query", "intern")
    # Radancy's edge only clears specific Chrome TLS fingerprints; the
    # curl_cffi "chrome" alias floats and is currently blocked here, so pin a
    # known-good profile (overridable per-company if it rots).
    impersonate = cfg.get("impersonate", "chrome124")
    postings: list[Posting] = []
    seen: set[str] = set()
    page = 1
    while True:
        resp = _impersonated_get(
            f"{base}/jobs/search.json?q={query}&page={page}",
            headers={"Accept": "application/json"},
            impersonate=impersonate,
        )
        if not resp.ok:
            log.warning("%s: ttcportals HTTP %s", company, resp.status_code)
            break
        try:
            data = json.loads(resp.text)
        except json.JSONDecodeError:
            log.warning("%s: ttcportals non-JSON page %d", company, page)
            break
        entries = data.get("entries", [])
        if not entries:
            break
        for e in entries:
            title = e.get("title", "") or ""
            if not is_internship(title):
                continue
            jid, perma = e.get("id", ""), e.get("permalink", "")
            url = f"{base}/jobs/{jid}-{perma}" if jid else f"{base}/jobs/{perma}"
            if url in seen:
                continue
            seen.add(url)
            loc = e.get("location") or {}
            parts = [
                loc.get("locality", ""),
                loc.get("region_abbr") or loc.get("region_full") or "",
            ]
            country = loc.get("country", "")
            if country and country not in ("United States", "USA", "US"):
                parts.append(country)
            location = ", ".join(
                p for p in dict.fromkeys(p for p in parts if p)
            )
            postings.append(Posting(company, title, location, url, today))
        per_page = data.get("per_page") or len(entries)
        total = data.get("total_entries", 0)
        if page * per_page >= total or page > 50:
            break
        page += 1
    return postings


def fetch_breezy(company: str, cfg: dict[str, Any], today: str) -> list[Posting]:
    """Breezy HR public board (North Wind's
    ``north-wind-group.breezy.hr``).

    Breezy exposes every published position as JSON at
    ``https://<subdomain>.breezy.hr/json`` — a flat list with ``name``,
    ``url``, ``type`` and a ``location`` object. One request, no per-job
    fetches.

    cfg requires:
      - subdomain: e.g. "north-wind-group"
    """
    sub = cfg["subdomain"]
    resp = http_get(f"https://{sub}.breezy.hr/json")
    resp.raise_for_status()
    postings: list[Posting] = []
    seen: set[str] = set()
    for job in resp.json():
        title = job.get("name", "") or ""
        if not is_internship(title):
            continue
        url = job.get("url") or f"https://{sub}.breezy.hr/p/{job.get('friendly_id','')}"
        if url in seen:
            continue
        seen.add(url)
        loc = job.get("location") or {}
        location = loc.get("name", "") if isinstance(loc, dict) else ""
        postings.append(Posting(company, title, location, url, today))
    return postings


def fetch_oracle_orc(company: str, cfg: dict[str, Any], today: str) -> list[Posting]:
    """Oracle Cloud Recruiting (ORC) "Candidate Experience" site (Howmet's
    ``fa-exty-saasfaprod1.fa.ocs.oraclecloud.com``).

    The CE SPA is backed by a public REST endpoint,
    ``/hcmRestApi/resources/latest/recruitingCEJobRequisitions``, that returns
    a ``requisitionList`` of ``{Id, Title, PrimaryLocation}``. ``keyword`` is a
    broad full-text match (Oracle returns everything mentioning the term), so
    the precise filter is still ``is_internship`` on the title. The server
    caps the page size, so paginate via ``offset`` until ``TotalJobsCount``.

    cfg requires:
      - base_url:    e.g. "https://fa-exty-saasfaprod1.fa.ocs.oraclecloud.com"
      - site_number: e.g. "CX_1"
    cfg optional:
      - keyword: full-text seed (default "intern")
    """
    base = cfg["base_url"].rstrip("/")
    site = cfg["site_number"]
    keyword = cfg.get("keyword", "intern")
    postings: list[Posting] = []
    seen: set[str] = set()
    offset = 0
    for _ in range(60):  # hard cap on pagination
        # Oracle ORC ignores top-level ?limit/?offset; they must live INSIDE
        # the finder expression. The server still caps the page at ~25.
        finder = (
            f"findReqs;siteNumber={site},keyword={keyword},"
            f"sortBy=POSTING_DATES_DESC,limit=200,offset={offset}"
        )
        url = (
            f"{base}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
            f"?onlyData=true&expand=requisitionList&finder={finder}"
        )
        resp = http_get(url)
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if not items:
            break
        info = items[0]
        reqs = info.get("requisitionList", [])
        if not reqs:
            break
        for j in reqs:
            title = j.get("Title", "") or ""
            if not is_internship(title):
                continue
            jid = j.get("Id", "")
            job_url = (
                f"{base}/hcmUI/CandidateExperience/en/sites/{site}/job/{jid}"
            )
            if job_url in seen:
                continue
            seen.add(job_url)
            postings.append(
                Posting(company, title, j.get("PrimaryLocation", ""),
                        job_url, today)
            )
        total = info.get("TotalJobsCount", 0)
        offset += len(reqs)
        if offset >= total:
            break
    return postings


# ---------------------------------------------------------------------------
# Classification: US-only filter and category buckets
# ---------------------------------------------------------------------------

_US_STATE_ABBREVS = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc", "pr", "gu", "vi", "as", "mp",
}
_US_STATE_NAMES = {s.replace("-", " ") for s in _US_STATES}
_US_LAST_SEGMENTS = (
    _US_STATE_NAMES
    | _US_STATE_ABBREVS
    | {"us", "usa", "united states", "united states of america",
       "puerto rico", "guam", "remote"}
)

_FOREIGN_TERMS = [
    # Countries / territories
    "united kingdom", "uk", "england", "scotland", "wales", "ireland",
    "france", "germany", "deutschland", "italy", "italia", "spain", "españa",
    "portugal", "netherlands", "belgium", "luxembourg", "switzerland",
    "austria", "poland", "czechia", "czech republic", "slovakia", "hungary",
    "romania", "bulgaria", "greece", "denmark", "norway", "sweden", "finland",
    "estonia", "latvia", "lithuania", "ukraine", "serbia", "croatia",
    "turkey", "türkiye", "turkiye",
    "canada", "mexico", "méxico", "brazil", "brasil", "argentina", "chile",
    "colombia", "peru", "costa rica", "panama",
    "india", "china", "japan", "south korea", "korea", "taiwan", "hong kong",
    "singapore", "malaysia", "indonesia", "thailand", "vietnam",
    "philippines", "pakistan", "bangladesh",
    "australia", "new zealand",
    "israel", "saudi arabia", "united arab emirates", "uae", "qatar",
    "kuwait", "bahrain", "oman", "jordan", "egypt", "morocco",
    "south africa", "nigeria", "kenya",
    # Foreign cities / regions that appear on these boards without a country
    "london", "gloucestershire", "bristol uk",
    "toulouse", "marseille", "bordeaux",
    "hamburg", "munich", "münchen", "berlin", "frankfurt", "stuttgart",
    "bremen", "manching", "stade", "immenstaad", "ottobrunn", "donauwörth",
    "madrid", "getafe", "sevilla", "seville", "barcelona",
    "rome", "milan", "turin", "torino", "brindisi",
    "prague", "praha", "vilnius", "warsaw", "krakow", "bucharest",
    "budapest", "vienna", "amsterdam", "brussels", "geneva", "zurich",
    "istanbul", "ankara",
    "abu dhabi", "dubai", "doha", "riyadh", "jeddah", "dhahran",
    "tel aviv", "cairo", "casablanca",
    "beijing", "shanghai", "tianjin", "guangzhou", "shenzhen", "suzhou",
    "chengdu", "taipei", "seoul", "tokyo", "osaka", "nagoya",
    "bengaluru", "bangalore", "hyderabad", "mumbai", "new delhi", "chennai",
    "pune", "noida", "bangkok", "kuala lumpur", "selangor", "subang",
    "jakarta", "hanoi", "manila",
    "sydney", "melbourne", "brisbane", "adelaide", "canberra", "queensland",
    "new south wales", "tasmania",
    "ontario", "quebec", "québec", "alberta", "manitoba", "saskatchewan",
    "british columbia", "nova scotia", "new brunswick", "newfoundland",
    "toronto", "montreal", "montréal", "mississauga", "calgary", "winnipeg",
    "mirabel",
]
_FOREIGN_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in _FOREIGN_TERMS) + r")\b"
)

_SOFTWARE_PATTERN = re.compile(
    r"\b(software|swe|developer|programmer|computer science"
    r"|information technology|information systems|informatics"
    r"|cyber\w*|data|artificial intelligence|machine learning"
    r"|deep learning|ai|ml|devops|cloud|full[ -]?stack|front[ -]?end"
    r"|back[ -]?end|web|firmware|database|sql|python|java\w*)\b",
    re.IGNORECASE,
)
_ENGINEERING_PATTERN = re.compile(
    r"\b(engineer\w*|mechanical|electrical|electronic\w*|aerospace"
    r"|aeronautic\w*|astronautic\w*|avionics|manufacturing|materials"
    r"|metallurg\w*|weld\w*|machinist|machining|cnc|fabrication"
    r"|structural|structures|civil|chemical|chemistry|chemist|nuclear"
    r"|mechatronic\w*|propulsion|thermal|aerodynamic\w*|gnc|hardware"
    r"|asic|fpga|pcb|rf|radar|antenna|optics|photonics|laser|physics"
    r"|physicist|scientist|quality|technician|drafter|drafting|cad"
    r"|ndt|non[ -]?destructive|naval architect\w*)\b",
    re.IGNORECASE,
)

CATEGORY_SOFTWARE = "software"
CATEGORY_ENGINEERING = "engineering"
CATEGORY_NON_ENGINEERING = "non-engineering"


def is_non_us(location: str) -> bool:
    """True when *location* clearly indicates a place outside the United
    States. Ambiguous or empty strings return False (i.e. kept): the tracked
    companies are US defense contractors, so US is the safe default. US
    checks run before the foreign-term match so that US towns sharing a name
    with foreign cities ("Hamburg, NY", "Vienna, VA") survive.
    """
    low = re.sub(r"\s+", " ", location).strip().lower()
    if not low:
        return False
    if "united states" in low or re.search(r"\b(usa?|u\.s\.a?\.?)\b", low):
        return False
    if low.startswith(("us-", "usa-")):
        return False
    segments = [s.strip() for s in re.split(r"[,;]", low) if s.strip()]
    last = segments[-1].replace(".", "").strip() if segments else ""
    if last in _US_LAST_SEGMENTS:
        return False
    if _FOREIGN_PATTERN.search(low):
        return True
    # Trailing 2/3-letter region code that isn't a US state or territory
    # ("Vilnius, LT", "Mirabel, CAN").
    if len(segments) > 1 and len(last) in (2, 3) and last.isalpha():
        return True
    return False


def categorize(title: str, disciplines: Iterable[str] = ()) -> str:
    """Bucket a posting as software, engineering, or non-engineering from
    its title (and discipline tags when the board provides them). Software
    wins over engineering, so "Software Engineering Intern" is software.
    """
    text = " ".join([title, *disciplines])
    # "IT" is matched case-sensitively; a case-insensitive \bit\b would hit
    # the English word "it".
    if _SOFTWARE_PATTERN.search(text) or re.search(r"\bIT\b", text):
        return CATEGORY_SOFTWARE
    if _ENGINEERING_PATTERN.search(text):
        return CATEGORY_ENGINEERING
    return CATEGORY_NON_ENGINEERING


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "workday": fetch_workday,
    "talentbrew": fetch_talentbrew,
    "talemetry": fetch_talemetry,
    "ttcportals": fetch_ttcportals,
    "breezy": fetch_breezy,
    "oracle_orc": fetch_oracle_orc,
    "hii": fetch_hii,
    "rss_feed": fetch_rss_feed,
    "clinch_sitemap": fetch_clinch_sitemap,
    "phenom_sitemap": fetch_phenom_sitemap,
    "dejobs_sitemap": fetch_dejobs_sitemap,
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


def collect(
    companies: Iterable[dict[str, Any]], today: str
) -> tuple[list[Posting], set[str]]:
    results: list[Posting] = []
    errored: set[str] = set()
    for entry in companies:
        name = entry["name"]
        platform = entry["platform"].lower()
        fetcher = FETCHERS.get(platform)
        if fetcher is None:
            log.warning("%s: unknown platform %r — skipping", name, platform)
            errored.add(name)
            continue
        try:
            found = fetcher(name, entry.get("config", {}), today)
        except requests.HTTPError as e:
            log.error("%s: HTTP error %s", name, e)
            errored.add(name)
            continue
        except requests.RequestException as e:
            log.error("%s: request failed: %s", name, e)
            errored.add(name)
            continue
        except Exception as e:
            log.error("%s: unexpected error (%s): %s", name, type(e).__name__, e)
            errored.add(name)
            continue
        log.info("%s (%s): %d internship postings", name, platform, len(found))
        results.extend(found)
    return results, errored


def diff_and_merge(
    existing: list[dict[str, Any]],
    current: list[Posting],
    errored_companies: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    existing_by_key = {f"{e['company']}::{e['url']}": e for e in existing}
    current_by_key = {p.key: p for p in current}

    # Only preserve existing entries when the scrape actually errored. A clean
    # run that returns zero means every previously-tracked posting was taken
    # down — drop them so the tracker doesn't accumulate dead links.
    existing_counts = Counter(e["company"] for e in existing)
    preserve_companies = {
        c for c in errored_companies if existing_counts.get(c, 0) > 0
    }
    for c in preserve_companies:
        log.warning(
            "%s: scrape errored — preserving %d existing entries",
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
    # When a posting has no location, fall back to its title: sitemap-derived
    # titles (Parsons/Clinch) embed the location in the slug, so a clearly
    # foreign title with no location is a foreign posting.
    existing = [
        e for e in existing
        if not is_non_us(e.get("location") or e.get("title", ""))
    ]

    current, errored = collect(companies, today)
    current = [p for p in current if not is_non_us(p.location or p.title)]
    merged, added, removed = diff_and_merge(existing, current, errored)
    # Recomputed every run so keyword-rule changes propagate to old entries.
    for entry in merged:
        entry["category"] = categorize(
            entry.get("title", ""), entry.get("disciplines") or ()
        )
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
