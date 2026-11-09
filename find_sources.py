#!/usr/bin/env python3
"""Suggest new sources for the reader profile's directions, then check each X account through the local RSSHub.

Codex (with web search) proposes X accounts, blogs and arXiv categories; nothing is added to sources.json
here, the list in data/source-candidates.json is for a person to pick from.

Usage: python find_sources.py [--profile personal] [--skip-search]
"""
import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser

import digest
import radar

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "source-candidates.json"
STR = {"type": "string"}
TIERS = ["core", "public"]


def schema(directions):
    x = digest.obj(["handle", "name", "affiliation", "direction", "tier", "why", "evidence_url"],
                   handle=STR, name=STR, affiliation=STR, direction={"type": "string", "enum": directions},
                   tier={"type": "string", "enum": TIERS}, why=STR, evidence_url=STR)
    blog = digest.obj(["name", "url", "direction", "tier", "why"], name=STR, url=STR,
                      direction={"type": "string", "enum": directions}, tier={"type": "string", "enum": TIERS}, why=STR)
    arxiv = digest.obj(["query", "direction", "why"], query=STR, direction={"type": "string", "enum": directions}, why=STR)
    return digest.obj(["x", "blogs", "arxiv"], x=digest.arr(x), blogs=digest.arr(blog), arxiv=digest.arr(arxiv))


def search(profile):
    sources = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))["sources"]
    have = [s.get("handle") or s["name"] for s in sources]
    text = digest.prompt("find_sources", FOCUS=digest.focus_text(profile), HAVE="\n".join(f"- {h}" for h in have))
    directions = [f["name"] for f in profile["focus"]]
    log = ROOT / "logs" / "find-sources.log"
    log.parent.mkdir(exist_ok=True)
    return digest.run_codex(text, schema(directions), log, search=True)


def x_activity(handle, base, days=30):
    """Original posts in the last `days` days, read the same way radar.py reads X."""
    url = f"{base}/twitter/user/{handle}/count=40&includeRts=false&includeReplies=false"
    try:
        parsed = feedparser.parse(radar.fetch(url, timeout=90, retries=0))
    except Exception as error:
        return {"ok": False, "reason": f"{type(error).__name__}"[:80]}
    if not parsed.entries:
        return {"ok": False, "reason": "读不到贴文（账号不存在、受保护或被限流）"}
    since = datetime.now(timezone.utc) - timedelta(days=days)
    times = [radar.to_utc(e.get("published_parsed")) for e in parsed.entries]
    recent = [t for t in times if t and t >= since]
    latest = max((t for t in times if t), default=None)
    return {"ok": True, "posts_30d": len(recent), "latest": latest.date().isoformat() if latest else ""}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default=digest.PROFILE)
    parser.add_argument("--skip-search", action="store_true", help="reuse the last search, only recheck X")
    args = parser.parse_args()
    radar.load_dotenv(ROOT / ".env")
    found = json.loads(OUT.read_text(encoding="utf-8")) if args.skip_search else search(digest.load_profile(args.profile))
    OUT.write_text(json.dumps(found, ensure_ascii=False, indent=1), encoding="utf-8")
    base = os.environ.get("RSSHUB_URL", "http://127.0.0.1:1200").rstrip("/")
    for i, account in enumerate(found["x"]):
        if i:
            time.sleep(20)  # one request every 20 s keeps well under X's rate limits for the logged-in account
        account["activity"] = x_activity(account["handle"].lstrip("@"), base)
        OUT.write_text(json.dumps(found, ensure_ascii=False, indent=1), encoding="utf-8")
        print(account["handle"], account["activity"], flush=True)
    print(f"x: {len(found['x'])}, blogs: {len(found['blogs'])}, arxiv: {len(found['arxiv'])} -> {OUT}")


if __name__ == "__main__":
    main()
