#!/usr/bin/env python3
"""Forced command for the Ubuntu machine's SSH key: read one delivery from stdin and send it to WeCom.

stdin is JSON, either {"mpnews": {title, author, digest, content}, "images": {name: base64}} or {"text": "..."}.
Images are the article's pictures; send.py uploads them and puts their WeCom URLs into the content.
authorized_keys entry: command="python3 ~/x-daily-wecom/receive.py",restrict ssh-ed25519 AAAA... x-daily-send
"""
import base64
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HOME = Path.home() / "x-daily-wecom"
MAX_BYTES = 16_000_000  # the article plus up to 5 pictures of at most 2 MB each, base64-encoded
IMAGE_NAME_RE = re.compile(r"^[\w-]{1,40}\.(?:png|jpg)$")


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
        images = payload.get("images") or {}
        if images:
            folder = inbox / stamp
            folder.mkdir()
            for name, data in images.items():
                if IMAGE_NAME_RE.match(name):  # names come from the sender; keep them to plain file names
                    (folder / name).write_bytes(base64.b64decode(data))
            args += ["--images", str(folder)]
    elif isinstance(payload.get("text"), str) and payload["text"]:
        args = ["--text", payload["text"][:1000]]
    else:
        raise SystemExit("unknown payload")
    result = subprocess.run([sys.executable, str(HOME / "send.py"), *args], capture_output=True, text=True, timeout=120)
    print(result.stdout.strip() or result.stderr.strip()[-500:])
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
