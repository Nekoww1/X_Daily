#!/usr/bin/env python3
"""Forced command for the Ubuntu machine's SSH key: read one delivery from stdin and send it to WeCom.

stdin is JSON, either {"mpnews": {title, author, digest, content}} or {"text": "..."}.
authorized_keys entry: command="python3 ~/x-daily-wecom/receive.py",restrict ssh-ed25519 AAAA... x-daily-send
"""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HOME = Path.home() / "x-daily-wecom"
MAX_BYTES = 2_000_000


def main():
    raw = sys.stdin.buffer.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise SystemExit("too large")
    payload = json.loads(raw)
    inbox = HOME / "inbox"
    inbox.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if "mpnews" in payload:
        article = payload["mpnews"]
        if not all(isinstance(article.get(k), str) and article[k] for k in ("title", "content")):
            raise SystemExit("article needs title and content")
        path = inbox / f"{stamp}.json"
        path.write_text(json.dumps(article, ensure_ascii=False), encoding="utf-8")
        args = ["--mpnews", str(path), "--thumb", str(HOME / "logo-paper.png")]
    elif isinstance(payload.get("text"), str) and payload["text"]:
        args = ["--text", payload["text"][:1000]]
    else:
        raise SystemExit("unknown payload")
    result = subprocess.run([sys.executable, str(HOME / "send.py"), *args], capture_output=True, text=True, timeout=120)
    print(result.stdout.strip() or result.stderr.strip()[-500:])
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
