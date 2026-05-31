#!/usr/bin/env python3
"""Accumulate GitHub repo clone/view counts into docs/metrics/traffic.json.

Called by .github/workflows/repo-traffic-badges.yml (daily). The Traffic API
only exposes the last 14 days per request; we merge by day timestamp so the
badge total grows over time instead of resetting every fortnight.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
METRICS_PATH = REPO_ROOT / "docs" / "metrics" / "traffic.json"
API = "https://api.github.com/repos/{owner}/{repo}/traffic"


def _api_get(path: str, token: str) -> dict:
    owner, repo = os.environ["GITHUB_REPOSITORY"].split("/", 1)
    req = urllib.request.Request(
        f"{API.format(owner=owner, repo=repo)}/{path}?per=day",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("GITHUB_TOKEN not set", file=sys.stderr)
        return 1
    if "GITHUB_REPOSITORY" not in os.environ:
        print("GITHUB_REPOSITORY not set (run inside GitHub Actions)", file=sys.stderr)
        return 1

    if METRICS_PATH.is_file():
        data = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
    else:
        data = {"clones_total": 0, "views_total": 0, "clone_days": {}, "view_days": {}}

    clone_days: dict[str, int] = data.get("clone_days") or {}
    view_days: dict[str, int] = data.get("view_days") or {}

    try:
        clones = _api_get("clones", token)
        for row in clones.get("clones") or []:
            ts = (row.get("timestamp") or "")[:10]
            if ts:
                clone_days[ts] = int(row.get("count") or 0)
    except urllib.error.HTTPError as exc:
        print(f"clones API failed: {exc}", file=sys.stderr)

    try:
        views = _api_get("views", token)
        for row in views.get("views") or []:
            ts = (row.get("timestamp") or "")[:10]
            if ts:
                view_days[ts] = int(row.get("count") or 0)
    except urllib.error.HTTPError as exc:
        print(f"views API failed: {exc}", file=sys.stderr)

    out = {
        "clones_total": sum(clone_days.values()),
        "views_total": sum(view_days.values()),
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "clone_days": clone_days,
        "view_days": view_days,
    }
    METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    METRICS_PATH.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {METRICS_PATH}: clones={out['clones_total']} views={out['views_total']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
