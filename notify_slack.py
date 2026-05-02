#!/usr/bin/env python3
"""Post a Slack message summarizing today's new internship postings.

Reads diff.json (written by tracker.py) and posts a formatted message to
SLACK_WEBHOOK_URL. No-ops if there are no new postings or no webhook set.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DIFF_FILE = ROOT / "diff.json"
MAX_LIST = 25


def main() -> int:
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        print("SLACK_WEBHOOK_URL not set; skipping")
        return 0

    if not DIFF_FILE.exists():
        print("diff.json missing; skipping")
        return 0

    diff = json.loads(DIFF_FILE.read_text())
    added = diff.get("added", [])
    if not added:
        print("no new postings; skipping")
        return 0

    site_url = os.environ.get("SITE_URL", "")
    date = diff.get("date", "")

    header = f"*{len(added)} new internship posting{'s' if len(added) != 1 else ''}* ({date})"
    if site_url:
        header += f" — <{site_url}|view all>"

    lines = [header, ""]
    for p in added[:MAX_LIST]:
        title = p.get("title", "")
        company = p.get("company", "")
        location = p.get("location", "")
        url = p.get("url", "")
        loc_part = f" — {location}" if location else ""
        lines.append(f"• <{url}|{title}> _{company}_{loc_part}")

    if len(added) > MAX_LIST:
        lines.append(f"_…and {len(added) - MAX_LIST} more_")

    text = "\n".join(lines)
    resp = requests.post(webhook, json={"text": text}, timeout=20)
    if resp.status_code >= 300:
        print(f"slack post failed: {resp.status_code} {resp.text[:200]}", file=sys.stderr)
        return 1
    print(f"posted {len(added)} new postings to Slack")
    return 0


if __name__ == "__main__":
    sys.exit(main())
