#!/usr/bin/env python3
"""Builds a labelled evaluation set from public gitlab-org/gitlab issues.

The ground truth is the type:: label that GitLab's own team put on each issue.
No token is needed; the issues are public.

    python3 eval/build_dataset.py --per-type 50
"""

import argparse
import json
import random
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://gitlab.com/api/v4/projects/278964/issues"  # gitlab-org/gitlab
TYPES = ["bug", "feature", "maintenance"]
HERE = Path(__file__).parent
CACHE = HERE / ".cache"


def fetch(type_, after, before, max_pages):
    for page in range(1, max_pages + 1):
        query = urllib.parse.urlencode({
            "labels": f"type::{type_}", "created_after": after, "created_before": before,
            "scope": "all", "state": "all", "order_by": "created_at", "per_page": 100, "page": page,
        })
        with urllib.request.urlopen(f"{API}?{query}", timeout=60) as res:
            issues = json.load(res)
        if not issues:
            return
        yield from issues
        time.sleep(0.5)


def clean(description):
    """Drops template comments and anything that would leak the answer."""
    text = re.sub(r"<!--.*?-->", "", description or "", flags=re.S)
    lines = [line for line in text.splitlines()
             if not line.lstrip().startswith("/") and "type::" not in line]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--per-type", type=int, default=50)
    p.add_argument("--after", default="2026-09-20T00:00:00Z", help="after Jev's release, to avoid issues it may have seen")
    p.add_argument("--before", default="2026-10-13T00:00:00Z")
    p.add_argument("--max-pages", type=int, default=5)
    p.add_argument("--seed", type=int, default=7)
    args = p.parse_args()

    rng = random.Random(args.seed)
    rows = []
    for type_ in TYPES:
        pool = []
        for issue in fetch(type_, args.after, args.before, args.max_pages):
            types = [label for label in issue["labels"] if label.startswith("type::")]
            title = issue["title"]
            description = clean(issue["description"])
            if len(types) != 1 or title.startswith(("[FF]", "[Experiment")) or len(description) < 80:
                continue
            pool.append({"iid": issue["iid"], "type": type_, "title": title,
                         "description": description[:6000], "url": issue["web_url"]})
        picked = rng.sample(pool, min(args.per_type, len(pool)))
        print(f"type::{type_}: {len(pool)} usable, picked {len(picked)}")
        rows += picked

    rng.shuffle(rows)
    CACHE.mkdir(exist_ok=True)
    with open(CACHE / "issues.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    # The issue numbers are committed so the set can be rebuilt; the texts stay in the cache.
    with open(HERE / "dataset.csv", "w", encoding="utf-8") as f:
        f.write("iid,type,url\n")
        for r in sorted(rows, key=lambda r: r["iid"]):
            f.write(f"{r['iid']},{r['type']},{r['url']}\n")
    print(f"wrote {len(rows)} issues to {CACHE / 'issues.jsonl'} and {HERE / 'dataset.csv'}")


if __name__ == "__main__":
    main()
