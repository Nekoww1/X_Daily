#!/usr/bin/env python3
"""Start the local RSSHub with the X cookie from .env. It listens on this machine only."""
import os
import subprocess
import sys
from pathlib import Path

from radar import ROOT, load_dotenv

# On Windows it lives on Z: because C: and D: are short of space; set RSSHUB_DIR elsewhere.
DEFAULT_RSSHUB_DIR = "Z:/software/x-daily-rsshub"


def main():
    load_dotenv(ROOT / ".env")
    rsshub_dir = Path(os.environ.get("RSSHUB_DIR", DEFAULT_RSSHUB_DIR))
    if not os.environ.get("TWITTER_AUTH_TOKEN"):
        sys.exit("TWITTER_AUTH_TOKEN is not set in .env (see .env.example)")
    if not (rsshub_dir / "dist" / "index.mjs").exists():
        sys.exit("RSSHub is not built; see README.md")

    env = dict(os.environ, NODE_ENV="production", LISTEN_INADDR_ANY="0", NO_LOGFILES="true",
               NODE_OPTIONS="--max-http-header-size=32768")
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if proxy:
        env.setdefault("PROXY_URI", proxy)
    return subprocess.call(["node", "dist/index.mjs"], cwd=rsshub_dir, env=env)


if __name__ == "__main__":
    sys.exit(main())
