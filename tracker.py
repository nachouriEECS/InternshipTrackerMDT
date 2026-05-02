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
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

ROOT = Path(__file__).resolve().parent
COMPANIES_FILE = ROOT / "companies.json"
DATA_FILE = ROOT / "internships.json"
DIFF_FILE = ROOT / "diff.json"

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
        job_url = job.get("absolute_url", "")
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


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "workday": fetch_workday,
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

    added_keys = current_by_key.keys() - existing_by_key.keys()
    removed_keys = existing_by_key.keys() - current_by_key.keys()

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

    merged.sort(key=lambda p: (p["company"], p["title"]))
    added.sort(key=lambda p: (p["company"], p["title"]))
    return merged, added, len(removed_keys)


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
    save_json(DIFF_FILE, {"date": today, "added": added})

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
