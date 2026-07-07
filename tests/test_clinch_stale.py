"""fetch_clinch_sitemap must drop postings whose detail page 404s.

Clinch/iCIMS sitemaps (Parsons) keep listing removed jobs; the job page
itself 404s once the requisition closes. With ``fetch_details`` on, a 404
means the posting is gone — but transient failures (timeout, 5xx) must
still fall back to slug-derived values rather than dropping live postings.

Run with ``pytest tests/`` or ``python tests/test_clinch_stale.py``.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import tracker

BASE = "https://jobs.example.com"
LIVE = f"{BASE}/jobs/roadway-engineering-co-op-fall-2026-akron-r-1-jobs--internship-program--"
DEAD = f"{BASE}/jobs/python-developer-intern-r-2-jobs--internship-program--"
FLAKY = f"{BASE}/jobs/bridge-engineering-co-op-fall-2026-boston-r-3-jobs--internship-program--"

SITEMAP = f"""<?xml version="1.0"?>
<urlset>
  <url><loc>{LIVE}</loc></url>
  <url><loc>{DEAD}</loc></url>
  <url><loc>{FLAKY}</loc></url>
</urlset>
"""

LIVE_PAGE = """
var jobTitle = "Roadway Engineering Co-Op - Fall 2026";
var addressLocality = "Akron";
var addressRegion = "Ohio";
"""


class FakeResp:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def raise_for_status(self) -> None:
        if not self.ok:
            raise AssertionError(f"HTTP {self.status_code}")


def fake_http_get(url, **kwargs):
    if url.endswith("sitemap.xml"):
        return FakeResp(200, SITEMAP)
    if url == LIVE:
        return FakeResp(200, LIVE_PAGE)
    if url == DEAD:
        return FakeResp(404)
    if url == FLAKY:
        return FakeResp(503)
    raise AssertionError(f"unexpected URL {url}")


def test_stale_404_posting_dropped_but_transient_failure_kept():
    real = tracker.http_get
    tracker.http_get = fake_http_get
    try:
        postings = tracker.fetch_clinch_sitemap(
            "Parsons",
            {
                "sitemap_url": f"{BASE}/sitemap.xml",
                "jobs_path_segment": "/jobs/",
                "fetch_details": True,
            },
            "2026-07-08",
        )
    finally:
        tracker.http_get = real

    urls = {p.url for p in postings}
    assert DEAD not in urls, "404 posting should be dropped"
    assert LIVE in urls, "live posting should be kept"
    assert FLAKY in urls, "transient 5xx should keep slug fallback"

    by_url = {p.url: p for p in postings}
    assert by_url[LIVE].title == "Roadway Engineering Co-Op - Fall 2026"
    assert by_url[LIVE].location == "Akron, Ohio"
    # Flaky page falls back to slug-derived title, empty-ish location
    assert by_url[FLAKY].title.startswith("Bridge Engineering Co Op")


if __name__ == "__main__":
    test_stale_404_posting_dropped_but_transient_failure_kept()
    print("PASS test_stale_404_posting_dropped_but_transient_failure_kept")
