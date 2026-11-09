#!/usr/bin/env python3
"""Send a text or an image to everyone in the WeCom app (reaches personal WeChat via 微信插件).

Usage: python3 send.py --text "hello"   |   --image report.png   |   --mpnews article.json --thumb cover.png
Reads ~/x-daily-wecom/.env; never prints the secrets.
"""
import argparse
import json
import urllib.request
import uuid
from pathlib import Path

ENV = Path.home() / "x-daily-wecom" / ".env"
API = "https://qyapi.weixin.qq.com/cgi-bin"


def settings():
    values = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def call(url, data=None, headers=None):
    request = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(request, timeout=60) as response:
        result = json.load(response)
    if result.get("errcode", 0) != 0:
        raise SystemExit(f"WeCom error {result.get('errcode')}: {result.get('errmsg')}")
    return result


def upload_image(token, path):
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"media\"; filename=\"{path.name}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    result = call(f"{API}/media/upload?access_token={token}&type=image", body,
                  {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    return result["media_id"]


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--text")
    group.add_argument("--image", type=Path)
    group.add_argument("--mpnews", type=Path, help="JSON article (title, author, digest, content HTML) or a list of up to 8")
    parser.add_argument("--thumb", type=Path, help="cover image for --mpnews")
    args = parser.parse_args()

    conf = settings()
    token = call(f"{API}/gettoken?corpid={conf['WECOM_CORP_ID']}&corpsecret={conf['WECOM_SECRET']}")["access_token"]
    message = {"touser": "@all", "agentid": int(conf["WECOM_AGENT_ID"])}
    if args.text:
        message.update(msgtype="text", text={"content": args.text})
    elif args.image:
        message.update(msgtype="image", image={"media_id": upload_image(token, args.image)})
    else:
        articles = json.loads(args.mpnews.read_text(encoding="utf-8"))
        articles = articles if isinstance(articles, list) else [articles]
        thumb = upload_image(token, args.thumb)
        for article in articles:
            article["thumb_media_id"] = thumb
        message.update(msgtype="mpnews", mpnews={"articles": articles})
    call(f"{API}/message/send?access_token={token}", json.dumps(message, ensure_ascii=False).encode(),
         {"Content-Type": "application/json"})
    print("sent")


if __name__ == "__main__":
    main()
